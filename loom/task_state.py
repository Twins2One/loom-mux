# -*- coding: utf-8 -*-
"""任务最小状态层（设计《任务准备与计划承接架构设计-20261108》§4/§5/§8/§13.1）。

职责（刻意收窄，不做通用调度框架）：
- TaskState：跨轮任务状态（§4.1 字段表全项）、revision 乐观锁（expected_revision CAS）、
  生命周期转换（§5 状态机）、事件折叠与可重建快照。
- 持久化：任务事件日志（jsonl，append-only）+ 快照（缓存，可由日志重建）。
  书内落点：`<书根>/.任务状态/<conversation|run>/<task_id>.jsonl`（paths.TASKS_DIR）。
  library 落点：`<知识库根>/.loom-tasks/<role>/<conversation_id>/<task_id>.jsonl`。
  隔离维度：book/library + conversation/run + task_id（§8），读写作用域由服务端解析。
- 快照读取不触发模型——load_* 系列只读盘。

生命周期（§5）：
    ready → running → (ready | waiting_author | paused | blocked | completed | cancelled)
  - completed/cancelled 的任务不因迟到结果重新激活（apply 里硬拦）。
  - 每个作用域同一时间只有一个活动任务；建新任务时旧活动任务自动 paused。
  - 恢复未完成任务沿用 task_id；轮数耗尽/解析失败是 paused/blocked，不是 completed。

并发（§8）：每次更新带 expected_revision，冲突抛 RevisionConflict，调用方重读再改；
  状态提交的临界区不跨模型请求（本模块只提供文件级原子写，不持锁到模型调用）。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1

# §5 状态机:合法的 status 集合与转换由执行结果和作者事件确认
STATUSES = ("ready", "running", "waiting_author", "paused", "blocked",
            "completed", "cancelled")
# 迟到结果不许激活的终态
_TERMINAL = ("completed", "cancelled")


class RevisionConflict(Exception):
    """expected_revision 对不上:重读状态再改,不盲目覆盖(§8)。"""


class LifecycleError(Exception):
    """非法生命周期转换(如对已完成任务再推进)。"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _new_task_id() -> str:
    return "t-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# ── 存储落点（§13.1,目录名 = 开工台账）────────────────────────────────────
def book_task_dir(root: Path, conversation: str = "默认") -> Path:
    """书内任务子区:<书根>/.任务状态/<会话代际>/。conversation 是另起对话的代际标识。"""
    return Path(root) / ".任务状态" / (conversation or "默认")


def library_task_dir(role: str, conversation_id: str = "默认") -> Path:
    """书房(library)任务子区:<知识库根>/.loom-tasks/<角色>/<会话>/。"""
    from .study.library import library_root
    return library_root() / ".loom-tasks" / (role or "书房伙伴") / (conversation_id or "默认")


