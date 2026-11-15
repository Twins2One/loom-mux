# -*- coding: utf-8 -*-
"""红线：「立项卡只进设定师」——现在有了**结构性**判据(作者 2026-10-21 裁定 C:排除名单)。

以前的断言查不出问题,因为它比的是字符串成员(`"外置大脑/设定集/00-立项.md" not in reads`),
而 `reads` 里记的是**目录**(`外置大脑/设定集`),目录里当然不含那个字符串 → 恒真。

现在判据落在 `agents._READS_EXCLUDE` 上,这里直接验装配结果:
  · 章节设定师(负责立规矩的那一棒)**看得到**立项卡;
  · 章节细纲师 / 章节分镜师 / 责任编辑 **看不到**;
  · 但三条被排除的棒**仍然看得到世界观**(证明排除的是那一张卡,不是把设定集整包丢了)。
"""
from __future__ import annotations

from loom import agents, paths

CARD_MARK = "对标样本《某某书》—— 决策用"
WORLD_MARK = "本世界的规矩:灵气来自地脉"


def _seed(project):
    card = project / paths.SET_CARD_REL
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(f"# 立项卡\n\n{CARD_MARK}\n", encoding="utf-8")
    world = project / paths.WORLD_REL
    world.write_text(f"# 世界观\n\n{WORLD_MARK}\n", encoding="utf-8")


def _texts(project, role):
    _agent, items = agents._knowledge_items(project, 1, role)
    return {rel: text for rel, text in items}


def test_setter_sees_the_card(project):
    _seed(project)
    got = _texts(project, "章节设定师")
    assert paths.SET_CARD_REL in got, f"章节设定师该看到立项卡,实际拿到 {sorted(got)}"
    assert CARD_MARK in got[paths.SET_CARD_REL]


def test_other_steps_do_not_see_the_card(project):
    _seed(project)
    for role in ("章节细纲师", "章节分镜师", "责任编辑"):
        got = _texts(project, role)
        assert paths.SET_CARD_REL not in got, f"{role} 不该看到立项卡,实际拿到 {sorted(got)}"
        joined = "\n".join(got.values())
        assert CARD_MARK not in joined, f"{role} 的上下文里漏进了立项卡内容"


def test_exclusion_does_not_drop_the_rest_of_the_setbook(project):
    """排除的是那一张卡,不是整个设定集 —— 否则等于把「角色说没材料」那个老毛病修回来。"""
    _seed(project)
    for role in ("章节细纲师", "章节分镜师", "责任编辑"):
        got = _texts(project, role)
        assert paths.WORLD_REL in got, f"{role} 该看得到世界观,实际拿到 {sorted(got)}"
        assert WORLD_MARK in got[paths.WORLD_REL]


def test_writer_is_unaffected(project):
    """写手本来就不读设定集目录(它吃硬设定逐字块 + 状态账本),这张排除表不该影响它。"""
    _seed(project)
    got = _texts(project, "写手")
    assert CARD_MARK not in "\n".join(got.values())


def test_exclude_matches_by_file_name():
    """`exclude` 只比文件名:调用方传 `paths.SET_CARD_REL`(带目录)也能命中。"""
    from pathlib import Path

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "00-立项.md").write_text("x", encoding="utf-8")
        (d / "10-世界观.md").write_text("y", encoding="utf-8")
        kept = {f.name for f in agents._iter_dir_md(d, (paths.SET_CARD_REL,))}
        assert kept == {"10-世界观.md"}
        assert {f.name for f in agents._iter_dir_md(d)} == {"00-立项.md", "10-世界观.md"}
