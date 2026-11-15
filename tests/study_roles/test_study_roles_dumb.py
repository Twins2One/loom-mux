# -*- coding: utf-8 -*-
"""书房角色分支 · 哑故障类表征测试（缺陷 #3 #4 #6 #7 #8 #10 #11）。

约定同 test_study_roles_crash.py：
- 「表征」测试断言**当前（有缺陷）行为**，现在必须通过；W1 修复后同步改写。
- 「目标」测试标 xfail(reason="W1 修复缺陷#N 后转正")，修完摘标记即绿。
- 全部假后端/桩，不出网不花钱；LOOM_LIBRARY_ROOT 由 conftest 隔离到 tmp。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ── #3 文本降级固定判 study.tools.TOOLS，不认 partner_tools 专业工具 ───────
def test_3_文本协议按当前工具面解析(monkeypatch):
    """W1 修复 #3 后的表征转正：判据按工具面传入，专业工具在文本协议下可解析。"""
    from loom.study import ask as A
    # 修复后默认口径（不传参）：仍是书房伙伴四把——书房伙伴行为零改动
    got2 = A._parse_text_tools("用：搜书房\n查询：节奏")
    assert got2 and got2[0][0] == "搜书房"
    # 知识顾问契约里教模型写的标准形状，传入角色工具面后解析得出来
    got = A._parse_text_tools("用：落笔记\n标题：测试\n稿子：内容",
                              tool_names=A._ROLE_TOOLS["知识顾问"])
    assert got and got[0][0] == "落笔记"


def test_3_修复后_文本协议应解析出专业工具(monkeypatch):
    from loom.study import ask as A
    got = A._parse_text_tools("用：落笔记\n标题：测试\n稿子：内容",
                              tool_names=A._ROLE_TOOLS["知识顾问"])
    assert got and got[0][0] == "落笔记"


# ── #4 角色工具包装不传 backend → 查知识必败 ──────────────────────────────
class _EchoBackend:
    def complete(self, system, user, on_chunk=None, **kw):
        return "## 主题\n甲\n\n## 结论\n乙\n\n## 依据\n- 依据一\n\n## 不确定\n无\n\n## 关联\n无"


def test_4_查知识经书房包装必收_没有可用后端(monkeypatch, tmp_path):
    root = tmp_path / "知识库"
    root.mkdir()
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(root))
    from loom.study import ask as A
    ts = A._toolset_for("知识顾问")
    # 表征：即使调用方给了 backend，_RoleToolset.run 也不透传 → handler 报"没有可用后端"
    got = ts.run("lookup_knowledge", {"问题": "明代秀才"})
    assert got.get("ok") is False
    assert "没有可用后端" in str(got.get("text") or "")


def test_4_对照_直接调run_tool带backend则能出结果(monkeypatch, tmp_path):
    """证明 #4 不是知识库空的锅：同一份桩后端，run_tool 直接带 backend 就能走通。"""
    from loom.scaffold import init
    from loom import partner_tools as pt
    proj = init("对照书", parent=tmp_path)
    got = pt.run_tool(proj, "查知识", {"问题": "明代秀才"},
                      ts="t4", backend=_EchoBackend())
    assert got.get("t") == "result"
    assert "结论" in str(got.get("text") or "")


def test_4_修复后_书房包装查知识应能出结果(monkeypatch, tmp_path):
    root = tmp_path / "知识库"
    root.mkdir()
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(root))
    from loom.study import ask as A
    ts = A._toolset_for("知识顾问")
    # 目标行为：包装层把调用方的 backend 透传给 run_tool（注入约定由 W1 定），
    # 这里锁定"给足条件后查知识能拿到结论"这一结果。
    got = ts.run("lookup_knowledge", {"问题": "明代秀才"}, backend=_EchoBackend())
    assert got.get("ok") is True
    assert "结论" in str(got.get("text") or "")


# ── #6 查知识解析键(主题/结论/依据/不确定/关联) 与包装摘取键(结论/要点/…) 交集只剩"结论" ──
def test_6_查知识结果透出依据与不确定(monkeypatch, tmp_path):
    """W1 修复 #6 后的表征转正：解析键（主题/结论/依据/不确定/关联）全部透传。"""
    from loom.scaffold import init
    from loom import partner_tools as pt
    proj = init("对照书6", parent=tmp_path)
    raw = ("## 主题\n甲\n\n## 结论\n结论乙\n\n## 依据\n- 依据一条\n- 依据二条\n\n"
           "## 不确定\n- 存疑丙\n\n## 关联\n- 人物/甲.md")
    text = pt._handle_cha_zhishi(proj, 问题="明代秀才",
                                 backend=type("B", (), {
                                     "complete": lambda s, a, b, **kw: raw})())
    assert "结论乙" in text
    assert "依据一条" in text and "依据二条" in text
    assert "存疑丙" in text


def test_6_修复后_查知识应透出依据与不确定(monkeypatch, tmp_path):
    from loom.scaffold import init
    from loom import partner_tools as pt
    proj = init("对照书6b", parent=tmp_path)
    raw = ("## 主题\n甲\n\n## 结论\n结论乙\n\n## 依据\n- 依据一条\n\n"
           "## 不确定\n- 存疑丙\n\n## 关联\n无")
    text = pt._handle_cha_zhishi(proj, 问题="q",
                                 backend=type("B", (), {
                                     "complete": lambda s, a, b, **kw: raw})())
    assert "依据一条" in text
    assert "存疑丙" in text


