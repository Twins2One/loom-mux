"""AI 腔「对比句式」本地检测器:抓「不是A而是B / 不是A,是B / 不是A是B」这类高频 AI 翻转句。

只在本地跑、不发 LLM、不消耗 token、**不打分**——命中只产出一条「带原文证据的硬伤」(gates.Issue),
喂进既有的「去AI味」关卡当**确定性地板**,跟 LLM 复审合流(见 agents._GATES['润色师'])。判据是
「命中清单空不空」,不是分数;残留照常进审稿留痕、绝不硬阻断(守 ADR-0006)。

**指纹护栏(像你≠AI腔,守 ADR-0002)**:命中前先比对【写作指纹】的 anchor 例句——是作者逐字签名
的句子就豁免;并且默认**不报跨句号的「。是」翻转**(cross_sentence=False)——因为句号切断的并列
否定常是作者的声音(「那不是笑。」「不是忍。是不在意。」),逗号并排才是 AI 写法。只报高精度的
AI 形态:紧贴「而是」、软分隔后的「,是/,而是」、以及同句无分隔的「不是A是B」。

检测算法移植自 oh-story-claudecode 的 `skills/story-deslop/scripts/check-ai-patterns.js`
(MIT License, Copyright (c) 2025-2026 oh-story-claudecode),用 Python 重写;按 Loom「指纹优先」
把跨句号翻转收敛为可选(默认关)。两边都是 MIT,保留其版权与许可声明于此。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .gates import Issue
from .paths import FINGERPRINT_REL as REL_FINGERPRINT  # 指纹路径常量收敛到 paths(与 fingerprint 同源)
from .paths import SLOP_POLICY_REL as REL_SLOP_POLICY

_STOP = set("。！？!?\n")
_SOFT_SEP = set("，,、；;：:")
_HARD_SEP = set("。.！!？?")
_MAX_NEG_SPAN = 80   # 否定铺垫最多向后扫多少字找肯定翻转
_MAX_POS_SPAN = 80   # 肯定项最多截取多少字作证据,免得引一长段
_EITHER_OR_PREV = set("不就也")  # 「不是A就是B / 也是B」里紧贴的「是」是连词,不是肯定项系动词
_TAG_PARTICLES = set("吗吧嘛")   # 「…,是吗/吧/嘛」是反问尾巴,不是否定后的肯定翻转

_KIND = "AI腔·对比句式"
_MSG = "高频 AI 对比句式;删掉否定铺垫,直接写后项,或改成动作/细节呈现。"
_KIND_BANNED = "AI腔·禁用表达"
_MAX_HITS = 6  # 与 CRITIC_去AI味 的「最多 6 条」对齐,免得刷屏
_BANNED_SPAN = 30   # 禁用表达命中时,证据取它前后各多少字(再按句末标点收敛)

_FENCE = re.compile(r"^(`{3,}|~{3,})")
_TRAIL = re.compile(r"[\s|）)】\]]+$")


# ── 文风指纹 anchor 例句:逐字签名句,命中要豁免 ──────────────────────────────

def load_anchors(root: Path | str) -> list[str]:
    """从 外置大脑/写作指纹.md 的「anchor 例句」段取作者逐字保留的原句(去掉 > 引用前缀)。"""
    p = Path(root) / REL_FINGERPRINT
    if not p.is_file():
        return []
    anchors: list[str] = []
    in_anchor = False
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#"):
            in_anchor = "anchor" in s.lower()
            continue
        if in_anchor and s.startswith(">"):
            a = s.lstrip(">").strip()
            if a:
                anchors.append(a)
    return anchors


# ── 去AI味口径(作者可调的三件事,数据在 外置大脑/分析/去AI味口径.md)──────────────
# :「让作者能靠着和他对话,优化'去AI味'逻辑」。
# 边界写得死死的:
#   · 代码侧的检测规则(哪些句式算 AI 腔)**不许模型改** —— 那是安全攸关的口径,
#     同 artifacts.py 那条红线:用户删一行 md 不该让关卡消失。
#   · 可调的只有三件**本来就属于作者**的事:①我的签名句(命中豁免)②我绝不这么写的表达(加报)
#     ③跨句号的「。是」算不算 AI 腔(默认不算,句号切断的并列否定常是人的声音)。
#   · 口径文件缺失 = 全用默认值,老书零迁移。
@dataclass(frozen=True)
class Policy:
    """一次检测要用的全部口径。缺省值 = 今天的出厂行为(不带口径文件也一模一样)。"""

    anchors: tuple[str, ...] = ()        # 命中即豁免的作者签名句
    banned: tuple[str, ...] = ()         # 作者自列的「绝不这么写」,命中即报
    cross_sentence: bool = False         # 跨句号的「。是」翻转算不算 AI 腔

    def merged_anchors(self) -> list[str]:
        return list(self.anchors)


_POLICY_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$")
_POLICY_ITEM = re.compile(r"^\s*(?:[-*>•]|\d+[.)])\s*(.+?)\s*$")
_POLICY_QUOTES = "「」『』“”\"'‘’`"


def _policy_item(line: str) -> str:
    """口径文件里的一行 → 一条口径(去掉列表符/引号;占位行返回空串)。"""
    m = _POLICY_ITEM.match(line or "")
    if not m:
        return ""
    s = m.group(1).strip().strip(_POLICY_QUOTES).strip()
    if not s or s.startswith(("(", "（")):     # 「(待填)」这类占位不是口径
        return ""
    return s


def load_policy(root: Path | str) -> Policy:
    """读「写作指纹 anchor」+「去AI味口径」→ 一次检测的完整口径。任何异常都退化为默认。"""
    anchors: list[str] = []
    try:
        anchors = load_anchors(root)
    except Exception:          # noqa: BLE001 - 口径读不出来就按默认跑,绝不拖累出稿
        anchors = []
    banned: list[str] = []
    cross = False
    p = Path(root) / REL_SLOP_POLICY
    try:
        if p.is_file():
            section = ""
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                head = _POLICY_HEAD.match(line)
                if head:
                    section = head.group(1)
                    continue
                if "豁免" in section:
                    item = _policy_item(line)
                    if item:
                        anchors.append(item)
                elif "禁用" in section or "绝不" in section or "不这么写" in section:
                    item = _policy_item(line)
                    if item:
                        banned.append(item)
                elif "跨句号" in section:
                    t = (line or "").strip()
                    if t and not t.startswith(("(", "（")):
                        cross = ("不算" not in t) and ("算" in t)
    except Exception:          # noqa: BLE001
        pass
    # 去重保序:同一句既在指纹 anchor 又在口径豁免里,只算一次
    uniq: list[str] = []
    for a in anchors:
        if a and a not in uniq:
            uniq.append(a)
    return Policy(anchors=tuple(uniq), banned=tuple(banned), cross_sentence=cross)


# ── 对外:扫一段文本 → 硬伤清单 ──────────────────────────────────────────────

def detect(text: str, anchors: list[str] | None = None, *, cross_sentence: bool = False,
           banned: tuple[str, ...] | list[str] = ()) -> list[Issue]:
    """扫文本里的 AI 腔,返回 gates.Issue 列表(去重、封顶 _MAX_HITS)。

    anchors 命中即豁免(作者签名句);cross_sentence=True 才报跨句号的「。是」翻转;
    banned 是作者自列的「绝不这么写」——命中即报(带原文证据)。

    三类口径都来自 `load_policy`;不传 = 出厂默认,call site 可以只传自己要的那几项。
    """
    text = text or ""
    anchors = anchors or []
    issues: list[Issue] = []
    seen: set[str] = set()
    for block in _blocks(text):
        for _line, _col, raw in _scan_block(block, cross_sentence):
            # 豁免只认「检测句落在 anchor 里」(raw in a)一个方向:反向 a in raw 太宽,
            # 一条短 anchor 会放跑所有包含它的 AI 腔整句。
            if any(raw in a for a in anchors):
                continue  # 作者逐字签名句,豁免(像你≠AI腔)
            ev = _compact(raw)
            if ev in seen:
                continue
            seen.add(ev)
            issues.append(Issue(kind=_KIND, desc=_MSG, evidence=ev))
            if len(issues) >= _MAX_HITS:
                return issues
    if banned and len(issues) < _MAX_HITS:
        issues += _banned_hits(text, banned, anchors, seen, _MAX_HITS - len(issues))
    return issues


def _banned_hits(text: str, banned, anchors, seen: set[str], room: int) -> list[Issue]:
    """作者自列的「绝不这么写」→ 命中清单。同一个表达最多报 2 处,总共不超 room 条。"""
    hits: list[Issue] = []
    for exp in banned:
        if len(hits) >= room:
            break
        n = 0
        start = 0
        while n < 2 and len(hits) < room:
            i = text.find(exp, start)
            if i < 0:
                break
            start = i + len(exp)
            ev = _compact(_around(text, i, len(exp)))
            if any(ev in a for a in anchors):
                continue          # 签名句里出现同词,豁免优先
            key = f"{exp}|{ev}"
            if key in seen:
                continue
            seen.add(key)
            n += 1
            hits.append(Issue(kind=_KIND_BANNED,
                              desc=f"你自己列为「绝不这么写」的表达:「{exp}」。换成动作或具体细节。",
                              evidence=ev))
    return hits


def _around(text: str, i: int, size: int) -> str:
    """取命中处前后一段,收敛到句末标点为止(给作者看的原文证据,别引太长)。"""
    lo = max(0, i - _BANNED_SPAN)
    hi = min(len(text), i + size + _BANNED_SPAN)
    seg = text[lo:hi]
    cut = seg.rfind("。", 0, i - lo)
    if cut >= 0:
        seg = seg[cut + 1:]
    end = seg.find("。")
    if end >= 0:
        seg = seg[: end + 1]
    return seg.strip().replace("\n", " ")


# ── 内部:分块(跳 YAML frontmatter / 代码围栏)→ 逐块定位扫描 ─────────────────

def _blocks(text: str) -> list[list[tuple[str, int]]]:
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    out: list[list[tuple[str, int]]] = []
    block: list[tuple[str, int]] = []
    fence: tuple[str, int] | None = None
    in_fm = _has_frontmatter(lines)
    for i, line in enumerate(lines):
        t = line.strip()
        if in_fm:
            if i > 0 and t == "---":
                in_fm = False
            continue
        m = _FENCE.match(t)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            continue
        if m:
            if block:
                out.append(block)
                block = []
            fence = (m.group(1)[0], len(m.group(1)))
            continue
        block.append((line, i + 1))
    if block:
        out.append(block)
    return out


def _has_frontmatter(lines: list[str]) -> bool:
    if not lines or lines[0].strip() != "---":
        return False
    saw = False
    for ln in lines[1:40]:
        t = ln.strip()
        if t == "---":
            return saw
        if re.match(r"^[A-Za-z0-9_-]+:\s*", t):
            saw = True
    return False


def _scan_block(block: list[tuple[str, int]], cross_sentence: bool) -> list[tuple[int, int, str]]:
    text = "\n".join(ln for ln, _ in block)
    starts: list[tuple[int, int]] = []
    cur = 0
    for ln, no in block:
        starts.append((cur, no))
        cur += len(ln) + 1
    return _find(text, starts, cross_sentence)


def _pos(starts: list[tuple[int, int]], offset: int) -> tuple[int, int]:
    lo, hi = 0, len(starts) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        off, no = starts[mid]
        nxt = starts[mid + 1] if mid + 1 < len(starts) else None
        if offset < off:
            hi = mid - 1
        elif nxt and offset >= nxt[0]:
            lo = mid + 1
        else:
            return no, offset - off + 1
    return starts[0][1], 1


def _find(text: str, starts: list[tuple[int, int]], cross_sentence: bool) -> list[tuple[int, int, str]]:
    findings: list[tuple[int, int, str]] = []
    offset = 0
    n = len(text)
    while offset < n:
        start = text.find("不是", offset)
        if start == -1:
            break
        if start > 0 and text[start - 1] == "是":  # 「是不是」问句片段,跳过
            offset = start + 2
            continue
        cand = text[start:]
        end = _flip_end(cand, cross_sentence)
        if end == -1:
            offset = start + 2
            continue
        raw = _TRAIL.sub("", _extract(cand, end))
        if len(raw) >= 4:
            line, col = _pos(starts, start)
            findings.append((line, col, raw))
        offset = start + max(len(raw), 2)
    return findings


def _flip_end(cand: str, cross_sentence: bool) -> int:
    """从「不是」往后找肯定翻转的结束位;找不到返回 -1。逐分支移植自 check-ai-patterns.js。"""
    i = 2  # 跳过「不是」
    scanned = 0
    crossed = False  # 已越过分隔符:其后紧贴的「是」可能是 只是/可是/于是 的词尾,不算翻转
    L = len(cand)
    while i < L and scanned <= _MAX_NEG_SPAN:
        ch = cand[i]
        if cand[i:i + 2] == "而是":
            return i + 2
        if ch in _SOFT_SEP:
            nxt = _skip_gap(cand, i + 1)
            if cand[nxt:nxt + 2] == "而是":
                return nxt + 2
            if nxt < L and cand[nxt] == "是" and not (nxt + 1 < L and cand[nxt + 1] in _TAG_PARTICLES):
                return nxt + 1
            crossed = True
        if ch in _HARD_SEP:
            nxt = _skip_gap(cand, i + 1)
            # 跨句号的「。是」翻转默认不报(那是作者的声音,逗号并排才是 AI 写法)
            if cross_sentence and nxt < L and cand[nxt] == "是" and not (nxt + 1 < L and cand[nxt + 1] in _TAG_PARTICLES):
                return nxt + 1
            if ch != ".":
                break
            crossed = True
        if ch in _STOP:
            break
        # 同句无分隔的紧凑式「不是A是B」(分隔符之前才认,之后的「是」多是连词词尾)
        if ch == "是" and cand[i - 1] not in _EITHER_OR_PREV and not crossed:
            return i + 1
        i += 1
        scanned += 1
    return -1


def _extract(cand: str, marker_end: int) -> str:
    e = marker_end
    limit = min(len(cand), marker_end + _MAX_POS_SPAN)
    while e < limit and cand[e] not in _STOP:
        e += 1
    return cand[:e]


def _skip_gap(text: str, i: int) -> int:
    """跳过行内空白;允许跨一个换行(再跳其后空白),让「,\\n而是」也能接上。"""
    L = len(text)
    while i < L and text[i] in " \t\r":
        i += 1
    if i < L and text[i] == "\n":
        i += 1
        while i < L and text[i] in " \t\r":
            i += 1
    return i


def _compact(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s[:77] + "..." if len(s) > 80 else s
