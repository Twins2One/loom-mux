# -*- coding: utf-8 -*-
"""chunker 单测:B2 设计稿 v2 §2 切分规则 + §1 账本原子写/增量锚定。"""
from __future__ import annotations

import json

from loom.study import chunker as C


def _md(chapters: list[tuple[str, str]]) -> str:
    parts = []
    for t, body in chapters:
        parts.append(t + "\n\n" + body)
    return "\n\n".join(parts)


def test_chapter_split_basic():
    text = _md([("第一章 起步", "甲" * 100), ("第二章 转折", "乙" * 100)])
    chs = C.split_chapters(text)
    assert len(chs) == 2
    assert chs[0]["title"] == "第一章 起步"
    assert len(chs[0]["body"]) == 100


def test_chapter_under_max_is_one_block():
    """裁定③乙:章 ≤10000 字整章一块,绝不二次切。"""
    text = _md([("第一章", "字" * 9000)])
    blocks = C.chunk_text(text)
    assert len(blocks) == 1
    assert len(blocks[0]["body"]) == 9000


def test_long_chapter_split_by_scene():
    """章 >10000 → 场景符二次切,块上限 10000。"""
    body = ("段" * 1200 + "\n\n***\n\n") * 10   # 每场景 ~1210 字,共 ~12100
    text = _md([("第一章 长章", body)])
    blocks = C.chunk_text(text)
    assert len(blocks) >= 2
    assert all(len(b["body"]) <= C.CHUNK_MAX + 50 for b in blocks)   # 允许换行等少量误差
    # 标题带续段标记
    assert "续" in blocks[1]["title"]


def test_no_title_hard_split():
    """无章节标记 → 6000 字硬切。"""
    text = "\n\n".join(["这是第%d段的正文内容，讲了一个很长的故事。" % i + "续" * 1450 for i in range(12)])  # ~1500字/段×12≈18000字
    blocks = C.chunk_text(text)
    assert len(blocks) >= 2
    assert all(len(b["body"]) <= C.RAW_MAX * 1.25 for b in blocks)   # 空行间隔导致超限到下一空行,放宽25%


def test_chapter_hash_anchored_increment(tmp_path):
    """裁定⑥乙:中间插入只重蒸所在章,前后章照旧跳过。"""
    arch = tmp_path / "档案A"
    arch.mkdir()
    orig = arch / "原文.md"
    orig.write_text(_md([("第一章", "甲" * 500), ("第二章", "乙" * 500), ("第三章", "丙" * 500)]),
                    encoding="utf-8")
    idx1 = C.rebuild_index(arch, orig)
    assert idx1["块数"] == 3
    assert all(b["status"] == "pending" for b in idx1["blocks"])

    # 模拟蒸馏完成
    for b in idx1["blocks"]:
        b["status"] = "distilled"
    C.save_index(arch, idx1)

    # 在第二章中间插入一段 → 只有第二章 chapter_hash 变
    text2 = _md([("第一章", "甲" * 500), ("第二章", "乙" * 250 + "【新增】" + "乙" * 250),
                 ("第三章", "丙" * 500)])
    orig.write_text(text2, encoding="utf-8")
    idx2 = C.rebuild_index(arch, orig)
    st = {b["title"]: b["status"] for b in idx2["blocks"]}
    assert st["第一章"] == "distilled"      # 继承
    assert st["第二章"] == "pending"        # 变了,重蒸
    assert st["第三章"] == "distilled"      # 位置后移但 hash 没变 → 继承(锚定生效)


def test_atomic_write(tmp_path):
    """账本写完不留 .tmp;坏 JSON 当空。"""
    arch = tmp_path / "档案B"
    arch.mkdir()
    C.save_index(arch, {"version": 1, "blocks": [{"chunk_id": "chunk_001"}]})
    p = C.index_path(arch)
    assert p.is_file()
    assert not p.with_suffix(".json.tmp").exists()
    # 坏 JSON
    p.write_text("{broken", encoding="utf-8")
    idx = C.load_index(arch)
    assert idx["blocks"] == []
