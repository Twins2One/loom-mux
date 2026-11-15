# -*- coding: utf-8 -*-
"""知识顾问的最小可用后半段：③理解 → ④沉淀（作者 2026-10-27 晚裁定）。

守四件：
1. 空稿**不调模型**（不白花钱）；
2. 一次请求只调一次模型，事件流齐（status / delta / done）；
3. 落点只在 `知识库/素材/<科目>/`，**重名加后缀，绝不覆盖**；
4. 没接模型、模型报错、写不进盘，都给人话，不抛栈。
"""
from __future__ import annotations

import json

import pytest

from loom.study import material as M


class FakeBackend:
    def __init__(self, reply="# 标题\n\n## 一句话\n把节奏讲清楚了。\n\n## 要点\n- 慢写要先停一拍\n"):
        self.reply = reply
        self.seen: list[tuple[str, str]] = []

    def complete(self, system, user, on_chunk=None, **kw):
        self.seen.append((system, user))
        if on_chunk:
            on_chunk(self.reply)
        return self.reply


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "知识库"
    for sub in ("教材", "语料库", "素材"):
        (root / sub).mkdir(parents=True)
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(root))
    return root


# ── ③④ 主通路 ─────────────────────────────────────────────────────────────
def test_整理后落进素材层(lib):
    fake = FakeBackend()
    events: list[dict] = []
    M.ingest("非线性叙事", "稿子正文……", events.append, backend=fake)
    kinds = [e["t"] for e in events]
    assert kinds[0] == "status" and "delta" in kinds and kinds[-1] == "done", kinds
    done = events[-1]
    assert done["标题"] == "非线性叙事"
    saved = lib / "素材" / "取稿笔记" / "非线性叙事.md"
    assert saved.is_file()
    assert "慢写要先停一拍" in saved.read_text(encoding="utf-8")


def test_提示词里带标题与来源(lib):
    fake = FakeBackend()
    M.ingest("第 3 集", "正文", lambda ev: None, backend=fake, source="BV1xx411c7mD")
    _system, user = fake.seen[0]
    assert "【这份稿子的标题】第 3 集" in user and "BV1xx411c7mD" in user
    assert "【稿子】" in user


def test_科目决定落点目录(lib):
    M.ingest("某集", "正文", lambda ev: None, backend=FakeBackend(), subject="拆书·影视")
    assert (lib / "素材" / "拆书·影视" / "某集.md").is_file()


def test_重名不覆盖只加后缀(lib):
    """落盘的是**模型整理出来的笔记**，所以用不同的输出区分两份。"""
    M.ingest("同名稿", "稿一", lambda ev: None,
             backend=FakeBackend(reply="# 标题\n\n## 一句话\n第一版正文\n"))
    M.ingest("同名稿", "稿二", lambda ev: None,
             backend=FakeBackend(reply="# 标题\n\n## 一句话\n第二版正文\n"))
    d = lib / "素材" / "取稿笔记"
    names = sorted(p.name for p in d.glob("*.md"))
    assert names == ["同名稿(2).md", "同名稿.md"], names
    assert "第一版正文" in (d / "同名稿.md").read_text(encoding="utf-8")
    assert "第二版正文" in (d / "同名稿(2).md").read_text(encoding="utf-8")


def test_标题里的非法字符被换掉(lib):
    M.ingest('a/b\\c:d*e?"f<g>h|i', "正文", lambda ev: None, backend=FakeBackend())
    saved = list((lib / "素材" / "取稿笔记").glob("*.md"))
    assert len(saved) == 1 and "/" not in saved[0].name and ":" not in saved[0].name


def test_空稿不调模型(lib):
    fake = FakeBackend()
    events: list[dict] = []
    M.ingest("空", "   ", events.append, backend=fake)
    assert events and events[0]["t"] == "error" and events[0]["code"] == "empty_text"
    assert fake.seen == []
    assert not list((lib / "素材").rglob("*.md"))


def test_稿子太长会截断并告知(lib):
    fake = FakeBackend()
    events: list[dict] = []
    M.ingest("长稿", "字" * (M.MAX_TEXT_CHARS + 500), events.append, backend=fake)
    assert any("太长" in (e.get("text") or "") for e in events if e["t"] == "status")
    assert len(fake.seen[0][1]) < M.MAX_TEXT_CHARS + 200


def test_模型报错走事件且不落盘(lib):
    class Bad(FakeBackend):
        def complete(self, system, user, on_chunk=None, **kw):
            raise ValueError("模型那边 502 了")
    events: list[dict] = []
    M.ingest("某集", "正文", events.append, backend=Bad())
    assert events[-1]["t"] == "error" and "502" in events[-1]["text"]
    assert not list((lib / "素材").rglob("*.md"))


def test_没接模型给人话(lib, monkeypatch):
    def boom(_cfg):
        raise RuntimeError("deepseek key 没配")
    monkeypatch.setattr("loom.backends.get_backend", boom)
    events: list[dict] = []
    M.ingest("某集", "正文", events.append, backend=None)
    assert events[-1]["t"] == "error" and events[-1]["code"] == "no_backend"


# ── HTTP ─────────────────────────────────────────────────────────────────
def _client():
    from fastapi.testclient import TestClient
    from loom import server
    return TestClient(server.app, base_url="http://127.0.0.1:8611")


def test_接口返回ndjson并落盘(lib, monkeypatch):
    monkeypatch.setattr("loom.backends.get_backend", lambda _cfg: FakeBackend())
    events = []
    with _client().stream("POST", "/api/study/material",
                          json={"title": "某集", "text": "稿子正文"}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    assert events[0]["t"] == "status" and events[-1]["t"] == "done"
    assert (lib / "素材" / "取稿笔记" / "某集.md").is_file()
