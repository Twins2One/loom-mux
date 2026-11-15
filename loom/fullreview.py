# -*- coding: utf-8 -*-
r"""编辑总监「一次审全书」——照真编辑的节奏：先体检，再通读，最后写审读报告。

作者 2026-10-20 追加的要求：「不挂链，但我单独要求它审的时候要能够一次审全书。」
（形态与理由见 docs/角色联动设计-待审查.md §十三；这里只落地。）

为什么不是「把全书塞进一条 prompt」：一本 100 章 ≈ 20 万字，远超窗口，而且
`agent_chat` 对谈通道没有分块、也没有上下文守卫。真编辑也不这么干——他们是
**先做分项校对（可机械化）→ 再把全稿通读一遍、边读边做笔记 → 最后写一份《审读报告》**。
本模块就是这三段：

① `triage()`  **体检（0 次模型调用）**：跑既有的本地检测器 —— 章纲 vs 正文、悬空伏笔、
   事实账本损坏、跨层冲突、违禁词、AI 腔句式、跨章雷同 —— 出一张「问题地图」。
② `run_block()`  **通读一块（50 章 1 次调用）**：只产出**块记**（结构/人物/伏笔/疑问），
   不下判决。块记落 `.审稿留痕/全书审-块记-{起}-{止}.md`，它同时是**续跑账本**。
③ `finish()`   **收口（1 次调用）**：块记 + 问题地图 + 卷纲/全书大纲 → 《全书审读报告》。

三条硬边界：
- **开工前报账**（`plan()` 给出调用数与预计分钟，人点头才跑；一次一块，可中断续跑）；
- **一个字都不改稿子**：只写 `.审稿留痕/` 下的两个文件（块记 + 报告）；
- **诚实标注独立性**：初审/复审是同一个本地模型换了两个提示词，达不到真「三审」的
  独立性要求（制度要求不同人），所以报告页首明写「这是线索，不是判决」。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import paths, volplan
from .align import BLOCK   # 分块口径的唯一真相（50 章），不在这里再写一个字面量

REVIEW_DIR = paths.REVIEW_DIR                     # ".审稿留痕"
_H1 = re.compile(r"^\s*#\s+.*$", re.M)
_WS = re.compile(r"\s+")

#: 章末钩启发式里的「转折/悬念」词（只用来**标可疑**，不判好坏）
_HOOK_WORDS = ("却", "竟", "居然", "忽然", "突然", "然而", "但是", "只是", "可", "难道",
               "偏偏", "谁知", "不料", "猛地", "骤然", "原来", "直到")

#: 每类最多留多少条（报告要能读，不是把全书问题倒出来）
PER_KIND_CAP = 40
TOTAL_CAP = 200


# ── 小工具 ────────────────────────────────────────────────────────────────
def _strip_h1(text: str) -> str:
    return _H1.sub("", text or "", count=1).strip()


def _body(root: Path, n: int) -> str:
    p = paths.chapter_path(root, n)
    try:
        return _strip_h1(p.read_text(encoding="utf-8", errors="replace")) if p.is_file() else ""
    except OSError:
        return ""


def _chars(text: str) -> int:
    return len(_WS.sub("", _strip_h1(text)))


def block_note_rel(lo: int, hi: int) -> str:
    return f"{REVIEW_DIR}/全书审-块记-{lo}-{hi}.md"


def report_rel(stamp: str) -> str:
    return f"{REVIEW_DIR}/全书审-{stamp}.md"


def blocks_of(root: Path | str) -> list[tuple[int, int]]:
    """按**已写到的章**切块（不按卷纲；卷纲可能还没填）。"""
    nums = paths.chapter_numbers(root)
    if not nums:
        return []
    out: list[tuple[int, int]] = []
    n = 1
    last = nums[-1]
    while n <= last:
        out.append((n, n + BLOCK - 1))
        n += BLOCK
    return out


# ── ① 体检（0 调用）──────────────────────────────────────────────────────
def _triage_chapters(root: Path, target: int) -> tuple[list[dict], dict]:
    """章纲 vs 正文 + 篇幅 + 章末钩（启发式）。返回 (条目, 统计)。"""
    nums = paths.chapter_numbers(root)
    rows = volplan.card_state(root)          # {章号: 章行内容}（0 调用、纯读）
    items: list[dict] = []
    stat = {"章数": len(nums), "有章纲行": 0, "缺章纲行": 0, "篇幅偏离": 0, "章末钩可疑": 0}
    if not nums:
        return items, stat
    last = nums[-1]
    for n in nums:
        body = _body(root, n)
        card = (rows.get(n) or "").strip()
        if card:
            stat["有章纲行"] += 1
        else:
            stat["缺章纲行"] += 1
            items.append({"章": n, "类": "未登纲", "说明": "正文有、卡章纲没有这一行",
                          "证据": body[:60].replace("\n", " ")})
        cnt = _chars(body)
        if target > 0 and cnt:
            if cnt < target * 0.6 or cnt > target * 1.6:
                stat["篇幅偏离"] += 1
                items.append({"章": n, "类": "篇幅偏离",
                              "说明": f"{cnt} 字 / 目标 {target} 字",
                              "证据": body[-60:].replace("\n", " ")})
        tail = body[-120:]
        if tail and not re.search(r"[？！…]", tail) and not any(w in tail for w in _HOOK_WORDS):
            stat["章末钩可疑"] += 1
            items.append({"章": n, "类": "章末钩(启发式)", "说明": "末段没有问号/感叹/省略号，也没有转折或悬念词",
                          "证据": tail.replace("\n", " ")})
    # 中间缺章（章纲有、正文没有）：只报到「最大已写章」为止，后面没写的章不算问题
    missing = [n for n in sorted(rows) if n <= last and not paths.chapter_path(root, n).is_file()]
    for n in missing[:PER_KIND_CAP]:
        items.append({"章": n, "类": "漏章", "说明": "卡章纲有这一行，但正文没有这一章",
                      "证据": (rows.get(n) or "")[:60]})
    return items, stat


def _triage_detectors(root: Path, only: tuple[int, int] | None = None) -> tuple[list[dict], dict]:
    """既有本地检测器。**每一个都单独兜住**：某个检测器坏了，只标「未接入」，不拖垮整张地图。

    `only=(lo, hi)` 时逐章检测器只扫这一段（通读一块时用，别为了 50 章去扫全书 1000 章）。
    """
    items: list[dict] = []
    status: dict[str, str] = {}

    # 伏笔悬空 + 事实账本损坏（facts）
    try:
        from . import facts
        rows = facts.load(root)
        nums = paths.chapter_numbers(root)
        cur = nums[-1] if nums else 0
        hooks = facts.view_hooks(rows, current=cur)
        loose = [h for h in hooks if h.get("悬空")]
        for h in loose[:PER_KIND_CAP]:
            items.append({"章": h.get("最后推进章") or h.get("埋设章") or 0,
                          "类": "伏笔悬空",
                          "说明": f"伏笔「{h.get('伏笔')}」埋在第 {h.get('埋设章')} 章，"
                                  + (f"最后推进第 {h.get('最后推进章')} 章" if h.get("最后推进章") else "此后没再推进")
                                  + (f"，建议第 {h.get('该收章')} 章前收" if h.get("该收章") else ""),
                          "证据": ""})
        status["伏笔悬空"] = f"ok: {len(loose)} 条"
    except Exception as e:      # noqa: BLE001
        status["伏笔悬空"] = f"未接入: {type(e).__name__}: {e}"

    try:
        from . import facts
        dmg = facts.damage(root)
        n_bad = int(dmg.get("坏行") or dmg.get("行数") or 0) if isinstance(dmg, dict) else 0
        bad = (dmg or {}).get("条目") if isinstance(dmg, dict) else None
        if bad:
            for b in bad[:PER_KIND_CAP]:
                items.append({"章": int((b or {}).get("章") or 0), "类": "事实账本损坏",
                              "说明": str((b or {}).get("说明") or (b or {}).get("原因") or b),
                              "证据": str((b or {}).get("证据") or "")})
        status["事实账本"] = "ok" + (f": {n_bad} 条" if n_bad else "")
    except Exception as e:      # noqa: BLE001
        status["事实账本"] = f"未接入: {type(e).__name__}: {e}"

    # 跨层冲突（conflicts：确定性检测，不调模型）
    try:
        from . import conflicts
        for fn, label in ((conflicts.cross_layer, "跨层同键不同值"),
                          (conflicts.volume_overlap, "卷起止重叠"),
                          (conflicts.alias_clash, "别称冲突")):
            try:
                rows = fn(root) or []
            except Exception as e:      # noqa: BLE001
                status[label] = f"未接入: {type(e).__name__}: {e}"
                continue
            status[label] = f"ok: {len(rows)} 条"
            for r in rows[:PER_KIND_CAP]:
                items.append({"章": 0, "类": label,
                              "说明": str((r or {}).get("说明") or (r or {}).get("键") or r)[:120],
                              "证据": str((r or {}).get("值") or "")[:80]})
    except Exception as e:      # noqa: BLE001
        status["跨层冲突"] = f"未接入: {type(e).__name__}: {e}"

    # 逐章：违禁词 / AI 腔 / 跨章雷同（都是本地、0 调用）
    nums = paths.chapter_numbers(root)
    if only:
        nums = [n for n in nums if only[0] <= n <= only[1]]
    counters = {"违禁词": 0, "AI 腔句式": 0, "跨章雷同": 0}
    errs: dict[str, str] = {}
    try:
        from . import aitell, fatigue, sensitive
    except Exception as e:      # noqa: BLE001
        status["逐章检测器"] = f"未接入: {type(e).__name__}: {e}"
        return items, status
    policy = None
    try:
        policy = aitell.load_policy(root)      # 签名句 + 「绝不这么写」+ 跨句号口径
    except Exception:           # noqa: BLE001
        policy = None
    anchors = list(policy.anchors) if policy else []
    for n in nums:
        body = _body(root, n)
        if not body:
            continue
        try:
            for hit in sensitive.scan(body, root)[:5]:
                counters["违禁词"] += 1
                items.append({"章": n, "类": "违禁词",
                              "说明": str((hit or {}).get("词") or (hit or {}).get("说明") or hit)[:80],
                              "证据": str((hit or {}).get("上下文") or (hit or {}).get("证据") or "")[:80]})
        except Exception as e:      # noqa: BLE001
            errs["违禁词"] = f"{type(e).__name__}: {e}"
        try:
            for issue in aitell.detect(body, anchors,
                                       cross_sentence=bool(policy and policy.cross_sentence),
                                       banned=list(policy.banned) if policy else [])[:5]:
                counters["AI 腔句式"] += 1
                items.append({"章": n, "类": "AI 腔句式",
                              "说明": str(getattr(issue, "desc", "") or getattr(issue, "kind", "")),
                              "证据": str(getattr(issue, "evidence", ""))[:80]})
        except Exception as e:      # noqa: BLE001
            errs["AI 腔句式"] = f"{type(e).__name__}: {e}"
        try:
            for issue in fatigue.scan(root, n, body, anchors)[:5]:
                counters["跨章雷同"] += 1
                items.append({"章": n, "类": "跨章雷同",
                              "说明": str(getattr(issue, "desc", "") or getattr(issue, "kind", "")),
                              "证据": str(getattr(issue, "evidence", ""))[:80]})
        except Exception as e:      # noqa: BLE001
            errs["跨章雷同"] = f"{type(e).__name__}: {e}"
    for k, v in counters.items():
        status[k] = (f"未接入: {errs[k]}" if k in errs and v == 0 else f"ok: {v} 条")
    return items, status


def triage(root: Path | str, only: tuple[int, int] | None = None) -> dict:
    """问题地图：**0 次模型调用**。任何一处失败都只降级那一项，不抛。

    `only=(lo, hi)`：只体检这一段（通读某一块时用；`plan()`/`finish()` 不传 = 全书）。
    """
    root = Path(root)
    target = 0
    try:
        from .config import load_config
        target = int(getattr(load_config(root), "chapter_chars", 0) or 0)
    except Exception:           # noqa: BLE001 - 取不到字数目标就跳过篇幅项
        target = 0
    items, stat = _triage_chapters(root, target)
    if only:
        items = [it for it in items if only[0] <= (it.get("章") or 0) <= only[1]]
        stat["章数"] = len([n for n in paths.chapter_numbers(root) if only[0] <= n <= only[1]])
    d_items, det_stat = _triage_detectors(root, only)
    items += d_items

    # 按类各留 PER_KIND_CAP 条，总量再压一次
    by_kind: dict[str, list[dict]] = {}
    for it in items:
        by_kind.setdefault(it["类"], []).append(it)
    kept: list[dict] = []
    counts: dict[str, int] = {}
    for k, lst in by_kind.items():
        counts[k] = len(lst)
        kept += lst[:PER_KIND_CAP]
    kept.sort(key=lambda x: (x["章"] or 0, x["类"]))
    return {"章数": stat["章数"], "统计": stat, "检测器": det_stat,
            "分类计数": counts, "条目": kept[:TOTAL_CAP], "条目总数": len(items),
            "字数目标": target}


# ── ② 报账（0 调用）──────────────────────────────────────────────────────
def plan(root: Path | str) -> dict:
    root = Path(root)
    blks = blocks_of(root)
    done = [(lo, hi) for lo, hi in blks if (root / block_note_rel(lo, hi)).is_file()]
    todo = [(lo, hi) for lo, hi in blks if (lo, hi) not in done]
    calls = len(todo) + (1 if blks else 0)
    tri = triage(root)
    return {
        "ok": True, "章数": len(paths.chapter_numbers(root)), "块数": len(blks),
        "块": [{"起": lo, "止": hi, "已跑": (lo, hi) in done} for lo, hi in blks],
        "已跑块": len(done), "待跑块": len(todo),
        "预计调用数": calls, "预计分钟": [calls * 1, calls * 3],
        "体检": tri, "会落盘": [REVIEW_DIR + "/全书审-块记-*.md",
                            REVIEW_DIR + "/全书审-<时间>.md"],
    }


# ── ② 通读一块（1 次调用）─────────────────────────────────────────────────
_BLOCK_SYS = (
    "你是这本书的「编辑」，正在通读其中的一块。**只做读后笔记，不下判决、不改稿**。\n"
    "输出 markdown，四节：## 这一块讲了什么 / ## 人物与关系 / ## 伏笔与线索 / ## 我读着不对劲的地方。\n"
    "每条都写清「第几章」。读着没问题就写「无」，不要凑数、不要复述剧情。"
)


def _block_prompt(root: Path, lo: int, hi: int, tri: dict) -> str:
    def _read(rel: str, cap: int) -> str:
        p = root / rel
        try:
            return p.read_text(encoding="utf-8").strip()[:cap] if p.is_file() else ""
        except OSError:
            return ""

    rows = volplan.card_state(root)
    cards = "\n".join(f"- 第{n}章:{(rows.get(n) or '').strip()}"
                      for n in range(lo, hi + 1) if (rows.get(n) or "").strip())
    bodies = []
    for n in range(lo, hi + 1):
        body = _body(root, n)
        if body:
            bodies.append(f"【第{n}章】(节选 {min(len(body), 700)} 字)\n{body[:700]}")
    mine = [it for it in tri.get("条目") or [] if lo <= (it.get("章") or 0) <= hi][:20]
    hot = "\n".join(f"- 第{it['章']}章 {it['类']}:{it['说明']}" for it in mine)
    return (
        f"【全书大纲】\n{_read(paths.OVERVIEW_REL, 1200) or '(还没写)'}\n\n"
        f"【卷纲】\n{_read(paths.VOLUMES_REL, 1200) or '(还没写)'}\n\n"
        f"【这一块的章纲（第 {lo}-{hi} 章）】\n{cards or '(这一块还没有章纲行)'}\n\n"
        f"【体检已经挑出来的硬问题（供你参考，不必逐条复核）】\n{hot or '(无)'}\n\n"
        f"【正文节选】\n" + "\n\n".join(bodies)
    )


def run_block(root: Path | str, lo: int, hi: int, backend, *, force: bool = False) -> dict:
    """跑一块：1 次调用，落块记。已有块记 → 幂等返回（不重复花钱）；`force=True` 重跑。"""
    root = Path(root)
    rel = block_note_rel(lo, hi)
    if (root / rel).is_file() and not force:
        return {"ok": True, "跳过": "这一块已经跑过（块记在盘上）", "块记": rel, "调用": 0}
    tri = triage(root, only=(lo, hi))
    prompt = _block_prompt(root, lo, hi, tri)
    raw = backend.complete(_BLOCK_SYS, prompt, max_chars=1600)
    text = (raw or "").strip()
    if not text:
        return {"ok": False, "reason": "模型这一次返回空，块记没写（下次再跑这一块）"}
    head = (f"# 全书审 · 块记 第 {lo}-{hi} 章\n\n"
            f"> 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}。这是编辑「通读时做的笔记」，"
            f"不是判决；收口时会并进《全书审读报告》。\n\n")
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    # 原子写:块记是**续跑账本**,写一半崩掉会让下一轮把它当「已跑」而跳过
    from .fsutil import atomic_write_text
    atomic_write_text(p, head + text + "\n")
    return {"ok": True, "块记": rel, "字数": len(text), "调用": 1}


# ── ③ 收口（1 次调用）─────────────────────────────────────────────────────
_FINISH_SYS = (
    "你是这本书的「编辑总监」，要写一份《全书审读报告》。**只出问题与建议，一个字都不改**。\n"
    "结构固定：## 总体评价 / ## 结构与节奏 / ## 人物与逻辑 / ## 跨章一致性 / ## 伏笔回收 / "
    "## 语言与文字 / ## 修改建议（分「必须改 / 建议改 / 可不改」三档，每条带章号）。\n"
    "凡是没有把握的，写明「不确定」；不要把猜测写成结论。"
)


def finish(root: Path | str, backend) -> dict:
    """把块记 + 体检地图 + 大纲压成一份报告。1 次调用。"""
    root = Path(root)
    blks = blocks_of(root)
    notes = []
    for lo, hi in blks:
        p = root / block_note_rel(lo, hi)
        if p.is_file():
            try:
                notes.append(f"--- 第 {lo}-{hi} 章 ---\n" + p.read_text(encoding="utf-8")[:1800])
            except OSError:
                continue
    if not notes:
        return {"ok": False, "reason": "还没有块记可收口；先跑第 ①②段（通读）。"}
    tri = triage(root)
    counts = tri.get("分类计数") or {}
    hot = "\n".join(f"- 第{it['章']}章 {it['类']}：{it['说明']}"
                   for it in (tri.get("条目") or [])[:60])
    def _read(rel: str, cap: int) -> str:
        p = root / rel
        try:
            return p.read_text(encoding="utf-8").strip()[:cap] if p.is_file() else ""
        except OSError:
            return ""
    prompt = (
        f"【全书大纲】\n{_read(paths.OVERVIEW_REL, 1500) or '(还没写)'}\n\n"
        f"【卷纲】\n{_read(paths.VOLUMES_REL, 1500) or '(还没写)'}\n\n"
        f"【各块的通读笔记】\n" + "\n\n".join(notes) + "\n\n"
        f"【体检问题地图（按类计数）】\n"
        + "\n".join(f"- {k}：{v} 条" for k, v in sorted(counts.items())) + "\n\n"
        f"【其中一部分具体条目】\n{hot or '(无)'}"
    )
    raw = backend.complete(_FINISH_SYS, prompt, max_chars=5000)
    text = (raw or "").strip()
    if not text:
        return {"ok": False, "reason": "模型这一次返回空，报告没写。"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rel = report_rel(stamp)
    name = "未知"
    try:
        from .config import load_config
        name = getattr(load_config(root), "title", "") or "未命名"
    except Exception:           # noqa: BLE001
        pass
    head = (
        f"# 全书审读报告 · {name}\n\n"
        f"> 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}；共 {len(paths.chapter_numbers(root))} 章，"
        f"{len(notes)} 块通读笔记。\n"
        f"> **这是线索，不是判决**：初审与复审是同一个本地模型换了两个提示词，"
        f"达不到出版业「三审不得同一人」的独立性；每条都由你定夺。\n"
        f"> 本文件只写这一份报告，**没有改过任何稿子**；整份可删。\n\n"
        f"## 体检摘要（0 次调用、本地检测器）\n\n"
        + "\n".join(f"- {k}：{v} 条" for k, v in sorted(counts.items()))
        + "\n\n---\n\n"
    )
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    from .fsutil import atomic_write_text
    atomic_write_text(p, head + text + "\n")
    return {"ok": True, "报告": rel, "字数": len(text), "块记数": len(notes), "调用": 1}