class TaskState:
    """§4.1 字段表全项。dict 为真源,本类只包读写;json 可直接进快照。"""

    def __init__(self, data: dict):
        self._d = data

    # ── 构造 ──
    @classmethod
    def create(cls, scope: dict, goal: str, *, origin_event_id: str = "",
               task_id: str = "") -> "TaskState":
        tid = task_id or _new_task_id()
        return cls({
            "schema_version": SCHEMA_VERSION,
            "task_id": tid,
            "revision": 1,
            "scope": dict(scope or {}),          # book/library 身份、domain、角色、conversation/run、章号等
            "goal": (goal or "").strip(),
            "origin_event_id": origin_event_id or "",
            "constraints": [],                   # [{content, source_event, kind(显式/推测), scope, superseded}]
            "phase": "",                         # 当前工作阶段(取材/分析/写作…),不混充 status
            "status": "ready",
            "stop_reason": "",
            "plan": {"plan_revision": 0, "steps": []},
            "decisions": [],                     # [{proposal_id, kind(accepted/rejected), ts, note}]
            "pending_questions": [],
            "evidence_refs": [],                 # [{材料, 版本/摘要, 范围, 可用}]
            "last_outcome": "",
            "next_step": "",
            "created_ms": now_ms(),
            "updated_ms": now_ms(),
            "resumed_once": False,               # §8:一次接受最多一次续轮的服务端记账
        })

    # ── 序列化 ──
    def to_dict(self) -> dict:
        return dict(self._d)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskState":
        return cls(d)

    # ── 只读视图 ──
    @property
    def task_id(self) -> str:
        return self._d["task_id"]

    @property
    def revision(self) -> int:
        return self._d["revision"]

    @property
    def status(self) -> str:
        return self._d["status"]

    def is_active(self) -> bool:
        return self._d["status"] not in _TERMINAL

    def plan_steps(self) -> list[dict]:
        return list(self._d["plan"].get("steps") or [])

    # ── 事件折叠（唯一入口:apply）────────────────────────────────────────
    def apply(self, event: dict, *, expected_revision: int | None = None) -> dict:
        """折叠一条任务事件并 bump revision。返回实际生效的折叠结果(含 revision)。

        event 形如 {kind, ...(载荷), ts, event_id}。kind ∈
          start / status / plan_update / constraint / decision / outcome / resume_flag
        §5 硬约束:
        - 终态(completed/cancelled)任务只接受 status=noop 类迟到记录,不复活;
        - revision 冲突抛 RevisionConflict,不覆盖。
        """
        if expected_revision is not None and expected_revision != self._d["revision"]:
            raise RevisionConflict(
                f"任务 {self._d['task_id']} revision 冲突:期望 {expected_revision},"
                f"当前 {self._d['revision']}——请重读状态再改。")
        kind = (event.get("kind") or "").strip()
        if not kind:
            raise ValueError("任务事件缺 kind")
        d = self._d
        d["updated_ms"] = int(event.get("ts") or now_ms())
        applied: dict = {"kind": kind, "task_id": d["task_id"]}

        if kind == "status":
            new = (event.get("status") or "").strip()
            if new and new in STATUSES:
                if d["status"] in _TERMINAL and new not in (d["status"],):
                    # 迟到结果不激活已完成任务(§5):留原因,不改状态
                    applied["ignored"] = f"终态 {d['status']} 不接受 {new}"
                    return applied
                d["status"] = new
                d["stop_reason"] = (event.get("stop_reason") or "")
                if event.get("phase") is not None:
                    d["phase"] = str(event.get("phase") or "")
                applied["status"] = new
        elif kind == "plan_update":
            steps = event.get("steps")
            if isinstance(steps, list):
                # 保留已完成步骤的原结果引用;新版本把失效步骤标 pending(不靠重编号抹进度,§5)
                old_by_id = {s.get("id"): s for s in d["plan"].get("steps") or []}
                merged = []
                for s in steps:
                    sid = (s.get("id") or "").strip()
                    prev = old_by_id.get(sid, {})
                    st = s.get("status") or prev.get("status") or "pending"
                    # 计划修订语义:步骤条目被新版改写(标题变了)→ 即使旧版已完成也回 pending 待重做;
                    # 步骤条目原样保留 → 沿用旧状态与结果引用(不靠重编号抹进度)。
                    if st == "completed" and s.get("title") and \
                            prev.get("title") and s["title"] != prev["title"]:
                        st = "pending"
                    # 6b 修复（F7，历史会话 深度审查）：result_ref 优先取**事件里的新值**
                    # （步骤完成的产出证据随事件走，日志重建不丢）；事件没带才沿用旧值。
                    # deps 同样从事件条目复制（原来从不复制——依赖校验在新条目上
                    # 实际无效，审查 F7 第 2 点）。
                    merged.append({
                        "id": sid or f"s{len(merged) + 1}",
                        "title": (s.get("title") or s.get("交付") or "").strip()[:120],
                        "status": st if st in ("pending", "running", "waiting_author",
                                               "completed", "skipped", "failed") else "pending",
                        "skipped_reason": s.get("skipped_reason") or prev.get("skipped_reason", ""),
                        "result_ref": s.get("result_ref") if s.get("result_ref") is not None
                                      else prev.get("result_ref", ""),
                        "deps": [x for x in (s.get("deps") or prev.get("deps") or [])
                                 if isinstance(x, str)],
                    })
                # 循环依赖防御(§13.4):deps 引用必须存在 + 成环剪除（6b：deps
                # 随事件复制后，无效引用与循环引用都在合并后的全量上过滤——
                # 原测试契约 test_cycle_deps_are_filtered 锁定 a↔b 双环**整边剪空**：
                # 依赖边参与环即剪（比「解一边留一边」更干净，语义也简单）。
                ids = {m["id"] for m in merged}
                by_id = {m["id"]: m for m in merged}
                # 原始依赖图（剪边判定必须基于**未剪的快照**——边是否在环上
                # 与处理顺序无关；先剪后检会把环的另一边判成「不在环上」）。
                _orig_deps = {m["id"]: [x for x in (m.get("deps") or []) if x in ids]
                              for m in merged}

                def _on_cycle(dep_from: str, dep_to: str) -> bool:
                    # 依赖边 (from → to) 是否处于环上：原始图上从 to 能否回到 from。
                    stack, seen = [dep_to], {dep_to}
                    while stack:
                        cur = stack.pop()
                        if cur == dep_from:
                            return True
                        for d2 in _orig_deps.get(cur, []):
                            if d2 not in seen:
                                seen.add(d2)
                                stack.append(d2)
                    return False

                for m in merged:
                    m["deps"] = [x for x in _orig_deps.get(m["id"], [])
                                 if x in ids and not _on_cycle(m["id"], x)]
                d["plan"] = {"plan_revision": int(d["plan"].get("plan_revision", 0)) + 1,
                             "steps": merged}
                applied["plan_revision"] = d["plan"]["plan_revision"]
            if event.get("reason"):
                d["last_outcome"] = f"计划修订:{str(event['reason'])[:120]}"
        elif kind == "constraint":
            c = {"content": str(event.get("content") or "")[:400],
                 "source_event": event.get("source_event") or "",
                 "kind": "显式" if event.get("explicit", True) else "推测",
                 "scope": event.get("scope") or "任务",
                 "superseded": False, "ts": d["updated_ms"]}
            if c["content"]:
                d["constraints"].append(c)
                applied["constraint"] = c["content"][:60]
        elif kind == "supersede_constraint":
            idx = int(event.get("index", -1))
            if 0 <= idx < len(d["constraints"]):
                d["constraints"][idx]["superseded"] = True
                applied["superseded"] = idx
        elif kind == "decision":
            rec = {"proposal_id": event.get("proposal_id") or "",
                   "kind": event.get("decision_kind") or "accepted",
                   "ts": d["updated_ms"], "note": str(event.get("note") or "")[:200],
                   "source_event": event.get("source_event") or ""}
            d["decisions"].append(rec)
            applied["decision"] = rec["kind"]
        elif kind == "pending_question":
            q = str(event.get("question") or "")[:200]
            if q:
                d["pending_questions"].append(q)
                applied["question"] = q[:60]
        elif kind == "outcome":
            d["last_outcome"] = outcome_summary(event.get("summary"))
            if event.get("next_step") is not None:
                d["next_step"] = str(event.get("next_step") or "")[:200]
            applied["outcome"] = d["last_outcome"][:60]
        elif kind == "evidence":
            e = {"材料": str(event.get("材料") or "")[:120],
                 "版本": str(event.get("版本") or "")[:80],
                 "可用": bool(event.get("可用", True))}
            d["evidence_refs"].append(e)
            applied["evidence"] = e["材料"][:40]
        elif kind == "resume_flag":
            d["resumed_once"] = True
            applied["resumed_once"] = True
        elif kind == "resume_claim":
            decision_id = str(event.get("decision_id") or "").removeprefix("confirm:")
            claims = d.setdefault("resume_claims", [])
            if not decision_id or decision_id in claims or f"confirm:{decision_id}" in claims:
                raise RevisionConflict("该决策的续轮已经领取")
            claims.append(decision_id)
            applied["resume_claim"] = decision_id
        else:
            raise ValueError(f"未知任务事件 kind: {kind}")

        d["revision"] = int(d["revision"]) + 1
        applied["revision"] = d["revision"]
        return applied

    # 便捷转换(给各域 adapter 用)
    def set_step(self, step_id: str, status: str, *, result_ref: str = "",
                 skipped_reason: str = "") -> None:
        """把计划里某一步置状态(不动 plan_revision——这是执行推进,不是计划修订)。"""
        for s in self._d["plan"].get("steps") or []:
            if s.get("id") == step_id:
                s["status"] = status
                if result_ref:
                    s["result_ref"] = result_ref[:120]
                if skipped_reason:
                    s["skipped_reason"] = skipped_reason[:120]
                return

    def active_constraints(self) -> list[str]:
        return [c["content"] for c in self._d["constraints"]
                if not c.get("superseded") and c.get("kind") == "显式"]


