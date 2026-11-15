# -*- coding: utf-8 -*-
"""「查知识」工具）。

需求背景：「书房写教材和素材，书内原“知识顾问”能否打包为一个工具或者 skill？
用来帮助“设定师”、“大纲师”、“情绪节奏设计师”、“战斗设计师”甚至更多角色
拓展思路和，增加节丰富度？」

守三件：
1.四个目标角色看得到，写手/领航员看不到（不该越权的不给）；
2.**只出提案、不落盘**（要落盘另走「提设定」）；
3.没后端就明说，不回一个假的“查过了”。
"""
from __future__ import annotations

import pytest

from loom import partner_tools as T


def test_工具已注册且有描述():
    spec = T.REGISTRY.get("查知识")
    assert spec is not None
    assert spec.params == ("问题", "范围")
    assert not spec.mutates, "它不该改盘"
    assert "未落盘" in spec.desc


@pytest.mark.parametrize("role", ["设定师", "大纲师", "战斗设计师", "情绪节奏设计师"])
def test_四个目标角色都看得到(role):
    assert "查知识" in T.visible_tools(role)


@pytest.mark.parametrize("role", ["写手", "章节细纲师", "领航员", "设定师", "润色师"])
def test_所有角色都看得到(role):
    """：知识顾问已工具化，**所有智能体都可调**。

    旧规则是"只有智库四人看得到"，那条已作废 —— 工具化就是为了让写手卡住时自己查。
    """
    assert "查知识" in T.visible_tools(role)


def test_空问题直接报错不发请求(tmp_path):
    with pytest.raises(ValueError) as ei:
        T._handle_cha_zhishi(tmp_path, "")
    assert "具体问题" in str(ei.value)


def test_没后端就明说不装成功(tmp_path):
    with pytest.raises(ValueError) as ei:
        T._handle_cha_zhishi(tmp_path, "明代秀才束发哪年改的", backend=None)
    assert "模型调用" in str(ei.value)
