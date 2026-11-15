"""R5 材料冲突:先把**能确定的**挑出来,不确定的绝不猜。

作者 2026-10-18 裁定:「设定大于章纲大于正文,但当人作出不同裁定后,其他所有冲突项必须请示
每处是否更改。」——这条裁定管的是**怎么判**,不管**怎么找**。而此前 `/api/arbitrate` 的入参
冲突清单**全仓没有任何地方产出**:没有检测器,人拿不到清单,仲裁就永远跑不起来。

本模块只做**确定性**检测(纯字符串/正则,不调模型、不写盘):

1. **跨层同键不同值**——设定层(`世界观/*.md`、`人物/*.md`、`世界观.md`、`人物卡.md`、`状态账本.md`)
   与章纲层(`卷纲.md`、`卡章纲.md`)里都写了 `- 键:值`,同一个键两处值不一样。
   这是真正能交给 `arbitrate.plan` 的那种冲突(两边各有一层、各有值、各有行号)。
2. **卷起止重叠**——同一章被两卷同时覆盖。**这不是「以谁为准」能解决的**,所以单独列,
   不假装它是可仲裁项。
3. **词典别名冲突**——同一个串指向两个实体(`entities.index` 的既有产出)。同样单独列:
   合并实体是不可逆的信息损失,只能由人裁。

正文里的「写错了」这类判断需要读懂句子,不在本模块范围内——不做,也不假装做了。
"""

from __future__ import annotations

import re
from pathlib import Path

from . import entities, paths

LAYERS = ("设定", "章纲")

# `- 键:值` / `* 键:值` / 行首 `键:值`。键不许带冒号、竖线、井号,长度 <=16(太长的不是「键」)。
_KV = re.compile(r"^\s*(?:[-*+]\s*)?([^\s:：|#]{1,16})\s*[:：]\s*(.+?)\s*$")
# 值看起来像占位/空壳,就不算「写了」:避免用「(占位示例)」去跟任何东西比
_PLACEHOLDER = re.compile(r"^[\(（].*[\)）]$|^[·—\-_\s]*$")
_RANGE = re.compile(r"起止\s*[:：]\s*第?\s*(\d+)\s*[-–—~～]\s*第?\s*(\d+)\s*章?")
_MAX_ITEMS = 60


def _layer_files(root: Path) -> dict[str, list[Path]]:
    """两层各有哪些文件参与比较(只读存在的)。"""
    brain = root / paths.BRAIN_DIR
    set_files: list[Path] = []
    for rel in (paths.WORLD_REL, paths.CHARS_REL, paths.STATEBOOK_REL):
        p = root / rel
        if p.is_file():
            set_files.append(p)
    for sub in (paths.WORLD_DIR_REL, paths.CHARS_DIR_REL):
        d = root / sub
        if d.is_dir():
            # 成长档案是 learn 的 AI 追加区,不是人写的口径 —— 拉进来只会自己跟自己打架
            set_files += [p for p in sorted(d.glob("*.md")) if p.name != paths.GROWTH_NAME]
    card_files: list[Path] = []
    for rel in (paths.VOLUMES_REL, paths.CARD_REL):
        p = root / rel
        if p.is_file():
            card_files.append(p)
    return {"设定": set_files, "章纲": card_files}


