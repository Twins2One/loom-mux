# -*- coding: utf-8 -*-
"""distiller 单测:状态机/重试/连续失败中止/human_edited 跳过/ONNX 去重兜底。"""
from __future__ import annotations

import json

from loom.study import chunker as C
from loom.study import distiller as D


class FakeBackend:
    """可编排的假后端:fail_times=N 前 N 次抛错;deep_text 控制深度轮返回。"""

    def __init__(self, *, fail_times: int = 0,
                 deep_entries: str = "- 本章以冲突升级完成转折，新信息在对话末尾抛出（证据：「他站起来说。」）\n- 场景切换用空行加时间状语过渡（证据：「第二天清晨。」）"):
        self.fail_times = fail_times
        self.calls = 0
        self.deep_entries = deep_entries
        self.saw_deep_prompt = False

    def complete(self, system, user, **kw):
        self.calls += 1
        if "叙事手法分析师" in system:
            self.saw_deep_prompt = True
            return self.deep_entries
        if self.calls > self.fail_times + (1 if self.saw_deep_prompt else 0):
            pass
        if self.fail_times > 0 and self._general_calls() <= self.fail_times:
            raise ValueError("模拟网络失败")
        return "## 文风特征\n- 短句为主（证据：「他走了。」）\n- 白描（证据：「桌上有一杯水。」）\n## 叙事手法\n- 第三人称（证据：「他想。」）\n"

    def _general_calls(self):
        return self.calls


def _mk_archive(tmp_path, n=3):
    arch = tmp_path / "档案"
    orig = arch / "原文.md"
    orig.parent.mkdir(parents=True)
    orig.write_text("\n\n".join("第%d章\n\n%s" % (i, "字" * 100) for i in range(1, n + 1)),
                    encoding="utf-8")
    C.rebuild_index(arch, orig)
    C.write_chunk_files(arch, orig)
    return arch


def test_all_success(tmp_path):
    arch = _mk_archive(tmp_path, 3)
    stats = D.run_block_distill(arch, arch / "原文.md", FakeBackend())
    assert stats["done"] == 3 and stats["failed"] == 0 and not stats["aborted"]
    idx = C.load_index(arch)
    assert all(b["status"] == "distilled" for b in idx["blocks"])
    assert (arch / "chunks" / "chunk_001_distill.md").is_file()


def test_retry_then_fail_continues(tmp_path):
    """单块重试 2 次仍败 → 终态 failed 继续下一块(不中止)。"""
    arch = _mk_archive(tmp_path, 4)
    # fail_times=10:每块的 3 次尝试(1+2重试)全失败
    stats = D.run_block_distill(arch, arch / "原文.md", FakeBackend(fail_times=99))
    assert stats["aborted"]          # 连续3块 failed → 中止整批(设计口径)
    idx = C.load_index(arch)
    failed_n = sum(1 for b in idx["blocks"] if b["status"] == "failed")
    assert failed_n == 3             # 中止在第3块后
    assert all("fail_reason" in b for b in idx["blocks"] if b["status"] == "failed")


def test_consecutive_fail_aborts(tmp_path):
    """连续 3 块 failed → 中止整批。"""
    arch = _mk_archive(tmp_path, 6)
    stats = D.run_block_distill(arch, arch / "原文.md", FakeBackend(fail_times=99))
    # 6 块全失败但连续 3 块就中止 → calls 只到第 3 块
    assert stats["aborted"]


def test_human_edited_skipped(tmp_path):
    arch = _mk_archive(tmp_path, 3)
    idx = C.load_index(arch)
    idx["blocks"][1]["status"] = "human_edited"
    C.save_index(arch, idx)
    stats = D.run_block_distill(arch, arch / "原文.md", FakeBackend())
    assert stats["skipped"] >= 1 and stats["done"] == 2
    # human_edited 块没被动过
    assert C.load_index(arch)["blocks"][1]["status"] == "human_edited"


def test_deep_mode_writes_deep_section(tmp_path):
    """深度开:深度轮产出经 ONNX/兜底去重后写入 §深度。"""
    arch = _mk_archive(tmp_path, 2)
    stats = D.run_block_distill(arch, arch / "原文.md", FakeBackend(), deep=True)
    assert stats["done"] == 2
    text = (arch / "chunks" / "chunk_001_distill.md").read_text(encoding="utf-8")
    assert "## 深度" in text
    assert "冲突升级完成转折" in text


def test_parse_entries():
    text = "## 文风特征\n- 条目甲（证据：「原句」）\n说明文字\n- 条目乙\n"
    got = D.parse_entries(text)
    assert got == ["条目甲（证据：「原句」）", "条目乙"]


def test_dedupe_literal_fallback(monkeypatch):
    """semantic 不可用时的字面包含兜底。"""
    import loom.study.semantic as sem
    def boom(*a, **k):
        raise RuntimeError("no embed in test")
    monkeypatch.setattr(sem, "embed", boom, raising=False)
    kept = D.dedupe_against(["短句为主，白描"], ["短句为主"], threshold=0.85)
    assert kept == []
    kept2 = D.dedupe_against(["爱写打斗"], ["短句为主"], threshold=0.85)
    assert kept2 == ["爱写打斗"]


def test_pending_count(tmp_path):
    arch = _mk_archive(tmp_path, 3)
    assert D.pending_count(arch) == 3
    D.run_block_distill(arch, arch / "原文.md", FakeBackend())
    assert D.pending_count(arch) == 0
