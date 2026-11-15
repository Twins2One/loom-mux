"""导出方案与写盘的一致性(2026-10-21 回归)。

**真机 bug**:全书 10 章,选「按卷分」导出合计 **12 章**——`卷外章节.txt` 里 5、6 两章重复
(它们本来在第 2 卷)。根因:plan 给「卷外章节」只记了 `起4-止7` 这个**区间**,而它是个
**不连续**集合(只有 4 和 7),写盘时按区间取章就把夹在中间的 5、6 也收进来了。

修法:plan 给每个文件多带一份**显式章号清单** `章号`,写盘只认它。这里钉住四件事。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from loom import budget, export


@pytest.fixture
def book(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "书"
    (root / "正文").mkdir(parents=True)
    (root / "loom.toml").write_text('[novel]\ntitle="t"\n"章节字数"=800\n', encoding="utf-8")
    for n in range(1, 11):
        (root / "正文" / f"第{n}章.md").write_text(
            f"# 第{n}章 标题{n}\n\n第{n}章正文。\n", encoding="utf-8")
    return root


def _heads(p: Path) -> list[int]:
    return [int(m) for m in re.findall(r"第(\d+)章 标题", p.read_text(encoding="utf-8"))]


def _fake_volumes(ranges):
    return lambda root: ranges


# ── 1. 卷之间有缺口:卷外章节不许把别人的章重复收进来 ──────────────────

def test_卷外章节只收真正的卷外章(book, monkeypatch):
    """1-3 / 5-6 / 8-10 三卷,4 和 7 是卷外。旧实现会把 5、6 也塞进卷外章节。"""
    monkeypatch.setattr(budget, "volumes", _fake_volumes([(1, 3, "前段"), (5, 6, "中段"), (8, 10, "后段")]))
    pl = export.plan(book, fmt="txt", split="卷")
    assert pl["ok"]
    stray = [f for f in pl["文件"] if f["文件名"].startswith("卷外章节")][0]
    assert stray["章号"] == [4, 7]          # 关键:不是区间 [4..7]

    res = export.export(book, fmt="txt", split="卷")
    assert res["ok"], res
    assert res["合计章数"] == 10             # 不许变成 12
    assert _heads(book / "导出" / "卷外章节.txt") == [4, 7]
    # 每章恰好出现一次
    seen: list[int] = []
    for w in res["写出"]:
        seen += _heads(book / "导出" / w["文件"])
    assert sorted(seen) == list(range(1, 11))


# ── 2. 卷的起止重叠:同一章只进靠前那一卷 ──────────────────────────────

def test_卷重叠时每章只出现一次(book, monkeypatch):
    monkeypatch.setattr(budget, "volumes", _fake_volumes([(1, 6, "上"), (5, 10, "下")]))
    res = export.export(book, fmt="txt", split="卷")
    assert res["ok"], res
    assert res["合计章数"] == 10
    seen: list[int] = []
    for w in res["写出"]:
        seen += _heads(book / "导出" / w["文件"])
    assert sorted(seen) == list(range(1, 11))


# ── 3. 卷纲没盖全:卷外章节必须兜住,不许悄悄丢章 ──────────────────────

def test_卷纲没盖全时卷外章节兜底(book, monkeypatch):
    monkeypatch.setattr(budget, "volumes", _fake_volumes([(1, 3, "上")]))
    res = export.export(book, fmt="txt", split="卷")
    assert res["ok"], res
    assert res["合计章数"] == 10
    assert _heads(book / "导出" / "卷外章节.txt") == list(range(4, 11))


# ── 4. 同名整批拒绝(绝不覆盖) ────────────────────────────────────────

def test_同名文件已存在就整批拒绝(book):
    first = export.export(book, fmt="txt", split="书")
    assert first["ok"], first
    before = (book / "导出" / "全书.txt").read_bytes()
    again = export.export(book, fmt="txt", split="书")
    assert again["ok"] is False and again["code"] == "exists"
    assert (book / "导出" / "全书.txt").read_bytes() == before   # 一个字都没动


# ── 5. 打印稿(pdf)同样不许重复收章 ──────────────────────────────────

def test_打印稿按显式章号写(book, monkeypatch):
    monkeypatch.setattr(budget, "volumes", _fake_volumes([(1, 3, "上"), (5, 6, "中"), (8, 10, "下")]))
    res = export.export(book, fmt="pdf", split="卷")
    assert res["ok"], res
    assert res["方式"] == "打印"
    assert res["合计章数"] == 10
    stray = book / "导出" / "卷外章节.打印稿.html"
    assert stray.is_file()
    assert _heads(stray) == [4, 7]
