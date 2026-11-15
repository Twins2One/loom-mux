# -*- coding: utf-8 -*-
"""侧栏角色指示灯(:「把含未读消息的和正在思考的角色在侧栏角色框
右上角加个指示灯」)。

两盏灯必须是**两种事实**,不能靠猜:
- 在思考 = 进程内真的有线程在跑它;
- 有欠账 = 它最后一条是"我说的",作者还没回 —— **而且得是近况**(见 RECENT_MINUTES)。
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from loom import agent_chat, agent_status as st
from loom import paths


def _now(minutes_ago: float = 0.0) -> str:
    """事件时间戳(与 Loom 写盘同一种格式)。"""
    t = _dt.datetime.now() - _dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y%m%d-%H%M%S")


def _append(root, role, ev):
    """直接往该角色的对话流水追加(模拟一轮对话留下的记录)。"""
    rel = paths.PARTNER_CUR_REL if role == "领航员" else f".伙伴对话/agent-{role}.jsonl"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


# ── 在思考:进程内登记 ─────────────────────────────────────────────────────
def test_忙碌登记与收尾():
    st.mark_busy("设定师")
    assert st.busy_roles().get("设定师") is True
    st.mark_busy("设定师", False)
    assert not st.busy_roles().get("设定师")


def test_重复登记要重复收尾():
    """议事厅里同一角色理论上可能被派两次;收掉一次不该把它判成空闲。"""
    st.mark_busy("大纲师")
    st.mark_busy("大纲师")
    st.mark_busy("大纲师", False)
    assert st.busy_roles().get("大纲师") is True
    st.mark_busy("大纲师", False)
    assert not st.busy_roles().get("大纲师")


def test_多收尾几次也不会变负(project):
    st.mark_busy("批评家", False)
    st.mark_busy("批评家", False)
    assert st.one(project, "批评家")["在思考"] is False


def test_空角色名不登记():
    st.mark_busy("")
    assert "" not in st.busy_roles()


# ── 有欠账:从流水尾巴读 ───────────────────────────────────────────────────
def test_最后一句是我说的就是欠账(project):
    _append(project, "设定师", {"t": "user", "text": "改吧", "ts": _now()})
    assert st.one(project, "设定师")["有欠账"] is False      # 轮到它
    _append(project, "设定师", {"t": "assistant", "text": "改好了", "ts": _now()})
    s = st.one(project, "设定师")
    assert s["有欠账"] is True and s["最后一条是谁"] == "我说"


def test_候选卡待拍板也算欠账(project):
    _append(project, "大纲师", {"t": "proposal", "id": "p-1", "slot": "x", "content": "y", "ts": _now()})
    s = st.one(project, "大纲师")
    assert s["有欠账"] is True and s["待拍板"] is True


def test_作者又说话了就清账(project):
    _append(project, "大纲师", {"t": "assistant", "text": "要不要这么改?", "ts": _now()})
    _append(project, "大纲师", {"t": "user", "text": "可以", "ts": _now()})
    assert st.one(project, "大纲师")["有欠账"] is False


def test_没有记录的角色不算欠账(project):
    s = st.one(project, "知识顾问")
    assert s["有欠账"] is False and s["有记录"] is False and s["在思考"] is False


def test_出错的轮次也算欠账(project):
    """那一轮没跑成,作者得知道要回去看(不然红灯亮不起来、也没人管)。"""
    _append(project, "知识顾问", {"t": "error", "text": "网关 502", "ts": _now()})
    s = st.one(project, "知识顾问")
    assert s["有欠账"] is True and s["待拍板"] is False


def test_拍板确认之后不算欠账(project):
    """领航员这类流水常以 confirm 收尾:那件事已经落盘了,不该一直挂着灯。"""
    _append(project, "领航员", {"t": "assistant", "text": "定了", "ts": _now()})
    _append(project, "领航员", {"t": "confirm", "ts": _now(), "id": "p-1"})
    s = st.one(project, "领航员")
    assert s["有欠账"] is False and s["有记录"] is True


def test_领航员也在名单里能被点亮(project):
    """作者 2026-10-25:领航员也要有灯。它本来就在快照里,这里把这条钉住。"""
    _append(project, "领航员", {"t": "proposal", "id": "p-9", "slot": "x", "content": "y", "ts": _now()})
    s = st.snapshot(project)["领航员"]
    assert s["有欠账"] is True and s["待拍板"] is True


def test_领航员读的是另一份流水(project):
    _append(project, "领航员", {"t": "assistant", "text": "先说你要写什么", "ts": _now()})
    assert st.one(project, "领航员")["有欠账"] is True


# ── 大文件只读尾巴 ─────────────────────────────────────────────────────────
def test_大流水只读尾巴不会读整个文件(project):
    p = project / ".伙伴对话" / "agent-设定师.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    filler = json.dumps({"t": "assistant", "text": "x" * 300}, ensure_ascii=False)
    with p.open("w", encoding="utf-8") as f:
        for _ in range(1200):          # ≈ 400 KB
            f.write(filler + "\n")
        f.write(json.dumps({"t": "user", "text": "最后一句是作者说的"}, ensure_ascii=False) + "\n")
    assert p.stat().st_size > 300_000
    s = st.one(project, "设定师")
    assert s["有欠账"] is False        # 读到了真正的最后一行
    assert s["最后一条是谁"] == "你说"


def test_坏行不影响判读(project):
    p = project / ".伙伴对话" / "agent-批评家.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"t": "assistant", "text": "说话", "ts": _now()}, ensure_ascii=False)
    p.write_text(good + "\n这行不是 json\n", encoding="utf-8")
    assert st.one(project, "批评家")["有欠账"] is True


# ── 近况窗口(:灯要反映近况)──────────────────────────────
def test_隔了几小时的候选卡不再点灯(project):
    """原来只看"最后一条是谁说的",一张没点的卡能挂一整夜 —— 那盏灯就没人信了。"""
    _append(project, "设定师", {"t": "proposal", "id": "p-1", "slot": "x",
                                "content": "y", "ts": _now(120)})
    s = st.one(project, "设定师")
    assert s["待拍板"] is False and s["有欠账"] is False
    assert s["近况"] is False and s["距今分钟"] > 100


def test_刚出的候选卡照样点灯(project):
    _append(project, "设定师", {"t": "proposal", "id": "p-1", "slot": "x",
                                "content": "y", "ts": _now(1)})
    s = st.one(project, "设定师")
    assert s["待拍板"] is True and s["有欠账"] is True
    assert s["窗口分钟"] == st.RECENT_MINUTES


def test_时间窗口的边界(project):
    """窗口内 < 窗口 <= 窗口外:门槛就压在 RECENT_MINUTES 上。"""
    _append(project, "大纲师", {"t": "assistant", "text": "说完了", "ts": _now(st.RECENT_MINUTES - 2)})
    assert st.one(project, "大纲师")["有欠账"] is True
    _append(project, "批评家", {"t": "assistant", "text": "说完了", "ts": _now(st.RECENT_MINUTES + 2)})
    assert st.one(project, "批评家")["有欠账"] is False


def test_认不出时间不敢点灯(project):
    """ts 缺失/写坏 → 不拿未知去点亮一盏灯(否则又会变成永久亮)。"""
    _append(project, "语料分析师", {"t": "assistant", "text": "说话", "ts": "t1"})
    s = st.one(project, "语料分析师")
    assert s["有欠账"] is False and s["近况"] is False and s["距今分钟"] is None


def test_在思考不受时间窗约束(project):
    """「正在思考」是实时事实:线程在跑就是跑着,不该因为"上次说话很久以前"而不亮。"""
    _append(project, "战斗设计师", {"t": "user", "text": "改一下", "ts": _now(300)})
    st.mark_busy("战斗设计师")
    try:
        s = st.one(project, "战斗设计师")
    finally:
        st.mark_busy("战斗设计师", False)
    assert s["在思考"] is True and s["近况"] is False


# ── 名单:只给有对话意义的角色 ──────────────────────────────────────────────
def test_名单是领航员加可见智库(project):
 """领航员 + 作者智库 8 人（含 2026-11-06 市场分析师、2026-11-12 人物侧写师）。

 ：知识顾问与语料分析师**已工具化**（查知识/看数据/改指纹），
 不再是"人"，名册从 8 减到 6；此后两次入册：市场分析师（2026-11-06）、
 人物侧写师（2026-11-12）→ 可见智库 8 人（含领航员共 9 盏灯）。

 注意：藏的是**展示**不是数据 —— AUTHOR_ROLES 本体里它们还在（速记本/对话/端点全留），
 所以这条只断言 snapshot（给界面看的灯），别拿它当"已删除"的证据。
 """
 snap = st.snapshot(project)
 assert "领航员" in snap
 for r in ("设定师", "大纲师", "战斗设计师", "情绪节奏设计师",
           "人物侧写师", "编辑总监", "批评家"):
     assert r in snap, r
 assert "知识顾问" not in snap, "知识顾问已工具化，不该再挂灯"
 assert "语料分析师" not in snap, "语料分析师已工具化，不该再挂灯"
 assert "写手" not in snap and "章节细纲师" not in snap   # 流水线工人不挂灯
 assert len(snap) == 9                                    # 领航员 + 智库8人(市场分析师 + 人物侧写师)


def test_状态里同时给出在思考与有欠账(project):
    _append(project, "编辑总监", {"t": "assistant", "text": "这几章有问题", "ts": _now()})
    st.mark_busy("编辑总监")
    try:
        s = st.one(project, "编辑总监")
    finally:
        st.mark_busy("编辑总监", False)
    assert s["在思考"] is True and s["有欠账"] is True
