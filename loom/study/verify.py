# -*- coding: utf-8 -*-
"""B2 蒸馏分页 · 质量验收(设计稿 v2.1 §6,2026-11-06 定稿)。

L1 覆盖率(零 LLM,自动):块产出齐全?禁用词逐条转录进卡片集?关键实体被引用?
  <90% 标红。关键实体=人名册(_name_roster_for)+章标题专名;被引用=原稿或卡片集字面出现。
L2 对比仿写(1 次调用,验收非提取——作者确认它对蒸馏强度零增益,价值=作者直观看蒸馏度):
  取一块未参与蒸馏的原文,按卡片集仿写,与原文并排给作者目测。
L3 A/B 对拍(2 次,手动)不做在本模块——作者页面上手动两开对话即可。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .chunker import load_index
from .distiller import parse_entries


def _roster_entities(archive_dir: Path, original_path: Path) -> list[str]:
    """关键实体 = 人名册(书内才有;书房档案无书根 → 退化为章标题里的专名词)。"""
    ents: list[str] = []
    try:
        from .agents import _name_roster_for
        raw = _name_roster_for(Path(original_path).parent) or ""
        for line in raw.splitlines():
            s = line.strip().lstrip("-").strip()
            if "·" in s:
                s = s.split("·", 1)[1].strip()
            if s:
                ents.append(s)
    except Exception:
        pass
    # 章标题专名(书名号内/引号内词)作补充
    idx = load_index(archive_dir)
    for b in idx.get("blocks", []):
        t = b.get("title") or ""
        for seg in t.replace("》", "《").split("《")[1:]:
            w = seg.split("》")[0]
            if w:
                ents.append(w)
    return list(dict.fromkeys(ents))   # 去重保序


def l1_coverage(archive_dir: Path, original_path: Path) -> dict:
    """L1 覆盖率(零 LLM)。返回 {块齐全率, 实体命中率, 禁用转录率, 红线 bool, 明细}。

    三项各一票,任一 <0.9 → 红线(设计稿 §6)。
    """
    archive_dir = Path(archive_dir)
    idx = load_index(archive_dir)
    blocks = idx.get("blocks", [])
    distillable = [b for b in blocks if b.get("status") != "human_edited"]
    done = [b for b in distillable if b.get("status") == "distilled"]
    block_rate = len(done) / len(distillable) if distillable else 1.0

    # 禁用词逐条转录(审查意见3:三层衰减最狠的线)
    src_cards = archive_dir / "写手参考.md"
    cards = src_cards.read_text(encoding="utf-8") if src_cards.is_file() else ""
    banned_hit = banned_total = 0
    banned_missing: list[str] = []
    for f in sorted((archive_dir / "chunks").glob("chunk_*_distill.md")):
        for e in parse_entries(f.read_text(encoding="utf-8", errors="replace")):
            if e.startswith("✗") or "禁用" in e[:6]:
                banned_total += 1
                word = e.lstrip("✗ ").split("（")[0].strip()
                if word and word in cards:
                    banned_hit += 1
                else:
                    banned_missing.append(word)
    ban_rate = banned_hit / banned_total if banned_total else 1.0

    # 实体命中
    ents = _roster_entities(archive_dir, original_path)
    main = archive_dir / "蒸馏原稿.md"
    merged = main.read_text(encoding="utf-8") if main.is_file() else ""
    hit = sum(1 for e in ents if e and e in merged) if ents else 1
    ent_rate = hit / len(ents) if ents else 1.0

    rates = [block_rate, ban_rate, ent_rate]
    detail = {
        "块齐全率": round(block_rate, 3),
        "禁用转录率": round(ban_rate, 3),
        "实体命中率": round(ent_rate, 3),
        "禁用缺失": banned_missing[:10],
        "实体缺失": [e for e in ents if e not in merged][:10],
        "蒸馏时间": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    red = any(r < 0.9 for r in rates)
    out = {"红线": red, **detail}
    meta_p = archive_dir / "蒸馏原稿.meta.json"
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.is_file() else {}
        meta["l1"] = out
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return out


def l2_imitate(archive_dir: Path, backend) -> dict:
    """L2 对比仿写(1 次调用)。取一块未参与蒸馏的原文按卡片集仿写,返回并排材料。

    选块策略:优先 pending/human_edited 块(它们不在蒸馏集里);没有就取最后一块
    (蒸馏时它也在集里,但仿写仍可作"复现度"参考——注明局限)。
    """
    archive_dir = Path(archive_dir)
    idx = load_index(archive_dir)
    candidates = [b for b in idx.get("blocks", [])
                  if b.get("status") in ("pending", "human_edited")]
    if not candidates:
        candidates = [b for b in idx.get("blocks", [])
                      if b.get("status") == "distilled"][-1:]
    if not candidates:
        return {"ok": False, "reason": "没有可用块(先分块+蒸馏)。"}
    blk = candidates[0]
    body_f = archive_dir / "chunks" / (blk["chunk_id"] + ".md")
    if not body_f.is_file():
        return {"ok": False, "reason": "块正文缺失。"}
    body = body_f.read_text(encoding="utf-8", errors="replace")
    cards_f = archive_dir / "写手参考.md"
    if not cards_f.is_file():
        return {"ok": False, "reason": "还没有写手参考卡片集(先合并+压卡)。"}
    cards = cards_f.read_text(encoding="utf-8")

    prompt = ("""你是模仿写手。给你一份「写手参考卡片集」和一段原文的**内容梗概式改述**
（防止你照抄原文）。请按卡片集的文风，**重新写出**这段内容——像原作者会怎么写。
只输出仿写正文，不要解释。保持与原文相同的信息点，但句子组织、措辞、节奏要按卡片集来。"""
              )
    # 防照抄:给"内容点清单"而非原文
    points = "；".join(line.strip() for line in body.splitlines() if line.strip())[:1500]
    fake_input = f"【卡片集】\n{cards}\n\n【要写的内容点】\n{points}"
    imitated = (backend.complete(prompt, fake_input) or "").strip()
    return {"ok": True, "chunk_id": blk["chunk_id"], "title": blk.get("title") or "",
            "原文": body[:3000], "仿写": imitated[:3000]}