# ── 持久化:事件日志 + 快照 ────────────────────────────────────────────────
def sanitize_conversation_id(raw: str) -> str:
    """会话代际清洗(夜间补缺 P1-2,验收报告 C.1):客户端可控的 conversation_id
    禁作路径成分——含路径分隔符或 `..` 段的一律回退「默认」,不给越界建目录任何口子。
    F10 回执整改（历史回执 审查记录 P2）：补冒号（Windows 驱动器形状）——
    与 sanitize_role 同源纪律。"""
    s = (raw or "").strip()
    if not s or s in (".", "..") or "/" in s or "\\" in s or s.startswith("~") \
            or ":" in s:
        return "默认"
    return s


def sanitize_role(raw: str, *, default: str = "书房伙伴") -> str:
    """F10（分片5，历史会话 深度审查）：客户端可控的 role 禁作路径成分。

    书房任务目录 = library/.loom-tasks/<role>/<conversation>——role 原样拼接时
    绝对路径（C:\\...）会让 base 逃出任务子区（实证）。清洗规则与
    sanitize_conversation_id 同源：分隔符/.. 段/绝对路径形状/波浪号开头一律
    回退 default；角色名册本身在 collab._ALL_ROLES（合法中文名不含分隔符）。
    """
    s = (raw or "").strip()
    if not s or s in (".", "..") or "/" in s or "\\" in s or s.startswith("~") \
            or ":" in s:
        return default
    return s


