# -*- coding: utf-8 -*-
"""作者智库八人的互相询问 + 代码级补读。

需求背景:「能否使得作者智库 8 人间各有联动,例如大纲师在安排情节时会自动询问
情绪节奏设计师和战斗设计师的意见。」
"""
from __future__ import annotations

import pytest

from loom import agent_chat, collab, partner_tools as pt
from loom.agents import load_agent


class Echo:
    def __init__(self, reply="打斗要写代价,不然读者会觉得无敌。"):
        self.reply = reply
        self.calls = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None):
        self.calls.append((system, user))
        return self.reply


def test_大纲师知道该问谁_流水线角色不知道():
    txt = collab.instruction("大纲师")
    assert "情绪节奏设计师" in txt and "战斗设计师" in txt
    assert "问同事" in txt
    assert collab.instruction("写手") == ""      # 流水线角色不吃这套


def test_问同事会真的发一次调用并拿回意见(project):
    collab.begin_turn("大纲师")
    be = Echo()
    ev = pt.run_tool(project, "问同事", {"角色": "战斗设计师", "问题": "这段打斗怎么写得有代价?"},
                     ts="t", backend=be)
    assert ev["t"] == "result", ev
    assert "代价" in ev.get("text", ""), ev
    assert len(be.calls) == 1
    assert "战斗设计师" in be.calls[0][0]          # 用的是对方的角色设定
    assert "这段打斗怎么写得有代价" in be.calls[0][1]


def test_每轮最多问两次(project):
    collab.begin_turn("大纲师")
    be = Echo()
    for i in range(collab.MAX_CONSULTS_PER_TURN):
        assert pt.run_tool(project, "问同事", {"角色": "战斗设计师", "问题": f"q{i}"},
                           ts=f"t{i}", backend=be)["t"] == "result"
    over = pt.run_tool(project, "问同事", {"角色": "战斗设计师", "问题": "再一次"},
                       ts="t9", backend=be)
    assert over["t"] == "result" and "问过" in over.get("error", "")
    assert len(be.calls) == collab.MAX_CONSULTS_PER_TURN


def test_新的一轮预算重置(project):
    collab.begin_turn("大纲师")
    be = Echo()
    pt.run_tool(project, "问同事", {"角色": "战斗设计师", "问题": "a"}, ts="t1", backend=be)
    collab.begin_turn("大纲师")                    # 下一轮
    assert pt.run_tool(project, "问同事", {"角色": "战斗设计师", "问题": "b"},
                       ts="t2", backend=be)["t"] == "result"


def test_问不存在的人会被拒(project):
    collab.begin_turn("大纲师")
    ev = pt.run_tool(project, "问同事", {"角色": "隔壁老王", "问题": "在吗"}, ts="t", backend=Echo())
    assert ev["t"] == "result" and ev.get("error")


def test_没有后端时说清楚(project):
    collab.begin_turn("大纲师")
    ev = pt.run_tool(project, "问同事", {"角色": "战斗设计师", "问题": "x"}, ts="t", backend=None)
    assert "模型调用" in ev.get("error", "")


def test_代码级补读把情绪曲线接进细纲师(project):
    """那份外部分析点名的孤岛:情绪曲线说"消费者是细纲师",而细纲师 reads 里没有它。"""
    p = project / "外置大脑/分析/情绪曲线.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# 情绪曲线\n\n第1章 憋 0.2 / 第2章 爆 0.9\n", encoding="utf-8")
    agent = load_agent(project, "章节细纲师")
    items = agent_chat._knowledge_items(project, agent)
    rels = [rel for rel, _t in items]
    assert any("情绪曲线" in r for r in rels), rels


@pytest.mark.parametrize("role", ["编辑总监", "语料分析师"])
def test_补读表里的角色都有条目(role):
    assert collab.EXTRA_READS.get(role)


def test_补读表里每一条路径在新书里都真实存在(project):
    """2026-10-25 回归:语料分析师那条曾写成 "loom/skills/去AI味.md" —— 书根下没有 loom/,
    于是「跳过缺失文件」静默吞掉,等于从来没读到过。补读路径必须与书内真实结构一致。"""
    for role, rels in collab.EXTRA_READS.items():
        for rel in rels:
            assert (project / rel).exists(), f"{role} 的补读路径不存在:{rel}"


def test_语料分析师真的读到了去AI味方法论(project):
    agent = load_agent(project, "语料分析师")
    items = agent_chat._knowledge_items(project, agent)
    rels = [rel for rel, _t in items]
    assert "skills/去AI味.md" in rels or any(r.endswith("去AI味.md") for r in rels), rels


# ── 2026-10-25 裁定:报告类产物要有落点;发现问题必须给选项 ──────────────────
def test_三个报告角色有落盘契约_其余没有():
    from loom import role_contracts as rc
    for role in ("战斗设计师", "编辑总监", "批评家"):
        txt = rc.contract_text(role)
        assert "外置大脑/分析/" in txt, role
    assert rc.contract_text("设定师") == ""


def test_分析目录下可以新建报告文件(project):
    from loom import slots
    plan = slots.plan_new_slot(project, "外置大脑/分析/战斗设计.md#第1卷·终战")
    assert plan["kind"] == "file" and plan["key"] == "第1卷·终战"


def test_口径统一_只挑不改的角色要给方案():
    from loom import role_contracts as rc
    for role in ("批评家", "编辑总监", "知识顾问"):
        txt = rc.advice_text(role)
        assert "2~3 个" in txt, role
    assert rc.advice_text("设定师") == ""
