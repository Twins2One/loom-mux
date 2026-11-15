# -*- coding: utf-8 -*-
r"""议事例会。

需求背景:「我对领航员说『我想要 A 在前期装杯,后期被打脸』,领航员自动喊来大纲师、
情绪节奏设计师甚至设定师、批评家,分别分析该改动涉及多大范围的大纲重写 / 对整体及部分
情绪节奏的影响 / 涉及哪些设定需要细化 / 会不会造成中心偏离、代入感被破坏……」

一句话需求 → 领航员点名 → 几个角色**真并发**各给一段分析 → 作者看分歧点。

## 三条硬边界(都写在这里,免得以后被"顺手优化"掉)

1. **钱是乘法的**:N 个角色 = N 次模型调用(谁再问同事,每人最多再 +2)。
   所以派发前先出名单(/api/panel/plan),作者点了「开议」才花钱——这是 Loom 的老规矩:
   没有自动花钱。
2. **并发上限是作者的格子**:`loom.toml [backend] 并发上限`(1-4,默认 2)。
   超出的人**排队,不丢弃**;排队状态会明确报到前端(panel_state=queued),绝不假装在跑。
3. **记录进两条流水**:
   - 各角色自己的 `.伙伴对话/agent-<角色>.jsonl`,事件带 `via="议事"` ——
     下次一对一它还认得这次讨论(作者 2026-10-25 选定:要写进去、界面上标灰「议事例会」);
   - 一份合并的 `.伙伴对话/议事/<轮次>.jsonl`,前端照着它画群聊。

## 为什么开会的不包括写作引擎六棒

六棒是"按章落笔"的一条流水线,它们的作用是产出正文,不是审议方案。
开会讨论"这个改动多大、值不值得"是作者智库八人的活(准备/审视)。
真要找细纲师,那是**派活**而不是**议事**,属于另外的入口。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .config import clamp_concurrency
from .scratchpad import VISIBLE_AUTHOR_ROLES

PANEL_DIR = ".伙伴对话/议事"

# 参会人数上限(不是并发上限:并发上限是"同时几个在跑",这里是"这一场最多几个人")
MAX_ROLES = 6

# 能参会的角色:作者智库**六人**(2026-10-31 起;知识顾问与语料分析师已工具化,不再算人)
PANEL_ROLES: tuple[str, ...] = tuple(sorted(VISIBLE_AUTHOR_ROLES))

_APPEND_LOCK = threading.Lock()


# ── 并发上限 ────────────────────────────────────────────────────────────────
def cap(config) -> int:
    """这本书允许同时几个角色在跑(1-4,来源:loom.toml [backend] 并发上限)。"""
    return clamp_concurrency(getattr(config, "max_concurrency", 2))


# ── 议事记录的读写 ──────────────────────────────────────────────────────────
def round_rel(round_id: str) -> str:
    return f"{PANEL_DIR}/{(round_id or '').strip()}.jsonl"


def append_event(root: Path | str, round_id: str, ev: dict) -> None:
    """往合并流水里追加一行。并发安全:一把进程内锁。"""
    p = Path(root) / round_rel(round_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(ev, ensure_ascii=False)
    with _APPEND_LOCK:
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_round(root: Path | str, round_id: str) -> list[dict]:
    """读回一轮议事的全部事件。坏行跳过(手改坏了不该让界面崩)。"""
    p = Path(root) / round_rel(round_id)
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        return []
    return out


def list_rounds(root: Path | str, *, limit: int = 12) -> list[dict]:
    """列出最近的几场议事(倒序)。只读文件名 + 粗统计,不读正文。"""
    d = Path(root) / PANEL_DIR
    if not d.is_dir():
        return []
    rows = []
    for f in d.iterdir():
        if not f.is_file() or f.suffix != ".jsonl":
            continue
        ts = f.stem
        whose: list[str] = []
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                r = (ev.get("role") or "").strip() if isinstance(ev, dict) else ""
                if r and r != "领航员" and r not in whose:
                    whose.append(r)
        except OSError:
            continue
        rows.append({"id": ts, "at": _pretty_ts(ts), "角色": whose})
    rows.sort(key=lambda x: x["id"], reverse=True)
    return rows[:limit]


def _pretty_ts(ts: str) -> str:
    """20261025-143012 → 2026-10-25 14:30。认不出来就原样返回。"""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", ts or "")
    if not m:
        return ts or ""
    y, mo, d, hh, mm = m.groups()
    return f"{y}-{mo}-{d} {hh}:{mm}"


# ── 第一步:领航员点名 ──────────────────────────────────────────────────────
_PICK_SYSTEM = """你是这本书的「领航员」——不是写手,也不是任何一个专业角色。
你的活是:听作者说一句想改什么,判断该找哪几位**作者智库**的同事来一起分析。

