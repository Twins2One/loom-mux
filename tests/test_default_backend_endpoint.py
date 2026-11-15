"""用户级默认后端的 HTTP 端点(走 TestClient,防「函数对但端点 500」的漏网,如 provider_catalog 未导入)。"""
from __future__ import annotations

import pytest

from conftest import require_http_transport  # noqa: E402

require_http_transport()
from starlette.testclient import TestClient  # noqa: E402

from loom import server  # noqa: E402
from loom.config import MAX_CONTEXT_LEN  # noqa: E402


def _client():
    return TestClient(server.app, base_url="http://127.0.0.1")


def test_get_default_backend_ok_when_unset():
    r = _client().get("/api/backend/default")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["has_default"] is False
    assert d["provider"] == "deepseek"
    assert isinstance(d["providers"], list) and len(d["providers"]) >= 9
    assert "keys_set" in d


def test_put_then_get_roundtrip():
    c = _client()
    r = c.put("/api/backend/default", json={"provider": "zhipu", "model": "glm-x",
                                               "api_key": "zp-1", "context_len": 256000})
    assert r.status_code == 200, r.text
    d = c.get("/api/backend/default").json()
    assert d["has_default"] and d["provider"] == "zhipu" and d["model"] == "glm-x"
    assert d["context_len"] == d["context_limit"] == 256000
    assert d["context_len_source"] == "手填"
    assert d["keys_set"]["zhipu"] is True


def test_default_context_limit_is_128k_when_unset():
    d = _client().get("/api/backend/default").json()
    assert d["context_len"] == 0
    assert d["context_limit"] == 128000
    assert d["context_len_source"] == "默认"


def test_study_context_only_slot_is_not_reported_as_following_default(monkeypatch):
    from loom import userconf

    monkeypatch.setattr(userconf, "load_default_backend", lambda: {
        "provider": "deepseek", "model": "default", "base_url": "", "context_len": 256000})
    monkeypatch.setattr(userconf, "load_user_routes", lambda: {
        "知识顾问": {"context_len": 64000}})

    rows = _client().get("/api/study/routes").json()["槽"]
    knowledge = next(row for row in rows if row["用途"] == "知识顾问")
    assert knowledge["跟随默认"] is False
    assert knowledge["provider"] == "deepseek"
    assert knowledge["model"] == "default"
    assert knowledge["context_len"] == knowledge["context_limit"] == 64000
    assert knowledge["context_len_source"] == "用途手填"


@pytest.mark.parametrize("bad", [-1, True, "256000"])
def test_put_rejects_invalid_context_len(bad):
    r = _client().put("/api/backend/default", json={"provider": "deepseek", "model": "x",
                                                     "context_len": bad})
    assert r.status_code in (400, 422), r.text


def test_default_backend_accepts_max_safe_context_len():
    c = _client()
    r = c.put("/api/backend/default", json={"provider": "deepseek", "model": "x",
                                                     "context_len": MAX_CONTEXT_LEN})
    assert r.status_code == 200, r.text
    assert c.get("/api/backend/default").json()["context_len"] == MAX_CONTEXT_LEN


def test_all_context_len_write_apis_reject_above_max_safe_integer(tmp_path):
    c = _client()
    too_large = MAX_CONTEXT_LEN + 1
    root = c.post("/api/project/create", json={"name": "安全整数边界", "parent": str(tmp_path)}).json()["root"]
    cases = [
        ("put", "/api/backend/default", {"provider": "deepseek", "model": "x",
                                           "context_len": too_large}),
        ("put", "/api/config", {"root": root, "provider": "deepseek", "model": "x",
                                  "chapter_chars": 800, "context_len": too_large}),
        ("post", "/api/study/routes", {"purpose": "知识顾问", "context_len": too_large}),
        ("post", "/api/routes", {"root": root, "purpose": "对话", "context_len": too_large}),
    ]
    for method, path, body in cases:
        r = getattr(c, method)(path, json=body)
        assert r.status_code == 400, (path, r.status_code, r.text)
        payload = r.json()
        assert str(MAX_CONTEXT_LEN) in (payload.get("error") or payload.get("reason") or ""), (path, payload)


def test_put_custom_requires_base_url():
    r = _client().put("/api/backend/default", json={"provider": "openai_compat", "model": "x", "api_key": "k"})
    assert r.status_code == 400   # 自定义要先填 base_url


# ---- 项目书架(用户级注册表):新建/导入/样例自动入架,欢迎页点选 ----

def test_shelf_records_on_create_and_open(tmp_path):
    c = _client()
    r = c.post("/api/project/create", json={"name": "书架测试甲", "parent": str(tmp_path)})
    assert r.status_code == 200, r.text
    root = r.json()["root"]
    shelf = c.get("/api/projects").json()["projects"]
    assert shelf and shelf[0]["root"] == root and shelf[0]["exists"] is True
    assert shelf[0]["title"] == "书架测试甲" and shelf[0]["chapters"] == 0
    # 再开另一本 → 提到最前;重开第一本 → 又回到最前(最近优先)
    c.post("/api/project/create", json={"name": "书架测试乙", "parent": str(tmp_path)})
    assert c.get("/api/projects").json()["projects"][0]["title"] == "书架测试乙"
    c.post("/api/project/open", json={"root": root})
    assert c.get("/api/projects").json()["projects"][0]["root"] == root


def test_shelf_forget_and_missing_dir(tmp_path):
    import shutil
    c = _client()
    root = c.post("/api/project/create", json={"name": "要消失的书", "parent": str(tmp_path)}).json()["root"]
    shutil.rmtree(root)                                     # 用户手动删了文件夹
    row = next(p for p in c.get("/api/projects").json()["projects"] if p["root"] == root)
    assert row["exists"] is False                            # 书架如实标灰,不炸
    c.post("/api/projects/forget", json={"root": root})
    assert all(p["root"] != root for p in c.get("/api/projects").json()["projects"])
