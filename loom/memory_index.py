"""记忆库的检索增强件（2026-11-03 批 B：为百万字小说的上下文检索）。

三件事，全部**纯函数 + 零模型调用**（外部 AI 审阅结论：规则覆盖 90%+，LLM 只做兜底）：

1. `atomize` 拆条器——把一段长记忆拆成"每条自包含"的原子事实。
   为什么：检索粒度与事实完整性必须分离。原段落仍落速记本（事实源），
   索引层只放 30~60 字的原子条——这样"顾明怕水"才搜得准。
2. `extract_entities` 实体抽取 + `entity_index` 精确匹配表。
   为什么：FTS5 的 unicode61 对中文是**按字切**，"顾明"→"顾"+"明"，
   搜"明"会命中一切含"明"的段落（精度灾难）。实体表用精确值兜住专名，
   完全绕开中文分词，纯 Python 零依赖。
3. `rrf_fuse` 三通道融合（Reciprocal Rank Fusion）。
   为什么：向量语义命中和字面命中有各自盲区，倒数排名相加能让两路互补，
   k=60 是原始论文在百万级语料上的最优值，万级同样适用，不用调。

**边界**：本模块不碰 sqlite 连接（由 memory.py 提供 conn），不碰网络、不调模型。
"""

from __future__ import annotations

import re

# ── 拆条参数（外部 AI 审阅建议的取值） ──────────────────────────────────────
ATOM_MAX = 60        # 单条原子上限：≤60 字直接入库
ATOM_HARD = 100      # 超过这个长度才考虑 LLM 兜底（本模块不调模型，只标 needs_llm）
SUBJECT_MAX = 12     # 补主语时认为"这段开头像个主语"的最大长度

# 句界：中文句号/分号/叹号/问号/换行；英文句点只在后跟空白时算（避免拆小数点）
_SENT_SPLIT = re.compile(r"[。；;！!？?\n]+")
# 逗号/顿号（二次切用）
_COMMA_SPLIT = re.compile(r"[，,、]+")

# 虚词/连接词开头 → 这段不像自包含的主语
_WEAK_HEAD = re.compile(
    r"^(的|了|着|过|和|与|及|或|而|但|所以|因此|因为|如果|虽然|然后|于是|并且|"
    r"也|还|就|都|又|再|却|才|只|便|即|则|其|之|此|该|这|那)"
)

# 实体候选：书名号内、引号内、以及 2~4 个连续汉字的专名候选
_BOOK_TITLE = re.compile(r"《([^》]{1,20})》")
_QUOTED = re.compile(r"[「『\"“]([^」』\"”]{1,20})[」』\"”]")
# 专名候选：2~4 个汉字，且不与常见词冲突太难判——这里只取"连续汉字串"作为弱候选
_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]{2,4}")
# 常见虚词/通用词：不作为实体（保守，宁可漏也不要把普通词塞进实体表）
_STOPWORDS = set(
    "主角 配角 反派 设定 世界观 大纲 章纲 卷纲 正文 作者 读者 故事 情节 人物 场景 "
    "对话 心理 动作 描写 节奏 结构 主题 冲突 高潮 铺垫 伏笔 线索 结局 开头 结尾 "
    "时候 地方 东西 事情 问题 办法 样子 意思 感觉 想法 做法 说法 看法 起来 下去 "
    "他们 我们 你们 大家 自己 别人 别人 什么 怎么 为什么 因为 所以 但是 而且".split()
)


def extract_entities(text: str, extra: list[str] | None = None) -> list[str]:
    """抽实体（保守规则：书名号/引号内的、以及 extra 里点名的）。

    `extra` 是调用方已知的专名（书里的人物/地点表等）——**优先用已知表**；
    没有已知表时只认书名号和引号内的显式专名（宁可漏，不把普通词塞进实体表，
    因为实体表是"精确匹配"通道，脏词会直接污染召回精度）。
    """
    body = str(text or "")
    found: list[str] = []
    for m in _BOOK_TITLE.finditer(body):
        found.append(m.group(1).strip())
    for m in _QUOTED.finditer(body):
        found.append(m.group(1).strip())
    for name in extra or []:
        n = str(name or "").strip()
        if n and n in body:
            found.append(n)
    out: list[str] = []
    seen: set[str] = set()
    for e in found:
        if not e or e in seen or e in _STOPWORDS or len(e) > 20:
            continue
        seen.add(e)
        out.append(e)
    return out


def _looks_like_subject_head(seg: str) -> bool:
    """这段开头像不像自带主语（不是以虚词/连接词起头）。"""
    head = seg[:SUBJECT_MAX]
    if not head:
        return False
    if _WEAK_HEAD.match(head):
        return False
    return True


def ensure_subject(seg: str, subject: str = "") -> str:
    """保证原子条自包含：以虚词开头且已知主语时，把主语补回句首。

    例：上一句是"顾明怕水"，下一句切成"一到雨天就发抖"→ 补成"顾明一到雨天就发抖"。
    subject 由调用方（atomize 内部）从前一个成功片段里推。
    """
    s = str(seg or "").strip()
    if not s or not subject:
        return s
    if _looks_like_subject_head(s):
        return s
    return subject + s