可点名的同事(只能从这些里挑,写错名字等于没点):
{roster}

## 怎么挑
- 只挑**真能就这件事给出专业判断**的人,2~4 位最合适;宁少勿滥。
- 不要凑数:跟这件事无关的角色点了只是让作者多花钱。
- 如果这件事只有一个人能答,就只点一个。

## 输出格式(严格遵守,一行一位,不要任何别的话)
角色名|一句话说明他为什么该来(≤25 字)

例如:
大纲师|要判断这条改线会牵动多少章的纲
情绪节奏设计师|前期爽点与后期打脸的落差怎么铺

不要解释、不要编号、不要 Markdown 标题。一行一位。"""


def _roster_text() -> str:
    from .collab import COLLEAGUES

    lines = []
    for r in PANEL_ROLES:
        peers = "、".join(n for n, _ in COLLEAGUES.get(r, ())) or "—"
        lines.append(f"- {r}(常一起议事的:{peers})")
    return "\n".join(lines)


def _parse_picks(raw: str) -> list[dict]:
    """把模型的输出解析成 [{role, why}].认不出的行直接丢掉,不猜。"""
    out: list[dict] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        s = line.strip().lstrip("-*0123456789.、）) ").strip()
        if not s:
            continue
        # 分隔符:竖线优先,其次中英文冒号、逗号、破折号
        parts = re.split(r"\s*[|｜]\s*", s, maxsplit=1)
        if len(parts) == 1:
            parts = re.split(r"\s*[:：,—–\-]\s*", s, maxsplit=1)
        name = (parts[0] or "").strip()
        why = (parts[1] if len(parts) > 1 else "").strip()
        # 去掉可能带的引号/书名号/「」/括号
        name = name.strip("「」『』《》<>()（）[]【】\"'“” ")
        if name not in PANEL_ROLES or name in seen:
            continue
        seen.add(name)
        out.append({"role": name, "why": why[:60]})
        if len(out) >= MAX_ROLES:
            break
    return out


def pick(root: Path | str, text: str, backend) -> dict:
    """让领航员点名(1 次调用)。失败/点不出人 → 退回关键词路由,并在 note 里说清楚。

    为什么要退:点不出人时如果直接报错,作者会以为这功能坏了;
    关键词路由虽然笨,但总能给出**可改的名单**——名单本来就要给作者过目。
    """
    text = (text or "").strip()
    if not text:
        return {"roles": [], "source": "empty", "note": "先说一句想改什么。"}

    picks: list[dict] = []
    note = ""
    try:
        raw = backend.complete(_PICK_SYSTEM.format(roster=_roster_text()),
                               f"## 作者说的\n{text}\n\n## 现在按格式点名",
                               max_chars=400) or ""
        picks = _parse_picks(raw)
    except Exception as e:      # noqa: BLE001 - 点名失败不该把整场会堵死
        note = f"点名调用失败({type(e).__name__}),先用关键词给你圈了几个。"

    source = "llm"
    if not picks:
        source = "fallback"
        if not note:
            note = "领航员没点出人名,我先按关键词圈了几个 —— 你可以改。"
        picks = _keyword_picks(root, text)
    return {"roles": picks, "source": source, "note": note}


def _keyword_picks(root: Path | str, text: str) -> list[dict]:
    """兜底:沿用「该找谁」的关键词表,过滤掉不能参会的角色。"""
    from .navigator import route_multi_from_project

    roles: list[str] = []
    try:
        for r in route_multi_from_project(root, text):
            if r in PANEL_ROLES and r not in roles:
                roles.append(r)
    except Exception:      # noqa: BLE001 - 路由表读不出来就用默认名单
        pass
    if not roles:
        roles = ["大纲师", "批评家"]
    return [{"role": r, "why": "关键词命中(你可以改)"} for r in roles[:MAX_ROLES]]


# ── 第二步:并发派发 ────────────────────────────────────────────────────────
def run_panel(root: Path | str, text: str, roles: list[str], config, *, get_backend_for,
              emit, ts: str = "", should_cancel=None, protocol: str = "",
              note: str = "", source: str = "") -> str:
    """一场议事:roles 里的角色**并行**各跑一轮,事件带 role 汇进同一条流。

    `get_backend_for` 是「按用途取后端」的函数(routes.get_backend_for),每个角色单独
    调一次——不共用实例:后端对象身上带着 last_usage 之类的本轮状态,共用会互相踩。
    返回本轮 id(也是合并流水的文件名)。
    """
    root = Path(root)
    round_id = ts or time.strftime("%Y%m%d-%H%M%S")
    text = (text or "").strip()
    roles = [r for r in dict.fromkeys(roles or []) if r in PANEL_ROLES][:MAX_ROLES]
    if not roles:
        emit({"t": "error", "text": "没有可参会的角色。"})
        return round_id

    limit = cap(config)

    def say(ev: dict) -> None:
        """统一出口:补 panel/ts → 记合并流水 → 交给前端。"""
        ev = dict(ev)
        ev.setdefault("panel", round_id)
        ev.setdefault("ts", round_id)
        append_event(root, round_id, ev)
        emit(ev)

    say({"t": "panel_begin", "roles": [r for r in roles], "cap": limit,
         "text": text, "at": _pretty_ts(round_id),
         "note": note or "", "source": source or ""})

    from . import agent_chat as _ac

    errors: list[str] = []
    err_lock = threading.Lock()

    def one(role: str, slot: threading.Semaphore) -> None:
        if should_cancel is not None and should_cancel():
            return
        with slot:                       # 拿不到槽就在这儿排队,前端会看到 queued
            from . import agent_status as _st
            _st.mark_busy(role)          # 侧栏指示灯:这个角色正在思考
            try:
                say({"t": "panel_state", "role": role, "state": "running"})
                try:
                    backend = get_backend_for(config, "对话")
                except Exception as e:       # noqa: BLE001 - 一个角色没后端,不该拖垮整场
                    msg = f"{type(e).__name__}: {e}"
                    with err_lock:
                        errors.append(f"{role}:{msg}")
                    say({"t": "panel_state", "role": role, "state": "error", "reason": msg})
                    return

                def role_emit(ev: dict) -> None:
                    ev = dict(ev)
                    if ev.get("t") == "user":
                        return               # 作者那句只记一次(panel_begin 里),别刷 N 遍
                    ev["role"] = role
                    say(ev)

                try:
                    # 夜间补缺最终班 P1:议事传独立 conversation_id(`议事-<round_id>`)——
                    # 议事建的任务不落「默认」目录,不被独聊/其他角色静默认领(§8 可区分身份)。
                    _ac.run_turn(root, role, text, backend, emit=role_emit, ts=round_id,
                                 should_cancel=should_cancel, protocol=protocol,
                                 conversation_id=f"议事-{round_id}",
                                 extra={"via": "议事", "panel": round_id})
                except Exception as e:       # noqa: BLE001 - 兜底:一个角色炸了,别人照旧
                    msg = f"{type(e).__name__}: {e}"
                    with err_lock:
                        errors.append(f"{role}:{msg}")
                    role_emit({"t": "error", "text": msg})
                    say({"t": "panel_state", "role": role, "state": "error", "reason": msg})
                    return
                say({"t": "panel_state", "role": role, "state": "done"})
            finally:
                # 无论走哪条出口,灯都要灭 —— 卡着一盏常亮的红灯比没有灯更糟
                _st.mark_busy(role, False)

    slot = threading.Semaphore(limit)
    for r in roles:
        say({"t": "panel_state", "role": r, "state": "queued"})
    threads = [threading.Thread(target=one, args=(r, slot), daemon=True) for r in roles]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    say({"t": "panel_end", "cap": limit, "errors": errors})
    return round_id


# ── 第三步:领航员收口 ──────────────────────────────────────────────────────
_WRAP_SYSTEM = """你是这本书的「领航员」。刚才几位同事各自就作者的同一个改动给了分析,
现在轮到你**收口**——不是重讲一遍,是把分歧摊开、把能走的路摆出来。

