# -*- coding: utf-8 -*-
"""「看数据」背后的本地语料统计( B)。

要守的三件事:
1. 范围解析错就给**人话**错误,不许静默返回空报告;
2. 统计口径稳定(句/段/短句比/标点),别今天一套明天一套;
3. 一文不花:纯本地,报告里不该出现任何模型调用痕迹。
"""
from __future__ import annotations

import pytest

from loom import corpus, paths


def _mk(project, n: int, body: str) -> None:
    p = paths.chapter_path(project, n)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"第{n}章 试\n\n{body}\n", encoding="utf-8")


BODY = ("他站在门口。风从走廊里灌进来，吹得灯晃了一下。\n"
        "「你来了。」老人没有回头。\n"
        "他没有回答，只是把手里的伞收了起来，靠在墙边，然后慢慢走过去。\n")


# ── 范围解析 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scope,expect", [
    ("", "全书"), ("全书", "全书"), ("整本", "全书"),
    ("第2章", "第 2 章"), ("2", "第 2 章"),
    ("第1-2章", "第 1-2 章"), ("1~2", "第 1-2 章"),
    ("最近1章", "最近 1 章"),
])
def test_范围解析(scope, expect):
    label, _nums = corpus.parse_scope(scope, [1, 2, 3])
    assert label == expect


@pytest.mark.parametrize("bad", ["第三章", "第9章", "第7-9章", "从开头"])
def test_范围写错给人话错误(bad):
    with pytest.raises(ValueError) as e:
        corpus.parse_scope(bad, [1, 2, 3])
    assert "范围这么写认不出" in str(e.value) or "没有" in str(e.value)


def test_没有正文时直接说清(project):
    with pytest.raises(ValueError) as e:
        corpus.collect(project, "")
    assert "还没有正文" in str(e.value)


# ── 统计口径 ────────────────────────────────────────────────────────────────
def test_句段字三个口径():
    st = corpus.stats(BODY)
    assert st["字数"] == len("".join(BODY.split()))
    assert st["句数"] >= 4                     # 「他站在门口。」这类短句都算一句
    assert st["段数"] == 3
    assert 0 < st["平均句长"] < 60
    assert 0 <= st["短句比"] <= 100 and 0 <= st["长句比"] <= 100


def test_短句长句分界(monkeypatch):
    short = "他走了。" * 1                       # 4 字 → 短句
    long_one = "他" + "慢慢" * 20 + "走。"        # >30 字 → 长句
    st = corpus.stats(short + long_one)
    assert st["短句比"] > 0 and st["长句比"] > 0
    assert st["最长句"] > corpus.LONG_SENT


def test_标点比例是百分比():
    st = corpus.stats("他来，他看。")
    assert st["标点"]
    total = sum(p for _ch, p in st["标点"])
    assert 99.0 <= total <= 101.0              # 四舍五入误差内


def test_高频片段不报虚词():
    text = "仿佛" * 5 + "。" + "的了是在" * 4 + "。"
    got = [g for g, _c in corpus.stats(text)["高频片段"]]
    assert "仿佛" in got
    assert not any("的" in g for g in got)      # 带「的」的片段一律不报


def test_句首统计出来的是句首():
    st = corpus.stats("他没有说话。他没有回头。他没有动。他没有笑。")
    heads = [h for h, _c in st["句首"]]
    assert "他没" in heads


def test_超长样本的高频片段只取尾巴(monkeypatch):
    monkeypatch.setattr(corpus, "MAX_PHRASE_SAMPLE", 40)
    text = "开头专用词。" * 20 + "结尾专用词。" * 20
    st = corpus.stats(text)
    assert st["高频片段只取了尾巴"] is True
    got = [g for g, _c in st["高频片段"]]
    assert not any("开头" in g for g in got)     # 尾巴之外的词不参与


# ── 报告全文 ────────────────────────────────────────────────────────────────
def test_报告含全部指标且不花token(project):
    _mk(project, 1, BODY * 4)
    _mk(project, 2, BODY * 8)
    txt = corpus.report(project, "")
    for key in ("【语料数据】", "句:", "段:", "标点:", "句首:", "高频片段", "章长:"):
        assert key in txt, key
    assert "没花 token" in txt
    assert "2 章" in txt


def test_小样本会先提醒(project):
    _mk(project, 1, "他走了。")
    txt = corpus.report(project, "")
    assert "样本太小" in txt


def test_报告会算出口径命中(project):
    _mk(project, 1, "仿佛他又回到了那一天。" * 6)
    policy_dir = project / paths.ANALYSIS_DIR_REL
    policy_dir.mkdir(parents=True, exist_ok=True)
    (project / paths.SLOP_POLICY_REL).write_text(
        "# 去AI味口径\n\n## 额外禁用表达\n\n- 仿佛\n", encoding="utf-8")
    txt = corpus.report(project, "")
    assert "绝不这么写" in txt and "仿佛" in txt


def test_只统计选中的章(project):
    _mk(project, 1, BODY)
    _mk(project, 2, "他笑了。" * 40)
    txt1 = corpus.report(project, "第1章")
    txt2 = corpus.report(project, "第2章")
    assert "第 1 章" in txt1 and "第 2 章" in txt2
    assert txt1 != txt2


# ── 端到端:真从对话里调一次 ────────────────────────────────────────────────
def test_对话里调看数据能拿到真实统计(project):
    from conftest import ScriptedBackend
    from loom import agent_chat

    _mk(project, 1, BODY * 3)
    be = ScriptedBackend(["```loom\nTOOL: 看数据\n范围: 全书\n```", "数据我看了,短句偏多。"])
    evs: list[dict] = []
    agent_chat.run_turn(project, "语料分析师", "先看看我的文风数据",
                        be, emit=evs.append, ts="20261025-120000")
    res = next(e for e in evs if e.get("t") == "result")
    assert "【语料数据】" in res["text"]
    assert "没花 token" in res["text"]
