# -*- coding: utf-8 -*-
"""B2 蒸馏分页 · 合并器(设计稿 v2.1 §4,2026-11-06 定稿)。

三级流水:
1. 条目收集(零 LLM):全部 chunk_NNN_distill.md → parse_entries → 按节分组;
2. ONNX 语义去重(零 LLM):同节内条目两两比对,≥0.90(起跑值,真语料校准后可调)
   视为同义保留先出现;频次=条目出现的块数,同块内两轮同义不叠加;
3. 模型归纳(1~6 次):去重清单 ≤6000 字 → 一次归纳;超限 → 按节分组各归纳
   (≤5 次)再一次拼接归纳。硬约束:不添加清单外信息+每条附 quote 证据锚。

human_edited 时的增量(审查 Q5-2):新块条目走一次小归纳,输出形态与主稿一致,
追加到稿尾——卡片集压缩只面对一种文体。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .chunker import load_index, text_hash
from .distiller import parse_entries, _strip_evidence

DEDUPE_THRESHOLD = 0.90     # 起跑值;真语料抽检校准后可调(设计稿 §4.2)
MERGE_INPUT_LIMIT = 6_000   # 归纳输入上限(超限走分节归纳)
SECTION_INDENTIFY = "## "


def collect_entries(archive_dir: Path) -> dict[str, list[tuple[str, str, str]]]:
    """收集全部块产出条目 → {分节: [(条目, 块id, 原文证据)]}。

    条目格式(蒸馏提示词强制):`- 特征描述（证据：「原句片段」）`——
    代码按行解析,零 LLM;没有证据锚的条目在蒸馏侧已被 entry_ok 剔除。
    """
    chunks_dir = Path(archive_dir) / "chunks"
    out: dict[str, list[tuple[str, str, str]]] = {}
    idx = load_index(archive_dir)
    distilled = {b["chunk_id"] for b in idx.get("blocks", [])
                 if b.get("status") == "distilled"}
    for f in sorted(chunks_dir.glob("chunk_*_distill.md")):
        cid = f.name.replace("_distill.md", "")
        if cid not in distilled:
            continue   # human_edited/failed 块的旧产出不进合并
        section = "未分节"
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith(SECTION_INDENTIFY):
                section = s[3:].strip()
                out.setdefault(section, [])
                continue
            entries = parse_entries(s)
            for e in entries:
                out.setdefault(section, []).append((e, cid, _strip_evidence(e)))
    return out


def semantic_dedupe(grouped: dict[str, list[tuple[str, str, str]]],
                    *, threshold: float = DEDUPE_THRESHOLD
                    ) -> tuple[dict[str, list[dict]], int]:
    """ONNX 语义去重(零 LLM)。返回 ({节: [{text, quote, blocks, freq}]}, 剔除数)。

    - 同节内两两比对(不同节不比——「文风」与「节奏」的同形条目语义本不同);
    - 频次 = 同义簇覆盖的块数;同块内两轮同义(同 chunk_id)不叠加频次;
    - semantic 不可用 → 字面包含兜底,永不抛。
    """
    try:
        from . import semantic as _sem
        embed = _sem.embed
    except Exception:
        embed = None
    removed = 0
    result: dict[str, list[dict]] = {}
    for section, items in grouped.items():
        clusters: list[dict] = []   # [{text, quote, chunk_ids}]
        # 2026-11-16：簇向量缓存（与 clusters 同序的平行表）。
        # 原来内层每比一次就重新 `embed([cl["text"]])` —— 320 条条目要跑约 5 万次 ONNX 推理
        # （每次几十毫秒），实测多核空转十几分钟仍不回；真语料上等于「合并卡死」。
        # 每个簇只算一次向量后，总推理数从 O(n²) 降到 O(n)，判定结果完全不变。
        # 注意：向量存在这张平行表里，**不写进 clusters 的 dict**——那些 dict 会进 result
        # 并被序列化，塞 numpy 数组进去会炸 JSON。
        cluster_vecs: list = []
        for text, cid, quote in items:
            core = _strip_evidence(text)
            dup_cluster = None
            v = None
            if embed is not None:
                try:
                    import numpy as np
                    v = np.asarray(embed([core])[0]).ravel()
                    for i, cl in enumerate(clusters):
                        ov = cluster_vecs[i]
                        denom = float(np.linalg.norm(v) * np.linalg.norm(ov))
                        if denom > 0 and float(np.dot(v, ov) / denom) >= threshold:
                            dup_cluster = cl
                            break
                except Exception:
                    # 2026-11-16：这里会把 embed 置空并**对整个函数生效**（后面所有节都走字面兜底），
                    # 不是只影响"本节剩余"——语义向量一旦不可用就不该再反复试。
                    # 属质量降级（去重精度变差），不崩、不去数据，故不动行为，只把注释写准。
                    embed = None
                    cluster_vecs.clear()
                    v = None
            if dup_cluster is None and embed is None:
                for cl in clusters:
                    if core in cl["text"] or cl["text"] in core:
                        dup_cluster = cl
                        break
            if dup_cluster is not None:
                removed += 1
                if cid not in dup_cluster["chunk_ids"]:   # 同块两轮同义不叠加
                    dup_cluster["chunk_ids"].append(cid)
                # 证据锚取更长的(信息多)
                if len(quote) > len(dup_cluster["quote"]):
                    dup_cluster["quote"] = quote
            else:
                clusters.append({"text": core, "quote": quote, "chunk_ids": [cid]})
                if v is not None:
                    cluster_vecs.append(v)
        # 频次=覆盖块数,降序;同频按先出现
        for cl in clusters:
            cl["freq"] = len(cl["chunk_ids"])
        result[section] = sorted(clusters, key=lambda c: -c["freq"])
    return result, removed


def _render_list(deduped: dict[str, list[dict]], *, max_chars: int | None = None) -> str:
    """去重簇 → 归纳输入文本(条目行+quote+频次标注)。"""
    parts = []
    total = 0
    for section, clusters in deduped.items():
        sec_lines = [f"## {section}（{len(clusters)} 条,按跨章出现频次排序）"]
        for cl in clusters:
            line = f"- {cl['text']}（出现 {cl['freq']} 章;证据：「{cl['quote']}」）"
            sec_lines.append(line)
        body = "\n".join(sec_lines)
        if max_chars is not None and total + len(body) > max_chars:
            break
        parts.append(body)
        total += len(body)
    return "\n\n".join(parts)


MERGE_PROMPT = """你是文风综合师。下面是从一部参考作品多个章节提取的**特征条目清单**
（已按跨章频次排序,每条带原文证据）。请把它们综合成一份**连贯的蒸馏原稿**。

