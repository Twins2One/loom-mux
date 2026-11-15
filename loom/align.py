# -*- coding: utf-8 -*-
r"""领航员 · 大纲与正文符合度复核( 3)。

需求背景:「领航员与作者复核大纲与内容符合度,以及是否需要更改」。

做法:
- 取一段章(默认按 50 章一块),把每章的「章纲要什么」和「正文实际写了什么(开头节选)」摆给
  **长窗口那一槽**(「长上下文检索」)对照;
- 只产出**提案**:逐章「符合 / 偏离 + 理由 + 建议改哪边(改章纲 / 改正文 / 不改)」;
- 人勾过之后才落成 `外置大脑/复核/大纲符合度-第LO-HI章.md` —— **绝不自动改章纲或正文**。
  理由同事实抽取:一旦 AI 猜错并直接落盘,章纲与正文谁是真的就再也说不清。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .backends import Backend
from . import paths

VERDICTS = ("符合", "偏离")
SUGGESTS = ("改章纲", "改正文", "不改")
PAIR_CHARS = 600     # 每章正文取开头多少字去对照
BLOCK = 50           # 与「铺章纲」「实体词典」同一个单位
_REVIEW_DIR = "外置大脑/复核"

_RULES = """你是这本长篇小说的「领航员」。作者要知道:**章纲要的东西,正文到底写出来没有**。

输入是若干章,每章两段:「章纲」和「正文节选」。逐章判断:

- **符合**:章纲的要点在正文里都有,走向没变;
- **偏离**:正文少写了章纲里的要点、或走向变了、或章纲本身和前后文对不上。

