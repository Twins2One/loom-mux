# -*- coding: utf-8 -*-
"""B2 蒸馏分页 · 分块器与块账本（设计稿 v2 §1/§2,2026-11-06 作者定稿）。

三层纪律:
1. **纯确定性,零 LLM**——分块是每本档案都跑的活,不许花钱;
2. **chapter_hash 锚定**(作者裁定⑥乙)——增量判定按「章标题+章内容 hash」,
   不按字符位置:作者在旧章中间插入内容只重蒸该章,前后章照旧跳过;
3. **原子写**(审查盲区1)——index.json 是唯一真相,临时文件+rename,崩溃不留半个文件。

切分规则(v2 §2,作者裁定③乙):
- 章节标题正则优先;**一章 ≤10000 字整章一块,绝不二次切**(网文章节 3-6k 字为主,
  结构级文风特征天然保住);
- 超 10000 字的章按场景符(连续空行/***/---)二次切,块上限 10000 字;
- 无章节标记的纯文本按 6000 字硬切,切点回退到最近句号/段落末尾。
- 零重叠(审查取舍 A:重叠费 10-20% 成本还制造去重负担)。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# ── 常量(v2 §2,作者定稿值) ─────────────────────────────────────────────
CHAPTER_MAX = 10_000    # 一章超过这个字数才二次切(≤ 则整章一块)
CHUNK_MAX = 10_000      # 二次切后的块上限
RAW_MAX = 6_000         # 无章节标记纯文本的硬切段
FALLBACK_MAX = 12_000   # 超长章二次切时单场景仍超 → 硬切兜底的起点

# 章节标题:中文数字/阿拉伯数字 + 章节卷回(标题行整体短,防把正文误判成标题),
# 或 Markdown 标题。行尾锚定 + 标题长度上限。
_CHAPTER_RE = re.compile(
    r"^(第[0-9一二三四五六七八九十百千两零〇]{1,7}\s*[章节卷回部篇]\s*[^。！？]{0,40}"
    r"|#{1,3}\s+.{1,60})\s*$")
# 场景分隔符:连续空行 / 星号线 / 横线线
_SCENE_RE = re.compile(r"^(?:\*{3,}|-{3,}|={3,}|~{3,})\s*$")
_BLANK_RE = re.compile(r"^\s*$")


# ── hash 工具 ──────────────────────────────────────────────────────────
def _h16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def text_hash(text: str) -> str:
    """一段文本的内容指纹(16 位,够判重不必全长)。"""
    return _h16(text)


# ── 章切分 ──────────────────────────────────────────────────────────────
def split_chapters(text: str) -> list[dict]:
    """按章节标题切。无标题的全文算一章(标题留空)。

    返回 [{title, body}]——title 含标题行本身,body 是章正文(不含标题行)。
    """
    lines = text.splitlines()
    chapters: list[dict] = []
    cur_title = ""
    cur_body: list[str] = []
    for line in lines:
        if _CHAPTER_RE.match(line.strip()):
            if cur_body or cur_title:
                chapters.append({"title": cur_title, "body": "\n".join(cur_body).strip()})
            cur_title = line.strip()
            cur_body = []
        else:
            cur_body.append(line)
    chapters.append({"title": cur_title, "body": "\n".join(cur_body).strip()})
    # 空章(标题后没正文)也保留——块账本要与原文结构对齐
    return chapters


def _split_by_scene(body: str, *, limit: int) -> list[str]:
    """章内二次切:优先场景符,场景仍超 limit → 句号/段落末尾回退硬切。零重叠。"""
    paragraphs = body.split("\n")
    blocks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in paragraphs:
        cur.append(line)
        cur_len += len(line)
        hit_limit = cur_len >= limit
        hit_scene = _SCENE_RE.match(line.strip()) or (
            _BLANK_RE.match(line) and _BLANK_RE.match(paragraphs[paragraphs.index(line) - 1].strip() if paragraphs.index(line) > 0 else "") if False else False)
        if (hit_scene and cur_len > limit // 2) or (hit_limit and _BLANK_RE.match(line.strip())):
            blocks.append("\n".join(cur).strip())
            cur, cur_len = [], 0
        elif cur_len >= FALLBACK_MAX:
            # 场景符兜底也没等到——硬切在最近句号
            joined = "\n".join(cur)
            cut = max(joined.rfind("。"), joined.rfind("！"), joined.rfind("？"), joined.rfind("”"))
            if cut > limit // 2:
                blocks.append(joined[: cut + 1].strip())
                rest = joined[cut + 1:]
                cur = [rest] if rest.strip() else []
                cur_len = len(rest)
            else:
                blocks.append(joined.strip())
                cur, cur_len = [], 0
    if cur and "\n".join(cur).strip():
        blocks.append("\n".join(cur).strip())
    return [b for b in blocks if b]


def chunk_text(text: str) -> list[dict]:
    """全文 → 块清单。每块 {title, body, chapter_hash, source_hash}。

    - 章节按 split_chapters;
    - 章 ≤ CHAPTER_MAX → 整章一块;
    - 章 > CHAPTER_MAX → 场景符二次切(CHUNK_MAX 上限);
    - 无标题全文 → RAW_MAX 硬切。
    """
    out: list[dict] = []
    chapters = split_chapters(text)
    for ch in chapters:
        body_len = len(ch["body"])
        no_title = not ch["title"]
        if no_title:
            # 无章节标记:RAW_MAX 硬切
            for seg in _split_by_scene(ch["body"], limit=RAW_MAX) if body_len > RAW_MAX else [ch["body"]]:
                if not seg:
                    continue
                out.append({"title": "", "body": seg})
        elif body_len <= CHAPTER_MAX:
            out.append({"title": ch["title"], "body": ch["body"]})
        else:
            for i, seg in enumerate(_split_by_scene(ch["body"], limit=CHUNK_MAX), 1):
                if not seg:
                    continue
                out.append({"title": f'{ch["title"]}（{i}/{i}段）' if i == 1 else
                            f'{ch["title"]}（续{i}）', "body": seg})
    for blk in out:
        blk["chapter_hash"] = text_hash(blk["title"] + "\n" + blk["body"])
        blk["source_hash"] = text_hash(blk["body"])
    return out


# ── 块账本(原子写) ──────────────────────────────────────────────────────
def index_path(archive_dir: Path) -> Path:
    return Path(archive_dir) / "chunks" / "index.json"


def load_index(archive_dir: Path) -> dict:
    """读账本。没有 → 空账本。坏 JSON → 当空(下次全量重建,不抛)。"""
    p = index_path(archive_dir)
    if not p.is_file():
        return {"version": 1, "blocks": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("blocks"), list):
            return {"version": 1, "blocks": []}
        return d
    except Exception:   # noqa: BLE001 - 坏账本当空,重建比报错友好
        return {"version": 1, "blocks": []}


def save_index(archive_dir: Path, index: dict) -> None:
    """原子写:临时文件 + os.replace(同盘 rename 是原子的)。崩溃不留半个文件。"""
    p = index_path(archive_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def rebuild_index(archive_dir: Path, original_path: Path) -> dict:
    """按原文重切并生成新账本。

    增量判定(作者裁定⑥乙,chapter_hash 锚定):
    - chapter_hash 与旧账本一致 → 保留旧块状态(distilled/human_edited 等全继承);
    - hash 变了(新章/改章) → 新块,status=pending;
    - human_edited 的块:hash 没变就保留;hash 变了 = 作者改了原文,人工态让位给重蒸
      (原稿另有 .v0 保护,这里不丢数据只重算)。
    """
    text = Path(original_path).read_text(encoding="utf-8", errors="replace")
    blocks = chunk_text(text)
    old = load_index(archive_dir)
    old_by_hash = {b.get("chapter_hash"): b for b in old.get("blocks", [])}
    out_blocks = []
    for i, blk in enumerate(blocks, 1):
        prev = old_by_hash.get(blk["chapter_hash"])
        if prev and prev.get("chapter_hash") == blk["chapter_hash"]:
            # 同一章:继承旧状态
            state = {k: prev.get(k) for k in
                     ("status", "distill_version", "last_distilled_at", "fail_reason")}
            state = {k: v for k, v in state.items() if v is not None}
        else:
            state = {"status": "pending"}
        out_blocks.append({
            "chunk_id": "chunk_%03d" % i,
            "title": blk["title"],
            "chapter_hash": blk["chapter_hash"],
            "source_hash": blk["source_hash"],
            "chars": len(blk["body"]),
            **state,
        })
    index = {"version": 1, "blocks": out_blocks,
             "原文_hash": text_hash(text), "块数": len(out_blocks)}
    save_index(archive_dir, index)
    return index


def write_chunk_files(archive_dir: Path, original_path: Path) -> int:
    """把原文块落成 chunk_NNN.md(只读中间产物)。返回块数。幂等:内容没变不重写。"""
    text = Path(original_path).read_text(encoding="utf-8", errors="replace")
    blocks = chunk_text(text)
    d = Path(archive_dir) / "chunks"
    d.mkdir(parents=True, exist_ok=True)
    for i, blk in enumerate(blocks, 1):
        f = d / ("chunk_%03d.md" % i)
        content = (("# " + blk["title"] + "\n\n") if blk["title"] else "") + blk["body"]
        if not f.is_file() or f.read_text(encoding="utf-8") != content:
            f.write_text(content, encoding="utf-8")
    return len(blocks)
