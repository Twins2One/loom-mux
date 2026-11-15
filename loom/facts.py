"""R3 知情矩阵 + R4 事实三视图:**同一批事实,一张表,四个投影**。

**为什么是一张表**:审计 A-B 的原话是「伏笔登记表 + 剧情线账本 + 章节时间线」如果各自录入,
三处必然互相矛盾(同一件事录了三遍,改一处忘两处)。所以只有一套 schema:

```
| 章 | 类型 | 主体 | 对象 | 值 | 来源 |
```

`类型` 只有四种:**伏笔 / 剧情线 / 时间 / 知情**。三视图与知情矩阵都是这张表的**投影**
——看的角度不同,录的只有一处。落盘在 `外置大脑/事实账本.md`(md 为真相,人能改)。

**录入从哪来(不重复录)**:
- **伏笔** 从卡章纲的缩进子行(`[埋设]/[推进]/[回收]`)自动灌入 —— 那边本来就是唯一真源
  (`hooks.parse_hooks`)。自动灌入的行 `来源=卡章纲`,重算时只替换这一批,**手写的行一律不动**。
- 剧情线 / 时间 / 知情:目前**手写**(AI 抽取有猜的风险,猜错的「谁不知道什么」会直接污染正文)。
  这是刻意的缺口,记在待裁定清单里。

**知情矩阵的读法**:
- 「第 N 章时,X 知不知道 Y」= 取 `类型=知情 且 主体=X 且 对象=Y 且 章<=N` 的**最后一条**。
- 没记录 → `None`(不知道?不知道),**绝不默认「知道」**——默认知道会让越界检查永远是绿的。
"""

from __future__ import annotations

from pathlib import Path

from . import budget, paths
from .fsutil import atomic_write_text

FACTS_REL = paths.brain_rel("事实账本")
COLS = ("章", "类型", "主体", "对象", "值", "来源")
TYPES = ("伏笔", "剧情线", "时间", "知情")
KNOW_YES, KNOW_NO = "知道", "不知道"

_HEAD = """# 事实账本

> 一张表,四个投影:**伏笔登记表 / 剧情线账本 / 章节时间线 / 知情矩阵**。
> 同一件事只录一次——三视图看到的都是这张表,不另建表。
>
> - `类型` 只认:伏笔 / 剧情线 / 时间 / 知情
> - 伏笔行(来源=卡章纲)是**自动灌入**的,重新灌入时只替换这一批,手写的行不动
> - 知情行的 `值` 只认「知道 / 不知道」;没记录的按「不知道」算(宁严不宽)
"""


# ---------------------------------------------------------------- 表读写
def _split(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _bad_row(line: str) -> bool:
    """这一行「看着像数据行,却解析不出」——表头与分隔行不算。"""
    cells = _split(line)
    if len(cells) < 5:
        return True
    if cells[0] == COLS[0]:                      # 表头
        return False
    if cells[0] and set(cells[0]) <= {"-", ":"}:  # |---|:--| 分隔行
        return False
    if cells[0].isdigit() and cells[1] in TYPES:
        return False
    return True


def _looks_like_row_without_pipe(line: str) -> bool:
    """开头少了竖线的数据行同样会被 parse_table 丢掉,整表重写就把它删了。

    判据故意收紧(≥5 段、首段是章号),免得把正文里偶然出现的竖线当坏行把写入口全堵上。
    """
    t = (line or "").strip()
    if not t or t.startswith("|") or t.startswith("#"):
        return False
    cells = _split(t)
    return len(cells) >= 5 and cells[0].strip().isdigit()


def damage(root: Path | str) -> dict:
    """账本里「有、但读不成事实」的行。**这是数据会丢的报警**,不是格式建议。

    `parse_table` 读不出就跳过;而 `load→改→save` 会整表重写 ⇒ 这些行会被**静默删掉**
    (审查 B 的 P1)。所以写之前一律先问 `damage()`:有坏行就拒绝写,让人先手工修表。
    """
    p = Path(root) / FACTS_REL
    if not p.is_file():
        return {"坏行数": 0, "样例": []}
    bad = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
           if (ln.lstrip().startswith("|") and _bad_row(ln))
           or _looks_like_row_without_pipe(ln)]
    return {"坏行数": len(bad), "样例": bad[:3]}