# ── #7 搜书房判 ok 键恒 False + 片段取键"文"实际是"正文" ──────────────────
def test_7_搜书房返回节选正文(monkeypatch, tmp_path):
    """W1 修复 #7 后的表征转正：存在性判断 + 正确的片段键「正文」。"""
    root = tmp_path / "知识库"
    d = root / "教材" / "01-夜课"
    d.mkdir(parents=True)
    (d / "节奏.md").write_text("# 节奏\n\n## 慢写\n\n该慢的地方要慢。一拍静。\n", encoding="utf-8")
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(root))
    from loom.study import ask as A
    import loom.partner_tools as pt
    A.reset_cache_for_tests()
    # 底层返回形状不变（无 ok 键）——消费侧按存在性判断，不再误判不可用
    got = A.search("节奏 慢写", semantic=False)
    assert got["节选"] and "正文" in got["节选"][0]
    out = pt._handle_sou_shufang(root, 查询="节奏 慢写")
    assert "搜到" in out and "该慢的地方要慢" in out


def test_7_修复后_搜书房应返回节选正文(monkeypatch, tmp_path):
    root = tmp_path / "知识库"
    d = root / "教材" / "01-夜课"
    d.mkdir(parents=True)
    (d / "节奏.md").write_text("# 节奏\n\n## 慢写\n\n该慢的地方要慢。一拍静。\n", encoding="utf-8")
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(root))
    from loom.study import ask as A
    import loom.partner_tools as pt
    A.reset_cache_for_tests()
    out = pt._handle_sou_shufang(root, 查询="节奏 慢写")
    assert "搜到" in out and "该慢的地方要慢" in out


# ── #8 蒸馏 import distill_sync 失败 ──────────────────────────────────────
def test_8_蒸馏工具给人话结果而非崩(monkeypatch, tmp_path):
    """W1 修复 #8 后的表征转正：import 纳入 try、入口对齐 distill_docs。
    没给 backend → 人话报"没有可用后端"，绝不 ImportError 冒出。"""
    import loom.partner_tools as pt
    import loom.stylepack as sp
    assert hasattr(sp, "distill_docs")   # 真实入口存在
    out = pt._handle_zhengliu(tmp_path, 标题="某篇", 文本="正文")
    assert isinstance(out, str) and "ImportError" not in out


def test_8_修复后_蒸馏工具应给人话结果而非崩(monkeypatch, tmp_path):
    import loom.partner_tools as pt
    out = pt._handle_zhengliu(tmp_path, 标题="某篇", 文本="正文")
    assert isinstance(out, str) and "ImportError" not in out


# ── #10 看档案逐层读单文件但前两层是目录 ──────────────────────────────────
def test_10_看档案能读到前两层目录内容(monkeypatch, tmp_path):
    """W1 修复 #10 后的表征转正：目录层按 .md 列表/拼接读取，单文件层照旧。"""
    import loom.partner_tools as pt
    base = Path(os.environ["LOOM_LIBRARY_ROOT"]) / "语料库" / "某档案"
    (base / "原文").mkdir(parents=True)
    (base / "蒸馏原稿").mkdir(parents=True)
    (base / "原文" / "a.md").write_text("原文内容ABC", encoding="utf-8")
    (base / "蒸馏原稿" / "d.md").write_text("蒸馏内容DEF", encoding="utf-8")
    (base / "写手参考.md").write_text("参考GHI", encoding="utf-8")
    out = pt._handle_kan_dangan(base, 档案="某档案")
    assert "原文内容ABC" in out
    assert "蒸馏内容DEF" in out
    assert "参考GHI" in out           # 第三层单文件路径不受影响


def test_10_修复后_看档案应能读到前两层内容(monkeypatch, tmp_path):
    import loom.partner_tools as pt
    base = Path(os.environ["LOOM_LIBRARY_ROOT"]) / "语料库" / "某档案2"
    (base / "原文").mkdir(parents=True)
    (base / "原文" / "a.md").write_text("原文内容ABC", encoding="utf-8")
    out = pt._handle_kan_dangan(base, 档案="某档案2")
    assert "原文内容ABC" in out


# ── #11 看数据走 corpus.report（书内语料域），语料分析师的便条任意文本进不去 ──
def test_11_看数据区分书内与书房两个输入域(monkeypatch, tmp_path):
    """W1 修复 #11 后的表征转正：书内语境走 corpus.report；书房语境统计便条文本。"""
    import loom.partner_tools as pt
    lib = Path(os.environ["LOOM_LIBRARY_ROOT"])
    (lib / "便条文本.md").write_text("他推开门。屋里很静。" * 30, encoding="utf-8")
    # 书房语境（无 loom.toml）：不再对 corpus.collect 抛 ValueError——走轻量文本统计
    out = pt._handle_kandata(lib)
    assert "语料数据" in out
    # 指名便条文件也能统计
    out2 = pt._handle_kandata(lib, 范围="便条文本.md")
    assert "语料数据" in out2 and "便条文本" in out2
    # 书内语境：行为不变——统计章正文
    from loom.scaffold import init
    proj = init("数据书", parent=lib)
    ch = proj / "正文" / "第1章.md"
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text("书里的章正文，跟便条完全无关。" * 20, encoding="utf-8")
    out3 = pt._handle_kandata(proj)
    assert "语料数据" in out3 and "1 章" in out3


def test_11_修复后_书房看数据应能统计便条文本(monkeypatch, tmp_path):
    import loom.partner_tools as pt
    lib = Path(os.environ["LOOM_LIBRARY_ROOT"])
    (lib / "便条文本.md").write_text("他推开门。屋里很静。" * 30, encoding="utf-8")
    out = pt._handle_kandata(lib, 范围="便条文本.md")
    assert "语料数据" in out