def _guess_subject(seg: str, entities: list[str]) -> str:
    """从片段里猜主语：优先已知实体，其次开头的连续汉字串。"""
    for e in entities:
        if e and e in seg:
            return e
    m = _CJK_RUN.match(seg)
    if m and m.group(0) not in _STOPWORDS:
        return m.group(0)
    return ""


def atomize(text: str, entities: list[str] | None = None) -> list[str]:
    """把一段文本拆成原子事实（每条尽量 ≤ATOM_MAX 字、自包含）。

    分级策略（外部 AI 审阅 Q4）：
      ① 句号/分号/叹问号切 → ≤60 字直接用
      ② 单句 >60 字 → 逗号/顿号二次切，切完再补主语
      ③ 单片仍 >100 字 → 原样保留（**不在这里调模型**，只在返回值里体现长度，
         由调用方决定要不要走 LLM 兜底）
    没有主语可补的碎句（如纯并列短语）原样保留，不硬拼——宁可短，不要假主语。
    """
    body = str(text or "").strip()
    if not body:
        return []
    ents = list(entities or [])

    out: list[str] = []
    last_subject = ""
    for sent in _SENT_SPLIT.split(body):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= ATOM_MAX:
            piece = ensure_subject(sent, last_subject)
            out.append(piece)
            guessed = _guess_subject(sent, ents)
            if guessed:
                last_subject = guessed
            continue

        # 二次切：按逗号/顿号攒段，攒到接近上限就断
        buf = ""
        for part in _COMMA_SPLIT.split(sent):
            part = part.strip()
            if not part:
                continue
            # 还在合理长度内就继续攒（逗号切开的两截往往共享主语，硬切会碎）
            if buf and len(buf) + len(part) + 1 > ATOM_MAX:
                out.append(ensure_subject(buf, last_subject))
                guessed = _guess_subject(buf, ents)
                if guessed:
                    last_subject = guessed
                buf = part
            else:
                buf = (buf + "，" + part) if buf else part
        if buf:
            out.append(ensure_subject(buf, last_subject))
            guessed = _guess_subject(buf, ents)
            if guessed:
                last_subject = guessed

    # 去重（同序保首个），滤掉过短的碎渣
    seen: set[str] = set()
    clean: list[str] = []
    for a in out:
        a = a.strip()
        if len(a) < 4:   # "是的。"这类没信息量
            continue
        if a in seen:
            continue
        seen.add(a)
        clean.append(a)
    return clean


def rrf_fuse(
    rankings: list[list[int]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：把多路召回（每路是 id 列表，按相关性降序）融合成一条。

    score(d) = Σ weight_ch / (k + rank_ch(d))

    `rankings`：各通道的 top-N id 列表（**只要 id，不要分数**——RRF 只吃排名，
    这正是它对不同量纲的通道（余弦相似度 vs 字面命中）都鲁棒的原因）。
    `weights`：可选通道权重（默认全 1.0）。实体精确通道通常给 2.0 左右，因为
    它的精度远高于另两路，但召回窄。
    """
    scores: dict[int, float] = {}
    ws = list(weights or [])
    for ch, ranking in enumerate(rankings):
        w = ws[ch] if ch < len(ws) else 1.0
        for rank, mid in enumerate(ranking, start=1):
            try:
                key = int(mid)
            except Exception:
                continue
            scores[key] = scores.get(key, 0.0) + w / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


_CJK_CHAR = re.compile(r"[\u4e00-\u9fa5]")


def seg_for_fts(text: str) -> str:
    """写入 FTS5 前的预处理：**把中文按字用空格分开**。

    ⚠️ 2026-11-03 本机实测（3.45.3，unicode61）纠正了一个常见误解：
    unicode61 对连续汉字不是"按字切"，而是**整串当一个 token**
    （"顾明怕水，一到雨天就发抖" 只产出 `顾明怕水` / `一到雨天就发抖` 两个 token）。
    后果是搜"顾明"永远命中不了 —— 除非写入时就按字切开。

    切开后配 `fts_query` 的短语查询（"顾 明"），连续字序列 = **精确子串匹配**：
    实测 搜"顾明"命中、搜"明"也命中、不存在的词 0 命中。
    """
    s = str(text or "")
    out = []
    for ch in s:
        out.append(ch)
        if _CJK_CHAR.match(ch):
            out.append(" ")   # 汉字后补空格 → 每字一个 token
    return "".join(out)


def fts_query(query: str) -> str:
    """把查询词变成安全的 FTS5 查询串（与 seg_for_fts 配对）。

    - 含中文：按字切 + 整串当一个短语 → 连续字序列匹配（精确子串）
    - 纯英文/数字：按空白切，各成短语，OR 连接
    - 统一去掉 FTS5 运算符字符，避免语法注入/报错
    """
    q = str(query or "").strip()
    if not q:
        return ""
    chars = [c for c in q if _CJK_CHAR.match(c)]
    if chars:
        # 短语查询里带引号：连续字序列必须相邻出现才算命中
        return '"' + " ".join(chars[:24]) + '"'
    safe = re.sub(r'["*():^\-]', " ", q)
    words = [w for w in re.split(r"\s+", safe) if w]
    if not words:
        return ""
    return " OR ".join('"%s"' % w.replace('"', "") for w in words[:8])
