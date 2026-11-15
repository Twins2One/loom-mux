"""T4（2026-11-11）：领航员专属记录本 —— 书间隔离，可记「世界信息缺口观察」。

刻意不并入 ALL_ROLES（那张表驱动智库/引擎名册与卡片）；只放开速记本读写入口。
"""
from __future__ import annotations

from loom import scratchpad as sp


def test_navigator_notebook_path_is_per_book(tmp_path):
    a, b = tmp_path / "书A", tmp_path / "书B"
    pa = sp.scratchpad_path(a, sp.NAVIGATOR_ROLE)
    pb = sp.scratchpad_path(b, sp.NAVIGATOR_ROLE)
    assert pa.name == "速记本-领航员.md"
    assert pa != pb, "两个书根必须落到各自的记录本（书间隔离）"
    assert a in pa.parents and b in pb.parents


def test_navigator_notebook_append_and_read_back(tmp_path):
    sp.append_labeled(sp.NAVIGATOR_ROLE, tmp_path, "世界信息缺口：矿脉来源未交代。",
                      author_note="世界信息缺口观察")
    snippets = sp.load_snippets(sp.NAVIGATOR_ROLE, tmp_path, max_entries=5)
    assert snippets and "矿脉来源未交代" in snippets[-1]["body"]
    assert snippets[-1]["note"] == "世界信息缺口观察"


def test_navigator_notebook_is_isolated_between_books(tmp_path):
    a, b = tmp_path / "书A", tmp_path / "书B"
    sp.append(sp.NAVIGATOR_ROLE, a, "属于书A的观察")
    assert sp.load_snippets(sp.NAVIGATOR_ROLE, a)
    assert sp.load_snippets(sp.NAVIGATOR_ROLE, b) == [], "书B不得读到书A的记录"


def test_navigator_is_not_added_to_role_roster(tmp_path):
    """名册不被污染：领航员不是智库/引擎成员，也不进 list_scratchpads。"""
    assert sp.NAVIGATOR_ROLE not in sp.ALL_ROLES
    sp.append(sp.NAVIGATOR_ROLE, tmp_path, "记录")
    assert sp.NAVIGATOR_ROLE not in sp.list_scratchpads(tmp_path)


def test_unknown_role_still_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        sp.append("不存在的角色", tmp_path, "x")