def _log_path(base_dir: Path, task_id: str) -> Path:
    return _contained(base_dir, base_dir / f"{task_id}.jsonl")


def _snap_path(base_dir: Path, task_id: str) -> Path:
    return _contained(base_dir, base_dir / f"{task_id}.快照.json")


def _contained(base_dir: Path, p: Path) -> Path:
    """task_id 防御(夜间补缺 P2):解析后必须仍在任务目录内,越界即拒绝(不留写面)。"""
    rp = p.resolve()
    if not rp.is_relative_to(base_dir.resolve()):
        raise ValueError(f"task_id 越界: {p.name}")
    return rp


def append_task_event(base_dir: Path, task_id: str, event: dict) -> None:
    """任务事件日志 append(末尾不完整行不算已完成事件,§8)。"""
    base_dir.mkdir(parents=True, exist_ok=True)
    ev = dict(event)
    ev.setdefault("event_id", uuid.uuid4().hex[:12])
    ev.setdefault("task_id", task_id)
    ev.setdefault("ts", now_ms())
    with _scope_lock(base_dir):
        with _log_path(base_dir, task_id).open("ab+") as f:
            f.seek(0, 2)
            if f.tell():
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    # 保留中断字节并标记该行无效，不能仅补换行使其变成已提交事件。
                    f.write(b" [uncommitted-tail]\n")
            f.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))


def save_snapshot(base_dir: Path, state: TaskState) -> None:
    """快照只是缓存;写入走临时文件+replace 原子落盘。

    6a 修复（F5）：临时文件名带任务 id——原固定 .tmp 后缀，两个任务并发保存
    时可能互换/replace 失败（审查 F5 第三点）。"""
    base_dir.mkdir(parents=True, exist_ok=True)
    p = _snap_path(base_dir, state.task_id)
    tmp = p.with_suffix(".tmp-" + state.task_id)
    tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


def outcome_summary(text, limit=300):
    """有界任务摘要保留开头背景与尾部结论，不额外调用模型。"""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    marker = "\n…\n"
    head = (limit - len(marker)) // 3
    return text[:head] + marker + text[-(limit - len(marker) - head):]


def _valid_state(data, task_id):
    return (isinstance(data, dict) and data.get("task_id") == task_id
            and type(data.get("revision")) is int and data["revision"] >= 1
            and data.get("status") in STATUSES
            and isinstance(data.get("scope"), dict)
            and isinstance(data.get("plan"), dict)
            and isinstance(data["plan"].get("steps"), list)
            and all(isinstance(data.get(k), list) for k in
                    ("constraints", "decisions", "pending_questions", "evidence_refs"))
            and all(type(data.get(k)) is int for k in ("created_ms", "updated_ms")))


