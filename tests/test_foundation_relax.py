# -*- coding: utf-8 -*-
"""地基体系放宽(「两个都改」)。

① **入口自由**:领航员不再把六步当必经顺序(先有人物 / 先有画面也能起手)。
② **门禁放**:卡章纲从 must 降为可跳 —— 短篇和「先写一章看看」不再被锁在门外。

两条都走**代码**(注入提示词 / 改门禁常量),所以对旧书同样生效。
"""
from __future__ import annotations

from loom import agents, journey


def test_门禁只剩三格():
    assert journey._GATE_STAGES == ("立项", "世界观", "人物")


def test_卡章纲没打也不算缺(project):
    ok, missing = journey.writing_unlocked(project)
    assert "卡章纲" not in missing, missing
    assert not ok          # 三格都还空着,当然还没解锁
    assert {"立项", "世界观", "人物"} & set(missing)


def test_领航员有入口灵活段_别的角色没有(project):
    nav = agents.load_agent(project, "领航员")
    assert "起手方式" in nav.system_prompt
    assert "先有一个画面" in nav.system_prompt
    assert "起手方式" not in agents.load_agent(project, "设定师").system_prompt
