# -*- coding: utf-8 -*-
"""夜间收口：真实 run_turn 全链交错测试（复审 审查记录 原案时序）
时序（原案）：A 经真实 run_turn 建任务并挂起 → new 暂停 A → **先建 B(ready)** 并留存
B 的 outcome/plan/revision/status → 放行 A 迟到返回 → 断言 B 全部不变 + A paused 不复活。
protocol 显式指定（native/text 各一遍）——_GateBackend 双方法都有，协议由 run_turn 参数控制。
"""
from __future__ import annotations

import pathlib
import threading

from loom import task_state as ts
from loom.paths import TASKS_DIR


class _GateBackend:
    """闸控假后端：双方法齐全（complete_with_tools + complete），闸控制返回。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.gate = threading.Event()
        self.calls_fc = 0
        self.calls_text = 0

    def complete_with_tools(self, messages, tools, **kw):
        self.calls_fc += 1
        self.gate.wait(timeout=20)
        return {"content": self.reply, "tool_calls": []}

    def complete(self, system, user_prompt, **kw):
        self.calls_text += 1
        self.gate.wait(timeout=20)
        return self.reply


def _interleave_case(tmp_path, monkeypatch, protocol: str):
    from tests.conftest import require_http_transport
    require_http_transport()
    from fastapi.testclient import TestClient
    from loom.server import app as _app
    import loom.agent_chat as ac
    import loom.api.agents as _api_agents

    client = TestClient(_app, base_url="http://127.0.0.1:8611")
    root = str(tmp_path)
    (tmp_path / "loom.toml").write_text('[book]\ntitle="t"\n', encoding="utf-8")
    role = "设定师"
    conv = "默认"
    base = pathlib.Path(root) / TASKS_DIR / conv

    backend = _GateBackend("A 的答复")
    monkeypatch.setattr(ac, "load_agent", lambda root, role: type(
        "A", (), {"system_prompt": "s", "name": role})())
    monkeypatch.setattr(ac, "_knowledge_items", lambda root, agent: [])
    monkeypatch.setattr(ac, "collab", type("C", (), {
        "begin_turn": staticmethod(lambda role: None),
        "instruction": staticmethod(lambda role: "")})())
    # HTTP 层 backend 打桩（B chat 走真 HTTP 需要）
    b_backend = _GateBackend("B 的答复")
    b_backend.gate.set()  # B 正常返回
    monkeypatch.setattr(_api_agents, "get_backend", lambda cfg: b_backend)

    events: list = []
    # ── A：真实 run_turn，闸未开=挂起 ──
    th = threading.Thread(target=lambda: ac.run_turn(
        root=pathlib.Path(root), role=role, user_text="重做力量体系,这是任务",
        backend=backend, emit=events.append, ts="tA", protocol=protocol,
        should_cancel=None, locked_slot="", resume=False), daemon=True)
    th.start()
    import time
    task_a_id = ""
    for _ in range(60):
        snaps = list(base.glob("*.快照.json")) if base.is_dir() else []
        if snaps:
            st = ts.load_snapshot(base, snaps[0].name.replace(".快照.json", ""))
            if st and st.status in ("ready", "running", "waiting_author"):
                task_a_id = st.task_id
                break
        time.sleep(0.1)
    assert task_a_id, f"A 未建任务; evtypes={[e.get('t') for e in events]}"

    # ── new 暂停 A ──
    r = client.post("/api/agent/chat/new", json={"root": root, "role": role})
    assert r.status_code == 200
    assert ts.load_snapshot(base, task_a_id).status == "paused"

    # ── **先建 B**（原案顺序：B 在放行 A 之前建立）──
    r = client.post("/api/agent/chat", json={"root": root, "role": role,
                                             "text": "整理人物卡,这是任务"})
    assert r.status_code == 200
    q = client.get("/api/agent/task", params={"root": root, "role": role,
                                              "conversation_id": conv, "active_only": "true"}).json()
    ids = [t["task_id"] for t in q.get("tasks", [])]
    assert len(ids) == 1, f"应只有 B: {ids}"
    b_id = ids[0]
    assert b_id != task_a_id
    # 留存 B 的快照基线（outcome/plan/revision/status）
    b_snap_before = ts.load_snapshot(base, b_id)
    b_rev_before = b_snap_before.revision
    b_outcome_before = b_snap_before._d.get("last_outcome", "")
    b_plan_before = b_snap_before._d.get("plan", {})
    b_status_before = b_snap_before.status

    # ── 放行 A 迟到返回，等线程结束 ──
    backend.gate.set()
    th.join(timeout=15)
    assert not th.is_alive()

    # ── 断言：B 全部不变 ──
    b_after = ts.load_snapshot(base, b_id)
    assert b_after.revision == b_rev_before, f"B revision 被推进: {b_rev_before}->{b_after.revision}"
    assert b_after._d.get("last_outcome", "") == b_outcome_before
    assert b_after._d.get("plan", {}) == b_plan_before
    assert b_after.status == b_status_before
    # A paused 不复活
    assert ts.load_snapshot(base, task_a_id).status == "paused"
    # A/B 事件无 error
    errs = [e for e in events if e.get("t") == "error"]
    assert not errs, f"事件含 error: {errs}"
    # 对应后端方法实际被调用（native->calls_fc / text->calls_text）
    if protocol == "native":
        assert backend.calls_fc >= 1, "native 路径 complete_with_tools 未被调用"
    else:
        assert backend.calls_text >= 1, "text 路径 complete 未被调用"


def test_交错_native_A迟到不推进B(tmp_path, monkeypatch):
    _interleave_case(tmp_path, monkeypatch, protocol="native")


def test_交错_text_A迟到不推进B(tmp_path, monkeypatch):
    _interleave_case(tmp_path, monkeypatch, protocol="text")