两条硬约束:
1. **不添加清单中没有的信息**——你只组织语言与归类,不发明新特征;
2. **每条文风规则必须保留至少一个原文原句 quote 作证据锚**——磨平/规整化
   作者特有的表达在证据锚下无处藏身,那些"别扭"处恰是文风本体。

矛盾项(如"短句为主"vs"长句绵延")不要平均化,标注「视场景而定」并两边都保留。
保持分节结构输出。"""

SECTION_MERGE_PROMPT = MERGE_PROMPT + "\n本只归纳清单中属于「{section}」一节的条目。"

STITCH_PROMPT = """你是文风综合师。下面是几份分节归纳稿。请拼接成一份连贯蒸馏原稿:
保持各稿分节,合并重复节名,不添加任何新信息,保留全部证据锚。"""


def merge_distills(archive_dir: Path, backend, *, emit=lambda ev: None) -> dict:
    """合并入口:收集→去重→归纳→落 蒸馏原稿.md(旧版转 .v{N} 保留最多5版)。

    human_edited 原稿在(读者手改过) → 不重写主稿,只把新增块条目走一次小归纳
    追加稿尾(审查 Q5-2)。返回 {mode, calls, sections, entries_in}。
    """
    archive_dir = Path(archive_dir)
    grouped = collect_entries(archive_dir)
    deduped, removed = semantic_dedupe(grouped)
    n_entries = sum(len(v) for v in deduped.values())
    calls = 0

    meta_path = archive_dir / "蒸馏原稿.meta.json"
    meta = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    main = archive_dir / "蒸馏原稿.md"
    human_edited = meta.get("human_edited") is True

    listing = _render_list(deduped)
    new_text = ""
    if len(listing) <= MERGE_INPUT_LIMIT:
        new_text = backend.complete(MERGE_PROMPT, listing)
        calls += 1
    else:
        # 分节归纳(≤5 节,每节一次)→ 拼接
        section_texts = []
        for section, clusters in list(deduped.items())[:5]:
            sec_listing = _render_list({section: clusters})
            section_texts.append(backend.complete(
                SECTION_MERGE_PROMPT.replace("{section}", section), sec_listing))
            calls += 1
        new_text = backend.complete(STITCH_PROMPT, "\n\n---\n\n".join(section_texts))
        calls += 1

    if human_edited and main.is_file():
        # 只追加:新增块条目的小归纳稿贴到稿尾,作者手改的主体不动
        addendum = backend.complete(
            "把以下新增条目归纳成与蒸馏原稿一致的文体(短小即可,保留证据锚):", listing)
        calls += 1
        cur = main.read_text(encoding="utf-8")
        main.write_text(cur.rstrip() + "\n\n## 增量新增\n" + addendum.strip() + "\n",
                        encoding="utf-8")
    else:
        # 版本链:现有主稿转 .v{N}(最多5版),再写新版
        if main.is_file():
            _rotate_versions(main, meta)
        main.write_text(new_text.strip() + "\n", encoding="utf-8")

    meta["human_edited"] = False
    meta["last_approved_hash"] = text_hash(main.read_text(encoding="utf-8"))
    meta["last_merged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["版本"] = meta.get("版本", 0) + 1
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    emit({"t": "merge_done", "entries": n_entries, "removed": removed, "calls": calls})
    return {"mode": "append" if human_edited else "full",
            "calls": calls, "sections": len(deduped), "entries_in": n_entries,
            "removed": removed}


def _rotate_versions(main: Path, meta: dict) -> None:
    """旧主稿 → .v{N}.md;版本 >5 删最旧。"""
    import json as _json
    n = int(meta.get("版本", 0)) + 1
    backup = main.with_name(f"蒸馏原稿.v{n}.md")
    backup.write_text(main.read_text(encoding="utf-8"), encoding="utf-8")
    versions = sorted(main.parent.glob("蒸馏原稿.v*.md"),
                      key=lambda p: int(p.stem.split(".v")[-1]))
    for old in versions[:-5]:
        old.unlink()



def build_cards(archive_dir: Path, backend) -> str:
    """蒸馏原稿 → 写手参考卡片集(六节,§5)。禁用清单逐条全量转录禁止概括。"""
    src = (Path(archive_dir) / "蒸馏原稿.md").read_text(encoding="utf-8")
    prompt = """你是写手参考编辑。把下面的蒸馏原稿整理成**卡片集**,固定六节:
## 文风规则     （正例+反例格式;每条附证据 quote）
## 节奏规则
## 人物塑造
## 叙事手法     （宏观条目归这节;保持原样不要改成句级规则）
## 禁用清单     （✗ 开头,一行一条;**逐条全量转录,禁止概括、禁止合并、禁止遗漏**）
## 高频句式库

除禁用清单外,每节条目数宁缺毋滥;不添加原稿没有的信息。"""
    cards = backend.complete(prompt, src)
    out = Path(archive_dir) / "写手参考.md"
    out.write_text(cards.strip() + "\n", encoding="utf-8")
    # stale 清除:卡片集刚生成,与原稿同步
    meta_p = archive_dir / "蒸馏原稿.meta.json"
    if meta_p.is_file():
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta["cards_stale"] = False
        meta["cards_source_hash"] = text_hash(src)   # 压卡那一刻的原稿 hash
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return cards


def check_stale(archive_dir: Path) -> bool:
    """卡片集是否过期(审查盲区4):蒸馏原稿自上次压卡后有变化(含作者手改)→ stale。

    判据 = 原稿当前 hash ≠ meta.cards_source_hash(压卡那一刻的原稿 hash)。
    """
    p = Path(archive_dir)
    src, cards, meta_p = p / "蒸馏原稿.md", p / "写手参考.md", p / "蒸馏原稿.meta.json"
    if not cards.is_file() or not src.is_file() or not meta_p.is_file():
        return False
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        return text_hash(src.read_text(encoding="utf-8")) != meta.get("cards_source_hash")
    except Exception:
        return False
