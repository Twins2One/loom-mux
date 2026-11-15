# -*- coding: utf-8 -*-
"""「改指纹」:让作者靠对话优化写作指纹。

三条要守住的东西:
1. **只出候选卡**:工具绝不写盘,必须作者在卡上点头(`usecases.partner_confirm`)才落;
2. **只增不删**:落盘只往指定小节追加,既有规则与 anchor 一个字都不动;
3. **只有语料分析师能用**:别的角色连 schema 都看不见,硬调也拒。
"""
from __future__ import annotations

import pytest

from loom import collab, partner_store as ps, paths, usecases
from loom import partner_tools as pt


def _propose(project, 段, 内容, ts="t1"):
    """跑一次改指纹,并把候选卡落进 store(对话循环平时干的就是这一步)。"""
    payload = pt.run_tool(project, "改指纹", {"段": 段, "内容": 内容}, ts=ts)
    assert payload["t"] == "proposal", payload
    ps.append_event(project, {"t": "proposal", "ts": ts, "id": payload["id"], **{
        k: v for k, v in payload.items()
        if k in ("slot", "content", "before", "kind", "段")}})
    return payload


def _fp(project) -> str:
    return (project / paths.FINGERPRINT_REL).read_text(encoding="utf-8")


@pytest.fixture
def as_corpus():
    """把当前线程的「这一轮是谁在说话」设成语料分析师(越权门按它判)。"""
    collab.begin_turn("语料分析师")
    yield
    collab.begin_turn("")


# ── ：语料分析师已工具化，改指纹**所有智能体都可调** ──────
# 旧的「越权门」四条测试一并改写：那道门是给"专属角色"设的，角色没了，门自然开。
def test_任何角色都能调改指纹(project):
    collab.begin_turn("设定师")
    ev = pt.run_tool(project, "改指纹", {"段": "句式偏好", "内容": "爱用短句"}, ts="t")
    assert not ev.get("error"), ev
    assert ev.get("kind") == "写作指纹"


def test_没有对话上下文时也能调(project):
    collab.begin_turn("")
    ev = pt.run_tool(project, "改指纹", {"段": "句式偏好", "内容": "爱用短句"}, ts="t")
    assert not ev.get("error"), ev


def test_工具列表不再按角色过滤(project):
    for who in ("设定师", "写手", "润色师", "语料分析师"):
        assert "改指纹" in pt.visible_tools(who), who
        assert "看数据" in pt.visible_tools(who), who
    assert "改指纹" in pt.render_contract("设定师")
    assert "改指纹" in pt.render_contract("写手")


def test_原生函数调用也给全部角色schema():
    from loom import agent_chat
    for who in ("设定师", "写手"):
        names = {t["function"]["name"] for t in agent_chat._tools_schema("", who)}
        assert "tune_fingerprint" in names and "read_stats" in names, who


# ── 工具本身:只出卡,不写盘 ────────────────────────────────────────────────
def test_出卡不写盘(project, as_corpus):
    before = _fp(project)
    payload = _propose(project, "句式偏好", "爱用短句，单句成段；长句少。")
    assert payload["kind"] == "写作指纹"
    assert payload["段"] == "句式偏好"
    assert payload["slot"] == f"{paths.FINGERPRINT_REL}#句式偏好"
    assert _fp(project) == before


def test_段名认别名(project, as_corpus):
    for alias, expect in (("anchor 例句", "anchor 例句"), ("签名句", "anchor 例句"),
                          ("绝不这么写", "禁用词 / 绝不这么写"), ("口头禅", "口头禅 / 高频表达"),
                          ("节奏", "节奏")):
        ev = pt.run_tool(project, "改指纹", {"段": alias, "内容": "x"}, ts="t")
        assert ev["段"] == expect, alias


def test_段名认不出时把可选值告诉它(project, as_corpus):
    ev = pt.run_tool(project, "改指纹", {"段": "我的独特风格", "内容": "x"}, ts="t")
    assert ev.get("error") and "句式偏好" in ev["error"]


def test_内容为空直接拒(project, as_corpus):
    ev = pt.run_tool(project, "改指纹", {"段": "节奏", "内容": "  "}, ts="t")
    assert ev.get("error")


