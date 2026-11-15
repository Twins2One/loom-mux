# -*- coding: utf-8 -*-
"""候选卡里的「附言」：模型往往先写完设定正文，再补一句要对作者说的话
（「要不要我把这条也写进人物卡？」「另外我建议…」）。那句话不该落进设定集。

后加的判据：从第 2 行起，命中明确的「说话标志」才截，
截出来的那段放进候选卡的 note（界面单独显示、**永不落盘**）。
正文本身的问句（「他是谁？」）不会被误砍 —— 宁可漏判也不要错杀正文。
"""
from __future__ import annotations

from loom import partner_tools as pt

SLOT = "外置大脑/设定集/00-立项.md#分区"


def test_尾随的建议不进候选卡只当附言(project):
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "玄幻"}, ts="t",
                     body="| 字段 | 内容 |\n| --- | --- |\n| 核心 | 逆息折寿 |\n\n"
                          "要不要我把这条也写进人物卡？")
    assert ev["t"] == "proposal"
    assert "逆息折寿" in ev["content"], ev["content"]
    assert "要不要" not in ev["content"]
    assert "要不要" in ev.get("note", ""), ev


def test_附言以另外开头也会被剥掉(project):
    ev = pt.run_tool(project, "提设定", {"落点": "外置大脑/设定集/00-立项.md#题材",
                                         "内容": "东方玄幻"}, ts="t",
                     body="规则：逆息每用一次折损三年寿元\n另外我建议把寿元上限也定一下。")
    assert "折损三年寿元" in ev["content"], ev["content"]
    assert "另外" not in ev["content"]
    assert "另外" in ev.get("note", ""), ev


def test_正文里的问句不会被误剥(project):
    """设定正文自己带问句是正常的；只有从第 2 行起、命中说话标志才截。"""
    ev = pt.run_tool(project, "提设定", {"落点": "外置大脑/设定集/00-立项.md#题材",
                                         "内容": "东方玄幻"}, ts="t",
                     body="| 梗 | 说明 |\n| --- | --- |\n| 他是谁？ | 开篇悬念 |")
    assert "他是谁？" in ev["content"], ev["content"]
    assert not ev.get("note"), ev


def test_纯结构正文一条都不动(project):
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "玄幻"}, ts="t",
                     body="- 等级：凡→淬体→筑基\n- 代价：折寿")
    assert ev["content"].endswith("- 代价：折寿"), ev["content"]
    assert not ev.get("note"), ev
