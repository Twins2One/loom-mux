# -*- coding: utf-8 -*-
"""6c 表征测试（先红）：领航员 partner 通道未接任务准备层 + F4 续轮门。

锁定缺陷（历史会话 评估「部分接入」结论 + F4）：
1. partner.run_turn（领航员对话循环，真实模型 agent）不调 prepare_turn——
   复杂地基重建任务无跨轮承接（书内 agent_chat 有、伙伴通道无）；
2. F4：resumed_once 只打标无执行门——「一次接受最多一次续轮」无服务端强制。
"""
from __future__ import annotations

import pytest

from loom import task_state as ts
from loom import turn_prepare as tp


def test_领航员任务循环可承接(tmp_path):
    """6c-1：领航员建任务（复杂地基目标）→ 下一轮 prepare_turn 能承接
    （同 role「继续」定位到活动任务）——伙伴通道接入的前提。"""
    base = tmp_path / ".任务状态" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="领航员",
                         conversation_id="默认", goal="重建世界观地基,这是任务")
    tid = p1["task"]["task_id"]
    assert p1.get("task_binding") == "bound"
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="领航员",
                         conversation_id="默认", user_text="继续")
    assert (p2.get("task") or {}).get("task_id") == tid, \
        "领航员任务未被下一轮承接（伙伴通道接入前提不成立）"


class _NavBackend:
    """假领航员后端：回一句普通话（无工具行）——最小化收尾路径。"""
    def __init__(self, reply="地基扫过了。"):
        self.reply = reply

    def complete(self, system, user, *, max_chars=None, on_chunk=None, **kw):
        # 6c-1 核心断言素材：system 里必须含任务上下文块
        self.seen_system = system
        if on_chunk:
            on_chunk(self.reply[:5])
        return self.reply


def test_领航员任务事实进提示词(tmp_path):
    """6c-2：领航员对话轮的 system 必须含「当前任务」块（agent_chat 同款）。"""
    from loom import partner
    (tmp_path / "loom.toml").write_text("title = '测试书'\n", encoding="utf-8")
    base = tmp_path / ".任务状态" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="领航员",
                         conversation_id="默认", goal="重建世界观地基,这是任务")
    st = ts.load_snapshot(base, p1["task"]["task_id"])
    ts.commit(base, st, {"kind": "constraint", "content": "地基必须带代价",
                         "explicit": True}, expected_revision=st.revision)
    be = _NavBackend()
    seen = []
    partner.run_turn(tmp_path, "继续", be, emit=seen.append, ts="t1")
    sys_text = getattr(be, "seen_system", "")
    assert "当前任务" in sys_text, \
        "领航员 system 无任务上下文块（partner 通道未接准备层）"
    assert "重建世界观地基" in sys_text, "任务目标未进领航员提示词"
    assert "地基必须带代价" in sys_text, "任务约束未进领航员提示词"


def test_领航员简单对话零负担(tmp_path):
    """6c-3（对照）：无任务轮不注入任务块——开场/简单问答不受伙伴通道接入影响。"""
    from loom import partner
    (tmp_path / "loom.toml").write_text("title = '测试书'\n", encoding="utf-8")
    be = _NavBackend(reply="你好呀。")
    seen = []
    partner.run_turn(tmp_path, "你好", be, emit=seen.append, ts="t1")
    sys_text = getattr(be, "seen_system", "")
    assert "当前任务" not in sys_text, "无任务轮被注入任务块（简单问答零负担破坏）"