只输出一行一章,格式:**第N章:符合|理由** 或 **第N章:偏离|理由|改章纲** / **…|改正文** / **…|不改**。
第三格是你建议改哪边 —— 只能在这三个词里挑。**别改任何东西,你只出意见。**
理由一句话,说清「少了什么 / 变了什么」,不要泛泛说「写得不够好」。
没有问题的章也要写一行「符合」,不要漏章。
"""


def _card_rows(root: Path) -> dict[int, str]:
    p = root / paths.CARD_REL
    if not p.is_file():
        return {}
    out: dict[int, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*[-*]\s*第\s*(\d+)\s*章\s*[:：]\s*(.*)$", line)
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    return out


def block_range(chapter_n: int, span: int = BLOCK) -> tuple[int, int]:
    n = max(1, int(chapter_n))
    span = max(1, int(span))
    lo = (n - 1) // span * span + 1
    return lo, lo + span - 1


def pairs(root: Path | str, lo: int, hi: int, *, chars: int = PAIR_CHARS) -> list[dict]:
    """这一段里「章纲也有、正文也有」的章。两边缺一样就不进对照(没什么可比的)。"""
    root = Path(root)
    rows = _card_rows(root)
    out = []
    for n in range(int(lo), int(hi) + 1):
        card = rows.get(n, "").strip()
        p = paths.chapter_path(root, n)
        try:
            body = p.read_text(encoding="utf-8").strip() if p.is_file() else ""
        except OSError:
            body = ""
        if not card or not body:
            continue
        out.append({"章": n, "章纲": card, "正文": body[:chars],
                    "正文全字数": len(body)})
    return out


def _prompt(ps: list[dict]) -> str:
    blocks = []
    for it in ps:
        blocks.append(f"【第{it['章']}章】\n章纲:{it['章纲']}\n正文节选:{it['正文']}")
    return "\n\n".join(blocks)


def parse_verdicts(raw: str, chapters: list[int]) -> tuple[list[dict], list[str]]:
    """把「第N章:符合|理由|改章纲」这类行拆成结构化。读不懂的行不丢,记进「没读懂」。"""
    got: dict[int, dict] = {}
    bad: list[str] = []
    for line in (raw or "").splitlines():
        m = re.search(r"第\s*(\d+)\s*章\s*[:：]\s*(.*)$", line.strip())
        if not m:
            if line.strip():
                bad.append(line.strip()[:80])
            continue
        n = int(m.group(1))
        cells = [c.strip() for c in m.group(2).split("|")]
        verdict = cells[0].strip()
        if verdict not in VERDICTS:
            bad.append(line.strip()[:80])
            continue
        reason = cells[1].strip() if len(cells) > 1 else ""
        suggest = cells[2].strip() if len(cells) > 2 else ""
        if suggest not in SUGGESTS:
            suggest = "待定" if suggest else ("不改" if verdict == "符合" else "待定")
        got[n] = {"章": n, "判断": verdict, "理由": reason, "建议": suggest}
    verdicts = [got[n] for n in chapters if n in got]
    missing = [n for n in chapters if n not in got]
    return verdicts, bad + [f"第{n}章 模型没给判断" for n in missing]


def review(root: Path | str, lo: int, hi: int, backend: Backend, *, max_chars: int = 0) -> dict:
    """出一份提案。**不落盘**。"""
    ps = pairs(root, lo, hi)
    if not ps:
        return {"ok": False, "落盘": False,
                "reason": f"第 {lo}-{hi} 章里没有「章纲与正文都齐」的章,没什么可对照的。"}
    raw = backend.complete(_RULES, _prompt(ps), max_chars=max_chars or None)
    nums = [it["章"] for it in ps]
    verdicts, bad = parse_verdicts(raw, nums)
    off = [v["章"] for v in verdicts if v["判断"] == "偏离"]
    return {"ok": True, "落盘": False, "起": lo, "止": hi, "对照章数": len(ps),
            "逐章": verdicts, "偏离章": off, "没读懂的行": bad[:20],
            "模型原文长度": len(raw or ""), "生成于": time.strftime("%Y-%m-%d %H:%M:%S")}


def _doc(r: dict) -> str:
    lines = [f"# 大纲符合度复核 · 第 {r['起']}-{r['止']} 章", "",
             f"> 生成于 {r.get('生成于') or time.strftime('%Y-%m-%d %H:%M:%S')}。"
             "这是「领航员」的**复核意见**,不是判决:改章纲还是改正文,由作者逐条定。",
             "> 判据(2026-10-18 裁定):设定 > 章纲 > 正文;作者一旦另裁,其余冲突项逐处请示。",
             "", f"对照 {r['对照章数']} 章,其中 **{len(r['偏离章'])} 章偏离**。", "",
             "## 逐章", "", "| 章 | 判断 | 理由 | 建议 |", "|---|---|---|---|"]
    for v in r["逐章"]:
        cells = [str(v["章"]), v["判断"], v["理由"] or "", v["建议"] or ""]
        lines.append("| " + " | ".join(c.replace("|", "／") for c in cells) + " |")
    if r.get("没读懂的行"):
        lines += ["", "## 没读懂的行(模型没说清)", ""]
        lines += [f"- {x}" for x in r["没读懂的行"]]
    return "\n".join(lines) + "\n"


def accept(root: Path | str, r: dict) -> dict:
    """把复核意见落成文档(**人点过才调**)。同名不覆盖:加序号。"""
    root = Path(root)
    if not r.get("ok") or not r.get("逐章"):
        return {"ok": False, "reason": "这份复核是空的,没什么可存的。"}
    d = root / _REVIEW_DIR
    d.mkdir(parents=True, exist_ok=True)
    src = {"起": r.get("起") or 0, "止": r.get("止") or 0, "对照章数": r.get("对照章数", 0),
           "逐章": r.get("逐章") or {}, "偏离章": r.get("偏离章", []),
           "没读懂的行": r.get("没读懂的行", []), "生成于": r.get("生成于")}
    base = f"大纲符合度-第{src['起']}-{src['止']}章"
    p = d / f"{base}.md"
    i = 2
    while p.exists():
        p = d / f"{base}({i}).md"
        i += 1
    from .fsutil import atomic_write_text      # 半截文件比没有文件更糟(P2-1)
    atomic_write_text(p, _doc(src))
    return {"ok": True, "落盘": True, "文件": p.relative_to(root).as_posix()}


def listing(root: Path | str) -> dict:
    root = Path(root)
    d = root / _REVIEW_DIR
    files = sorted(x.name for x in d.glob("大纲符合度-*.md")) if d.is_dir() else []
    return {"ok": True, "条数": len(files), "文件": files}
