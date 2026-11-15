# -*- coding: utf-8 -*-
"""语料统计( B:给语料分析师一个「看数据」工具)。

为什么要有这个模块
----------------
在那之前,「语料分析师」说自己「统计词频、句式、节奏」,可**代码里根本没有统计**:
它只能靠「读文件」把几章正文塞进上下文,再用模型的眼睛估——那既贵又不准,
而且一章 3000 字就读不完整本。所以这个文件把「能算的都算准」这件事放到本地:
**不发模型、不计费、纯确定性**,结果作为工具返回值喂给它,它只负责解读与建议。

口径(写死,免得两处漂)：
- 字数 = `guard.visible_len`(去空白后的实字),与全文各处口径一致;
- 句 = `fingerprint._segment` 的切句(与 learn 学文风用的是同一把刀);
- 短句 ≤10 字、长句 >30 字(网文经验值,只作参照,不打分);
- 段 = 非空行(中文网文一自然段一行);
- 高频片段 = 2~4 字 n-gram 做「极大化」筛(短的那个若被更长的整体包含且次数不多,就不算一条),
  **因此它是「高频片段」不是严格分词**——报告里也这么写,不装成词典。

只做只读统计:不写盘、不改书、不消耗 token。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import paths
from .chaptertext import strip_title
from .guard import visible_len

SHORT_SENT = 10      # 短句上限(字)
LONG_SENT = 30       # 长句下限(字)
MIN_SAMPLE = 300     # 样本太小时先提醒,别拿一章半报一堆百分比
MAX_CHAPTERS = 999   # 兜底:全书都在预算内,这里只是防非法入参
# 高频片段要建 n-gram 字典,是全模块唯一随字数涨成本的一步。百万字全书也不该让
# 一次「看数据」卡住几秒,所以只取最近这一段(近况比开头更能代表现在的腔调)。
MAX_PHRASE_SAMPLE = 200_000

_PUNCT = "，。！？…、；：“”「」《》（）—"
_STOP_CHARS = set("的了着过是在和与就都也又很太不没我你他她它们这那有著地得吗呢吧啊呀嘛个把被给让对从向于而为以所之其此")
_SEP_RE = re.compile(r"[^\u4e00-\u9fff]+")


def parse_scope(scope: str, nums: list[int]) -> tuple[str, list[int]]:
    """把作者/模型给的范围说法翻成章号列表。

    认:空、「全书/全部」、`第3章`、`3`、`第3-8章`、`3-8`、`最近5章`。
    认不出就抛 ValueError(人话说清能怎么写),不要让模型猜着往下走。
    """
    raw = (scope or "").strip()
    have = list(nums)
    if not raw or raw in ("全书", "全部", "整本", "整本书"):
        return "全书", have
    m = re.fullmatch(r"最近\s*(\d+)\s*章?", raw)
    if m:
        k = max(1, int(m.group(1)))
        picked = have[-k:]
        if not picked:
            raise ValueError("这本书还没有已落盘的正文,没有可统计的样本。")
        return f"最近 {k} 章", picked
    m = re.fullmatch(r"第?\s*(\d+)\s*[-~到至]\s*(\d+)\s*章?", raw)
    if m:
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        picked = [n for n in have if lo <= n <= hi]
        if not picked:
            raise ValueError(f"第 {lo}-{hi} 章里没有已落盘的正文(书里现有:第{have[0]}章…第{have[-1]}章)。")
        return f"第 {lo}-{hi} 章", picked
    m = re.fullmatch(r"第?\s*(\d+)\s*章?", raw)
    if m:
        n = int(m.group(1))
        if n not in have:
            raise ValueError(f"第 {n} 章还没有已落盘的正文(书里现有:第{have[0]}章…第{have[-1]}章)。")
        return f"第 {n} 章", [n]
    raise ValueError("范围这么写认不出。可以写:全书 / 第12章 / 第5-9章 / 最近5章。")


def collect(root: Path | str, scope: str = "") -> tuple[str, list[int], str]:
    """按范围取正文 → (范围人话名, 章号列表, 拼接后的全文)。"""
    root = Path(root)
    nums = paths.chapter_numbers(root)[:MAX_CHAPTERS]
    if not nums:
        raise ValueError("这本书还没有正文(正文/ 里没有「第N章.md」),先写一章再来量。")
    label, picked = parse_scope(scope, nums)
    chunks: list[str] = []
    for n in picked:
        p = paths.chapter_path(root, n)
        if not p.is_file():
            continue
        try:
            chunks.append(strip_title(p.read_text(encoding="utf-8", errors="replace")).strip())
        except OSError:
            continue
    text = "\n\n".join(c for c in chunks if c)
    if not text.strip():
        raise ValueError("选中的章没有可读正文(文件是空的)。")
    return label, picked, text


def _sentences(text: str) -> list[str]:
    from .fingerprint import _segment      # 与 learn 学文风同一把刀,别另写一份切句
    return _segment(text)


def _paragraphs(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _top_phrases(text: str, *, limit: int = 10, min_count: int = 3) -> list[tuple[str, int]]:
    """2~4 字 n-gram 极大化 → 高频片段(不是严格分词,只当「你爱用什么组合」的线索)。"""
    counts: dict[str, int] = {}
    for seg in _SEP_RE.split(text):
        if len(seg) < 2:
            continue
        L = len(seg)
        for n in (2, 3, 4):
            for i in range(L - n + 1):
                g = seg[i:i + n]
                if g[0] in _STOP_CHARS or g[-1] in _STOP_CHARS or "的" in g:
                    continue        # 首尾是虚词/中间带「的」的多半是半个短语,不报
                counts[g] = counts.get(g, 0) + 1
    cands = [(g, c) for g, c in counts.items() if c >= min_count]
    # 极大化:长的那条次数不少于短的 → 短的是它的一部分,丢掉短的
    cands.sort(key=lambda t: (-len(t[0]), -t[1]))
    kept: list[tuple[str, int]] = []
    for g, c in cands:
        if any(g in long and (long != g) and lc >= c for long, lc in kept):
            continue
        kept.append((g, c))
    kept.sort(key=lambda t: (-t[1], -len(t[0])))
    return kept[:limit]


_SENT_HEAD_RE = re.compile(r"^[\s“「『]*(.{1,2})")


def _top_openers(sents: list[str], *, limit: int = 8, min_count: int = 4) -> list[tuple[str, int]]:
    """句首两字 → 分布。这是「腔调」最省事的一根尺子(他/我/那/这 开头各占多少)。"""
    counts: dict[str, int] = {}
    for s in sents:
        m = _SENT_HEAD_RE.match(s)
        if not m:
            continue
        head = m.group(1).strip()
        if not head:
            continue
        counts[head] = counts.get(head, 0) + 1
    items = [(h, c) for h, c in counts.items() if c >= min_count]
    items.sort(key=lambda t: (-t[1], t[0]))
    return items[:limit]


def _punct_ratio(text: str) -> list[tuple[str, float]]:
    total = sum(1 for ch in text if ch in _PUNCT)
    if not total:
        return []
    out = []
    for ch in "，。！？…—":
        n = text.count(ch)
        if n:
            out.append((ch, n * 100.0 / total))
    return out


def stats(text: str) -> dict:
    """一段正文 → 确定性统计(纯函数,零副作用;报告与测试共用它)。"""
    sents = _sentences(text)
    paras = _paragraphs(text)
    lens = [visible_len(s) for s in sents]
    para_lens = [visible_len(p) for p in paras]
    n_sent = len(lens)
    phrase_src = text[-MAX_PHRASE_SAMPLE:] if len(text) > MAX_PHRASE_SAMPLE else text
    out = {
        "字数": visible_len(text),
        "句数": n_sent,
        "平均句长": round(sum(lens) / n_sent, 1) if n_sent else 0.0,
        "短句比": round(sum(1 for x in lens if x <= SHORT_SENT) * 100.0 / n_sent, 0) if n_sent else 0,
        "长句比": round(sum(1 for x in lens if x > LONG_SENT) * 100.0 / n_sent, 0) if n_sent else 0,
        "最长句": max(lens) if lens else 0,
        "段数": len(para_lens),
        "平均段长": round(sum(para_lens) / len(para_lens), 1) if para_lens else 0.0,
        "最长段": max(para_lens) if para_lens else 0,
        "标点": _punct_ratio(text),
        "句首": _top_openers(sents),
        "高频片段": _top_phrases(phrase_src),
        "高频片段只取了尾巴": len(phrase_src) < len(text),
    }
    return out


def _fmt(n: int) -> str:
    return f"{n:,}"


def report(root: Path | str, scope: str = "") -> str:
    """给「看数据」工具用的成文报告(中文、紧凑、直接可读;不写盘)。"""
    root = Path(root)
    label, picked, text = collect(root, scope)
    st = stats(text)
    if st["字数"] < MIN_SAMPLE:
        head = f"【语料数据】范围:{label} · 仅 {_fmt(st['字数'])} 字 —— 样本太小,百分比别当真"
    else:
        head = f"【语料数据】范围:{label} · {len(picked)} 章 · {_fmt(st['字数'])} 字"

    lines = [head]
    lines.append(f"句:{_fmt(st['句数'])} 句 / 平均 {st['平均句长']} 字 / 短句(≤{SHORT_SENT}字) "
                 f"{st['短句比']:.0f}% / 长句(>{LONG_SENT}字) {st['长句比']:.0f}% / 最长 {st['最长句']} 字")
    lines.append(f"段:{_fmt(st['段数'])} 段 / 平均 {st['平均段长']} 字 / 最长 {st['最长段']} 字")
    if st["标点"]:
        lines.append("标点:" + " ".join(f"{ch}{pct:.0f}%" for ch, pct in st["标点"]))
    if st["句首"]:
        lines.append("句首:" + " ".join(f"「{h}」{c}" for h, c in st["句首"]))
    if st["高频片段"]:
        scope_note = "最近 20 万字" if st["高频片段只取了尾巴"] else "全部样本"
        lines.append(f"高频片段({scope_note};自动切,未必是词):"
                     + " ".join(f"「{g}」{c}" for g, c in st["高频片段"]))

    # 章长分布:只在全书/多章时有意义
    if len(picked) > 1:
        lens = []
        for n in picked:
            p = paths.chapter_path(root, n)
            try:
                lens.append((n, visible_len(strip_title(p.read_text(encoding="utf-8", errors="replace")))))
            except OSError:
                continue
        if lens:
            avg = sum(v for _, v in lens) / len(lens)
            lo = min(lens, key=lambda t: t[1])
            hi = max(lens, key=lambda t: t[1])
            lines.append(f"章长:平均 {_fmt(int(avg))} 字 / 最短 第{lo[0]}章 {_fmt(lo[1])} / "
                         f"最长 第{hi[0]}章 {_fmt(hi[1])}")
            off = [f"第{n}章({(v / avg - 1) * 100:+.0f}%)" for n, v in lens
                   if avg and abs(v / avg - 1) >= 0.5]
            if off:
                lines.append("偏离均值 ≥50% 的章:" + "、".join(off[:6])
                             + ("…" if len(off) > 6 else ""))

    # 口径命中:作者自己的「绝不这么写」在正文里出现了多少
    try:
        from . import aitell
        pol = aitell.load_policy(root)
    except Exception:            # noqa: BLE001 - 口径读不出来不影响统计
        pol = None
    if pol is not None and pol.banned:
        rows = []
        for exp in pol.banned:
            c = text.count(exp)
            if c:
                rows.append((exp, c))
        rows.sort(key=lambda t: -t[1])
        if rows:
            lines.append("你的「绝不这么写」被用上了:" + " ".join(f"「{e}」×{c}" for e, c in rows[:5])
                         + f"(共 {sum(c for _, c in rows)} 处)")
        else:
            lines.append(f"你的「绝不这么写」({len(pol.banned)} 条):本次样本 0 处,守住了。")
    if pol is not None:
        try:
            hits = aitell.detect(text, list(pol.anchors), cross_sentence=pol.cross_sentence,
                                 banned=list(pol.banned))
        except Exception:        # noqa: BLE001
            hits = []
        if hits:
            samples = "|".join(str(getattr(h, "evidence", ""))[:24] for h in hits[:3])
            lines.append(f"AI腔判定:{len(hits)} 处(最多报 {aitell._MAX_HITS})｜例:{samples}")
        else:
            lines.append("AI腔判定:0 处")
    lines.append("(纯本地统计,没花 token。要更细的某章,改「范围」再量一次。)")
    return "\n".join(lines)
