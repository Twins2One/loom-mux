"""对话循环:一轮 = 追加 user 事件 → assemble → complete(流式行缓冲) → 解析说话+工具 →
有工具则执行+回喂再 complete(≤6 次) → 无工具则终结(spec §3)。

**每轮重建,两轮之间零挂起**(ADR 0013「答案单点注入」的形状平移):本模块不留任何
进程内状态——`run_turn` 一次调用做完一整轮就返回,下一轮从 `.伙伴对话/当前.jsonl`
尾部 + 书文件现状重建一切。崩溃语义平庸化:任何时刻杀进程,最多丢正在写的一行。

**流式行缓冲纪律**(spec §5.2,critical:协议行绝不许漏到作者屏幕上):`backend.complete()`
的 `on_chunk` 增量按行缓冲——整行落定且(剥装饰后)以「用:」开头才停止对外转发;
在此之前的所有整行**照发**给 `emit()`(预览态,不落盘;真正落盘的说话段来自
`complete()` 返回的完整文本经 `parse.parse_tool_block` 解析出的结果,两者内容一致,
预览只是让调用方(P3 的 ndjson)能在生成过程中就把安全的文字转发出去)。chunk 可能
在行内截断(如「用」与「:」分两个 chunk 到达)——只在遇到换行符时才判行,天然免疫;
流结束时缓冲区残留的未终结行,非触发行则随最后一批照发(`_stream_line_relay` 的
`flush`)。

**ts 由调用方给**(无 Date.now 依赖):同一 `ts` 贯穿一整轮的所有事件;工具执行的
`ts` 按轮内序号派生(`f"{ts}-{轮次}"`),满足 `partner_tools.run_tool` 文档要求的
「同一轮内多次 mutates 调用的 ts 唯一性由调用方保证」,不引入挂钟。
"""
from __future__ import annotations

from pathlib import Path

from . import backends as _backends
from . import journey, parse as parse_mod, partner_context, partner_store, partner_tools
from .parse import _TOOL_KV_RE, _TOOL_USE_RE, parse_tool_blocks  # noqa: F401  引用面保留

_MAX_TOOL_ROUNDS = 6   # 每轮工具调用上限(spec §4 常量表:每轮工具调用 ≤6 次)
_MAX_TOOL_FAIL_STREAK = 2   # 连续「解析失败」(botched 工具调用)上限(spec §5.2)
_MAX_TOOLS_PER_MSG = 5   # FB-B:一条消息里最多执行几个「用:」块（= 一次最多几张候选卡；作者 2026-10-20 要求 1-5 都可选）


# 后端能力探测搬进 backends.py(两条 agent 通道共用);这里保薄别名保引用面。
_accepts_kwarg = _backends.accepts_kwarg


# 流式协议行纪律(spec §5.2 critical)搬进 parse.py:写章通道(writeloop)与伙伴通道
# 共用同一份判据——「协议行绝不许漏到作者屏幕」这条只能有一个实现,不许各写一份会漂的。
# 这里保薄别名保引用面(同 S7 的老做法)。
_is_trigger_line = parse_mod.is_trigger_line
_strip_protocol_lines = parse_mod.strip_protocol_lines
_stream_line_relay = parse_mod.stream_line_relay


def _ctx_limit_of_backend(backend) -> int:
    """返回本轮路由后端的有效窗口；无路由标签的旧后端统一回退 128k。"""
    from .config import effective_context_len
    return effective_context_len(getattr(backend, "loom_context_len", 0))