## 输出格式(就这三段,不要别的)
**一致的地方**:一句话。

**分歧点**:谁和谁意见相反、反在哪。没有分歧就写「没有明显分歧」。
不许假装大家一致 —— 真相反的地方必须点出来。

**可以怎么走**:2~3 个选项,每个一行,写清代价;最后用「推荐:」标出你倾向的那个和理由。

不要复述同事的原话,不要客套。"""


def _transcript(events: list[dict], *, per_role: int = 700) -> str:
    """把一轮议事压成给收口用的材料(每位角色一段,只取它最后那段正经回复)。"""
    said: dict[str, list[str]] = {}
    for ev in events:
        if ev.get("t") != "assistant":
            continue
        role = (ev.get("role") or "").strip()
        body = (ev.get("text") or "").strip()
        if not role or not body:
            continue
        said.setdefault(role, []).append(body)
    lines = []
    for role, chunks in said.items():
        joined = "\n".join(chunks)
        if len(joined) > per_role:
            joined = joined[:per_role] + "…"
        lines.append(f"### {role}\n{joined}")
    return "\n\n".join(lines)


def wrapup(root: Path | str, round_id: str, backend, emit, *, ts: str = "") -> str:
    """让领航员把这一场收口。**手动触发**(作者点按钮),不自动跑——多一次调用的钱要作者认。"""
    events = read_round(root, round_id)
    if not events:
        emit({"t": "error", "text": "这一场还没记录,收不了口。"})
        return ""
    material = _transcript(events)
    if not material.strip():
        emit({"t": "error", "text": "这一场还没有人说完话,收不了口。"})
        return ""
    user = f"## 作者想改的事\n{_topic_of(events)}\n\n## 同事各自说了什么\n{material}\n\n## 现在收口"
    stop = (ts or time.strftime("%Y%m%d-%H%M%S")) + "-收口"
    out = ""
    try:
        out = backend.complete(_WRAP_SYSTEM, user, max_chars=900) or ""
    except Exception as e:      # noqa: BLE001 - 收口失败不该把已有一场会弄丢
        emit({"t": "error", "text": f"收口没跑成:{type(e).__name__}: {e}"})
        return ""
    out = out.strip()
    if not out:
        emit({"t": "error", "text": "领航员这次没给出收口(空回复),可以再点一次。"})
        return ""
    ev = {"t": "assistant", "role": "领航员", "panel": round_id, "text": out,
          "ts": stop, "wrap": True}
    append_event(root, round_id, ev)
    emit(ev)
    return out


def _topic_of(events: list[dict]) -> str:
    for ev in events:
        if ev.get("t") == "panel_begin" and (ev.get("text") or "").strip():
            return ev["text"].strip()
    for ev in events:
        if ev.get("t") == "user" and (ev.get("text") or "").strip():
            return ev["text"].strip()
    return "(没记下原话)"
