# -*- coding: utf-8 -*-
"""PreparedTurn 组装（设计 §4.2/§6）——程序准备,角色在正常请求中出计划,零额外规划调用。

一次请求的准备包:定位任务 → 读对话/约束/进度 → 选材料记版本 → 取真实可用工具面 →
唯一输出契约 → 预算裁剪 → task_prepared 摘要。准备层失效发诊断并回退既有答复路径
(prepare_turn 抛异常时调用方 catch 后走原路径——§6"任何整理层失效都应发出诊断并回退")。

事件契约:t 信封 task_prepared / plan_updated / task_status,载荷带
task_id/revision/scope/request_id(§9);type 信封由 events.py 的同构构造转发。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from . import task_state as ts


class PrepareFallback(Exception):
    """准备层失效:调用方 catch 后回退既有答复路径(不把准备失败当任务失败)。"""


def _goal_changed(state: "ts.TaskState", goal: str) -> bool:
    """同一任务的目标是否被作者明确改向。goal 与当前 goal 完全一致 = 沿用;否则视为转向。

    刻意从严:只有显式传入新 goal 才触发转向;goal 为空(承接"继续")绝不换任务。
    """
    return (state._d.get("goal") or "").strip() != (goal or "").strip()


from .request_turn import preparation


@preparation
def prepare_turn(*, base_dir: Path, domain: str, role: str, conversation_id: str = "默认",
                 run_id: str = "", task_id: str = "", goal: str = "",
                 user_text: str = "", origin_event_id: str = "",
                 conversation_tail: list | None = None,
                 materials: list | None = None,
                 tool_names: list | None = None,
                 output_contract: str = "",
                 budget_chars: int = 2000,
                 request_id: str = "",
                 emit=None) -> dict:
    """组装一轮 PreparedTurn。返回:
      {prepared_turn_id, request_id, attempt, task(dict), constraints, history_tail,
       materials, tools, output_contract, plan_progress, budget, events(要发的 t 事件列表)}

    幂等(§3):相同请求重试复用同一快照——同一 conversation+task 已有活动任务则沿用,
    不重复创建;goal 为空且没有活动任务 → 任务包退化为"无计划直答"(简单问答,
    plan 为空,以本轮答复结束,§5)。
    """
    prepared_id = "pt-" + uuid.uuid4().hex[:10]
    rid = request_id or uuid.uuid4().hex[:12]
    events: list[dict] = []

    # 1. 定位任务:显式 task_id 优先,其次该作用域的活动任务(§5 恢复沿用 task_id)
    state = None
    explicit_terminal = False
    # 夜间补缺最终班 P1:活动任务承接必须校验 scope.role —— 跨角色不静默互认
    # (验收报告 §2f:议事里 A 角色建的任务,下一轮被 B 角色独聊直接认领)。role 为空
    # (如纯快照读取路径)保持旧行为不拦截。
    def _role_ok(cand: "ts.TaskState") -> bool:
        return not role or (cand._d.get("scope") or {}).get("role", "") == role

    # F1 修复（2026-11-08 分片2，历史会话 深度审查）：
    # 「可承接」= ready/running/waiting_author/blocked（is_active 去掉 paused）。
    # paused 是「归档/让位/另起对话」后的状态——普通「继续」绝不能隐式复活
    # （原 is_active 含 paused：读旧任务指导新答复，写了又拒写，行为自相矛盾）。
    def _resumable(cand: "ts.TaskState") -> bool:
        return cand.status in ("ready", "running", "waiting_author", "blocked")

    # 明确恢复语义：显式 task_id + goal 与原 goal 等值（沿用，不是转向）
    # ——这是作者点名「继续这个任务」的显式恢复通道；paused 也允许接。
    _explicit_resume = False
    if task_id:
        state = ts.load_or_rebuild(base_dir, task_id)
        if state is not None:
            _explicit_resume = bool(goal.strip() and not _goal_changed(state, goal))
            if state.status == "paused" and goal.strip() and _goal_changed(state, goal) and _role_ok(state):
                # 新目标直接另起；旧任务保持paused，不先恢复再暂停。
                state = None
            elif not state.is_active():
                # 迟到/旧任务的输入不开新计划,也不扫活任务顶替——退回普通答复路径(§5)
                explicit_terminal = True
                state = None
            elif not _role_ok(state):
                # F1-1：显式 task_id 绕过 role 校验——B 拿 A 的 task_id 当普通
                # 输入处理（不喂 A 的上下文给 B），也不扫活任务顶替。
                # 但 B 自带明确 goal 时是 B 的正当新任务——不被吞（分片2自查修）。
                state = None
                if not goal.strip():
                    explicit_terminal = True
            elif state.status == "paused" and not _explicit_resume:
                # F1-2：paused + 普通继续（无等值 goal）→ 不承接（退普通答复）。
                # 显式 task_id 不再是万能恢复通道——恢复必须带「继续这个任务」的
                # 明确语义（goal 等值沿用）。
                explicit_terminal = True
                state = None
    if state is None and not explicit_terminal:
        for candidate_id in ts.task_ids(base_dir):
            cand = ts.load_snapshot(base_dir, candidate_id)
            # F1-3：隐式扫描只认可承接状态（paused 不认）。
            if cand and _resumable(cand) and _role_ok(cand) and \
                    not (goal.strip() and _goal_changed(cand, goal)):
                state = cand
                break
    created = False
    # F1-4（分片2）：显式恢复（承接 paused 任务）→ 状态拉回 ready，
    # 并让位同 scope 的其他活动任务（恢复=唯一活动任务）。
    # 6a 修复（F5）：恢复（让位+落 ready）同临界区——回执挂账「恢复非原子」。
    if state is not None and state.status == "paused" and _explicit_resume:
        with ts._scope_lock(base_dir):
            # preparation 当前已覆盖首次读取；这里仍以临界区最新视图为准，
            # 使恢复本身不依赖外层锁范围，不用旧对象的恒等 CAS 落盘。
            latest = ts.load_or_rebuild(base_dir, state.task_id)
            if latest is None or not latest.is_active() or not _role_ok(latest) \
                    or _goal_changed(latest, goal):
                state = None
                explicit_terminal = True
            else:
                state = latest
                if state.status == "paused":
                    # 先让位，再恢复；最新状态已非paused时只承接，不再次让位。
                    for p in ts.pause_active_tasks(base_dir, reason="任务恢复,让位", role=role):
                        if p != state.task_id:
                            events.append({"t": "task_status", "task_id": p, "revision": 0,
                                           "scope": state._d.get("scope") or {}, "request_id": rid,
                                           "status": "paused", "stop_reason": "任务恢复,让位"})
                    ts.commit(base_dir, state,
                              {"kind": "status", "status": "ready", "stop_reason": "作者显式恢复"},
                              expected_revision=state.revision)
                    events.append(ts.t_event("task_status", state, request_id=rid,
                                             status="ready", stop_reason="作者显式恢复"))
    if state is not None and goal.strip() and _goal_changed(state, goal):
        # §5:作者明确转向 → 旧任务 paused,新目标另起任务(不把旧约束混入新任务)
        state = None
    if state is None and goal.strip() and not explicit_terminal:
        # §5:每个作用域同一时间只有一个活动任务。
        # 2026-11-08 午：按 role 过滤——跨角色不误伤（同 task_state.pause_active_tasks）。
        # 6a 修复（F5，历史回执 回执挂账）：暂停+新建同一临界区（持 base_dir
        # 作用域锁，RLock 可重入——pause_active_tasks 内部同锁不自死锁）——
        # 原来两步之间另一请求可建出双活动任务（并发测试实证）。
        with ts._scope_lock(base_dir):
            paused = ts.pause_active_tasks(base_dir, reason="新任务建立,旧任务让位", role=role)
            scope = {"domain": domain, "role": role,
                     "conversation_id": conversation_id, "run_id": run_id,
                     **({"book": str(base_dir.parents[1])} if "任务状态" in str(base_dir) else {})}
            state = ts.TaskState.create(scope, goal, origin_event_id=origin_event_id)
            start_ev = {"kind": "start", "scope": scope, "goal": state._d["goal"],
                        "initial_state": state.to_dict(), "ts": state._d["created_ms"]}
            ts.append_task_event(base_dir, state.task_id, start_ev)
            ts.save_snapshot(base_dir, state)
        created = True
        for p in paused:
            events.append({"t": "task_status", "task_id": p, "revision": 0,
                           "scope": scope, "request_id": rid, "status": "paused",
                           "stop_reason": "新任务建立,旧任务让位"})
        events.append(ts.t_event("task_prepared", state, request_id=rid,
                                 created=True, goal=state._d["goal"],
                                 status=state.status))
    if state is None:
        # 简单问答:无任务即无计划,直接给准备包(不建卡、不建文件)
        # F1 回执根修（历史回执 审查记录）：「准备层**明确拒绝**绑定」（explicit_terminal）
        # 与「本轮**没有**任务」必须区分——前者要抑制 agent_chat 侧的空绑定回退扫描
        # （否则 A 请求被拒后按 role 重扫认领 B，B.last_outcome 被 A 的输出污染）。
        # task_binding: "none"=无任务（普通问答，回退扫描允许）
        #               "refused"=准备层明确拒绝（跨 role task_id / paused+普通继续）
        #               "bound"=已绑定任务
        return {"prepared_turn_id": prepared_id, "request_id": rid, "attempt": 1,
                "task": None,
                "task_binding": "refused" if explicit_terminal else "none",
                "constraints": [], "history_tail": _tail(conversation_tail),
                "materials": list(materials or []), "tools": list(tool_names or []),
                "output_contract": output_contract, "plan_progress": None,
                "budget": {"chars": budget_chars}, "events": events}

    # 2. 有效约束(被取代的不带,推测的标"待核")
    constraints = []
    for c in state._d.get("constraints") or []:
        if c.get("superseded"):
            continue
        item = {"content": c["content"], "kind": c.get("kind", "显式")}
        if c.get("kind") != "显式":
            item["待核"] = True
        constraints.append(item)

    # 3. 有限对话尾部 + 材料版本(§6:摘要有长度预算)
    history_tail = _tail(conversation_tail)
    mats = []
    for m in list(materials or [])[:12]:
        if isinstance(m, dict):
            mats.append({"材料": str(m.get("材料") or m.get("title") or "")[:120],
                         "版本": str(m.get("版本") or m.get("version") or "")[:60],
                         "可用": bool(m.get("可用", True))})

    # 4. 计划进度摘要(只摘要,不代角色规划)
    plan_progress = None
    if state.plan_steps():
        plan_progress = {"plan_revision": state._d["plan"]["plan_revision"],
                         "steps": [{"id": s["id"], "title": s["title"],
                                    "status": s["status"]} for s in state.plan_steps()]}

    # 5. 待决问题带进包(§4.2:需作者决定事项)
    prepared = {
        "prepared_turn_id": prepared_id, "request_id": rid, "attempt": 1,
        "task": state.to_dict(),
        "task_binding": "bound",
        "constraints": constraints[:8],
        "history_tail": history_tail,
        "materials": mats,
        "tools": list(tool_names or []),
        "output_contract": (output_contract or "")[:400],
        "plan_progress": plan_progress,
        "pending_questions": list(state._d.get("pending_questions") or [])[-3:],
        "budget": {"chars": budget_chars},
        "events": events,
    }
    if not created:
        # 既有任务承接:发一条带当前 revision 的 task_status(§9 前端据此刷新)
        prepared["events"] = events + [ts.t_event("task_status", state, request_id=rid,
                                                  status=state.status,
                                                  phase=state._d.get("phase", ""))]
    return prepared


def _tail(conversation_tail: list | None, max_items: int = 6, max_chars: int = 500) -> list:
    """对话尾部预算裁剪:保最近 max_items 条,每条截 max_chars(§6 步骤 2)。"""
    out = []
    for h in list(conversation_tail or [])[-max_items:]:
        if not isinstance(h, dict):
            continue
        txt = str(h.get("text") or h.get("content") or "").strip()
        if not txt:
            continue
        out.append({"role": h.get("role") or h.get("who") or "",
                    "text": txt[:max_chars]})
    return out


# ── 计划更新解析(§13.4:小结构 + 无效只诊断)────────────────────────────────
_PLAN_HEAD = "TASK_PLAN:"
_MAX_STEPS = 8


def strip_plan_segment(text: str) -> str:
    """R-04（2026-11-10）：剥掉答复尾部的 TASK_PLAN 协议段，只留正文/结论。

    从首个行首 `TASK_PLAN:` 起整块去掉（含其后编号步骤）。**不做丢弃式截断**——
    结论常紧邻协议段之前，必须原样留下；协议段之前的内容一个字不动。
    没有协议段则原文返回；只有协议段时返回空串（本轮没有可摘要的正文）。
    """
    raw = str(text or "")
    lines = raw.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.strip().startswith(_PLAN_HEAD):
            return "".join(lines[:i]).rstrip()
    return raw


def parse_plan_update(text: str) -> dict | None:
    """从答复文本里解析轻量计划更新段(文本协议等价路径)。

    形如:
        TASK_PLAN: 简短变化原因
        1. 步骤一 [done]
        2. 步骤二
        3. 步骤三 [skip: 不再需要,因为…]
    解析失败返回 None(调用方保留上一份有效计划,不得当任务完成,§4.2)。
    返回 {reason, steps:[{title,status,skipped_reason}]}。
    """
    if not text:
        return None
    lines = text.splitlines()
    idx = next((i for i, ln in enumerate(lines)
                if ln.strip().startswith("TASK_PLAN:")), -1)
    if idx < 0:
        return None
    reason = lines[idx].split(":", 1)[1].strip()[:120]
    steps = []
    for ln in lines[idx + 1: idx + 1 + 2 * _MAX_STEPS]:
        s = ln.strip()
        if not s:
            if steps:
                break
            continue
        import re
        m = re.match(r"^(\d{1,2})[.、)]\s*(.+)$", s)
        if not m:
            if steps:
                break
            continue
        body = m.group(2)
        status = "pending"
        skipped = ""
        if "[done]" in body.lower():
            status, body = "completed", body.replace("[done]", "").replace("[DONE]", "")
        elif body.lower().startswith("[skip") or "[skip:" in body.lower():
            status = "skipped"
            skipped = (body.split(":", 1)[1].rstrip("]") if ":" in body else "不再需要")[:120]
            body = body.split("]")[0] if ":" not in body else body
        steps.append({"title": body.strip()[:100], "status": status,
                      "skipped_reason": skipped})
        if len(steps) >= _MAX_STEPS:
            break
    if not steps:
        return None        # 只有标题没有步骤 = 无效更新,只留诊断
    return {"reason": reason, "steps": steps}


def plan_update_event_text(state: ts.TaskState, reason: str) -> str:
    """给 plan_updated t 事件的一句话载荷。"""
    n = len(state.plan_steps())
    done = sum(1 for s in state.plan_steps() if s["status"] == "completed")
    return f"计划 v{state._d['plan']['plan_revision']}:共 {n} 步,已完成 {done}({reason[:60]})"