def run_turn(root, user_text, backend, *, emit, ts, should_cancel=None) -> None:
    """一轮:追加 user 事件 → assemble → complete(流式行缓冲) → 解析说话+工具 →
    有工具则执行+回喂再 complete(≤6 次) → 无工具则终结。emit(event) 转发给调用方(P3 的 ndjson)。
    两轮之间零挂起。ts 由调用方给(无 Date.now)。

    should_cancel(可选,spec §10.3):无参可调,返回 True 则在**轮边界**(while 顶)提前
    return——「停」的落点。正跑的 `backend.complete()` 不在此处中断(不碰 Protocol);取消
    在下一次 complete 前生效。**只在本地判定,绝不传给 `backend.complete()`**(测试替身/CLI
    后端签名里没这参数,传了会 TypeError)。默认 None → 行为与加此参数前逐字一致。

    6c（2026-11-08，历史会话 评估「部分接入」）：领航员对话循环接任务准备层——
    明确任务目标建任务（goal 判定同 agent_chat._is_task_goal 口径）、任务事实
    （目标/有效约束/计划）注入 system、TASK_PLAN 即答即折、收尾推进任务状态。
    简单对话/开场/出题卡协议零变化（无任务轮零负担）。
    领航员任务落 .任务状态/默认（对话域，role=领航员）——任务状态只是临时执行
    恢复，**不新增长期记忆**（速记本高阈值路径是上下文守卫既有行为，非任务层）。
    """
    root = Path(root)
    from . import request_turn as _requests
    _ticket = _requests.begin(root, "领航员", "默认")
    _original_cancel = should_cancel

    def _cancelled():
        return not _requests.current(_ticket) or bool(_original_cancel and _original_cancel())

    should_cancel = _cancelled
    _external_emit = emit

    def emit(event):
        with _requests.effect(_ticket) as allowed:
            if allowed:
                _external_emit(event)

    emit._request_ticket = _ticket
    from .agent_chat import _is_task_goal, _task_context_block, \
        _fold_constraints_from_user_text, _apply_plan_update_from_text, \
        _finish_turn

    def _persist(event: dict) -> None:
        with _requests.effect(_ticket) as allowed:
            if not allowed:
                return
            event.setdefault("ts", ts)
            partner_store.append_event(root, event)
            emit(event)

    def _preview(text: str) -> None:
        # 实时增量预览(P3 渐进渲染用),不落盘、不是权威说话段——权威的那条来自下面
        # `_persist({"t": "assistant", ...})`,每条回复恰好一条,两者事件类型分开、不重复。
        if text.strip():
            emit({"t": "assistant_delta", "ts": ts, "text": text})

    user_text = (user_text or "").strip()
    # 界面状态标记不是作者的话（2026-11-12 用户报的缺陷，与 agent_chat 同一处根因）：
    # 作者整块复制候选卡改完发回来时，「✓ 已落库 → 路径」会跟着进正文，
    # 模型读到它便以为那一格已经落盘，不再把作者改过的内容当新提案复核。
    from .agent_chat import strip_ui_marks as _strip_ui_marks
    user_text, _ui_marks = _strip_ui_marks(user_text)
    if _ui_marks:
        emit({"t": "info", "ts": ts,
              "text": f"（已去掉 {_ui_marks} 处界面标记，如「✓ 已落库 → …」；那是卡片上画的，不是你写的。）"})

    # ── 6c 任务准备层（agent_chat.run_turn 同款，role=领航员/对话域）────────
    prepared = None
    try:
        from . import turn_prepare as _tp
        from .paths import TASKS_DIR
        base = root / TASKS_DIR / "默认"
        _goal = user_text if (user_text and _is_task_goal(user_text)) else ""
        prepared = _tp.prepare_turn(
            base_dir=base, domain="chat", role="领航员", conversation_id="默认",
            goal=_goal, user_text=user_text, request_id=f"{ts}-领航员",
            request_ticket=_ticket)
        if prepared is not None and prepared.get("task") is not None:
            for ev in prepared.get("events") or ():
                emit(ev)
    except Exception as _pe:     # noqa: BLE001 - 准备层失效:诊断 + 回退(§6)
        try:
            emit({"t": "status", "text": f"任务准备层这次没接上（{_pe}），按普通对话继续。"})
        except Exception:        # noqa: BLE001
            pass
        prepared = None
    _bound_task_id = ((prepared.get("task") or {}).get("task_id", "")
                      if prepared and prepared.get("task") else "__refused__")
    _binding = (prepared or {}).get("task_binding") if prepared else None
    if _binding == "refused":
        _bound_task_id = "__refused__"

    if not user_text:
        # spec §2 开场幂等(第二道保险,前端 _partnerAutoOpened 是第一道):空 text 绝不
        # 落一条永久空 user 事件——真开场(jsonl 尚无事件)仍要往下跑,让伙伴开场发言。
        # bug4下一步:落盘后(末事件=confirm)的空 text 是「自动引下一格」,放行让领航员接着
        # 按固定顺序引导(不落假 user 气泡);其余「已有事件」的空 text 才是重复触发 → no-op。
        events = partner_store.read_events(root)
        last = events[-1] if events else None
        if events and not (last and last.get("t") == "confirm"):
            return
    else:
        # 6c：作者分句立约同样折进领航员活动任务（bound 语义同 agent_chat）
        _fold_constraints_from_user_text(root, "领航员", "默认", user_text,
                                         emit, f"{ts}-领航员",
                                         bound_task_id=_bound_task_id)
        _persist({"t": "user", "text": user_text})

    def _stop(reason, status="blocked"):
        from . import task_state
        with _requests.effect(_ticket) as allowed:
            if not allowed or _bound_task_id == "__refused__":
                return
            with task_state._scope_lock(base):
                state = task_state.load_snapshot(base, _bound_task_id)
                if state is None or state.status not in ("ready", "running", "waiting_author"):
                    return
                task_state.commit(base, state, {"kind": "status", "status": status,
                                               "stop_reason": reason},
                                  expected_revision=state.revision)
                emit({"t": "task_status", "task_id": state.task_id,
                      "revision": state.revision, "status": state.status,
                      "stop_reason": reason})

    tool_rounds = 0
    tool_fail_count = 0   # 连续「解析失败」(botched 工具调用)计数;成卡/成工具即清零
    complete_kwargs: dict = {}
    if _accepts_kwarg(backend, "agent_mode"):
        # 伙伴通道:解除 CLI 后端的反 agent 护栏——允许输出一个「用:」工具块 + 反问
        # (spec §3)。真实工具执行始终留在 loom 服务端(见 partner_tools.run_tool),
        # 这里传的只是 prompt 层面的护栏开关。
        complete_kwargs["agent_mode"] = True

    def _on_reasoning(text: str) -> None:
        # v2 思考层:思考型后端(DeepSeek)的思维链 → transient reasoning_delta(emit 不 _persist,
        # 与 assistant_delta 同性质:纯 UI 灰字,不落盘、不进 assemble 回喂,免撑大归档/污染上下文)。
        if text.strip():
            emit({"t": "reasoning_delta", "ts": ts, "text": text})
    if _accepts_kwarg(backend, "on_reasoning"):
        complete_kwargs["on_reasoning"] = _on_reasoning
    while True:
        if should_cancel is not None and should_cancel():
            _stop("cancelled", "paused")
            return   # 轮边界取消(spec §10.3):user 事件已落,正跑的 complete 不在此处
        tail = partner_store.read_events(root)
        system, user = partner_context.assemble(root, tail)
        # 6c：任务事实块进 system（agent_chat 同款形状；无任务零负担）
        if prepared is not None and prepared.get("task") is not None:
            prepared = _requests.refresh(prepared, base)
            _tctx = _task_context_block(prepared)
            if _tctx:
                system = system + "\n\n" + _tctx
        on_chunk, flush = _stream_line_relay(_preview, flexible=True)
        raw = backend.complete(system, user, on_chunk=on_chunk, **complete_kwargs)
        if not _requests.current(_ticket):
            return
        flush()

        # 新架构:上下文守卫检查( 2026-10-19:40%/60%)
        # 2026-11-05（作者裁定「审核上限真实性」）修两处:
        # ①已用量优先取后端返回的真实 usage(与 writeloop 同款),取不到才估算;
        #   估算从「字符÷2」改「字符×0.8」——中文一字≈0.8~1.2 token,÷2 系统性低估一半,
        #   显示 40% 时真实可能已 70%+。
        # ②上限优先取模型声明的 context_len(用户级 backend.json 或书 loom.toml),兜底 128k。
        _bu = getattr(backend, 'last_usage', None)
        ctx_used = None
        if _bu and _bu.get('total_tokens') is not None:
            ctx_used = _bu['total_tokens']
        elif _bu and _bu.get('prompt_tokens') is not None:
            ctx_used = _bu['prompt_tokens']
        if ctx_used is None:
            ctx_used = int((len(system) + len(user)) * 0.8)   # 备选:估算(中文加权)
        # backend 已由 routes.get_backend_for(cfg, "对话") 选定；守卫必须消费同一
        # provider/model/purpose 的窗口，不能再回头读取用户默认后端。
        ctx_limit = _ctx_limit_of_backend(backend)
        from .context_guard import check as _guard_check, build_summary as _guard_summary
        from . import scratchpad as _scratchpad
        gr = _guard_check(ctx_used, ctx_limit)
        if gr.level == "high":
            # 高阈值:先尽力把这一轮的结论落到速记本,再收尾。
            #「压缩前提醒记速记」就在这里兑现:压缩会把旧对话
            # 换成摘要,速记本里没写下的东西就真没了。摘要用**模型这一轮的回答**,
            # 而不是一句写死的话——原来写的是「上下文接近上限,对话强制收尾」,
            # 下一轮读回来等于没记。
            summary = _guard_summary(
                role="领航员",
                conclusions=("尚未解析执行的模型草稿，不构成工具或保存回执(截断):\n" + str(raw or "").strip()[:1200])
                if str(raw or "").strip() else "-",
                open_questions="见上方本轮回复",
                next_steps="下一轮从这份摘要接着说",
            )
            saved = False
            try:
                with _requests.effect(_ticket) as allowed:
                    if not allowed:
                        return
                    _scratchpad.append("领航员", root, summary, author_note="上下文守卫·高阈值自动生成")
                    saved = True
            except Exception: # 速记本坏了不影响收尾
                pass
            _persist({"t": "assistant", "text": (
                "【系统】上下文接近上限，已保存未验证草稿到速记本并收尾。" if saved else
                "【系统】上下文接近上限，本轮收尾；速记本未保存成功，请保留当前对话。")})
            _persist({"t": "summary", "text": summary})
            _stop("context_limit")
            return
        if gr.level == "low":
            # 低阈值:在回复前插入警告
            _persist({"t": "assistant", "text": gr.message})
        elif gr.level == "prep":
            # 预备阈值(那道):不收尾、不压缩,
            # 只提醒模型赶快把待记项写进速记本。
            _persist({"t": "assistant", "text": gr.message})

        # known_params(终审①critical,与写章通道 writeloop 同一份纪律):白名单外的键立刻停手
        # 转 body,防中文对白行(合法的「键:值」形状)被 params 扫描误吞。
        say, tools = parse_tool_blocks(raw, valid_names=set(partner_tools.REGISTRY),
                                       known_params={n: s.params for n, s in partner_tools.REGISTRY.items()},
                                       flexible=True)   # 对话通道认 调用:/工具:/tool: 等换词写法
        # critical(spec §5.2):say 里可能混入未被选中的「用:」协议行(工具名瞎编、或
        # 误触发块排在真工具前)——落盘/emit 前必须过滤掉,绝不许漏到作者屏幕。
        say, botched = _strip_protocol_lines(say, flexible=True)
        if say and not tools and not botched:
            import re as _re
            if _re.search(r"(?:已(?:经)?|我(?:调用|提好|生成)|候选卡已).{0,12}(?:提设定|落库|保存|出卡|候选卡)", say):
                say = "【本轮未产生新的候选卡或确认回执，以下状态说法尚未核验】\n" + say
                emit({"t": "status", "ts": ts, "code": "proposal_unverified",
                      "text": "本轮没有生成候选卡；角色文字中的完成说法不代表已执行。"})
        if say:
            if botched and not tools:
                say = "【工具格式未识别，本段仅为未执行的模型说明】\n" + say
            _persist({"t": "assistant", "text": say})
            # 6c：TASK_PLAN 即答即折（agent_chat 同款；§8 先持久化结果）
            if "TASK_PLAN:" in say:
                _apply_plan_update_from_text(root, "领航员", "默认", say,
                                              emit, f"{ts}-p",
                                              bound_task_id=_bound_task_id)

        if tools:
            # FB-B 多候选:一条消息可连发多个「用:提设定」,逐个执行、各 emit 一张卡(引子 say
            # 已在上面先落,故顺序=话在前、卡在后)。超 _MAX_TOOLS_PER_MSG 截断防刷屏;
            # 每个工具计入 tool_rounds,撞 _MAX_TOOL_ROUNDS 立即收尾(总量上界不变)。
            tool_fail_count = 0
            emitted_proposal = False
            for tool in tools[:_MAX_TOOLS_PER_MSG]:
                if not _requests.current(_ticket):
                    return
                tool_rounds += 1
                _persist({"t": "tool", "name": tool["name"], "params": tool["params"]})
                # 终审①critical:把本轮手上的 backend 传下去——「学改法」的 handler 要发一次
                # LLM 调用蒸增补,没有 backend 会直接 ValueError(见 partner_tools._handle_xuegaifa)。
                # run_tool 只把它转给声明了 backend 形参的 handler(既有机制),读类工具不受影响。
                result_ev = partner_tools.run_tool(root, tool["name"], tool["params"],
                                                    ts=f"{ts}-{tool_rounds}", backend=backend,
                                                    body=tool.get("body") or "",
                                                    request_ticket=_ticket)
                if not _requests.current(_ticket):
                    return
                # 6c（F2 同款）：proposal 携带服务端任务绑定——领航员的卡回连精准
                if _bound_task_id and isinstance(result_ev, dict) \
                        and result_ev.get("t") == "proposal":
                    result_ev = dict(result_ev)
                    result_ev["task_id"] = "" if _bound_task_id == "__refused__" else _bound_task_id
                    result_ev["conversation_id"] = "默认"
                _persist(result_ev)
                if result_ev.get("t") == "proposal":
                    emitted_proposal = True
                if tool_rounds >= _MAX_TOOL_ROUNDS:
                    if emitted_proposal:
                        _finish_turn(root, "领航员", "默认", say, emit, ts, True,
                                     bound_task_id=_bound_task_id)
                    else:
                        _stop("tool_round_limit")
                    return
            if emitted_proposal:
                # 提了候选卡=交给作者拍板,本轮到此终结,不再 re-complete。真机实测:若继续
                # 重新 complete,模型会「二次质疑自己的格式、重提同样的卡」→ 重复候选卡(3→6)。
                # 读类工具(看地基/读文件)出的是 result 不是 proposal,不触发终结,仍 continue
                # 让模型据结果继续(如看完地基再提设定)。
                _finish_turn(root, "领航员", "默认", say, emit, ts, True,
                             bound_task_id=_bound_task_id)
                return
            continue

        if say and not botched:
            _finish_turn(root, "领航员", "默认", say, emit, ts, False,
                         bound_task_id=_bound_task_id)
            return   # 有实质说话内容,正常终结(模型说完话、没有下一步动作)

        if not botched:
            _finish_turn(root, "领航员", "默认", "", emit, ts, False,
                         bound_task_id=_bound_task_id)
            return   # 真无话可说也没有协议行残留,平常终结

        # 解析失败回喂(spec §5.2):模型想调工具但名字没认出来,say 被剥空——不当静默
        # 死轮:回喂纠正让模型自我纠正,计入轮内次数;连续 2 次 → 终结本轮并留痕。
        tool_fail_count += 1
        tool_rounds += 1
        if tool_fail_count == 1:
            emit({"t": "status", "ts": ts, "code": "tool_format_retry",
                  "text": "这次工具格式没有识别成功，尚未生成候选卡；正在重试。"})
        if tool_fail_count >= _MAX_TOOL_FAIL_STREAK:
            emit({"t": "error", "ts": ts, "code": "tool_unparsed",
                  "text": "工具格式连续两次未识别，本轮没有生成候选卡，也没有确认任何设定。请重新提交。"})
            journey._nav_trace(root, stage="", sig="", why="tool_unparsed", backend=backend, raw=raw)
            _stop("tool_unparsed")
            return
        if tool_rounds >= _MAX_TOOL_ROUNDS:
            _stop("tool_round_limit")
            return
        names = "、".join(partner_tools.REGISTRY)
        _persist({"t": "result", "error": f"工具名没认出来。可用工具:{names}"})