def test_续轮门_resumed_once消费后拒绝(tmp_path):
    """F4：一次接受最多一次续轮——resumed_once 打标后第二次续轮请求被拒。

    服务端门：resume 轮的 decision_event_id 关联的任务已消费续轮
    （resumed_once=True 且已有一轮续跑记录）→ 本轮不再当续轮处理（降普通输入）。
    """
    from loom import usecases as uc
    base = tmp_path / ".任务状态" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="任务甲,这是任务")
    tid = p1["task"]["task_id"]
    st = ts.load_snapshot(base, tid)
    # 模拟：作者接受候选卡 → 决策回连打 resumed_once
    uc._link_decision_to_task(tmp_path, {"role": "设定师", "task_id": tid,
                                         "conversation_id": "默认"},
                              "p-1", accepted=True, ts="t")
    st = ts.load_snapshot(base, tid)
    assert st._d.get("resumed_once"), "前置：决策回连应打 resumed_once 标记"
    # 首次领取先于模型执行；第二次请求即使尚无 outcome 也必须拒绝。
    from loom import agent_chat as ac
    assert ac._resume_gate(tmp_path, "设定师", "默认", tid, "p-1") is True
    ts.commit_retry(base, st, {"kind": "outcome", "summary": "第一次续轮产出"})
    st = ts.load_snapshot(base, tid)
    # F4 门：任务已消费续轮 → 请求带 decision_event_id 的续轮应被识别为
    # 「续轮已用」，不再触发 resume 通道（验证接口存在且行为正确）
    from loom import agent_chat as ac
    gate = ac._resume_gate(tmp_path, "设定师", "默认", tid,
                           decision_event_id="p-1")
    assert gate is False, "已消费续轮的任务，第二次续轮未被门拒绝（F4 无执行门）"
    # 对照：未消费的任务门放行
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="批评家",
                         conversation_id="默认", goal="任务乙,这是任务")
    tid_b = p2["task"]["task_id"]
    ts.commit_retry(base, ts.load_snapshot(base, tid_b),
                    {"kind": "decision", "proposal_id": "p-2",
                     "decision_kind": "accepted", "ts": ts.now_ms()})
    st_b = ts.load_snapshot(base, tid_b)
    ts.commit_retry(base, st_b, {"kind": "resume_flag"})
    gate_b = ac._resume_gate(tmp_path, "批评家", "默认", tid_b,
                             decision_event_id="p-2")
    assert gate_b is True, "未消费续轮的任务被误拒（对照失败）"


def test_同一决策并发只领取一次(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from loom import agent_chat as ac
    base = tmp_path / ".任务状态" / "默认"
    p = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                        conversation_id="默认", goal="任务甲")
    st = ts.load_snapshot(base, p["task"]["task_id"])
    ts.commit(base, st, {"kind": "decision", "proposal_id": "p-atomic",
                         "decision_kind": "accepted"}, expected_revision=st.revision)
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=5)
        return ac._resume_gate(tmp_path, "设定师", "默认", st.task_id, "p-atomic")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sorted(results) == [False, True]
    assert not ac._resume_gate(tmp_path, "其他角色", "默认", st.task_id, "p-atomic")
    assert not ac._resume_gate(tmp_path, "设定师", "默认", st.task_id, "伪决策")


def test_领航员倒序返回不污染当前历史(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from loom import partner, partner_store
    (tmp_path / "loom.toml").write_text("title = '测试书'\n", encoding="utf-8")
    entered, release = Event(), Event()

    class Slow(_NavBackend):
        def complete(self, system, user, **kwargs):
            entered.set()
            assert release.wait(5)
            return "旧请求迟到产出"

    old_events, new_events = [], []
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(partner.run_turn, tmp_path, "你好", Slow(),
                              emit=old_events.append, ts="old")
        assert entered.wait(5)
        partner.run_turn(tmp_path, "新问题", _NavBackend("新请求产出"),
                         emit=new_events.append, ts="new")
        release.set()
        pending.result(timeout=5)
    history = partner_store.read_events(tmp_path)
    assert not any("旧请求迟到产出" in str(e) for e in history + old_events)
    assert any(e.get("text") == "新请求产出" for e in history)


def test_专家迟到答复不写历史(project):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from loom import agent_chat as ac
    entered, release = Event(), Event()

    class Slow(_NavBackend):
        def complete(self, system, user, **kwargs):
            entered.set()
            assert release.wait(5)
            return "旧专家请求产出"

    old_events, new_events = [], []
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(ac.run_turn, project, "设定师", "你好", Slow(),
                              emit=old_events.append, ts="old", protocol="text")
        assert entered.wait(5)
        ac.run_turn(project, "设定师", "新问题", _NavBackend("新专家产出，下一步：确认"),
                    emit=new_events.append, ts="new", protocol="text")
        release.set()
        pending.result(timeout=5)
    history = ac.history(project, "设定师")
    assert not any("旧专家请求产出" in str(e) for e in history + old_events)
    assert any("新专家产出" in str(e) for e in history)


def test_重复续轮不调用模型(project):
    from loom import agent_chat as ac
    from loom.paths import TASKS_DIR
    base = project / TASKS_DIR / "默认"
    p = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                        conversation_id="默认", goal="设计世界")
    state = ts.load_snapshot(base, p["task"]["task_id"])
    ts.commit(base, state, {"kind": "decision", "proposal_id": "p-once",
                           "decision_kind": "accepted"}, expected_revision=state.revision)

    class Count(_NavBackend):
        calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return "下一步：确认细节"

    be = Count()
    for _ in range(2):
        ac.run_turn(project, "设定师", "", be, emit=lambda e: None, protocol="text",
                    resume=True, task_id=state.task_id, decision_event_id="p-once")
    assert be.calls == 1