def _kv_in(path: Path) -> dict[str, tuple[str, int]]:
    """一份文件里的 `键:值`。同一个键出现多次时**取第一条**(后面的多半是子块补充)。"""
    out: dict[str, tuple[str, int]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or s.startswith("|") or s.startswith(">") or s.startswith("#"):
            continue
        m = _KV.match(line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if _PLACEHOLDER.match(val) or not val:
            continue
        if len(val) > 80:      # 一句散文不是「值」
            continue
        out.setdefault(key, (val, i))
    return out


def cross_layer(root: Path | str) -> list[dict]:
    """同一键在设定层与章纲层写了不同值 → 可交给 arbitrate.plan 的冲突条目。"""
    root = Path(root)
    files = _layer_files(root)
    per: dict[str, dict[str, tuple[str, str, int]]] = {}
    for layer, fl in files.items():
        for p in fl:
            rel = p.relative_to(root).as_posix()
            for key, (val, ln) in _kv_in(p).items():
                per.setdefault(key, {}).setdefault(layer, (val, rel, ln))
    out = []
    for key, by in per.items():
        if len(by) < 2:
            continue
        vals = {L: by[L][0] for L in by}
        if len(set(vals.values())) == 1:
            continue
        item: dict = {"项": key}
        for L in by:
            val, rel, ln = by[L]
            item[L] = {"值": val, "位置": rel, "行": ln}
        out.append(item)
        if len(out) >= _MAX_ITEMS:
            break
    return out


def volume_overlap(root: Path | str) -> list[dict]:
    """卷纲里有几卷的起止互相压住 → 同一章属于两卷。**列出,不替人决定。**"""
    root = Path(root)
    p = root / paths.VOLUMES_REL
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    vols: list[tuple[str, int, int, int]] = []
    title = ""
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("##"):
            title = s.lstrip("#").strip() or title
            continue
        m = _RANGE.search(s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if b >= a:
                vols.append((title or f"第{len(vols) + 1}卷", a, b, i))
    out = []
    for x in range(len(vols)):
        for y in range(x + 1, len(vols)):
            n1, a1, b1, l1 = vols[x]
            n2, a2, b2, l2 = vols[y]
            lo, hi = max(a1, a2), min(b1, b2)
            if lo <= hi:
                out.append({"项": f"第{lo}-{hi}章归属", "值": f"{n1} / {n2} 都盖住了",
                            "位置": paths.VOLUMES_REL, "行": l2,
                            "说明": f"{n1}(行{l1}) 与 {n2}(行{l2}) 的起止重叠。"})
    return out[:_MAX_ITEMS]


def alias_clash(root: Path | str) -> list[dict]:
    """同一个串指向两个实体。合并是不可逆的,所以只报。"""
    root = Path(root)
    try:
        ents = entities.load(root)
        _, clash = entities.index(ents)
    except Exception as e:                     # noqa: BLE001 - 词典读坏不该让整页崩,但要留痕
        return [{"项": "实体词典读不出来", "值": str(e), "位置": "外置大脑/实体词典.md", "行": 0}]
    # 文件在、却一条也没读出来:要么全是坏行,要么格式不认识。**不能当成「没有别名冲突」**
    # (entities.load 读不出来时返回 [] 而不是抛错,静默成空就是最坏的那种假象)。
    p = root / entities.TABLE_REL
    if p.is_file() and not ents:
        # 只有「表里明明有数据行、却一条也没解析出来」才算读坏。
        # 新书的词典只有表头(数据行 0 条),那是正常的空表,报它等于每次开新书都先撒一次假警报。
        rows = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip().startswith("|")]
        data = [ln for ln in rows
                if not re.match(r"^\|\s*(id|:?-{2,}|-{2,})", ln.strip(), re.I)
                and not set(ln.strip()) <= {"|", "-", " ", ":"}]
        # 还有一种坏法:文件里根本没有表(被写成了别的内容)。模板里除了注释只剩表头,
        # 所以「有非注释、非表格行」= 这份文件已经不是实体表了。
        stray = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip() and not ln.strip().startswith(("#", ">", "|"))]
        if data or stray:
            return [{"项": "实体词典读不出来",
                     "值": (f"表里看着有 {len(data)} 行数据,却一条实体也没解析出来(格式不认识或全是坏行)"
                             if data else "文件里没有实体表(可能被改坏了)"),
                     "位置": entities.TABLE_REL, "行": 0}]
    return [{"项": "别名冲突", "值": c, "位置": "外置大脑/实体词典.md", "行": 0} for c in clash]


def scan(root: Path | str) -> dict:
    """一次扫全:跨层冲突 + 两类只报不裁的结构问题。**纯只读,落盘=False。**"""
    root = Path(root)
    cl = cross_layer(root)
    ov = volume_overlap(root)
    al = alias_clash(root)
    files = _layer_files(root)
    return {
        "ok": True, "落盘": False,
        "冲突": cl,
        "其它": ov + al,
        "条数": len(cl),
        "扫描": {"设定": len(files["设定"]), "章纲": len(files["章纲"])},
        "口径": ("只做确定性检测:跨层同键不同值、卷起止重叠、词典别名冲突。"
                 "正文里的「写错了」要读懂句子,本版不做,也不假装做了。"),
    }

# ---------------------------------------------------------------- AI 读一遍(只出提案)
#
# 确定性检测只能比「同一个键写了两个值」。正文是散文,「第三段写他左手持剑,第十二章写他右手」
# 这种矛盾要读懂句子才看得见 —— 那只有模型能做。但模型会编,所以它**只出提案**:
# 每条都要带「正文原句」,人核过那句子真在正文里,才谈得上送进裁定。
_RULES = """你在做小说材料的交叉校对。只输出一个 JSON 数组,不要任何解释、不要 markdown 代码块。

每个元素:
{"项": "歧义最小的事实名,如 沈砚的惯用手", "正文": {"值": "一句话陈述正文怎么写的", "原句": "正文里的一句原文,逐字照抄"}, "设定": {"值": "设定怎么写的", "位置": "文件相对路径"}, "章纲": {"值": "章纲怎么写的", "位置": "文件相对路径"}}

硬性要求:
1. 只报**同一件事实两处说法不同**的情况。同一处只有一个说法的,不要报。
2. 「原句」必须是正文里**逐字出现**的片段(12-40 字),不许改写、不许拼接。找不到原句就不要报这一条。
3. 「设定」「章纲」里没有对应说法的层,整个键省略(不要写 null)。
4. 不要报文风、节奏、错别字、标点。只报事实层面的不一致。
5. 拿不准的不报。宁可少报,不许编。
"""


def extract_prompt(chapter: int, set_text: str, card_text: str, body: str) -> str:
    return (f"【第 {chapter} 章正文】\n{body}\n\n"
            f"【设定（世界观 / 人物 / 状态账本）】\n{set_text}\n\n"
            f"【章纲（卷纲 / 卡章纲）】\n{card_text}\n")


def parse_proposal(text: str) -> list[dict]:
    """宽松解析:模型常带 ```json 或解释。只认数组,认不出就空。"""
    if not text:
        return []
    t = text.strip()
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j <= i:
        return []
    import json

    try:
        rows = json.loads(t[i:j + 1])
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict) and (r.get("项") or "").strip()]


