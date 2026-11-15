import hashlib

import pytest
from fastapi.testclient import TestClient
from loom.server import app


def test_rename_http_confirm_and_result(project):
    rel = "外置大脑/设定集/20-人物/主角·未命名.md"
    source = project / rel
    data = source.read_bytes()
    body = dict(root=str(project), rel=rel, name="主角·测试.md", base_hash=hashlib.sha256(data).hexdigest())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/file/rename", json=body).status_code == 400
        out = client.post("/api/file/rename", json={**body, "confirm": True})
        assert out.status_code == 200, out.text
        assert (project / out.json()["rel"]).read_bytes() == data


def test_incomplete_foundation_blocks_both_panel_endpoints(project):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for endpoint in ("plan", "say"):
            out = client.post("/api/panel/" + endpoint, json={"root": str(project), "text": "讨论"})
            assert out.status_code == 409, out.text
            assert out.json()["code"] == "foundation_incomplete"


def test_failed_shelf_storage_is_not_reported_success(monkeypatch, project):
    from loom import userconf
    def fail(*args):
        raise OSError("test storage unavailable")
    monkeypatch.setattr(userconf, "forget_project", fail)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        out = client.post("/api/projects/forget", json={"root": str(project)})
        assert out.status_code == 409
        assert out.json()["ok"] is False