def test_接受决策别名共用领取键(tmp_path):
    from loom import agent_chat as ac
    base = tmp_path / ".任务状态" / "默认"
    p = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                        conversation_id="默认", goal="设计世界")
    state = ts.load_snapshot(base, p["task"]["task_id"])
    ts.commit(base, state, {"kind": "decision", "proposal_id": "p-alias",
                           "source_event": "confirm:p-alias", "decision_kind": "accepted"},
              expected_revision=state.revision)
    assert ac._resume_gate(tmp_path, "设定师", "默认", state.task_id, "confirm:p-alias")
    assert not ac._resume_gate(tmp_path, "设定师", "默认", state.task_id, "p-alias")


def test_拒绝准入不使合法请求失效(tmp_path):
    from loom import request_turn as rt
    first = rt.begin(tmp_path, "设定师", "会话一")
    assert rt.begin(tmp_path, "设定师", "会话一", authorize=lambda: False) is None
    assert rt.current(first)
    rt.begin(tmp_path, "设定师", "会话二")
    assert not rt.current(first)


def test_过期prepare不能暂停新任务(tmp_path):
    from loom import request_turn as rt
    base = tmp_path / ".任务状态" / "默认"
    old = rt.begin(tmp_path, "设定师", "默认")
    new = rt.begin(tmp_path, "设定师", "默认")
    p = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                        conversation_id="默认", goal="设计新任务", request_ticket=new)
    refused = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                              conversation_id="默认", goal="设计旧任务", request_ticket=old)
    assert refused["task_binding"] == "refused"
    assert ts.load_snapshot(base, p["task"]["task_id"]).status == "ready"


def test_归档主动退休旧请求(project):
    from loom import agent_chat as ac, request_turn as rt
    ticket = rt.begin(project, "设定师", "默认")
    ac.archive_current(project, "设定师", "archive")
    assert not rt.current(ticket)


def test_书房倒序完成保留新产出(tmp_path, monkeypatch):
    import asyncio
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    from loom import study
    from loom.study import ask as ask_module, library
    entered, release = Event(), Event()
    monkeypatch.setattr(library, "library_root", lambda: tmp_path)

    def ask(question, emit, **kwargs):
        if question == "old":
            entered.set()
            assert release.wait(5)
        emit({"t": "delta", "text": question.upper()})

    monkeypatch.setattr(ask_module, "ask", ask)

    async def consume(response):
        return [part async for part in response.body_iterator]

    first = study.ask_route(study.AskBody(question="old", role="知识顾问", goal="整理材料"))
    assert entered.wait(5)
    second = study.ask_route(study.AskBody(question="new", role="知识顾问"))
    asyncio.run(consume(second))
    release.set()
    asyncio.run(consume(first))
    base = tmp_path / ".loom-tasks" / "知识顾问" / "默认"
    snapshots = list(base.glob("*.快照.json"))
    state = ts.load_snapshot(base, snapshots[0].name.replace(".快照.json", ""))
    assert state._d["last_outcome"] == "NEW"


def test_工具返回后不再派发下一工具(project, monkeypatch):
    from loom import partner, partner_tools, request_turn as rt
    calls = []

    def tool(root, name, params, **kwargs):
        calls.append(name)
        rt.begin(root, "领航员", "默认")
        return {"t": "result", "text": "迟到工具结果"}

    monkeypatch.setattr(partner_tools, "run_tool", tool)
    backend = _NavBackend("用:看地基\n\n用:看地基")
    events = []
    partner.run_turn(project, "你好", backend, emit=events.append, ts="tools")
    assert len(calls) == 1
    assert not any(e.get("text") == "迟到工具结果" for e in events)


