# -*- coding: utf-8 -*-
"""候选卡自检:「让 agent 看一眼候选卡内容,决定要不要再改一次」)。

两条硬规矩,测试把它们钉住:
1. 模型只**指位置**,代码动刀 —— 只删,不改一个字;
2. 自检失败一律保持原样,不许因为它把一张本来能用的卡搞坏。
"""
from __future__ import annotations

import pytest

from loom import partner_tools as pt

SLOT = "外置大脑/设定集/00-立项.md#题材"


class Clean:
    """假后端:照稿回答。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None):
        self.calls.append((system, user))
        return self.reply


# ── ① 原生函数调用那条路也要剥附言(2026-10-25 的真 bug)────────────────────────
def test_native路径也剥附言(project):
    """以前只有「多行内容落进 body」时才剥;native 直接把内容参数送进卡里,一行没过滤。"""
    ev = pt.run_tool(project, "提设定",
                     {"落点": SLOT, "内容": "重生复仇流\n\n要不要我把这条也写进人物卡?"},
                     ts="t", body="")
    assert ev["t"] == "proposal"
    assert "要不要" not in ev["content"], ev["content"]
    assert ev["content"].strip() == "重生复仇流"
    assert ev.get("note")


def test_native路径也截对白行(project):
    # 人物册里有人在册时才会截;这里只验「不会把正常内容误伤」的反面:纯设定原样保留
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "重生复仇流,主角带记忆重来。"},
                     ts="t", body="")
    assert ev["content"] == "重生复仇流,主角带记忆重来。"


# ── ② 每张卡都看(作者 2026-10-25 选定,不做"可疑才看")────────────────────────
def test_干净的卡也会被看一眼(project):
    be = Clean("CLEAN")
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "重生复仇流。"},
                     ts="t", body="", backend=be)
    assert len(be.calls) == 1          # 每张卡都过一次,不再预筛
    assert ev["content"] == "重生复仇流。"
    assert not ev.get("reviewed")      # 说干净 → 一个字不动,也不弹说明


def test_自检的调用很短(project):
    """每张卡多一次调用,所以这次调用必须短:max_chars 压住,别让自检吃掉预算。"""
    seen = {}

    class Watch:
        def complete(self, system, user, *, max_chars=None, on_chunk=None):
            seen["max_chars"] = max_chars
            return "CLEAN"

    pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "重生复仇流。"},
                ts="t", body="", backend=Watch())
    assert seen["max_chars"] and seen["max_chars"] <= 200


def test_没有后端时一问不发(project):
    """自检是加分项:没后端就什么都不做,绝不报错。"""
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": "正文\n\n要不要我再补一条?"}, ts="t",
                     body="", backend=None)
    assert ev["t"] == "proposal"
    assert "要不要我再补一条" not in ev["content"]


# ── ③ 模型说 CLEAN ───────────────────────────────────────────────────────────
def test_模型说干净就一个字不动(project):
    be = Clean("CLEAN")
    out, why = pt._agent_review("正文第一行\n这段后面带问号,你是不是觉得太快了?", be)
    assert out == "正文第一行\n这段后面带问号,你是不是觉得太快了?"
    assert why == ""
    assert len(be.calls) == 1


# ── ④ 模型指位置 → 代码截断(只删不改)─────────────────────────────────────────
def test_模型指位置_代码从那儿截断(project):
    body = "第一条:主角带记忆回到十年前。\n第二条:记忆每用一次少一点。\n要不要我把这条也写进人物卡?"
    be = Clean("CUT: 要不要我把这条也写进")
    out, why = pt._agent_review(body, be)
    assert out == "第一条:主角带记忆回到十年前。\n第二条:记忆每用一次少一点。"
    assert "已截掉" in why
    # 保留下来的部分必须**逐字**是原文,不许被模型改写
    assert body.startswith(out)


def test_模型抄错位置就保持原样(project):
    body = "第一条:主角带记忆回到十年前。\n要不要我把这条也写进人物卡?"
    be = Clean("CUT: 这句话原文里根本没有出现过")
    out, why = pt._agent_review(body, be)
    assert out == body
    assert "没在原文里找到" in why


def test_截断太靠前就放弃(project):
    body = "要不要我继续?\n第一条:主角带记忆回到十年前。\n第二条:记忆每用一次少一点。"
    be = Clean("CUT: 要不要我继续")
    out, why = pt._agent_review(body, be)
    assert out == body          # 删了就没正文了 → 宁可不删
    assert "太靠前" in why


def test_模型胡答也保持原样(project):
    for junk in ["这个内容挺好的", "", "CUT:", "CUT: 短"]:
        body = "第一条:主角带记忆。\n要不要我再写一条?"
        out, why = pt._agent_review(body, Clean(junk))
        assert out == body, junk


# ── ⑤ 接进 run_tool:卡内容变干净,并在卡上留一句人话 ────────────────────────
def test_出卡前自检_卡变干净且留说明(project):
    # 注意:杂话**粘在同一行里**才算这道自检的活 —— 单独一行的附言上一层的机械清理就剥掉了
    be = Clean("CUT: 要不要我把这条也写进人物")
    ev = pt.run_tool(project, "提设定",
                     {"落点": SLOT, "内容": "重生复仇流。要不要我把这条也写进人物卡?"},
                     ts="t", body="", backend=be)
    assert ev["t"] == "proposal"
    assert ev["content"] == "重生复仇流。"
    assert ev.get("reviewed") is True
    assert "自检" in ev.get("note", "")


def test_自检说干净时_答复不会被当正文改掉(project):
    """模型回 CLEAN 时,卡内容必须**逐字**保持原样(不许顺手润色)。"""
    body = "重生复仇流。"
    ev = pt.run_tool(project, "提设定", {"落点": SLOT, "内容": body},
                     ts="t", body="", backend=Clean("CLEAN。" + "多余的话"))
    assert ev["content"] == body
    assert not ev.get("reviewed")
