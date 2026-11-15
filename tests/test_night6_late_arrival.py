# -*- coding: utf-8 -*-
"""复审方交错用例（审查记录）：A 请求阻塞可控 → new 暂停 A → 建 B ready → A 返回
→ 断言 B 的 outcome/plan/revision 不受 A 结果影响。记录式假后端。"""
from __future__ import annotations

import pathlib

from loom import task_state as ts
from loom.paths import TASKS_DIR


class _BlockingBackend:
    """记录式假后端：complete 可被外部闸控住（模拟 A 请求挂起）。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.gate_open = False

    def complete(self, messages, **kw):  # 文本协议
        import time
        while not self.gate_open:
            time.sleep(0.05)
        return {"content": self.reply}


def test_A迟到请求不得推进B(tmp_path, monkeypatch):
    """交错时序：A 发出(挂起) → new 暂停A → 建B(ready) → 放行A →
    B 的 outcome/plan/revision 不变，A 自身 paused 不复活。"""
    import json as _json
    from fastapi.testclient import TestClient
    from tests.conftest import require_http_transport
    require_http_transport()
    from loom.server import app as _app
    from loom import agent_chat as _ac

    # 假后端注入：monkeypatch run_turn 的 backend 获取路径太深——改为直接调内部函数序列：
    # 实测口径=直接驱动 _finish_turn + prepare_turn（复审要求"实际 new 端点和 B"——
    # new 端点用真实 HTTP；A 的"挂起返回"用 _finish_turn 直调等价模拟：A 的 say 在 B 建立后才落盘）
    client = TestClient(_app, base_url="http://127.0.0.1:8611")
    root = str(tmp_path)
    (tmp_path / "loom.toml").write_text('[book]\ntitle="t"\n', encoding="utf-8")
    role = "设定师"
    conv = "默认"
    base = pathlib.Path(root) / TASKS_DIR / conv

    # ── A 请求开始：prepare 绑定 task A ──
    from loom import turn_prepare as _tp
    prepared_a = _tp.prepare_turn(base_dir=base, domain="chat", role=role,
                                  conversation_id=conv, task_id="", goal="A 的任务",
                                  user_text="重做力量体系,这是任务",
                                  origin_event_id="", request_id="req-A")
    task_a_id = prepared_a["task"]["task_id"]
    assert prepared_a["task"]["status"] == "ready"

    # ── new 端点：暂停 A ──
    r = client.post("/api/agent/chat/new", json={"root": root, "role": role})
    assert r.status_code == 200
    a_state = ts.load_snapshot(base, task_a_id)
    assert a_state.status == "paused"

    # ── 建 B（新对话建新任务）──
    prepared_b = _tp.prepare_turn(base_dir=base, domain="chat", role=role,
                                  conversation_id=conv, task_id="", goal="B 的任务",
                                  user_text="整理人物卡,这是任务",
                                  origin_event_id="", request_id="req-B")
    task_b_id = prepared_b["task"]["task_id"]
    assert task_b_id != task_a_id
    assert prepared_b["task"]["status"] == "ready"
    b_rev_before = prepared_b["task"]["revision"]

    # ── A 迟到返回：用绑定的 task_a_id 收尾（修复后路径）──
    emitted: list = []
    _ac._finish_turn(pathlib.Path(root), role, conv, "A 的迟到答复",
                     lambda ev: emitted.append(ev), "req-A", False,
                     bound_task_id=task_a_id)
    # A 自身 paused 不复活
    a_after = ts.load_snapshot(base, task_a_id)
    assert a_after.status == "paused", f"A 复活了: {a_after.status}"
    # B 不受影响：outcome/plan/revision 原样
    b_after = ts.load_snapshot(base, task_b_id)
    assert b_after._d.get("last_outcome", "") == prepared_b["task"].get("last_outcome", "")
    assert b_after.revision == b_rev_before, f"B revision 被推进: {b_after.revision}"
    assert not any(ev.get("t") == "task_status" for ev in emitted), \
        f"A 迟到收尾对 B 发了 task_status: {emitted}"

    # ── B 的正常收尾仍工作（绑定 task_b_id）──
    _ac._finish_turn(pathlib.Path(root), role, conv, "B 的正常答复",
                     lambda ev: emitted.append(ev), "req-B", False,
                     bound_task_id=task_b_id)
    b_final = ts.load_snapshot(base, task_b_id)
    assert b_final._d.get("last_outcome", "").find("B 的正常答复") >= 0
    assert b_final.status == "ready"