def load_snapshot(base_dir: Path, task_id: str) -> TaskState | None:
    """读取缓存并按已提交日志水位追平；不触发模型。"""
    with _scope_lock(base_dir):
        p = _snap_path(base_dir, task_id)
        state = None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not _valid_state(data, task_id):
                raise ValueError("无效任务缓存")
            state = TaskState.from_dict(data)
        except (OSError, ValueError, TypeError):
            pass
        records = _read_log(base_dir, task_id)
        watermark = max((int(ev.get("applied_revision") or
                            (ev.get("initial_state") or {}).get("revision") or 0)
                         for ev in records), default=0)
        # 历史恢复写曾可能使用旧视图，让两个不同事件报同一 revision。
        # max 水位不能证明缓存包含全部事件；重复/倒退水位须按日志顺序重放。
        # 终态迟到 status 也可能合法地不涨 revision，重放保留其既有语义。
        previous = 0
        ambiguous_revision = False
        for ev in records:
            revision = (ev.get("initial_state") or {}).get("revision", 0) if ev.get("kind") == "start" \
                else ev.get("applied_revision", 0)
            if revision:
                if revision <= previous:
                    ambiguous_revision = True
                previous = revision
        if state is None or watermark > state.revision or ambiguous_revision:
            return rebuild_from_log(base_dir, task_id)
        if any(ev.get("kind") != "start" and "applied_revision" not in ev for ev in records):
            # 旧协议没有提交水位，只能实际重放后比较；不能把有效缓存当最新状态。
            rebuilt = rebuild_from_log(base_dir, task_id)
            if rebuilt is not None and rebuilt.revision > state.revision:
                return rebuilt
        return state


def _read_log(base_dir, task_id):
    p = _log_path(base_dir, task_id)
    if not p.is_file():
        return []
    import logging
    records = []
    for number, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True), 1):
        if not line.endswith("\n"):
            logging.getLogger("loom.task").warning("任务日志尾行未提交: %s:%s", task_id, number)
            break
        try:
            ev = json.loads(line)
            if not isinstance(ev, dict):
                raise ValueError("事件必须是对象")
            if ev.get("task_id", task_id) != task_id:
                raise ValueError("事件任务身份不符")
            scope = ev.get("scope")
            if scope is not None and (not isinstance(scope, dict) or
                                      scope.get("task_id", task_id) != task_id):
                raise ValueError("事件作用域不符")
            if "applied_revision" in ev and (type(ev["applied_revision"]) is not int or
                                               ev["applied_revision"] < 1):
                raise ValueError("事件水位无效")
            if ev.get("kind") == "start" and "initial_state" in ev and not _valid_state(ev["initial_state"], task_id):
                raise ValueError("初始状态无效")
            if "ts" in ev and type(ev["ts"]) is not int:
                raise ValueError("事件时间无效")
            records.append(ev)
        except (ValueError, TypeError):
            logging.getLogger("loom.task").warning("任务日志损坏行: %s:%s", task_id, number)
    return records


def task_ids(base_dir):
    """发现缓存和日志中的任务身份，包含仅日志任务。"""
    base_dir = Path(base_dir)
    return sorted({p.name.removesuffix(".快照.json") for p in base_dir.glob("*.快照.json")} |
                  {p.stem for p in base_dir.glob("*.jsonl")})


def rebuild_from_log(base_dir: Path, task_id: str) -> TaskState | None:
    """从事件日志重建状态(快照只是缓存)。空日志/坏行跳过;无法判定即 None(§8 不伪造)。"""
    import copy
    state: TaskState | None = None
    for ev in _read_log(base_dir, task_id):
        if state is None:
            if ev.get("kind") != "start":
                continue
            initial = ev.get("initial_state")
            if _valid_state(initial, task_id):
                state = TaskState.from_dict(copy.deepcopy(initial))
            else:
                # 旧日志缺失origin等字段时仅恢复可证明的字段，时间使用start事件。
                state = TaskState.create(ev.get("scope") or {}, ev.get("goal") or "",
                                         task_id=task_id, origin_event_id=ev.get("origin_event_id") or "")
                state._d["created_ms"] = state._d["updated_ms"] = int(ev.get("ts") or 0)
            continue
        candidate = TaskState.from_dict(copy.deepcopy(state.to_dict()))
        try:
            candidate.apply(ev)
        except (ValueError, TypeError, KeyError, AttributeError, LifecycleError, RevisionConflict) as exc:
            import logging
            logging.getLogger("loom.task").warning("任务日志事件未折叠: %s %s", task_id, type(exc).__name__)
            continue
        state = candidate
    return state


