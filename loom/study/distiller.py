# -*- coding: utf-8 -*-
"""B2 蒸馏分页 · 块蒸馏执行器（设计稿 v2.1 §3,2026-11-06 作者定稿）。

状态机(审查消歧后口径):
- 每块: pending → (蒸馏中) → distilled | failed
  自动重试 ≤2 次,重试后仍败 → 终态 failed,**继续下一块**;
  连续 3 块终态 failed → 中止整批(已完成块保留,续跑从断点起);
- human_edited 块跳过(增量保护的根);
- 串行执行(审查确认合理,天然避并发额度争抢);
- 深度蒸馏开关 → 每块追加 1 次多层粗到细调用(§3 三层,句级归第一轮);
- 深度轮混入与第一轮同义的条目 → ONNX 本地去重后才写入 §深度 节(零 LLM)。

每块产出落 chunks/chunk_NNN_distill.md,条目格式强制「一行一条 - 开头」
(合并器代码解析,零 LLM 抽取,成本口径不被破坏)。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .chunker import load_index, save_index, index_path

MAX_AUTO_RETRY = 2          # 单块自动重试上界(审查盲区6)
CONSECUTIVE_FAIL_STOP = 3    # 连续 failed 中止整批(防烧钱循环)
MERGE_DUP_LIMIT = 6_000     # 归纳输入字数上界(超限分节归纳,设计稿 §4.3)


# ── 提示词(v2.1 §3;深度轮输入构造含全书目录+前后章梗概) ────────────────
GENERAL_PROMPT = """你是文风分析师。下面是一段小说正文。请从五个维度提取**可模仿的写作特征**，
每个维度**至少 3 条，真没有就写「无」**；每条特征一行、以 `- ` 开头、行内不换行；
**每条必须引用原文原句作证据**（格式：`- 特征描述（证据：「原句片段」）`）。

## 文风特征
（句式长短偏好、用词习惯、修辞倾向、叙述口吻）
## 叙事手法
（视角运用、展示vs讲述、信息释放节奏）
## 节奏结构
（段落长短变化、场景切换方式、对话与叙述配比）
## 人物塑造
（人物出场手法、对话个性、内心独白写法）
## 世界观设定要素
（本段涉及的世界规则、术语、地理、势力——只记出现的，不推断）

只提取**这一段里真实存在的**，不推断整本书。"""

DEEP_PROMPT = """你是叙事手法分析师。给你的材料是：全书章节目录、相邻章节梗概、
**本章正文**，以及第一轮已提取的条目统计（只给数量，防止你重复）。
请从三个层级提取**作者会怎么写的可模仿操作**——不是文学评论：「本章是转折点」
这种判断没用，「作者用【突然，】单句起段完成转折」才有用。
每条一行、以 `- ` 开头；**每条必须引用本章原文原句作证据**（证据：「原句片段」）；
找不到就写「无」——**宁可 0 条，不要凑数**。

## 整章情节功能的实现手法
（参照章节目录：这一章承担什么功能——铺垫/转折/高潮/过渡？作者**用什么手法实现**它：
新信息怎么抛、冲突怎么升级、悬念怎么留。至少 3 条）
## 过渡手法
（场景切换用什么词/句式/空行、时间线跳转怎么标记、视角转换的信号词。至少 3 条）
## 描写密度与松紧
（哪段用短句提速、哪段用长描写放缓、对话与叙述怎么配比。至多 5 条）

