# -*- coding: utf-8 -*-
"""新书不预分卷；只有确认/手工落盘卷纲后才出现卷结构。"""
from __future__ import annotations

import shutil

from fastapi.testclient import TestClient

from loom import journey, scaffold, volplan
from loom.paths import VOLUMES_REL
from loom.server import app


def _client():
    return TestClient(app, base_url="http://127.0.0.1")


def test_new_book_has_volume_file_but_no_parseable_volume(tmp_path):
    root = scaffold.init("不预分卷", tmp_path)
    volume = root / VOLUMES_REL
    assert volume.is_file()
    assert volplan.volume_ranges(root) == []
    text = volume.read_text(encoding="utf-8")
    assert "新书默认不分卷" in text
    assert "## 第一卷" not in text
    assert "起止:第1-" not in text
    volume_stage = next(s for s in journey.journey_state(root)["stages"] if s["key"] == "卷纲")
    assert volume_stage["done"] is False


def test_chapter_tree_returns_real_empty_array_and_top_level_guide(tmp_path):
    root = scaffold.init("空卷树", tmp_path)
    response = _client().get("/api/chapters/tree", params={"root": str(root)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["卷"] == []
    assert "领航员或大纲师" in body["引导"]
    assert "未命名卷" not in response.text


def test_confirmed_or_manual_volume_then_appears_without_touching_bytes(tmp_path):
    root = scaffold.init("确认后分卷", tmp_path)
    volume = root / VOLUMES_REL
    confirmed = "# 卷纲\n\n## 第一卷·启程\n- 起止:第1-12章\n- 该卷主线:离开故乡\n"
    volume.write_text(confirmed, encoding="utf-8")

    response = _client().get("/api/chapters/tree", params={"root": str(root)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["卷"]) == 1
    assert body["卷"][0]["卷"] == "第一卷·启程"
    assert body["卷"][0]["起"] == 1 and body["卷"][0]["止"] == 12
    assert body["引导"] == ""
    assert volume.read_text(encoding="utf-8") == confirmed


def test_frontend_empty_state_uses_server_guide_and_cache_stamp():
    from loom.api._common import WEBUI_DIR

    app_js = (WEBUI_DIR / "app.js").read_text(encoding="utf-8")
    index = (WEBUI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'const guide = (d && d["引导"])' in app_js
    assert "先找领航员或大纲师确认卷纲" in app_js
    assert "/app.js?v=20261116f" in index


def test_missing_volume_template_fallback_is_still_empty(tmp_path, monkeypatch):
    templates = tmp_path / "templates-copy"
    shutil.copytree(scaffold.TEMPLATES_DIR, templates)
    (templates / VOLUMES_REL).unlink()
    monkeypatch.setattr(scaffold, "TEMPLATES_DIR", templates)

    root = scaffold.init("无卷纲模板", tmp_path / "books")
    text = (root / VOLUMES_REL).read_text(encoding="utf-8")
    assert "确认前 Loom 不猜卷数" in text
    assert volplan.volume_ranges(root) == []
