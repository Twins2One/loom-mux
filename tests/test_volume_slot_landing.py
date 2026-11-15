# -*- coding: utf-8 -*-
"""卷纲衔接:journey 落盘与「提设定」两条路径都必须产出可解析的卷结构。"""
from __future__ import annotations

from conftest import FakeBackend, const

from loom import journey, slots, volplan
from loom.api.chapters import chapters_tree
from loom.partner_tools import _handle_tishe
from loom.paths import VOLUMES_REL


def _prime(project, stage: str):
    """直接种一张待答卡进游标(绕过出题,单测落盘)。"""
    from loom.state import load_state, save_state
    st = load_state(project)
    j = journey._journey(st)
    j["card"] = {"stage": stage, "sig": "x", "question": "测试题?", "options": []}
    st["journey"] = j
    save_state(project, st)


def _vol_spec():
    return next(s for s in journey.STAGES if s.key == "卷纲")


def _tree(root):
    return chapters_tree(str(root))


def test_volume_stage_slots_are_addressable_h2():
    # 无书根也能扫:容器不存在时给空 body,但槽位本身必须存在(否则提设定无落点)
    from pathlib import Path
    got = slots.stage_slots(Path("."), _vol_spec())
    assert [s.key for s in got] == ["第一卷", "第二卷", "第三卷"]
    assert all(s.at == "h2" and s.container == VOLUMES_REL for s in got)
    assert all(not s.filled for s in got)


def test_land_answer_digest_without_range_fills_from_numbers(project):
    _prime(project, "卷纲")
    fake = FakeBackend(const("## 第一卷\n这一卷从第1章打到第30章,主角走出故乡。"))
    out = journey.land_answer(project, "第一卷 1-30 章,主角走出故乡", fake)
    assert out.get("warning") is None
    text = (project / VOLUMES_REL).read_text(encoding="utf-8")
    assert "- 起止:第1-30章" in text
    parsed = [v for v in volplan.volume_ranges(project) if v.get("起") and v.get("止")]
    assert [(v["起"], v["止"]) for v in parsed] == [(1, 30)]
    assert _tree(project)["卷"][0]["状态"] != "卷纲缺起止"


def test_land_answer_keeps_explicit_range(project):
    _prime(project, "卷纲")
    fake = FakeBackend(const("## 第一卷\n- 起止:第1-40章\n- 卷名:启程\n- 该卷主线:走出故乡"))
    journey.land_answer(project, "第一卷 1-40 章,卷名启程", fake)
    text = (project / VOLUMES_REL).read_text(encoding="utf-8")
    assert "- 起止:第1-40章" in text
    tree = _tree(project)
    assert tree["卷"][0]["起"] == 1 and tree["卷"][0]["止"] == 40


def test_land_answer_without_any_range_warns_and_keeps_answer(project):
    _prime(project, "卷纲")
    fake = FakeBackend(const("## 第一卷\n这一卷讲复仇。"))
    out = journey.land_answer(project, "这一卷讲复仇和成长", fake)
    assert out.get("warning")
    text = (project / VOLUMES_REL).read_text(encoding="utf-8")
    assert "这一卷讲复仇" in text          # 答案绝不静默丢
    assert "- 起止:第A-B章" in text       # 明确提示,不是静默成功


def test_tishe_proposal_then_land_yields_parseable_volume(project):
    slot = f"{VOLUMES_REL}#第一卷"
    prop = _handle_tishe(project, 落点=slot, 内容="- 起止:第1-24章\n- 卷名:雪夜\n- 该卷主线:从废柴到执掌宗门")
    assert prop["slot"] == slot
    landed = journey._land_slot(project, prop["slot"], prop["content"])
    assert landed == VOLUMES_REL
    text = (project / VOLUMES_REL).read_text(encoding="utf-8")
    assert "## 第一卷" in text and "- 起止:第1-24章" in text
    tree = _tree(project)
    assert tree["卷"][0]["起"] == 1 and tree["卷"][0]["止"] == 24


def test_tishe_proposal_without_range_gets_mechanical_fill(project):
    slot = f"{VOLUMES_REL}#第二卷"
    prop = _handle_tishe(project, 落点=slot, 内容="第二卷接着第一卷,第31章到第60章推主线")
    journey._land_slot(project, prop["slot"], prop["content"])
    text = (project / VOLUMES_REL).read_text(encoding="utf-8")
    assert "- 起止:第31-60章" in text


def test_existing_stage_slots_not_regressed(project):
    by_key = {s.key: slots.stage_slots(project, s) for s in journey.STAGES}
    assert by_key["立项"]                      # 平台行 + 四格
    assert by_key["世界观"] and by_key["人物"]  # 目录容器
    assert by_key["卡章纲"]                     # 章行/大弧
    assert [s.key for s in by_key["卷纲"]] == ["第一卷", "第二卷", "第三卷"]