def parse_table(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _split(line)
        if len(cells) < 5 or not cells[0].isdigit() or cells[1] not in TYPES:
            continue
        rows.append({"章": int(cells[0]), "类型": cells[1], "主体": cells[2],
                     "对象": cells[3], "值": cells[4] if len(cells) > 4 else "",
                     "来源": cells[5] if len(cells) > 5 else "手写"})
    return rows


def render_table(rows: list[dict]) -> str:
    out = [_HEAD, "", "| " + " | ".join(COLS) + " |", "|" + "|".join(["---"] * len(COLS)) + "|"]
    for r in sorted(rows, key=lambda x: (x["章"], TYPES.index(x["类型"]), x["主体"])):
        out.append("| " + " | ".join(str(r.get(c, "")).replace("|", "／") for c in COLS) + " |")
    return "\n".join(out) + "\n"


def load(root: Path | str) -> list[dict]:
    p = Path(root) / FACTS_REL
    return parse_table(p.read_text(encoding="utf-8")) if p.is_file() else []


def save(root: Path | str, rows: list[dict]) -> Path:
    p = Path(root) / FACTS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, render_table(rows))
    return p


def add(root: Path | str, *, chapter: int, kind: str, subject: str, obj: str = "",
        value: str = "", source: str = "手写") -> dict:
    """手写一条事实(三个视图与知情矩阵立刻都能看到它)。"""
    if kind not in TYPES:
        return {"ok": False, "reason": f"不认识的类型:{kind}(只认 {' / '.join(TYPES)})"}
    _dmg = damage(root)
    if _dmg["坏行数"]:
        return {"ok": False, "code": "table_broken",
                "reason": (f"账本里有 {_dmg['坏行数']} 行读不成事实(样例:{_dmg['样例'][:1]}),"
                           "现在写会把它们从盘上删掉。请先手工修好那几行,或者先备份账本。")}
    rows = load(root)
    rows.append({"章": int(chapter), "类型": kind, "主体": subject, "对象": obj,
                 "值": value, "来源": source})
    save(root, rows)
    return {"ok": True, "条数": len(rows)}


# ---------------------------------------------------------------- 从卡章纲灌伏笔
def ingest(root: Path | str) -> dict:
    """把卡章纲里的伏笔子行灌进账本。**只替换 来源=卡章纲 的行**,手写行不动。"""
    from . import hooks

    _dmg = damage(root)
    if _dmg["坏行数"]:
        return {"ok": False, "code": "table_broken",
                "reason": (f"账本里有 {_dmg['坏行数']} 行读不成事实(样例:{_dmg['样例'][:1]}),"
                           "灌伏笔会整表重写、把它们删掉。请先修表或备份。")}
    rows = [r for r in load(root) if r.get("来源") != "卡章纲"]
    try:
        hs = hooks.parse_hooks(root)
    except Exception:       # noqa: BLE001 - 章纲读不了不该让账本崩;保持手写行不动
        return {"ok": False, "reason": "卡章纲读不出来,账本没动。", "条数": len(rows)}
    seen: set[tuple] = set()
    for h in hs:
        key = (h.chapter, h.kind, h.text)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"章": h.chapter, "类型": "伏笔", "主体": _hook_name(h.text),
                     "对象": h.kind, "值": h.text, "来源": "卡章纲"})
    save(root, rows)
    return {"ok": True, "灌入": len(seen), "条数": len(rows)}


def _hook_name(text: str) -> str:
    """伏笔的名字:取文本里最像「对象」的那一段(冒号前 / 顿号前 / 前 12 字)。"""
    t = (text or "").strip()
    for sep in ("：", ":", "——", "—", "(", "(", ","):
        if sep in t:
            head = t.split(sep)[0].strip()
            if 1 <= len(head) <= 20:
                return head
    return t[:12]


# ---------------------------------------------------------------- 三视图 + 知情矩阵
def view_hooks(rows: list[dict], *, current: int = 0, distance: int = 8) -> list[dict]:
    """伏笔登记表:每条伏笔埋在哪章、最后推进到哪章、收了没、**该在哪一章收**。"""
    books: dict[str, dict] = {}
    for r in rows:
        if r["类型"] != "伏笔":
            continue
        b = books.setdefault(r["主体"], {"伏笔": r["主体"], "埋设章": None, "最后推进章": None,
                                        "回收章": None, "状态": ""})
        n, state = r["章"], r["对象"]
        if state == "埋设":
            b["埋设章"] = n if b["埋设章"] is None else min(b["埋设章"], n)
        elif state == "推进":
            b["最后推进章"] = n if b["最后推进章"] is None else max(b["最后推进章"], n)
        elif state == "回收":
            b["回收章"] = n if b["回收章"] is None else min(b["回收章"], n)
        b["状态"] = state
    out = []
    for b in books.values():
        anchor = b["埋设章"] or 0
        last = b["最后推进章"] or anchor
        b["该收章"] = (anchor + distance) if anchor else None
        b["距上次推进"] = (current - last) if (current and last) else None
        b["悬空"] = bool(current and not b["回收章"] and b["距上次推进"] is not None
                          and b["距上次推进"] > distance)
        b["已收"] = b["回收章"] is not None
        out.append(b)
    return sorted(out, key=lambda x: (x["埋设章"] or 0, x["伏笔"]))


