# -*- coding: utf-8 -*-
"""对白行 vs body 回填(作者 2026-10-21 裁定 B)。

`run_tool(body=...)` 是为了把**多行设定内容**(表格/列表/整段)接回「内容」参数;
但它同时会把对话里的对白行一起带进去(实测:`content` 变成 `"玄幻\n林三：这样如何？"`)。
边界定在这里:从第一行「人名 + 冒号」开头的行起,后面全部不收,并给出人话提示。
判据用人名册(agents._name_roster_for),不新造词表。
"""
from __future__ import annotations

from loom import partner_tools as pt
from loom import paths

SLOT = "外置大脑/设定集/00-立项.md#分区"


def _seed_char(project, head="主角·林三"):
    """人物卡的 H2 必须带「类型·名字」分隔符才会进专名册(不带的段名不进,防把段名当人名)。"""
    (project / paths.CHARS_REL).write_text(f"# 人物卡\n\n## {head}\n- 主角\n", encoding="utf-8")


def test_提设定_body里的对白行被截断并给出提示(project):
    _seed_char(project)
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "玄幻"},
                     ts="t", body="林三：这样如何？")
    assert ev["t"] == "proposal"
    assert ev["content"] == "玄幻", ev.get("content")
    assert "对白" in ev.get("note", ""), ev
    assert "林三" in ev.get("note", "")


def test_多行设定内容不被误截(project):
    """表格/列表这类多行设定必须原样收进来 —— 别把 body 机制一起废掉。"""
    _seed_char(project)
    ev = pt.run_tool(project, "提设定",
                     {"落点": "外置大脑/设定集/00-立项.md#题材", "内容": "东方玄幻"},
                     ts="t", body="| 字段 | 内容 |\n| --- | --- |\n| 核心 | 逆息折寿 |")
    assert ev["t"] == "proposal"
    assert "逆息折寿" in ev["content"]
    assert not ev.get("note"), "合法多行内容不该被截断"


def test_只在人名加冒号开头才截断(project):
    """「规则：…」这种设定行不是对白,不能因为它带冒号就被砍。"""
    _seed_char(project)
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "玄幻"},
                     ts="t", body="规则：逆息每用一次折损三年寿元")
    assert ev["content"].endswith("逆息每用一次折损三年寿元")
    assert not ev.get("note")


def test_名册为空时不按名字砍_但仍按附言规则剥(project):
    """没有人物名册 ⇒ 认不出「谁是说话人」,所以**不能按猜的名字**去砍设定。
    但 2026-10-23 起还有第二条判据:不以结构符开头、结尾是问句的短行,本来就是
    「说给作者听的话」,照样当附言剥掉(它不该写进设定集)。"""
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "玄幻"},
                     ts="t", body="林三：这样如何？")
    assert "这样如何" not in ev["content"], ev["content"]
    assert "这样如何" in ev.get("note", ""), ev
