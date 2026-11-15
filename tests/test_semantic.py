# -*- coding: utf-8 -*-
"""语义检索（0.8.3，）。

守四件：
1.缺件时 ready()如实报缺，不假称可用；
2.缺件时 embed()返回 None、rank()返回 []，**不抛**（上层靠这个退化到字面）；
3. perf()是零花费的本地读数，偏弱时才建议退化；
4.真的能搜（有模型时）：换词也能中，而不是只看字面重合。
"""
from __future__ import annotations

import pytest

from loom.study import semantic as S


def test_ready_reports_gaps_without_lying(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOM_EMBED_MODEL", str(tmp_path / "nope.onnx"))
    monkeypatch.setenv("LOOM_EMBED_TOKENIZER", str(tmp_path / "nope.json"))
    st = S.ready()
    assert st["ok"] is True
    assert st["可语义检索"] is False
    assert "向量模型" in st["缺"]
    assert "分词表" in st["缺"]


def test_embed_and_rank_degrade_quietly(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOM_EMBED_MODEL", str(tmp_path / "nope.onnx"))
    monkeypatch.setenv("LOOM_EMBED_TOKENIZER", str(tmp_path / "nope.json"))
    assert S.embed(["a"]) is None
    assert S.rank("问", ["文"]) == []
    assert S.rank("", ["文"]) == []
    assert S.rank("问", []) == []


def test_perf_is_local_and_answers_whether_to_degrade(monkeypatch, tmp_path):
    f = tmp_path / "m.onnx"
    f.write_bytes(b"x" * (10 *1048576))
    monkeypatch.setenv("LOOM_EMBED_MODEL", str(f))
    p = S.perf()
    assert p["ok"] is True
    assert p["模型MB"] == pytest.approx(10.0, abs=0.2)
    assert p["预计占用MB"] > p["模型MB"]
    assert isinstance(p["建议退化"], bool)
    assert isinstance(p["偏弱"], list)


def test_real_search_prefers_meaning_over_literal():
    if not S.ready()["可语义检索"]:
        pytest.skip("本机没装语义模型")
    docs = ["碎片化的时间顺序叙述", "番茄免费平台的签约规则",
        "非线性叙事把故事拆成多条时间线"]
    r = S.rank("怎么讲得不按时间顺序", docs, top_k=3)
    assert r, "应该能拿到排序"
    top = r[0][1]
    assert "非线性" in top or "碎片" in top, top
    #与问题毫不相干的那条应排在后面
    assert "番茄" not in r[0][1]