def view_lines(rows: list[dict], *, current: int = 0) -> list[dict]:
    """剧情线账本:每条线最后在哪章推进过、**多久没推进了**。"""
    lines: dict[str, dict] = {}
    for r in rows:
        if r["类型"] != "剧情线":
            continue
        g = lines.setdefault(r["主体"], {"剧情线": r["主体"], "推进次数": 0, "最后推进章": None})
        g["推进次数"] += 1
        g["最后推进章"] = r["章"] if g["最后推进章"] is None else max(g["最后推进章"], r["章"])
    out = list(lines.values())
    for g in out:
        g["多久没推进"] = (current - g["最后推进章"]) if (current and g["最后推进章"]) else None
    return sorted(out, key=lambda x: -(x["最后推进章"] or 0))


def view_timeline(rows: list[dict]) -> list[dict]:
    """章节时间线:每章发生的时间点与事件(按章升序)。"""
    by: dict[int, list[dict]] = {}
    for r in rows:
        if r["类型"] != "时间":
            continue
        by.setdefault(r["章"], []).append({"时间点": r["主体"], "事件": r["值"] or r["对象"]})
    return [{"章": n, "条目": by[n]} for n in sorted(by)]


def view_knowledge(rows: list[dict]) -> list[dict]:
    """知情矩阵(按人聚合):谁知道什么、什么时候知道的。"""
    who: dict[str, list[dict]] = {}
    for r in rows:
        if r["类型"] != "知情":
            continue
        who.setdefault(r["主体"], []).append({"章": r["章"], "事": r["对象"], "值": r["值"]})
    return [{"人物": k, "知道": sorted(v, key=lambda x: x["章"])} for k, v in sorted(who.items())]


def knows(rows: list[dict], chapter: int, who: str, what: str) -> dict:
    """「第 chapter 章时,who 知不知道 what」——取最后一条记录;**没记录 = 不知道**。"""
    hits = [r for r in rows if r["类型"] == "知情" and r["主体"] == who
            and r["对象"] == what and r["章"] <= chapter]
    if not hits:
        return {"判定": "未知", "依据": None,
                "说明": "账本里没有这条知情的记录;按「不知道」处理(宁严不宽)。"}
    # 同一章有多条时不能按写入顺序取「第一条」——那会朝"知道"的方向错(审查 B 的 P1)。
    # 先取最大章,再在同章里优先取「不知道」:宁可多提示一次越界,不可放过。
    top = max(r["章"] for r in hits)
    same = [r for r in hits if r["章"] == top]
    last = next((r for r in same if r["值"] == KNOW_NO), same[0])
    yes = last["值"] != KNOW_NO
    return {"判定": KNOW_YES if yes else KNOW_NO, "依据": last,
            "说明": f"依据第 {last['章']} 章那条记录"}


def out_of_bounds(root: Path | str, chapter: int, who: str, *, body: str = "") -> list[dict]:
    """越界检查(R3 判据「生成时能据此判越界」):谁在第 N 章就不该知道某件事,而正文里提到了它。

    **只提示不阻断**(ADR-0006)。判定全靠账本里明写的「不知道」,不猜。
    """
    rows = load(root)
    if not body:
        p = paths.chapter_path(root, chapter)
        body = p.read_text(encoding="utf-8") if p.is_file() else ""
    out: list[dict] = []
    # 先收候选(谁/哪件事 在第 N 章前出现过「不知道」),再问「到第 N 章为止他知不知道」
    # ——只看「曾经不知道」会误报:第 7 章他已经知道了,不该再提示。
    seen: set[tuple[str, str]] = set()
    for r in rows:
        if r["类型"] != "知情" or r["值"] != KNOW_NO or r["章"] > chapter:
            continue
        if r["主体"] != who:
            continue
        what = r["对象"]
        if not what or (who, what) in seen:
            continue
        seen.add((who, what))
        if knows(rows, chapter, who, what)["判定"] == KNOW_YES:
            continue                      # 到这一章已经知道了,不算越界
        if what in body:
            out.append({"人物": who, "不该知道": what, "记录章": r["章"],
                        "问题": f"第 {r['章']} 章时「{who}」还不知道「{what}」,但这一章提到了它",
                        "依据": r})
    return out


