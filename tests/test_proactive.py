# -*- coding: utf-8 -*-
"""「作者智库」八人的主动性。

需求背景:「作者智库的角色不够主动,能否让他们对作者更具启发性,像领航员一样
锲而不舍地完善内容,直到作者表示拒绝或要离开。」

落地三件事(都不靠模型自觉):
1. **主动性段落由代码注入**,不写进书内 agents 副本 —— 老书的角色提示词是各自一份,
   改出厂模板碰不到它们;注入才能一次改完全部书。
2. **跨轮承接**:上一轮它自己写的「下一步:…」会在下一轮 prompt 里被摆出来,
   作者只回一句「继续」时不会重新问一遍。
3. **不收工**:一轮结束时既没提候选卡、也没问任何问题 → 当场催一次(只催一次)。
"""
from __future__ import annotations

import pytest

from loom import agent_chat, proactive


class Echo:
    """假后端:按脚本依次返回文本;记下每次的 system/user 便于断言。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None):
        self.calls.append((system, user))
        out = self.replies.pop(0) if self.replies else ""
        if on_chunk and out:
            on_chunk(out)
        return out


def _run(project, role, text, backend):
    events = []
    agent_chat.run_turn(project, role, text, backend,
                        protocol="text", emit=events.append, ts="t1")
    return events


def test_智库角色拿到主动性段落(project):
    be = Echo(["先说结论:这块还缺代价。\n下一步:把逆息的代价写进力量体系。"])
    _run(project, "设定师", "帮我看看力量体系", be)
    assert proactive.INSTRUCTION.splitlines()[0] in be.calls[0][0]


def test_流水线角色不吃这段(project):
    be = Echo(["……\n下一步:随便。"])
    _run(project, "写手", "你好", be)
    assert proactive.INSTRUCTION.splitlines()[0] not in be.calls[0][0]


def test_只总结不收工会被催一次(project):
    """第一轮只说了句车轱辘话(无提问、无下一步、无候选卡)→ 应当催它一次。"""
    be = Echo(["这块确实还可以再丰富一些。", "好,我提一条:逆息每用一次折损三年寿元。\n下一步:确认这个代价。"])
    events = _run(project, "设定师", "力量体系怎么样", be)
    assert len(be.calls) == 2, [c[1][-60:] for c in be.calls]
    assert proactive.NUDGE[:12] in be.calls[1][1]
    # 催促的话只进模型上下文,不该出现在作者看到的正文里
    says = [e.get("text", "") for e in events if e.get("t") == "assistant"]
    assert all("提醒" not in s for s in says)


def test_已经问了问题就不再催(project):
    be = Echo(["现在还缺代价这一块。你更倾向「折寿」还是「失忆」？"])
    _run(project, "设定师", "力量体系怎么样", be)
    assert len(be.calls) == 1


def test_已经写了下一步就不再催(project):
    be = Echo(["这块缺代价。\n下一步:把代价补进力量体系。"])
    _run(project, "设定师", "力量体系怎么样", be)
    assert len(be.calls) == 1


def test_上一轮的下一步会承接进本轮(project):
    be = Echo(["这块缺代价。\n下一步:把逆息的代价补进力量体系。",
               "好,那就按折寿来。\n下一步:再确认一下有没有反制手段。"])
    _run(project, "设定师", "力量体系怎么样", be)
    _run(project, "设定师", "继续", be)
    assert "上一轮你自己说的「下一步」" in be.calls[1][1]
    assert "把逆息的代价补进力量体系" in be.calls[1][1]


@pytest.mark.parametrize("text,expect", [
    ("下一步:改这一格", True),
    ("你更想要哪一种?", True),
    ("要不要我把它写进人物卡", True),
    ("这块确实还能再丰富一些。", False),
    ("", False),
])
def test_收工判据(text, expect):
    assert agent_chat._actionable(text) is expect
