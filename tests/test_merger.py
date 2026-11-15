# -*- coding: utf-8 -*-
"""merger 单测:条目收集/语义去重兜底/归纳分批/human_edited 增量/版本链/stale。"""
from __future__ import annotations

import json

from loom.study import chunker as C
from loom.study import distiller as D
from loom.study import merger as M
from tests.test_distiller import FakeBackend  # noqa: F401  (D.FakeBackend 别名兼容)
D.FakeBackend = FakeBackend


class FakeBackend:
    """归纳用假后端:回显「归纳稿」+记录调用次数与输入。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, **kw):
        self.calls.append((system, user))
        return "## 综合文风\n- 归纳条目（证据：「原句」）\n"


class NoEmbedBackend(FakeBackend):
    pass


def _mk_distilled(tmp_path, n=3):
    """造一个蒸完 3 块的档案。"""
    arch = tmp_path / "档案"
    orig = arch / "原文.md"
    orig.parent.mkdir(parents=True)
    orig.write_text("\n\n".join("第%d章\n\n%s" % (i, "情节" * 50) for i in range(1, n + 1)),
                    encoding="utf-8")
    C.rebuild_index(arch, orig)
    C.write_chunk_files(arch, orig)
    D.run_block_distill(arch, orig, D.FakeBackend())
    return arch


def test_collect_entries(tmp_path):
    arch = _mk_distilled(tmp_path, 2)
    grouped = M.collect_entries(arch)
    assert "文风特征" in grouped
    texts = [t for t, _, _ in grouped["文风特征"]]
    assert any("短句为主" in t for t in texts)
    # failed/human_edited 块不进
    idx = C.load_index(arch)
    idx["blocks"][0]["status"] = "human_edited"
    C.save_index(arch, idx)
    grouped2 = M.collect_entries(arch)
    assert len(grouped2.get("文风特征", [])) < len(grouped["文风特征"])


def test_semantic_dedupe_literal_fallback(monkeypatch):
    import loom.study.semantic as sem
    monkeypatch.setattr(sem, "embed", lambda *a, **k: None, raising=False)
    grouped = {"文风特征": [("短句为主，白描", "chunk_001", "短句"),
                            ("短句为主", "chunk_002", "短句"),
                            ("爱写打斗", "chunk_003", "打斗")]}
    deduped, removed = M.semantic_dedupe(grouped, threshold=0.90)
    assert len(deduped["文风特征"]) == 2 and removed == 1
    # 频次=覆盖块数(同义簇合并了两块)
    top = deduped["文风特征"][0]
    assert top["freq"] == 2


def test_merge_full(tmp_path):
    arch = _mk_distilled(tmp_path, 2)
    fb = FakeBackend()
    stats = M.merge_distills(arch, fb)
    assert stats["mode"] == "full" and stats["calls"] >= 1
    assert (arch / "蒸馏原稿.md").is_file()
    meta = json.loads((arch / "蒸馏原稿.meta.json").read_text(encoding="utf-8"))
    assert meta["human_edited"] is False
    assert meta["last_approved_hash"]


def test_merge_version_rotation(tmp_path):
    arch = _mk_distilled(tmp_path, 2)
    fb = FakeBackend()
    for _ in range(7):
        M.merge_distills(arch, fb)
    versions = list(arch.glob("蒸馏原稿.v*.md"))
    assert len(versions) <= 5


def test_merge_human_edited_appends(tmp_path):
    """human_edited 主稿:不重写,增量小归纳追加稿尾。"""
    arch = _mk_distilled(tmp_path, 2)
    fb = FakeBackend()
    M.merge_distills(arch, fb)
    main = arch / "蒸馏原稿.md"
    main.write_text("作者手改的独家内容。", encoding="utf-8")
    meta = json.loads((arch / "蒸馏原稿.meta.json").read_text(encoding="utf-8"))
    meta["human_edited"] = True
    (arch / "蒸馏原稿.meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                             encoding="utf-8")
    before = main.read_text(encoding="utf-8")
    stats = M.merge_distills(arch, fb)
    assert stats["mode"] == "append"
    after = main.read_text(encoding="utf-8")
    assert after.startswith(before)          # 手改主体在前,没被覆盖
    assert "## 增量新增" in after


def test_merge_oversized_splits(tmp_path, monkeypatch):
    """清单超 6000 字 → 分节归纳+拼接(调用数>1)。

    2026-11-16：本机 vendor/embed 的 ONNX 模型在位，语义去重会真的把下面这批
    高度相似的条目折叠掉，`len(listing) > MERGE_INPUT_LIMIT` 这条前提就不再成立
    （测试变成"看环境里有没有模型"）。这条测的是**超限拆分逻辑**，不是去重质量，
    所以固定走字面兜底，让输入规模可复现。
    """
    import loom.study.semantic as sem
    monkeypatch.setattr(sem, "embed", lambda *a, **k: None, raising=False)
    arch = _mk_distilled(tmp_path, 2)
    # 伪造大量条目
    words = ["短句", "长句", "比喻", "白描", "排比", "反问", "独白", "对白", "插叙", "倒叙",
             "悬念", "伏笔", "留白", "重复", "递进", "转折", "呼告", "设问", "夸张", "通感"]
    lines = []
    n = 0
    for w in words:
        for i in range(16):
            n += 1
            lines.append(f"- {w}手法变体{i}：{w}与名词{n}的组合节奏（证据：「{w}句样例{n}。」）")
    big = arch / "chunks" / "chunk_001_distill.md"
    big.write_text("## 文风特征\n" + "\n".join(lines), encoding="utf-8")
    fb = FakeBackend()
    stats = M.merge_distills(arch, fb)
    # 300 条×~22 字 ≈ 6600 > 6000 → 必走分节路径(calls>=2)
    # 若实现只回 1 次,说明超限判定失效
    grouped = M.collect_entries(arch)
    listing = M._render_list(M.semantic_dedupe(grouped)[0])
    assert len(listing) > M.MERGE_INPUT_LIMIT, "测试数据应超限"
    assert stats["calls"] >= 2


def test_build_cards_and_stale(tmp_path):
    arch = _mk_distilled(tmp_path, 2)
    fb = FakeBackend()
    M.merge_distills(arch, fb)
    cards = M.build_cards(arch, fb)
    assert fb.calls and "卡片集" in fb.calls[-1][0]   # 用了卡片集提示词
    assert (arch / "写手参考.md").is_file()
    assert M.check_stale(arch) is False
    # 作者手改原稿 → stale
    (arch / "蒸馏原稿.md").write_text("改过了。", encoding="utf-8")
    assert M.check_stale(arch) is True
