"""T2（2026-11-11）：大纲师 B 级维度必须进**产出规格**，不能只停在判定表。

判定表（docs/世界信息充足度与开织确认-v3终稿-20261111.md 第 31 行）已列四项：
明线清单（起止章节+承担角色）、暗线清单（埋设点+揭示点）、支线与主线交叉点、
每卷情绪目标。本测试锁住「提示词模板里也要求按这个形状产出」，A 级内容不动。
"""
from __future__ import annotations

from pathlib import Path

TEMPLATE = (Path(__file__).resolve().parent.parent
            / "loom" / "templates" / "agents" / "大纲师.md")


def _text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_outline_template_requires_four_b_level_items():
    body = _text()
    for key in ("明线清单", "暗线清单", "交叉点", "每卷情绪目标"):
        assert key in body, f"产出规格缺 B 级维度：{key}"


def test_b_level_shapes_are_specified():
    body = _text()
    # 明线：起止章节 + 承担角色；暗线：埋设点 + 揭示点
    for key in ("起止章节", "承担角色", "埋设点", "揭示点"):
        assert key in body, f"B 级条目缺字段：{key}"


def test_a_level_and_existing_structure_preserved():
    body = _text()
    # A 级口径仍写明「开织前必须有」，且原三级结构分工与速记本段落仍在
    assert "开织前必须有" in body
    assert "## 三级结构的分工（别越界）" in body
    assert "速记本-大纲师.md" in body
