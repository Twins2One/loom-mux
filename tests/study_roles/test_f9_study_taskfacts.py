# -*- coding: utf-8 -*-
"""F9 表征测试（先红）：书房 PreparedTurn 与实际模型请求断开。

锁定缺陷现场（历史会话 深度审查 F9）：
study/__init__.py 生成了 _prepared（goal/约束/计划）并给 UI 发 task 事件，
但 _ask.ask 只收 question/history/volume/cancel/role——任务事实从未进模型提示词。
界面显示任务行，模型其实不知情：跨轮约束承接在书房域名不副实。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class _Rec:
    """记录 user 提示词的假后端。"""
    def __init__(self):
        self.users: list[str] = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None, **kw):
        self.users.append(user)
        return "书房答复。"


@pytest.fixture
def night_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "library").mkdir(parents=True)
    monkeypatch.setenv("LOOM_HOME", str(home))
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(home / "library"))
    import importlib
    import loom.config as _cfg
    importlib.reload(_cfg)
    yield home


def test_书房任务事实进模型提示词(night_home, monkeypatch):
    from loom.study import ask as study_ask
    from loom import turn_prepare as tp, task_state as ts
    # 预置一个带约束的任务（模拟前置轮折叠过约束）
    base = night_home / "library" / ".loom-tasks" / "知识顾问" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="study", role="知识顾问",
                         conversation_id="默认", goal="整理力量体系要点,这是任务")
    tid = p1["task"]["task_id"]
    st = ts.load_snapshot(base, tid)
    ts.commit(base, st, {"kind": "constraint", "content": "力量体系必须带代价",
                         "explicit": True, "scope": "任务"},
              expected_revision=st.revision)

    rec = _Rec()
    evs: list[dict] = []
    # ask 的 task/constraints 形参来自路由层传的 PreparedTurn（F9 分片4 接线）；
    # 这里直接传同形状（与 study/__init__.py worker 的传参一致）。
    st2 = ts.load_snapshot(base, tid)
    study_ask.ask("继续这个任务", evs.append, history=[], backend=rec, role="知识顾问",
                  task=st2.to_dict(),
                  constraints=[{"content": "力量体系必须带代价", "kind": "显式"}],
                  plan_progress=None, pending_questions=[])
    all_user = "\n".join(rec.users)
    assert "力量体系必须带代价" in all_user, \
        "任务约束未进书房模型的提示词（F9：任务行只是 UI 展示）"
    assert tid in all_user or "任务" in all_user, "任务身份/目标未进提示词"


def test_书房无任务时不注入任务块(night_home):
    from loom.study import ask as study_ask
    rec = _Rec()
    evs: list[dict] = []
    study_ask.ask("什么是三幕结构？", evs.append, history=[], backend=rec,
                  role="知识顾问")
    all_user = "\n".join(rec.users)
    assert "当前任务" not in all_user, "无任务时不得注入任务块（简单问答零负担）"


def test_书房伙伴分支同样注入任务事实(night_home):
    """历史回执 回执 P1（审查记录）：书房伙伴（role 空/书房伙伴默认分支）
    的任务事实同样进提示词——原分片4只接了知识顾问/语料分析师分支。"""
    from loom import turn_prepare as tp, task_state as ts
    from loom.study import ask as study_ask
    base = night_home / "library" / ".loom-tasks" / "书房伙伴" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="study", role="书房伙伴",
                         conversation_id="默认", goal="书房伙伴任务,这是任务")
    tid = p1["task"]["task_id"]
    st = ts.load_snapshot(base, tid)
    ts.commit(base, st, {"kind": "constraint", "content": "书房伙伴的约束",
                         "explicit": True, "scope": "任务"},
              expected_revision=st.revision)
    for role in ("", "书房伙伴"):
        rec = _Rec()
        evs: list[dict] = []
        study_ask.ask("继续", evs.append, history=[], backend=rec, role=role,
                      task=ts.load_snapshot(base, tid).to_dict(),
                      constraints=[{"content": "书房伙伴的约束", "kind": "显式"}])
        all_user = "\n".join(rec.users)
        assert "书房伙伴的约束" in all_user, \
            f"书房伙伴分支（role={role!r}）任务事实未进提示词（F9 三角色断链）"


def test_路由层真链路_约束进提示词(night_home, monkeypatch):
    """F9 端到端：POST /api/study/ask 显式 goal 建任务 → 折约束 → 第二轮
    「继续」时约束必须出现在发给模型的 user 里（路由层把 PreparedTurn 传给 ask）。"""
    import json as _json
    from fastapi.testclient import TestClient
    from tests.conftest import require_http_transport, FakeBackend
    require_http_transport()
    captured: list[str] = []

    def _responder(s, u):
        if isinstance(u, list):
            captured.append("".join(p.get("text", "") for p in u
                                    if isinstance(p, dict) and p.get("type") == "text"))
        else:
            captured.append(str(u))
        return "好。"

    monkeypatch.setattr("loom.backends.get_backend",
                        lambda _cfg: FakeBackend(responder=_responder))
    from loom.server import app as _app
    client = TestClient(_app, base_url="http://127.0.0.1:8611")

    def _post(payload):
        with client.stream("POST", "/api/study/ask", json=payload) as r:
            out = []
            for line in r.iter_lines():
                line = (line or "").strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    out.append(_json.loads(line))
                except ValueError:
                    continue
            return out

    evs = _post({"question": "整理力量体系要点", "role": "知识顾问",
                 "goal": "整理力量体系要点,这是任务", "conversation_id": "f9"})
    assert any(e.get("t") == "task_prepared" for e in evs), evs
    # 第二轮：折叠约束后「继续」
    captured.clear()
    evs2 = _post({"question": "记住：力量体系必须带代价。继续", "role": "知识顾问",
                  "conversation_id": "f9"})
    assert captured, "第二轮应发生模型调用"
    all_user = "\n".join(captured)
    assert "当前任务" in all_user, \
        "路由层未把任务事实传给模型（F9 断链仍在）"
    assert "力量体系必须带代价" in all_user, \
        "折进任务的约束未回注提示词（F9：约束承接名不副实）"