def load_or_rebuild(base_dir: Path, task_id: str) -> TaskState | None:
    """R-05（2026-11-10）：兜底重放也必须在同一作用域锁内。

    `load_snapshot` 自身的重建已在锁内，但它的 `or` 兜底分支原来在锁外跑
    `rebuild_from_log`——读路径于是脱离「同一临界区里的读-改-写」语义：与并发
    `commit`（锁内 append+save）交错时，可能返回「日志已有事件、快照尚未落盘」
    的前缀状态，即任何串行化执行里都不存在的 revision。RLock 可重入，
    `load_snapshot` 内部再取同锁安全（同进程语义；跨进程仍由上层 write_lock 负责）。
    """
    with _scope_lock(base_dir):
        return load_snapshot(base_dir, task_id) or rebuild_from_log(base_dir, task_id)


def pause_active_tasks(base_dir: Path, *, reason: str = "新任务建立",
                       role: str = "") -> list[str]:
    """§5:每个作用域同一时间只有一个活动任务——把该目录下所有活动任务 paused。

    2026-11-08 午（验收截图 505 抓到）：书内对话域所有角色共用 base_dir
    （.任务状态/默认），原实现扫全目录不区分 role——批评家开新任务会把
    润色师的活动任务一起暂停（跨角色误伤）。现加 role 过滤：只暂停
    scope.role 相同（或无主）的任务；role 为空保持旧行为（全暂停）。
    """
    paused: list[str] = []
    if not base_dir.is_dir():
        return paused
    # 6a 修复（F5）：暂停全程持作用域锁——原裸 apply+append+save 的恒等 CAS
    # 会覆盖并发 commit 的更新。锁内逐任务原子推进。
    with _scope_lock(base_dir):
        for candidate_id in task_ids(base_dir):
            st = load_snapshot(base_dir, candidate_id)
            if not (st and st.is_active() and st.status in ("ready", "running", "waiting_author", "blocked")):
                continue
            if role and (st._d.get("scope") or {}).get("role", "") not in ("", role):
                continue
            ev = {"kind": "status", "status": "paused", "stop_reason": reason,
                  "ts": now_ms()}
            st.apply(ev, expected_revision=st.revision)
            append_task_event(base_dir, st.task_id, {**ev, "applied_revision": st.revision})
            save_snapshot(base_dir, st)
            paused.append(st.task_id)
    return paused


def commit(base_dir: Path, state: TaskState, event: dict, *,
           expected_revision: int | None = None) -> dict:
    """标准提交:先持久化结果,再发展示事件(§8 顺序由调用方保证)。

    折叠事件 → append 日志 → 落快照。expected_revision 对**盘上最新快照**核验
    (盘是真源,内存对象可能是旧视图):冲突抛 RevisionConflict,不写任何盘。

    6a 修复（F5，历史会话 深度审查 + 历史回执 回执）：「读盘核验→折叠→日志→
    快照」全程持 base_dir 作用域锁——原来读-改-写无锁，两个 barrier 同步的
    线程同 revision 过检后并发写，后写覆盖先写（丢更新，并发测试实证）。
    锁是进程内线程锁（单 uvicorn 进程语义）；跨进程（CLI+开发实例同写一本
    书）由上层 write_lock 闸负责——已挂账 write_lock 桩点修复线。
    """
    with _scope_lock(base_dir):
        disk = load_snapshot(base_dir, state.task_id)
        disk_rev = disk.revision if disk is not None else 0
        if expected_revision is not None and expected_revision != disk_rev:
            # CAS 契约（§8 旧版即如此，契约测试锁定）：旧视图提交必须显式冲突
            # ——过期写被拒、调用方重读再改。6a 的锁保证的是「读-改-写不再
            # 互相穿插半态」（文件层原子性），不是把 CAS 变成静默合并。
            # 需要自动追盘重试的调用方用 commit_retry（下一函数）。
            raise RevisionConflict(
                f"任务 {state.task_id} revision 冲突:期望 {expected_revision},"
                f"盘上 {disk_rev}——请重读状态再改。")
        event = dict(event)
        event.setdefault("ts", now_ms())
        applied = state.apply(event)
        append_task_event(base_dir, state.task_id, {**event, **{
            "applied_revision": applied.get("revision", state.revision)}})
        save_snapshot(base_dir, state)
        return applied


