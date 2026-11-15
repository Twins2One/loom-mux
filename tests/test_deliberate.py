# -*- coding: utf-8 -*-
"""商讨模式:作者智库八人「先谈,再改」,轮次由模型自己规划。

需求背景:「当前『作者智库』的运行模式太封闭了,能否改成和领航员一样,
经多轮商讨(轮次由模型自行规划)后再提出修改方案。」

这套的关键不是"让它多说话",是**让它知道自己正在谈第几轮** —— 所以下面钉的
都是跨轮记账与刹车,而不是文风。
"""
from __future__ import annotations

import pytest

from loom import agent_chat, deliberate as dl


def _say(project, role, text, **extra):
    ev = {"t": "assistant", "text": text, "ts": "t"}
    ev.update(extra)
    agent_chat._append_event(project, role, ev)


# ── 谁吃这套 ────────────────────────────────────────────────────────────────
def test_只给作者智库八人():
    for r in ("设定师", "大纲师", "批评家", "编辑总监"):
        assert dl.applies(r) and dl.instruction(r)
    for r in ("写手", "章节细纲师", "领航员", ""):
        assert not dl.applies(r) and dl.instruction(r) == ""


def test_提示词里写清了计划与轮次上限():
    t = dl.instruction("设定师")
    assert "商讨计划" in t
    assert f"最多谈 {dl.MAX_QA_ROUNDS} 轮" in t
    assert "下一步" in t
    # 关键:出卡之前先谈 —— 不能一锤子买卖
    assert "先跟作者谈清楚" in t


# ── 解析 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,want", [
    ("商讨计划:第 1 轮问清代价,第 2 轮出方案(预计 2 轮)", "第 1 轮问清代价,第 2 轮出方案(预计 2 轮)"),
    ("## 商讨计划：先定基调，再拆卷", "先定基调，再拆卷"),
    ("没有计划的一轮", ""),
    ("", ""),
])
def test_计划行解析(text, want):
    assert dl.plan_of(text) == want


# ── 跨轮记账 ────────────────────────────────────────────────────────────────
def test_出卡之后谈了几轮():
    evs = [
        {"t": "user", "text": "改设定"},
        {"t": "assistant", "text": "商讨计划:2 轮\n下一步:先问代价"},
        {"t": "assistant", "text": "那代价怎么定?"},
        {"t": "proposal", "id": "p-1"},
        {"t": "user", "text": "好"},
        {"t": "assistant", "text": "那这条就按你说的办"},
    ]
    assert dl.rounds_since_proposal(evs) == 1     # 出卡之后只谈了一轮


def test_拍板也清零():
    evs = [{"t": "assistant", "text": "a"}, {"t": "confirm", "id": "p-1"},
           {"t": "assistant", "text": "b"}]
    assert dl.rounds_since_proposal(evs) == 1


def test_没有出卡就一直累加():
    evs = [{"t": "assistant", "text": str(i)} for i in range(5)]
    assert dl.rounds_since_proposal(evs) == 5


def test_空文本不算一轮():
    evs = [{"t": "assistant", "text": "  "}, {"t": "assistant", "text": "真话"}]
    assert dl.rounds_since_proposal(evs) == 1


def test_从流水里读出计划与下一步(project):
    _say(project, "设定师", "商讨计划:第 1~2 轮\n先问代价。\n下一步:等你定代价的量级")
    prog = dl.progress(project, "设定师")
    assert prog["轮次"] == 1
    assert prog["计划"].startswith("第 1~2 轮")
    assert "代价的量级" in prog["下一步"]


# ── 刹车 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("rounds,want", [(0, ""), (1, ""), (2, ""), (3, "不要再提问"), (5, "不要再提问")])
def test_谈满就刹车(rounds, want):
    txt = dl.force_block(rounds)
    assert (want in txt) if want else (txt == "")


def test_刹车要求落到方案而不是又一轮问题():
    txt = dl.force_block(dl.MAX_QA_ROUNDS)
    assert "必须落到方案" in txt
    assert "候选卡" in txt