def _layer_text(root: Path, layer: str, limit: int = 6000) -> str:
    files = _layer_files(root)[layer]
    out = []
    for p in files:
        try:
            body = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if body:
            out.append(f"--- {p.relative_to(root).as_posix()} ---\n{body}")
    return "\n\n".join(out)[:limit]


def extract(root: Path | str, chapter: int, backend, *, body: str = "",
            max_chars: int = 12000) -> dict:
    """让 AI 读这一章的正文与设定/章纲,给一份冲突提案。**一个字节都不写**(落盘=False)。"""
    root = Path(root)
    if not body:
        p = paths.chapter_path(root, chapter)
        body = p.read_text(encoding="utf-8") if p.is_file() else ""
    if not body.strip():
        return {"ok": False, "reason": f"第 {chapter} 章没有正文,没有可读的东西。", "冲突": []}
    text = backend.complete(_RULES, extract_prompt(chapter, _layer_text(root, "设定", 6000),
                                                   _layer_text(root, "章纲", 3000), body[:max_chars // 2]),
                            max_chars=max_chars)
    rows = parse_proposal(text)
    out = []
    for n, r in enumerate(rows, 1):
        item: dict = {"序号": n, "项": str(r.get("项") or "").strip()}
        # 三层都要收:LAYERS 只有设定/章纲,正文得自己算进来 —— 漏了它,下面那句
        # 「没有正文那一层就当不了冲突」会把**每一条**都扔掉(写出 0 条还以为模型没抽到)。
        for L in LAYERS + ("正文",):
            v = r.get(L)
            if not isinstance(v, dict):
                continue
            val = str(v.get("值") or "").strip()
            if not val:
                continue
            item[L] = {"值": val, "位置": str(v.get("位置") or "").strip(), "行": 0}
        if "正文" not in item:
            continue                      # 没有正文那一层 = 没跟正文对上,不算冲突
        quote = str((r.get("正文") or {}).get("原句") or "").strip()
        item["原句"] = quote
        # 编出来的原句不许冒充证据:查不到就标出来,人一眼能看见
        item["原句在正文里"] = bool(quote) and quote in body
        # 「序号 + 项 + 正文」永远是 3 个键,拿 len() 判断会漏掉「只有正文一层」这种没得比的提案
        if not any(L in item for L in LAYERS):
            continue
        out.append(item)
    tail = ("" if rows else
            "模型这次没给出符合格式的数组(也可能它认为没有冲突)。它原文开头是:"
            + text.strip()[:200].replace("\n", " "))
    return {"ok": True, "章": chapter, "落盘": False, "冲突": out, "条数": len(out),
            "可交给裁定": [x["序号"] for x in out if x["原句在正文里"]],
            "模型原文长度": len(text or ""),
            "说明": ("这些只是提案,一个字也没写进裁定记录。原句在正文里查不到的那些,是模型编的可能性很大,"
                     "别拿它当证据。核过再把要的交给「冲突与裁定」按默认优先级处理。" + tail)}