def commit_retry(base_dir: Path, state: TaskState, event: dict,
                 *, expected_revision: int | None = None,
                 max_attempts: int = 3) -> dict:
    """6a（F5）：自动追盘重试的提交——冲突时在锁内重读盘上最新状态并**重放**
    该事件（追加语义，双方更新都在）。

    适用：并发收尾/折叠这类「事件本身幂等追加、无需精确过期拦截」的路径
    （agent_chat 三处折叠/收尾、usecases 决策回连）。
    需要严格过期拦截（迟到输出边界 §8）的调用方仍用 commit（冲突必抛）。
    expected_revision 参数仅为签名兼容——replay 语义下它不参与拒绝。

    6a 回执根修（历史回执 审查记录 P1）：**status 类事件加重放守卫**——
    重放前盘上状态已离开可承接集（如被并发让位成 paused）时拒发，防
    「A 收尾 status ready 重放 → 复活被让位任务 → 双活动」回归。终态门
    （completed/cancelled）在 apply 内已拦，这里补的是 paused 侧的同款门。
    """
    _ = expected_revision
    kind = (event.get("kind") or "").strip()
    _guarded = kind == "status"
    for _ in range(max(1, max_attempts)):
        with _scope_lock(base_dir):
            disk = load_snapshot(base_dir, state.task_id)
            cur = disk if disk is not None else state
            if _guarded and cur.status not in ("ready", "running", "waiting_author",
                                               "blocked"):
                # 状态迁移事件的追盘守卫：盘已 paused/终态 → 拒发（不复活）。
                raise RevisionConflict(
                    f"任务 {state.task_id} 已离开可承接状态({cur.status}),"
                    f"拒绝重放 status 事件({event.get('status')})——不复活被让位任务")
            event = dict(event)
            event.setdefault("ts", now_ms())
            applied = cur.apply(event)
            append_task_event(base_dir, state.task_id, {**event, **{
                "applied_revision": applied.get("revision", cur.revision)}})
            save_snapshot(base_dir, cur)
            return applied
    raise RevisionConflict(f"任务 {state.task_id} 重试耗尽")


# ── 6a：base_dir 作用域锁（F5 原子性）─────────────────────────────────────
import threading as _threading

_SCOPE_LOCKS: dict[str, "_threading.RLock"] = {}
_SCOPE_LOCKS_GUARD = _threading.Lock()


def _scope_lock(base_dir) -> "_threading.RLock":
    """按 base_dir 解析（含规范化）取进程内作用域锁；同目录的 commit/pause/
    prepare 临界区互斥。锁不跨模型请求（持有时间=纯盘上读写，毫秒级）。

    RLock（可重入）：prepare_turn 持锁段内部会调 pause_active_tasks（它自持
    同一把锁）——可重入避免自死锁；同线程内嵌套安全，跨线程仍互斥。
    """
    import os
    key = os.path.normcase(str(Path(base_dir).resolve()))
    with _SCOPE_LOCKS_GUARD:
        lk = _SCOPE_LOCKS.get(key)
        if lk is None:
            lk = _threading.RLock()
            _SCOPE_LOCKS[key] = lk
        return lk


# ── 任务事件构造(t 信封:对话/书房域;type 信封由 events.py 另行构造)────────
def t_event(kind: str, state: TaskState, *, request_id: str = "", **extra) -> dict:
    """对话/书房 t 信封的任务事件,载荷与 §9 一致:task_id/revision/scope/request_id。"""
    ev = {"t": kind, "task_id": state.task_id, "revision": state.revision,
          "scope": state._d.get("scope") or {}, "request_id": request_id or ""}
    ev.update(extra)
    return ev