# ── 落盘:只增不删 ──────────────────────────────────────────────────────────
def test_拍板后落进指定小节且不删旧内容(project, as_corpus):
    before = _fp(project)
    payload = _propose(project, "禁用词", "仿佛\n宛如")
    out = usecases.partner_confirm(project, payload["id"], ts="t2")
    assert "error" not in out, out
    after = _fp(project)
    assert "- 仿佛" in after and "- 宛如" in after
    # 旧规则一条不少(只增不删)
    for line in before.splitlines():
        if line.strip():
            assert line.strip() in after, line
    # 落在「禁用词」那一节里,而不是另起一节
    seg = after.split("## 禁用词")[1].split("## ")[0]
    assert "仿佛" in seg


def test_anchor_例句自动加上引用标记并被检测器读到(project, as_corpus):
    payload = _propose(project, "anchor 例句", "那不是笑。是不在意。")
    assert usecases.partner_confirm(project, payload["id"], ts="t2").get("landed") == paths.FINGERPRINT_REL
    assert "> 那不是笑。是不在意。" in _fp(project)
    from loom import aitell
    assert "那不是笑。是不在意。" in aitell.load_anchors(project)


def test_重复落同一条会被告知而不是重复写(project, as_corpus):
    p1 = _propose(project, "节奏", "段落普遍短，三行一停。", ts="t1")
    assert usecases.partner_confirm(project, p1["id"], ts="t2").get("landed")
    p2 = _propose(project, "节奏", "段落普遍短，三行一停。", ts="t3")
    out = usecases.partner_confirm(project, p2["id"], ts="t4")
    assert out.get("error") and "已经在" in out["error"]
    assert _fp(project).count("段落普遍短，三行一停。") == 1


def test_卡片上能看到这一节现在写了什么(project, as_corpus):
    p1 = _propose(project, "句式偏好", "爱用短句。", ts="t1")
    usecases.partner_confirm(project, p1["id"], ts="t2")
    p2 = _propose(project, "句式偏好", "少用关联词。", ts="t3")
    assert "爱用短句" in p2["before"]


def test_候选卡过期不会崩(project):
    out = usecases.partner_confirm(project, "p-不存在", ts="t2")
    assert out.get("error")


def test_落盘不动别的文件(project, as_corpus):
    world = (project / paths.SET_WORLD_DIR_REL)
    snap = {p.name: p.read_text(encoding="utf-8") for p in world.glob("*.md")}
    payload = _propose(project, "句式偏好", "爱用短句。")
    usecases.partner_confirm(project, payload["id"], ts="t2")
    for name, text in snap.items():
        assert (world / name).read_text(encoding="utf-8") == text


# ── 路由:这两件事该找语料分析师,不是润色师 ─────────────────────────────────
def test_路由把调指纹和调口径的问题送对地方():
    from loom.navigator import route
    assert route("帮我调一下写作指纹") == "语料分析师"
    assert route("我想改去AI味口径") == "语料分析师"
    assert route("看看我的词频") == "语料分析师"
    assert route("这一段读起来像AI,润色一下") == "润色师"   # 织章那条路不受影响


# ── 端到端:真从对话里调一次改指纹,再在卡上拍板 ──────────────────────────────
def test_端到端_对话出卡再落库(project):
    from conftest import ScriptedBackend
    from loom import agent_chat

    be = ScriptedBackend([
        "```loom\nTOOL: 改指纹\n段: 句式偏好\n内容: 爱用短句，单句成段\n```",
        "我把它提成卡了,你点头我才写。",
    ])
    evs: list[dict] = []
    agent_chat.run_turn(project, "语料分析师", "我其实爱用短句、单句成段",
                        be, emit=evs.append, ts="20261025-120000")
    prop = next(e for e in evs if e.get("t") == "proposal")
    assert prop["kind"] == "写作指纹" and prop["段"] == "句式偏好"
    assert "- 爱用短句，单句成段" not in _fp(project)      # 卡还没点,一个字都不该落
    out = usecases.partner_confirm(project, prop["id"], ts="20261025-120001")
    assert "error" not in out, out
    assert "- 爱用短句，单句成段" in _fp(project)