【第一轮已产出（勿重复其维度，句级措辞不归你管）】
{first_round_stats}"""

# ── 条目解析(零 LLM) ───────────────────────────────────────────────────
_ENTRY_RE = re.compile(r"^[-*]\s+(.+)$")


def parse_entries(distill_text: str) -> list[str]:
    """蒸馏产出 → 特征条目列表(按 - 行解析,分节标题忽略)。合并器直接吃这个。"""
    out = []
    for line in distill_text.splitlines():
        m = _ENTRY_RE.match(line.strip())
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out


# ── 条目口径校验(二轮审查附 B:纯评价句剔除) ───────────────────────────
_EVAL_RE = re.compile(r"^(本章|这一章|全章|本段)?(节奏|情绪|情节|氛围|张力)[^「」]*$")


def entry_ok(entry: str) -> bool:
    """「可模仿动作」口径:必须含证据锚,且不是纯评价句(无引号的节奏/情绪类空话)。"""
    if "（证据：" not in entry and "（证据:" not in entry:
        return False
    core = _strip_evidence(entry)
    if _EVAL_RE.match(core):
        return False
    return True


def _first_round_stats(general_text: str) -> str:
    """第一轮产出 → 「分节标题+条数」清单(深度轮提示词的防重上下文,不给全文)。"""
    lines_out = []
    cur_section = ""
    count = 0
    for line in general_text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            if cur_section:
                lines_out.append(f"{cur_section}: {count} 条")
            cur_section = s[3:].strip()
            count = 0
        elif _ENTRY_RE.match(s):
            count += 1
    if cur_section:
        lines_out.append(f"{cur_section}: {count} 条")
    return "\n".join(lines_out) if lines_out else "（第一轮无产出）"


def _strip_evidence(entry: str) -> str:
    """去掉条目尾部的（证据：「…」）——语义比对只比对特征本身。"""
    return re.sub(r"（证据：.*?）$", "", entry).strip()


# ── 语义去重(深度轮写入前,零 LLM,审查 Q2 修正案) ───────────────────────
def dedupe_against(new_entries: list[str], existing_entries: list[str],
                   *, threshold: float = 0.85) -> list[str]:
    """深度轮条目 vs 第一轮已有条目:ONNX 语义相似 ≥threshold 的剔除。

    semantic 不可用(引擎坏了/无向量) → 退回字面包含判定,永不抛。
    """
    if not new_entries:
        return []
    try:
        from . import semantic as _sem
        embed = _sem.embed
    except Exception:
        embed = None
    if embed is None:
        # 兜底:字面包含(新条目与旧条目互含 → 视为同义)
        kept = []
        for e in new_entries:
            core = _strip_evidence(e)
            if not any(core in _strip_evidence(x) or _strip_evidence(x) in core
                       for x in existing_entries):
                kept.append(e)
        return kept
    try:
        import numpy as np
        new_vecs = [np.asarray(embed(_strip_evidence(e))) for e in new_entries]
        old_vecs = [np.asarray(embed(_strip_evidence(e))) for e in existing_entries] \
            if existing_entries else []
        kept = []
        for i, nv in enumerate(new_vecs):
            dup = False
            for ov in old_vecs:
                a, b = np.asarray(nv).ravel(), np.asarray(ov).ravel()
                denom = (np.linalg.norm(a) * np.linalg.norm(b))
                if denom > 0 and float(np.dot(a, b) / denom) >= threshold:
                    dup = True
                    break
            if not dup:
                kept.append(new_entries[i])
                old_vecs.append(nv)   # 深度条目之间也互查
        return kept
    except Exception:
        pass   # 向量化失败 → 落到字面兜底(与无 embed 同路径,不吞判定)
    # 字面包含兜底(与无 embed 同一份逻辑)
    kept = []
    for e in new_entries:
        core = _strip_evidence(e)
        if not any(core in _strip_evidence(x) or _strip_evidence(x) in core
                   for x in existing_entries):
            kept.append(e)
    return kept


# ── 上下文构造(深度轮层①,审查 Q1 大洞的修正案) ────────────────────────
def _deep_context(index: dict, blocks_text: dict[str, str], chunk_id: str) -> str:
    """全书章节目录 + 前后章梗概(各 ~300 字)。零额外调用,全部来自已有数据。"""
    blocks = index.get("blocks", [])
    toc = "\n".join(b.get("title") or b["chunk_id"] for b in blocks)
    parts = ["【全书章节目录】\n" + toc]
    for offset in (-1, 1):
        j = next((i for i, b in enumerate(blocks) if b["chunk_id"] == chunk_id), None)
        if j is None:
            continue
        k = j + offset
        if 0 <= k < len(blocks):
            body = blocks[k].get("title") or ""
            seg = blocks_text.get(blocks[k]["chunk_id"], "")[:300]
            parts.append(f"【{'前' if offset < 0 else '后'}一章·{body}梗概】\n{seg}")
    return "\n\n".join(parts)


# ── 执行器本体 ─────────────────────────────────────────────────────────
def run_block_distill(archive_dir: Path, original_path: Path, backend,
                      *, deep: bool = False, emit=lambda ev: None,
                      should_cancel=None) -> dict:
    """串行逐块蒸馏。返回统计 {done, failed, skipped, aborted, 总调用}。

    emit 事件:t=status(逐块进度) / t=block_done / t=block_failed / t=aborted。
    should_cancel 返回 True → 在块边界安全停止(已完成块保留)。
    """
    archive_dir = Path(archive_dir)
    index = load_index(archive_dir)
    blocks = index.get("blocks", [])
    chunks_dir = archive_dir / "chunks"

    # 块原文索引(chunk_NNN → 正文):write_chunk_files 已落盘,这里读
    blocks_text: dict[str, str] = {}
    for b in blocks:
        f = chunks_dir / (b["chunk_id"] + ".md")
        if f.is_file():
            blocks_text[b["chunk_id"]] = f.read_text(encoding="utf-8", errors="replace")

    stats = {"done": 0, "failed": 0, "skipped": 0, "aborted": False, "calls": 0}
    consec_fail = 0
    for b in blocks:
        if should_cancel and should_cancel():
            emit({"t": "aborted", "text": "作者取消,已完成块保留。"})
            stats["aborted"] = True
            break
        cid = b["chunk_id"]
        st = b.get("status", "pending")
        if st == "human_edited":
            stats["skipped"] += 1
            continue
        if st == "distilled":
            stats["skipped"] += 1
            continue
        body = blocks_text.get(cid, "")
        if not body:
            b["status"], b["fail_reason"] = "failed", "块正文缺失(先跑分块)"
            stats["failed"] += 1
            continue
        emit({"t": "status", "text": f"蒸馏 {cid}({b.get('title') or '无标题'} ,{len(body)}字)…"})
        # 自动重试 ≤2,终态 failed 继续下一块(审查 Q5-4 消歧口径)
        ok = False
        last_err = ""
        for attempt in range(1 + MAX_AUTO_RETRY):
            try:
                text = backend.complete(GENERAL_PROMPT, body)
                stats["calls"] += 1
                if not (text or "").strip():
                    raise ValueError("模型返回为空")
                if deep:
                    ctx = _deep_context(index, blocks_text, cid)
                    # 二轮审查 Q4:给「第一轮分节标题+条数」清单,不给全文——
                    # 独立视角与避免重复产出两坑都避,成本零
                    stats_lines = _first_round_stats(text)
                    deep_prompt = DEEP_PROMPT.replace("{first_round_stats}", stats_lines)
                    dtext = backend.complete(deep_prompt, ctx + "\n\n【本章正文】\n" + body)
                    stats["calls"] += 1
                    # 口径校验(剔纯评价句)→ ONNX 本地去重 → 写入 §深度 节
                    deep_entries = [e for e in parse_entries(dtext) if entry_ok(e)]
                    existing = parse_entries(text)
                    fresh = dedupe_against(deep_entries, existing)
                    if fresh:
                        text = text.rstrip() + "\n\n## 深度\n" + "\n".join("- " + e for e in fresh)
                dist = chunks_dir / (cid + "_distill.md")
                dist.write_text(text.strip() + "\n", encoding="utf-8")
                b["status"] = "distilled"
                b["distill_version"] = (b.get("distill_version") or 0) + 1
                b["last_distilled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                b.pop("fail_reason", None)
                ok = True
                break
            except Exception as e:   # noqa: BLE001 - 计费失败要给人话,不抛
                last_err = str(e) or type(e).__name__
                emit({"t": "status", "text": f"{cid} 第{attempt+1}次没成:{last_err}"})
        if ok:
            stats["done"] += 1
            consec_fail = 0
            emit({"t": "block_done", "chunk_id": cid})
        else:
            b["status"], b["fail_reason"] = "failed", last_err
            stats["failed"] += 1
            consec_fail += 1
            emit({"t": "block_failed", "chunk_id": cid, "reason": last_err})
            if consec_fail >= CONSECUTIVE_FAIL_STOP:
                emit({"t": "aborted",
                      "text": f"连续 {CONSECUTIVE_FAIL_STOP} 块失败,中止整批。已完成块保留,"
                              f"检查模型配置后可续跑。"})
                stats["aborted"] = True
                break
        save_index(archive_dir, index)   # 每块落一次账(原子写,断点可续)
    save_index(archive_dir, index)
    return stats


def pending_count(archive_dir: Path) -> int:
    """待蒸馏块数(UI 成本预告用)。"""
    return sum(1 for b in load_index(archive_dir).get("blocks", [])
               if b.get("status") not in ("distilled", "human_edited"))
