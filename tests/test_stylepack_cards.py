# -*- coding: utf-8 -*-
"""B2 卡片集注入(设计稿 §5):六节→句级/篇级映射 + 旧格式兼容 + 同层合并不覆盖。"""
from loom.stylepack import parse_archive

CARDS = """# 写手参考

## 文风规则
- 短句为主（证据：「他走了。」）

## 节奏规则
- 对话后接一句动作

## 人物塑造
- 配角先声音后外貌

## 叙事手法
- 场景切换用空行加时间状语

## 禁用清单
- ✗ 不由得

## 高频句式库
- 「……的时候，……已经……了。」
"""


def test_六节映射两层():
    p = parse_archive(CARDS)
    assert set(p) == {"句级", "篇级"}
    # 句级 = 文风规则 + 高频句式库(同层合并,不互覆盖)
    assert "短句为主" in p["句级"] and "已经" in p["句级"]
    # 篇级 = 节奏 + 人物 + 叙事手法 + 禁用清单(四节内容全在)
    assert "对话后接一句动作" in p["篇级"] and "不由得" in p["篇级"]
    assert "场景切换用空行" in p["篇级"] and "配角先声音" in p["篇级"]


def test_旧一级标题格式兼容():
    assert parse_archive("# 句级\n- 旧格式\n\n# 篇级\n- 旧篇级") == {
        "句级": "- 旧格式", "篇级": "- 旧篇级"}


def test_非节行不进():
    p = parse_archive(CARDS)
    assert "写手参考" not in p["句级"]
