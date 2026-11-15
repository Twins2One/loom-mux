"""回归：市场分析师的 agent_meta 必须可返回，并且三个入口不再 404。

来源：审查确认的 P1（2026-11-12）——`agent_api.agent_meta` 的硬编码映射缺「市场分析师」，
导致 `/api/agent/meta`、`/api/agent/chat`、`/api/agent/chat/new` 对该角色全部返回 404。

守的契约：
- 市场分析师：meta 可返回，字段与 app.js AUTHOR_META 口径一致；两个对话入口不再因角色未知而 404。
- 既有角色（设定师 / 人物侧写师）：行为不变。
- 角色未知：仍然 None / 404（不能因为补了一条就放宽成"什么都认"）。
"""
from __future__ import annotations

import pytest

from loom.agent_api import agent_meta
from loom.scratchpad import VISIBLE_AUTHOR_ROLES

ROLE = "市场分析师"


# ── 1. 函数级：meta 可返回且字段正确 ───────────────────────────────────────────
def test_市场分析师_meta可返回且字段正确():
    m = agent_meta(ROLE)
    assert m is not None, "市场分析师 meta 不应为 None（否则三个入口全 404）"
    assert m["name"] == ROLE
    assert m["role"] == ROLE
    assert m["category"] == "author"
    assert m["tag"] == "市场判断"                      # 与 app.js AUTHOR_META 一致
    assert m["produces"] == "市场分析报告"
    assert "卖点" in m["description"] and "违禁" in m["description"]
    assert isinstance(m["reads"], list) and m["reads"]
    assert "外置大脑/违禁词.md" in m["reads"]


def test_市场分析师确实在可见智库名册里():
    # 名册与 meta 必须同口径：名册里有、meta 却 404 ⇒ 界面有卡、点开就坏。
    assert ROLE in VISIBLE_AUTHOR_ROLES
    assert agent_meta(ROLE) is not None


# ── 2. 既有角色守原契约 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ["设定师", "人物侧写师", "批评家", "写手"])
def test_既有角色_meta不受影响(role):
    m = agent_meta(role)
    assert m is not None
    assert m["name"] == role


# ── 3. 角色未知仍守契约 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ["不存在的角色", "", "知识顾问X"])
def test_未知角色仍返回None(role):
    assert agent_meta(role) is None


# ── 4. HTTP 入口：meta / chat / chat/new 不再 404 ─────────────────────────────
def _client():
    from fastapi.testclient import TestClient

    from loom.server import app as _app

    # 本机 Host 闸要求（与 test_night5_wrapup 同口径）
    return TestClient(_app, base_url="http://127.0.0.1:8611")


def test_http_meta_市场分析师_200(tmp_path):
    r = _client().get("/api/agent/meta", params={"role": ROLE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == ROLE and body["tag"] == "市场判断"


def test_http_meta_未知角色仍404(tmp_path):
    r = _client().get("/api/agent/meta", params={"role": "不存在的角色"})
    assert r.status_code == 404


def test_http_chat_new_市场分析师_200(tmp_path):
    # 2026-11-12 P1 收口：chat/new 走 usecases.agent_new → archive_current →
    # agent_chat._chat_rel → collab._ALL_ROLES 校验。_ALL_ROLES 补上后应 200。
    root = str(tmp_path)
    (tmp_path / "loom.toml").write_text('[novel]\ntitle = "测试书"\n', encoding="utf-8")
    r = _client().post("/api/agent/chat/new", json={"root": root, "role": ROLE})
    assert r.status_code == 200, r.text


def test_http_chat_new_未知角色仍404(tmp_path):
    root = str(tmp_path)
    (tmp_path / "loom.toml").write_text('[novel]\ntitle = "测试书"\n', encoding="utf-8")
    r = _client().post("/api/agent/chat/new", json={"root": root, "role": "不存在的角色"})
    assert r.status_code == 404


# ── 5. chat 流内无 error（P1 收口：_ALL_ROLES 补上后 run_turn 不再抛"未知角色"）────
def test_http_chat_市场分析师_流内无error(tmp_path, monkeypatch):
    """POST /api/agent/chat 市场分析师 → ndjson 流里不得出现 t=="error" 事件。

    桩后端参照 test_night7_interleave._GateBackend 模式：
    complete_with_tools / complete 双方法齐全、gate 预开、返回固定字符串。
    monkeypatch load_agent / _knowledge_items / collab / get_backend。
    """
    import json as _json

    import loom.agent_chat as ac
    import loom.api.agents as _api_agents

    class _StubBackend:
        def complete_with_tools(self, messages, tools, **kw):
            return {"content": "市场分析报告：这本书的核心卖点是……", "tool_calls": []}

        def complete(self, system, user_prompt, **kw):
            return "市场分析报告：这本书的核心卖点是……"

    backend = _StubBackend()
    monkeypatch.setattr(ac, "load_agent", lambda root, role: type(
        "A", (), {"system_prompt": "s", "name": role})())
    monkeypatch.setattr(ac, "_knowledge_items", lambda root, agent: [])
    monkeypatch.setattr(ac, "collab", type("C", (), {
        "begin_turn": staticmethod(lambda role: None),
        "instruction": staticmethod(lambda role: "")})())
    monkeypatch.setattr(_api_agents, "get_backend", lambda cfg: backend)

    root = str(tmp_path)
    (tmp_path / "loom.toml").write_text('[novel]\ntitle = "测试书"\n', encoding="utf-8")

    r = _client().post("/api/agent/chat", json={
        "root": root, "role": ROLE, "text": "分析这本书的市场卖点"})
    assert r.status_code == 200, r.text

    events = []
    for line in r.iter_lines():
        line = line.strip()
        if not line:
            continue
        events.append(_json.loads(line))

    assert len(events) > 0, "流内应有事件"
    for ev in events:
        assert ev.get("t") != "error", f"流内不应出现 error 事件: {ev}"