def bounds_any(root: Path | str, chapter: int, body: str) -> list[dict]:
    """不指定是谁时的越界检查:账本里**任何人**在第 N 章还不该知道的事,偏偏出现在正文里。

    供除虫/审稿报告用(repair.deterministic_items 把它并进两维报告),不调模型。
    """
    rows = load(root)
    who_all = sorted({r["主体"] for r in rows if r["类型"] == "知情"})
    out: list[dict] = []
    for who in who_all:
        out.extend(out_of_bounds(root, chapter, who, body=body))
    return out


def summary(root: Path | str, *, chapter: int = 0, distance: int = 0) -> dict:
    """一次给全三视图 + 知情矩阵(界面/端点用)。**纯只读。**"""
    rows = load(root)
    cur = chapter or (max(paths.chapter_numbers(root)) if paths.chapter_numbers(root) else 0)
    dist = distance or _distance(root)
    return {"ok": True, "当前章": cur, "落盘": False,
            "伏笔": view_hooks(rows, current=cur, distance=dist),
            "剧情线": view_lines(rows, current=cur),
            "时间线": view_timeline(rows),
            "知情": view_knowledge(rows),
            "条数": len(rows), "账本": FACTS_REL}


def _distance(root: Path | str) -> int:
    """伏笔该在第几章收:沿用 loom.toml 里的「伏笔提醒章距」,不另设一个旋钮。"""
    try:
        from .config import load_config

        return int(getattr(load_config(Path(root)), "foreshadow_distance", 8) or 8)
    except Exception:      # noqa: BLE001 - 配置读不出来就用默认,不影响查账
        return 8


# ---------------------------------------------------------------------------
# Q10(作者 2026-10-18 裁定「抽」):剧情线 / 时间线 / 知情 让 AI 抽。
#
# **口径:抽成提案,人点过才入账。** 理由是这一条自己的开头就写着的那句:
# 「知情」一旦被猜错并写进正史,越界检查从此不可信。所以:
#   · 抽取只返回 `提案`,落盘=False,一个字都不写;
#   · 入账必须显式给序号(accept)；且**证据那句必须真在正文里**,查不到就整条退回(防模型编)。
# 伏笔不在这里抽——它已经有确定来源(卡章纲的缩进行),重复抽会把同一件事记两遍。
# ---------------------------------------------------------------------------

EXTRACT_KINDS = ("剧情线", "时间", "知情")

_EXTRACT_RULES = """你是网文账本抽取器。只做一件事:从正文里抽「已经发生、后文要能查」的事实。

只抽三类:
- 剧情线 | 主体=谁/哪条线 | 对象=这条线叫什么 | 值=本章推进到哪一步
- 时间 | 主体=场景/事件 | 对象=- | 值=时间点或时长(如「第3日清晨」「三个时辰后」)
- 知情 | 主体=谁 | 对象=知道了什么 | 值=知道 或 不知道

硬规矩:
1. 一行一条,用竖线分隔五个字段:类型|主体|对象|值|证据
2. 「证据」必须是正文里**原样出现**的一句(不超过 30 字),用来核对你没编。
3. 拿不准就不写。宁可少抽,不要补脑。
4. 不要输出表头、不要解释、不要 Markdown 代码块,只输出这些行。
"""


def extract_prompt(chapter: int, body: str, people: list[str] | None = None) -> str:
    who = "、".join(people[:40]) if people else "(未给人物表)"
    return (f"第 {chapter} 章正文如下。已知人物:{who}。\n"
            f"请按规则抽事实行(类型|主体|对象|值|证据),没有就什么都不输出。\n\n"
            f"=== 正文开始 ===\n{body}\n=== 正文结束 ===")


def parse_proposal(text: str) -> list[dict]:
    """把模型输出解析成提案行。**宽松**:认全角/半角竖线、去掉代码围栏与表头;坏行丢弃不猜。"""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip().strip("`").strip()
        if not line or line.startswith("#") or line.startswith("==="):
            continue
        line = line.replace("｜", "|")
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 5:
            continue
        kind, subject, obj, value, evidence = parts[0], parts[1], parts[2], parts[3], "|".join(parts[4:])
        if kind not in EXTRACT_KINDS or not subject:
            continue
        if kind == "知情" and value not in (KNOW_YES, KNOW_NO):
            continue
        out.append({"类型": kind, "主体": subject, "对象": "" if obj in ("-", "—", "") else obj,
                    "值": value, "证据": evidence.strip()})
    for i, r in enumerate(out, start=1):
        r["序号"] = i
    return out


