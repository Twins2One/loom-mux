# -*- coding: utf-8 -*-
"""书房角色头像路由）：只认 PNG，别的与路径成分一律404。

为什么单测它：这条路由注册在 StaticFiles("/")之前，一旦它把请求放过去，
StaticFiles就会按规范化路径去读别的文件。所以「拒绝」这件事必须钉死，
不能指望浏览器或 curl的路径规范化替它兜。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loom import server


@pytest.fixture
def client():
    return TestClient(server.app, base_url="http://127.0.0.1:8611")


def test_存在的头像能取到(client):
    if not (server.STUDY_AVATARS / "partner.png").is_file():
        pytest.skip("这张头像还没生成")
    r = client.get("/study-avatars/partner.png")
    assert r.status_code ==200
    assert r.headers["content-type"].startswith("image/png")


def test_没有的名与非法后缀都404(client):
    assert client.get("/study-avatars/nope.png").status_code ==404
    assert client.get("/study-avatars/app.js").status_code ==404
    assert client.get("/study-avatars/x.txt").status_code ==404


def test_路径成分被拒(client):
    """带 ..或分隔符的一律404 ——不许它落到 StaticFiles手里去读别的文件。"""
    for bad in ("..%2Findex.html", "..%5Cindex.html", "sub%2Fx.png"):
        r = client.get("/study-avatars/" + bad)
        assert r.status_code ==404, (bad, r.status_code)
