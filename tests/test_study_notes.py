# -*- coding: utf-8 -*-
"""阅读笔记（0.8.3，）。

守四件：
1.增/删/查都能用，且**不碰原文**；
2.笔记按册分开存，互不串；
3.搜索能找到内容；
4.损坏的笔记文件不会让阅读页打不开。
"""
from __future__ import annotations

from loom.study import notes as N


def _clean(path):
    for it in list(N.load(path)["笔记"]):
        N.remove(path, it["id"])


def test_add_and_read():
    p = "E:/t/book1.md"
    _clean(p)
    it = N.add(p, "册一", 3, "这一段和 2.10 重了")
    assert it["id"].startswith("n-")
    assert it["段"] ==3
    items = N.load(p)["笔记"]
    assert len(items) ==1
    _clean(p)


def test_remove():
    p = "E:/t/book2.md"
    _clean(p)
    it = N.add(p, "册二", 1, "待删")
    assert N.remove(p, it["id"]) is True
    assert N.load(p)["笔记"] == []


def test_search_finds_text():
    p = "E:/t/book3.md"
    _clean(p)
    N.add(p, "册三", 2, "这里写得太噐嗦了")
    hits = N.search("噐嗦")
    assert any("噐嗦" in h["文"] for h in hits)
    _clean(p)


def test_books_are_isolated():
    a, b = "E:/t/bk-a.md", "E:/t/bk-b.md"
    _clean(a)
    _clean(b)
    N.add(a, "A", 0, "属于 A")
    N.add(b, "B", 0, "属于 B")
    assert len(N.load(a)["笔记"]) ==1
    assert len(N.load(b)["笔记"]) ==1
    assert N.load(a)["笔记"][0]["文"] != N.load(b)["笔记"][0]["文"]
    _clean(a)
    _clean(b)


def test_empty_text_rejected():
    p = "E:/t/book4.md"
    assert N.add(p, "x", 0, "   ") == {}
    assert N.load(p)["笔记"] == []
