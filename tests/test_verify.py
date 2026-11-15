# -*- coding: utf-8 -*-
"""verify 单测:L1 三项覆盖率+红线判定+L2 选块。"""
from __future__ import annotations

from tests.test_distiller import FakeBackend
from loom.study import chunker as C
from loom.study import distiller as D
from loom.study import merger as M
from loom.study import verify as V


def _mk_full(tmp_path, n=2):
    """分块+蒸馏+合并+压卡 全流程档案。"""
    arch = tmp_path / "档案"
    orig = arch / "原文.md"
    orig.parent.mkdir(parents=True)
    orig.write_text("\n\n".join("第%d章 主角林三遇到%s的麻烦。\n\n%s" % (i, w, "情节" * 40)
                                for i, w in enumerate(["妖兽", "官府"], 1)), encoding="utf-8")
    C.rebuild_index(arch, orig)
    C.write_chunk_files(arch, orig)
    D.run_block_distill(arch, orig, FakeBackend())
    M.merge_distills(arch, FakeBackend())
    M.build_cards(arch, FakeBackend())
    return arch


def test_l1_all_green(tmp_path):
    arch = _mk_full(tmp_path)
    r = V.l1_coverage(arch, arch / "原文.md")
    assert r["块齐全率"] == 1.0
    assert r["禁用转录率"] == 1.0     # 无禁用词 → 1.0
    assert r["红线"] is False


def test_l1_banned_missing_red(tmp_path):
    """禁用词没转录进卡片集 → 该项 0 → 红线。"""
    arch = _mk_full(tmp_path)
    d1 = arch / "chunks" / "chunk_001_distill.md"
    t = d1.read_text(encoding="utf-8")
    d1.write_text(t.rstrip() + "\n- ✗ 不由得（证据：「不由得笑出声」）\n", encoding="utf-8")
    r = V.l1_coverage(arch, arch / "原文.md")
    assert r["禁用转录率"] == 0.0
    assert "不由得" in r["禁用缺失"]
    assert r["红线"] is True


def test_l2_returns_both_sides(tmp_path):
    arch = _mk_full(tmp_path)
    r = V.l2_imitate(arch, FakeBackend())
    # 全部块都 distilled → 取最后一块作复现度参考
    assert r["ok"] is True and "原文" in r and "仿写" in r
    assert r["仿写"]


def test_l2_no_blocks(tmp_path):
    arch = tmp_path / "空档案"
    arch.mkdir()
    r = V.l2_imitate(arch, FakeBackend())
    assert r["ok"] is False