def test_重复续轮不会取消首份合法产出(project):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from loom import agent_chat as ac
    base = project / ".任务状态" / "默认"
    p = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                        conversation_id="默认", goal="设计世界")
    state = ts.load_snapshot(base, p["task"]["task_id"])
    ts.commit(base, state, {"kind": "decision", "proposal_id": "p-live",
                           "decision_kind": "accepted"}, expected_revision=state.revision)
    entered, release = Event(), Event()

    class Slow(_NavBackend):
        def complete(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return "合法续轮产出，下一步：确认"

    events = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(ac.run_turn, project, "设定师", "", Slow(),
                              emit=events.append, protocol="text", resume=True,
                              task_id=state.task_id, decision_event_id="p-live")
        assert entered.wait(5)
        ac.run_turn(project, "设定师", "", _NavBackend(), emit=lambda e: None,
                    protocol="text", resume=True, task_id=state.task_id,
                    decision_event_id="confirm:p-live")
        release.set()
        pending.result(timeout=5)
    assert any("合法续轮产出" in str(e) for e in ac.history(project, "设定师"))


def test_领航员取消留下停止原因(project):
    from loom import partner
    events = []
    partner.run_turn(project, "设计世界任务", _NavBackend(), emit=events.append,
                     ts="cancel", should_cancel=lambda: True)
    base = project / ".任务状态" / "默认"
    snap = next(base.glob("*.快照.json"))
    state = ts.load_snapshot(base, snap.name.replace(".快照.json", ""))
    assert state.status == "paused"
    assert state._d["stop_reason"] == "cancelled"


def test_书房不同会话保持独立(tmp_path, monkeypatch):
    import asyncio
    from threading import Event
    from loom import study
    from loom.study import ask as ask_module, library
    entered, release = Event(), Event()
    monkeypatch.setattr(library, "library_root", lambda: tmp_path)

    def ask(question, emit, **kwargs):
        if question == "old":
            entered.set()
            assert release.wait(5)
        emit({"t": "delta", "text": question.upper()})

    monkeypatch.setattr(ask_module, "ask", ask)

    async def consume(response):
        return [part async for part in response.body_iterator]

    first = study.ask_route(study.AskBody(question="old", role="知识顾问",
                            conversation_id="甲", goal="整理甲材料"))
    assert entered.wait(5)
    second = study.ask_route(study.AskBody(question="new", role="知识顾问",
                             conversation_id="乙", goal="整理乙材料"))
    asyncio.run(consume(second))
    release.set()
    asyncio.run(consume(first))
    for conversation, expected in [("甲", "OLD"), ("乙", "NEW")]:
        base = tmp_path / ".loom-tasks" / "知识顾问" / conversation
        snap = next(base.glob("*.快照.json"))
        assert ts.load_snapshot(base, snap.name.replace(".快照.json", ""))._d["last_outcome"] == expected


def test_领航员读工具达到上限留下停止原因(project, monkeypatch):
    from loom import partner, partner_tools
    monkeypatch.setattr(partner_tools, "run_tool", lambda *a, **k: {"t": "result", "text": "材料"})
    partner.run_turn(project, "设计世界任务", _NavBackend("用:看地基"),
                     emit=lambda e: None, ts="limit")
    base = project / ".任务状态" / "默认"
    snap = next(base.glob("*.快照.json"))
    state = ts.load_snapshot(base, snap.name.replace(".快照.json", ""))
    assert state.status == "blocked"
    assert state._d["stop_reason"] == "tool_round_limit"


def test_落笔记模型迟到不保存(project, tmp_path, monkeypatch):
    from loom import partner_tools, request_turn as rt
    from loom.study import material
    monkeypatch.setattr(material, "library_root", lambda: tmp_path / "library")
    ticket = rt.begin(project, "知识顾问", "默认")

    class Backend(_NavBackend):
        def complete(self, *args, **kwargs):
            rt.begin(project, "知识顾问", "默认")
            return "# 旧笔记\n旧材料"

    # material 的默认后端入口保持真实 ingest/save_note，仅后端用离线替身。
    from loom import backends
    from loom.study import ask
    monkeypatch.setattr(backends, "get_backend", lambda cfg: Backend())
    monkeypatch.setattr(ask, "studio_config", lambda: {})
    result = partner_tools.run_tool(project, "落笔记", {"标题": "旧材料", "稿子": "原稿"},
                                   ts="late", request_ticket=ticket)
    assert result.get("cancelled")
    assert not list((tmp_path / "library").rglob("*.md"))


def test_真实另起入口隔离旧答复(project):
    from loom import agent_chat as ac
    from loom.api.agents import AgentChatBody, agent_chat_new_ep

    class Backend(_NavBackend):
        def complete(self, *args, **kwargs):
            result = agent_chat_new_ep(AgentChatBody(root=str(project), role="设定师"))
            assert result["archived"]
            return "另起后的旧答复"

    events = []
    ac.run_turn(project, "设定师", "你好", Backend(), emit=events.append, protocol="text")
    assert not any("另起后的旧答复" in str(e) for e in ac.history(project, "设定师") + events)


def test_蒸馏迟到不保存派生产物(project, monkeypatch):
    from loom import partner_tools, request_turn as rt, stylepack
    ticket = rt.begin(project, "语料分析师", "默认")

    class Backend(_NavBackend):
        def complete(self, *args, **kwargs):
            rt.begin(project, "语料分析师", "默认")
            return "已整理"

    monkeypatch.setattr(stylepack, "parse_archive", lambda text: {k: "规则" for k in stylepack.LAYERS})
    result = partner_tools.run_tool(project, "蒸馏", {"标题": "迟到样本", "文本": "原稿"},
                                   ts="late", backend=Backend(), request_ticket=ticket)
    assert not stylepack._dist_dir(project, "迟到样本").exists()
    assert not (stylepack.pack_dir(project, "迟到样本") / stylepack.ARCHIVE_FILE).exists()
    # 已获准保存的输入原稿保留；模型返回后的派生内容不能提交。
    assert stylepack._docs_dir(project, "迟到样本").joinpath("迟到样本.md").exists()


def test_记忆向量迟到不提交事实(project, monkeypatch):
    from loom import partner_tools, request_turn as rt, memory
    ticket = rt.begin(project, "领航员", "默认")
    touched = []

    def embed(text):
        rt.begin(project, "领航员", "默认")
        return [0.0] * 4

    monkeypatch.setattr(memory, "_embed_one", embed)
    monkeypatch.setattr(memory, "_append_fact", lambda *a: touched.append(a))
    monkeypatch.setattr(partner_tools._handle_ji_yaodian, "_used", 0, raising=False)
    result = partner_tools.run_tool(project, "记要点", {"内容": "旧结论"},
                                   ts="late", request_ticket=ticket)
    assert result.get("cancelled")
    assert touched == []


def test_领航员高阈值状态可追溯(project):
    from loom import partner
    backend = _NavBackend("本轮结论")
    backend.last_usage = {"total_tokens": 1000000}
    partner.run_turn(project, "设计世界任务", backend, emit=lambda e: None, ts="high")
    base = project / ".任务状态" / "默认"
    snap = next(base.glob("*.快照.json"))
    state = ts.load_snapshot(base, snap.name.replace(".快照.json", ""))
    assert state.status == "blocked"
    assert state._d["stop_reason"] == "context_limit"


@pytest.mark.parametrize("kind", ["人格增补", "写作指纹"])
def test_特殊确认成功回连任务(project, monkeypatch, kind):
    from loom import usecases, partner_store, evolve, journey, agent_chat
    base = project / ".任务状态" / "默认"
    prepared = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                               conversation_id="默认", goal="设计世界")
    tid = prepared["task"]["task_id"]
    proposal = {"t": "proposal", "id": "p-special", "kind": kind, "角色": "设定师",
                "role": "设定师", "task_id": tid, "conversation_id": "默认",
                "内容": "增补", "content": "指纹", "段": "句式"}
    partner_store.append_event(project, proposal)
    monkeypatch.setattr(evolve, "confirm", lambda *a: project / "agents" / "设定师.md")
    monkeypatch.setattr(journey, "_land_fingerprint", lambda *a: "指纹落点")
    monkeypatch.setattr(journey, "journey_state", lambda *a: {})
    result = usecases.partner_confirm(project, "p-special", ts="confirm")
    assert "error" not in result
    assert agent_chat._resume_gate(project, "设定师", "默认", tid, "p-special")


@pytest.mark.parametrize("native", [False, True])
def test_书房过期工具返回后停止派发(monkeypatch, native):
    from loom.study import ask
    cancelled = False
    calls = []

    class Tools:
        @staticmethod
        def schema():
            return []
        @staticmethod
        def contract():
            return "工具约定"
        @staticmethod
        def run(name, args):
            nonlocal cancelled
            calls.append(name)
            cancelled = True
            return {"ok": True, "text": "迟到材料"}

    monkeypatch.setattr(ask, "_toolset_for", lambda role: Tools())
    monkeypatch.setattr(ask, "_parse_text_tools", lambda *a, **k: [("列书册", {}), ("列书册", {})])

    class Backend(_NavBackend):
        def complete_with_tools(self, *a, **k):
            return {"tool_calls": [{"id": str(i), "name": "list_books", "arguments": "{}"}
                                    for i in range(2)]}

    backend = Backend("工具输出") if native else _NavBackend("工具输出")
    result = ask._chat_with_tools(backend, "system", "user", lambda e: None, "t",
                                  should_cancel=lambda: cancelled)
    assert result == ""
    assert len(calls) == 1