# ── 接进 agent_chat ─────────────────────────────────────────────────────────
def test_智库的工具往返比流水线角色宽():
    assert agent_chat._tool_rounds("大纲师") == dl.AGENT_TOOL_ROUNDS
    assert agent_chat._tool_rounds("写手") == agent_chat._MAX_TOOL_ROUNDS
    assert dl.AGENT_TOOL_ROUNDS > agent_chat._MAX_TOOL_ROUNDS


def test_轮次与计划会带进下一轮的提示词(project):
    _say(project, "设定师", "商讨计划:2 轮,先定代价\n下一步:问你代价量级")
    txt = agent_chat._build_user_prompt(project, "设定师", [], "继续", [], "")
    assert "你自己的商讨计划" in txt
    assert "第 2 轮" in txt                       # 上一轮算第 1 轮,这轮是第 2 轮
    assert dl_force_absent(txt)                   # 还没谈满,不该刹车


def dl_force_absent(txt: str) -> bool:
    return "(刹车)" not in txt


def test_谈满之后提示词里出现刹车(project):
    for i in range(dl.MAX_QA_ROUNDS):
        _say(project, "设定师", f"商讨第{i + 1}轮\n下一步:再问一条")
    txt = agent_chat._build_user_prompt(project, "设定师", [], "继续", [], "")
    assert "(刹车)" in txt
    assert "不要再提问" in txt


def test_流水线角色不吃商讨这套(project):
    _say(project, "写手", "商讨计划:2 轮\n下一步:等你定")
    txt = agent_chat._build_user_prompt(project, "写手", [], "写第1章", [], "")
    assert "商讨计划" not in txt and "(刹车)" not in txt


# ── 落库后自动续一轮────────────────────────────────────
class _Echo:
    """假后端:记下被问了什么,回一句带下一步的话。"""

    def __init__(self, reply="好,这条我记下了。\n下一步:定一下代价的量级"):
        self.reply = reply
        self.calls = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None):
        self.calls.append((system, user))
        if on_chunk and self.reply:
            on_chunk(self.reply)
        return self.reply


def test_续轮的提示词交代了落库这件事(project):
    txt = agent_chat._build_user_prompt(project, "设定师", [], "", [], "", resume=True)
    assert "已经拍板落库" in txt
    assert "商讨计划" in txt            # 提醒它按自己的计划走
    assert "作者没有新的话" in txt       # 不能留空的「作者对你说」


def test_续轮不伪造作者的话(project):
    """落库触发的这一轮不是作者说的 —— 记成 user 气泡就是让对话记录撒谎。"""
    evs: list[dict] = []
    be = _Echo()
    from loom import task_state as ts_mod, turn_prepare
    from loom.paths import TASKS_DIR
    base = project / TASKS_DIR / "默认"
    prepared = turn_prepare.prepare_turn(base_dir=base, domain="chat", role="设定师",
                                         conversation_id="默认", goal="设计人物")
    state = ts_mod.load_snapshot(base, prepared["task"]["task_id"])
    ts_mod.commit(base, state, {"kind": "decision", "proposal_id": "p-resume",
                               "decision_kind": "accepted"}, expected_revision=state.revision)
    agent_chat.run_turn(project, "设定师", "", be, emit=evs.append, ts="20261025-120000",
                        resume=True, task_id=state.task_id, decision_event_id="p-resume")
    kinds = [e["t"] for e in evs]
    assert "resume" in kinds and "user" not in kinds
    assert any(e["t"] == "assistant" for e in evs)
    assert be.calls and "已经拍板落库" in be.calls[0][1]


def test_续轮照常受刹车约束(project):
    """谈满 3 轮后落库又来续一轮:还是不许再问,提示词里得带刹车。"""
    for i in range(dl.MAX_QA_ROUNDS):
        _say(project, "设定师", f"商讨第{i + 1}轮\n下一步:再问一条")
    txt = agent_chat._build_user_prompt(project, "设定师", [], "", [], "", resume=True)
    assert "(刹车)" in txt