def extract(root: Path | str, chapter: int, backend, *, body: str = "",
            people: list[str] | None = None, max_chars: int = 12000, progress=None) -> dict:
    """抽一版提案。**一个字节都不写**(落盘=False)。要入账请走 accept。"""
    if not body:
        p = paths.chapter_path(Path(root), chapter)
        body = p.read_text(encoding="utf-8") if p.is_file() else ""
    if not body.strip():
        return {"ok": False, "reason": f"第 {chapter} 章没有正文,没有可抽的东西。", "提案": []}
    if progress:
        progress({"stage": "抽取账本提案", "chapter": chapter})
    text = backend.complete(_EXTRACT_RULES, extract_prompt(chapter, body, people),
                            max_chars=max_chars)
    rows = parse_proposal(text)
    have = {(r["类型"], r["主体"], r["对象"], r["值"]) for r in load(root)}
    for r in rows:
        dup = (r["类型"], r["主体"], r["对象"], r["值"]) in have
        r["已有"] = dup
        r["证据在正文里"] = bool(r["证据"]) and r["证据"] in body
    return {"ok": True, "章": chapter, "落盘": False, "提案": rows, "条数": len(rows),
            "可直接入账": [r["序号"] for r in rows if r["证据在正文里"] and not r["已有"]],
            "说明": ("这些只是提案,一条也没写进账本。勾了序号再调 accept 才入账;"
                     "证据在正文里查不到的,入账会被退回来。")}


def accept(root: Path | str, chapter: int, picks: list[int] | list[dict], *,
           proposal: list[dict] | None = None, body: str = "") -> dict:
    """把**人点过**的几条写进账本。证据必须真在正文里;来源记成「AI抽取(已确认)」。"""
    root = Path(root)
    if not body:
        p = paths.chapter_path(root, chapter)
        body = p.read_text(encoding="utf-8") if p.is_file() else ""
    rows_in = proposal or []
    by_no = {r.get("序号"): r for r in rows_in if isinstance(r, dict)}
    chosen: list[dict] = []
    for pick in picks or []:
        r = pick if isinstance(pick, dict) else by_no.get(pick)
        if not r:
            continue
        chosen.append(r)
    if not chosen:
        return {"ok": False, "reason": "没选任何一条(要么没给序号,要么序号对不上提案)。"}
    bad, good = [], []
    for r in chosen:
        kind = r.get("类型")
        subj = (r.get("主体") or "").strip()
        if kind not in EXTRACT_KINDS or not subj:
            bad.append({**r, "原因": "类型或主体不合法"})
            continue
        ev = (r.get("证据") or "").strip()
        if ev and not body:
            # 正文读不到(文件不在/还是空的)⇒ **没法核对**,不能当成「核对通过」。
            # 以前这里写的是 ev and body and ...,正文缺失时直接跳过后面的比对,编造的证据能进账本。
            bad.append({**r, "原因": "读不到正文,没法核对证据(先把正文落盘再确认)"})
            continue
        if ev and ev not in body:
            bad.append({**r, "原因": "证据在正文里查不到(可能是模型编的)"})
            continue
        if kind == "知情":
            _v = (r.get("值") or "").strip()
            if _v not in ("知道", "不知道"):
                # 知情只能是二值:写成「也许」时 knows() 会当成「知道」,越界提示从此误报。
                bad.append({**r, "原因": "知情只能填「知道」或「不知道」"})
                continue
        if not ev:
            bad.append({**r, "原因": "没有证据句,不敢入账"})
            continue
        good.append(r)
    if bad:
        return {"ok": False, "code": "rejected", "退回": bad,
                "reason": f"{len(bad)} 条被退回(见「退回」),一条都没写。"}
    _dmg = damage(root)
    if _dmg["坏行数"]:
        return {"ok": False, "code": "table_broken",
                "reason": (f"账本里有 {_dmg['坏行数']} 行读不成事实,写进去会把它们删掉。"
                           "请先修表或备份。")}
    rows = load(root)
    for r in good:
        rows.append({"章": int(chapter), "类型": r["类型"], "主体": r["主体"].strip(),
                     "对象": (r.get("对象") or "").strip(), "值": (r.get("值") or "").strip(),
                     "来源": "AI抽取(已确认)"})
    save(root, rows)
    return {"ok": True, "入账": len(good), "条数": len(rows)}
