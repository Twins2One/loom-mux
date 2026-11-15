# -*- coding: utf-8 -*-
r"""长上下文检索:跨很多章找线索( 3)。

两个消费者,都只出提案、都不写盘:
  1) 知识顾问的「跨章取证」:先按问题从章目录里挑出相关的几章,再把那几章正文喂进主问;
  2) 领航员的「大纲符合度复核」(见 align.py):一次把几十章的「章纲要什么 / 正文写了什么」摆给长窗口模型。

这一槽的活儿是「吃窗口的粗筛与对照」,不是判决。判决永远是人按那三条(设定 > 章纲 > 正文,
人一旦另裁,其余逐处请示)来做。
"""
from __future__ import annotations

import re
from pathlib import Path

from .backends import Backend
from . import paths

# 章目录最多摆多少章(1200 章的书 ≈ 4 万字符,这一槽就是为吃这个窗口而设的)
CATALOG_MAX = 1200
# 挑章上限:多了等于把整本书塞回去,失去「筛」的意义
PICK_LIMIT = 10
# 取正文时每章截多少字(保开头,正文的关键信息几乎都在开头与结尾)
EVID_CHARS = 1200

_PICK_RULES = """你在帮一位作者做**跨章取证**。作者会给你一个问题,和一张「章目录」
(每行:章号 + 这一章的章纲)。你的活儿是从目录里**挑出**最可能回答这个问题的几章。

只输出你要挑的章,一行一章,格式:**第N章:一句话理由**。
不许写别的话,不许总结,不许编不存在的能力。挑不出来就只写一行:**没有**。
最多挑 10 章;宁可少挑,也不要凑数。
"""


def _card_rows(root: Path) -> dict[int, str]:
    """卡章纲 → {章号: 这一章的内容}。"""
    p = root / paths.CARD_REL
    if not p.is_file():
        return {}
    out: dict[int, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*[-*]\s*第\s*(\d+)\s*章\s*[:：]\s*(.*)$", line)
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    return out


def chapters_with_body(root: Path | str) -> list[int]:
    """盘上真有正文的章号(升序)。没正文的章不进目录 —— 挑出来也取不到证。"""
    root = Path(root)
    nums = []
    for n in paths.chapter_numbers(root):
        p = paths.chapter_path(root, n)
        try:
            if p.is_file() and p.read_text(encoding="utf-8").strip():
                nums.append(n)
        except OSError:
            continue
    return sorted(nums)


def catalog(root: Path | str, *, lo: int | None = None, hi: int | None = None,
            limit: int = CATALOG_MAX) -> dict:
    """章目录。没章纲的章只写章号(总比没有强,至少让模型知道这章存在)。"""
    root = Path(root)
    rows = _card_rows(root)
    nums = chapters_with_body(root)
    if lo is not None:
        nums = [n for n in nums if n >= lo]
    if hi is not None:
        nums = [n for n in nums if n <= hi]
    showed = nums[:limit]
    lines = [f"第{n}章:{rows.get(n) or '(这一章没写章纲)'}" for n in showed]
    return {"条数": len(showed), "总章数": len(nums), "截断": len(nums) > len(showed),
            "文本": "\n".join(lines)}


def parse_picked(raw: str, *, allowed: set[int], limit: int = PICK_LIMIT) -> tuple[list[int], list[str]]:
    """从模型回话里取章号。**只留盘上真有正文的章**,并按上限截断。

    返回 (章号列表, 被丢掉的章号说明)。丢掉的不静默:调用方要把说明带回去。
    """
    seen: list[int] = []
    for m in re.finditer(r"第\s*(\d+)\s*章", raw or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    ok = [n for n in seen if n in allowed][:limit]
    dropped = [f"第{n}章(盘上没有正文,挑不了)" for n in seen if n not in allowed]
    return ok, dropped


def pick(root: Path | str, question: str, backend: Backend, *, lo: int | None = None,
         hi: int | None = None, limit: int = PICK_LIMIT, max_chars: int = 0) -> dict:
    """让长窗口那一槽挑章。**不落盘**。"""
    root = Path(root)
    cat = catalog(root, lo=lo, hi=hi)
    if not cat["条数"]:
        return {"ok": False, "reason": "这本书还没有正文,没东西可取证。", "落盘": False}
    allowed = set(chapters_with_body(root))
    prompt = (f"【问题】\n{question.strip()}\n\n【章目录】\n{cat['文本']}\n\n"
              + ("(目录被截断了:只摆了前 %d 章,全书共 %d 章有正文)\n" % (cat["条数"], cat["总章数"])
                 if cat["截断"] else ""))
    raw = backend.complete(_PICK_RULES, prompt, max_chars=max_chars or None)
    picked, dropped = parse_picked(raw, allowed=allowed, limit=limit)
    return {"ok": True, "章": picked, "挑不出": not picked, "说明": (raw or "").strip(),
            "跳过的章": dropped, "目录条数": cat["条数"], "目录被截断": cat["截断"], "落盘": False}


def gather(root: Path | str, chapters: list[int], *, chars: int = EVID_CHARS) -> str:
    """把那几章的正文取出来(每章截前 chars 字)。取不到的章明确写出来。"""
    root = Path(root)
    blocks = []
    for n in chapters:
        p = paths.chapter_path(root, n)
        try:
            text = p.read_text(encoding="utf-8").strip() if p.is_file() else ""
        except OSError:
            text = ""
        if not text:
            blocks.append(f"【第{n}章】(盘上没有正文)")
            continue
        cut = text[:chars]
        tail = "" if len(text) <= chars else f"\n…(后略,原文 {len(text)} 字)"
        blocks.append(f"【第{n}章】\n{cut}{tail}")
    return "\n\n".join(blocks)
