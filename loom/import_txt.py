# -*- coding: utf-8 -*-
"""R26 半截稿逆推 · 第一步:切章认卷。

**这一步纯本地、零模型调用、一个字都不改原文。** 它只回答三个问题:
1. 这份 TXT 里有多少章?章号连不连号?缺哪几号?
2. 认没认出卷?(认出就在卷纲里按起止落;认不出就按长度分块,让人自己改)
3. 每章多少字?标题是什么?

为什么先做这一步:逆推整件事里,**只有切章是对错二值的**。切错了,后面抽章纲抽得再好
也是错的;而切章不需要花钱,所以必须先把这一步做到能让人一眼看出对错。

铁律:
- 原文**一个字不动**(不顺手改标点、不删空行、不把「的\"换成「的」)。
- 认不出就用长度切,并**明说**「这是按长度切的,章号是我给的」——不假装认出来了。
"""
from __future__ import annotations

import re
from pathlib import Path

# 第1章 / 第 12 章 / 第十二章 / 第12回 / Chapter 12
_CH = re.compile(
    r"^[ \t　]*(?:第[ \t]*([0-9０-９零〇一二三四五六七八九十百千两]{1,9})[ \t]*([章回节])"
    r"|Chapter[ \t]+(\d{1,5})|CHAPTER[ \t]+(\d{1,5}))[ \t:：、.．]?[ \t]*(.*)$"
)
# 第1卷 / 第一卷 / 卷一 / 上部 / 中部 / 下部
_VOL = re.compile(
    r"^[ \t　]*(?:第[ \t]*([0-9０-９零〇一二三四五六七八九十百千两]{1,9})[ \t]*卷"
    r"|卷[ \t]*([0-9０-９零〇一二三四五六七八九十百千两]{1,9})"
    r"|第?([上中下])(?:部|册))[ \t:：、.．]?[ \t]*(.*)$"
)

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9}
_UNIT = {"十": 10, "百": 100, "千": 1000}

DEFAULT_CHUNK = 3000          # 认不出章标记时按字数切


# 正文里的句子常以这些字接着章号走:「第3章的伏笔」「第2章是转折」「第1章里他……」
# 这些**不是**章标题。误判的代价很实在:一本 5 章的书会被切成 10 章(每句一个"章")。
_PROSE_TAIL = ("的", "是", "里", "中", "后", "前", "上", "下", "时", "得", "地",
               "了", "着", "过", "也", "就", "还", "又", "更", "最")


def _looks_like_title(line: str, title: str) -> bool:
    """判断「第N章 xxx」这一行到底是章标题,还是正文里的一句叙述。"""
    t = (line or "").strip()
    title = (title or "").strip()
    # 「上」「下」「中」这类单字卷/章标题是常见的,不能因为第一字在 _PROSE_TAIL 里就判成正文。
    if len(title) <= 2 and not t.endswith(("。", "！", "？", "!", "?")):
        return True
    if title[:1] in _PROSE_TAIL:
        return False
    if len(t) > 8 and t[-1:] in "。！？!?":
        return False            # 标题几乎不会以句号结尾
    return True


def cn_to_int(s: str) -> int:
    """'12' / '十二' / '一百零三' / '１２'(全角) → int。认不出返回 0。"""
    s = (s or "").strip().translate({ord("０") + i: ord("0") + i for i in range(10)})
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in _CN:
            cur = _CN[ch]
        elif ch in _UNIT:
            u = _UNIT[ch]
            total += (cur or 1) * u
            cur = 0
        else:
            return 0
    return total + cur


def write_chapters(root, chunks: list[dict], also_original: bool = True,
                   skip_empty: int = 30) -> dict:
    """把切好的章落成 `正文/第N章.md`,**逐字照抄,一个字都不改**。

    四条铁律(都被审查员抓到过一次,别改回去):
    - **本批内部重号 = 整批拒绝**:同一份 TXT 里第 2 章出现两次时,后一个会原子盖掉前一个,
      字就没了,而返回还报「落了 3 章」。这种稿子必须先让人处理,不猜。
    - **书里已有的章跳过、不覆盖**:早先做成「整批拒绝」,结果抽章纲失败一次之后再导就被
      永久堵死(正文已进书 ⇒ 每次都撞车)。现在逐章判断:已有的跳过并如实报出来。
    - **原稿不覆盖**:`正文/.原稿/第N章.md` 是护栏的「禁止」级位置、也是 learn/merge 的基线,
      已经存在就一个字都不动。
    - **疑似目录不落**:正文不到 `skip_empty` 字的章单列出来,不擅自当成章。
    """
    from . import chapterstate as _cs
    from . import paths as _paths
    from .fsutil import atomic_write_text

    root = Path(root)
    body_dir = root / _paths.BODY_DIR
    body_dir.mkdir(parents=True, exist_ok=True)
    if also_original:
        (body_dir / ".原稿").mkdir(parents=True, exist_ok=True)

    seen, dup, real, tiny, empty = {}, [], [], [], []
    for c in chunks:
        raw_n = c.get("号")
        if raw_n is None or raw_n == "":
            continue
        n = int(raw_n)          # 0 号 = 楔子/序章(裁定 2026-10-19 B),它是合法章号
        body = c.get("正文") or ""
        # 第 0 章(楔子)另算:它已经过 analyze 的「成篇」判据,再用 30 字的目录门槛卡它,
        # 短的楔子会被当成目录页丢掉(裁定 2026-10-19 B:楔子是单独一章)。
        _floor = 1 if n == 0 else skip_empty
        if len("".join(body.split())) < _floor:
            (tiny if "".join(body.split()) else empty).append(n)
            continue
        if n in seen:
            dup.append(n)
            continue
        seen[n] = True
        real.append((n, body))
    if dup:
        return {"ok": False, "落了几章": 0, "跳过": [], "已存在": [], "重复": sorted(set(dup))[:20],
                "疑似目录": tiny + empty,
                "说明": f"这份稿子里第 {sorted(set(dup))[0]} 章出现了不止一次,先整批不落(免得后一份盖掉前一份)"}
    if not real:
        return {"ok": False, "落了几章": 0, "跳过": [], "已存在": [], "重复": [],
                "疑似目录": tiny + empty,
                "说明": "切出来的章都是空的或太短(像是目录页),没有落盘"}

    wrote, skipped, 原稿保, _stamp_failed = [], [], 0, []
    for n, body in real:
        if (root / _paths.chapter_rel(n)).exists():
            skipped.append(n)          # 书里已经有这一章:跳过,绝不覆盖
            continue
        text = body if body.endswith("\n") else body + "\n"
        atomic_write_text(root / _paths.chapter_rel(n), text)
        wrote.append(n)
        # 落材料基线(交叉审查 P2-1):不基线的话,逆推出来的书 R9 整体失效 ——
        # 所有章都无基线 ⇒ dirty_now 恒空,"改了设定要复核"永远不提醒。
        try:
            _cs.stamp(root, n)
        except Exception as e:      # noqa: BLE001 - 基线没落成不该毁掉已经落好的正文
            _stamp_failed.append(f"第{n}章:{type(e).__name__}")
        if also_original:
            op = root / _paths.BODY_DIR / ".原稿" / f"第{n}章.md"
            if op.exists():
                原稿保 += 1            # 原稿已在:一个字都不动
            else:
                atomic_write_text(op, text)
    parts = [f"落了 {len(wrote)} 章正文"]
    if _stamp_failed:
        parts.append(f"{len(_stamp_failed)} 章的基线没落成({','.join(_stamp_failed[:3])})")
    if skipped:
        parts.append(f"书里已有的 {len(skipped)} 章跳过没动")
    if 原稿保:
        parts.append(f"{原稿保} 章原稿已存在,没覆盖")
    return {"ok": bool(wrote), "落了几章": len(wrote), "跳过": skipped[:20], "已存在": skipped[:20],
            "重复": [], "疑似目录": tiny + empty, "说明": ",".join(parts),
            "基线没落成": _stamp_failed[:10]}


def read_text(path: Path | str) -> tuple[str, str]:
    """读 TXT,返回 (正文, 使用的编码)。国内 txt 大多是 GBK/GB18030,不许猜错就抛。"""
    p = Path(path)
    raw = p.read_bytes()
    # UTF-16 必须看 BOM 先判:gb18030 几乎能解任意字节,排在它后面等于永远轮不到。
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") and len(raw) > 3:
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    # UTF-16 **无 BOM** 的稿子:gb18030 几乎能解任何字节,如实解下去就是一堆乱码进正文。
    # 判据不是「有没有 0 字节」(纯中文的 UTF-16 里几乎没有),而是**哪个解出来更像小说**:
    # 正常中文文本里,汉字/ASCII/中文标点/空白应占到 90% 以上;乱码会掉到 90% 以下。
    def _looks_like_prose(t: str) -> bool:
        if not t:
            return False
        ok = 0
        for ch in t:
            o = ord(ch)
            if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF    # 汉字
                    or 0x20 <= o <= 0x7E                              # 可见 ASCII
                    or o in (0x0A, 0x0D, 0x09)                        # 换行制表
                    or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF  # 中文标点/全角
                    or o in (0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x2014)):
                ok += 1
        return ok / len(t) >= 0.9

    if len(raw) >= 16 and len(raw) % 2 == 0:
        for _enc in ("utf-16-le", "utf-16-be"):
            try:
                _t = raw.decode(_enc)
            except UnicodeDecodeError:
                continue
            if "\x00" in _t:
                continue
            try:
                _g = raw.decode("gb18030")
            except UnicodeDecodeError:
                _g = ""
            if _looks_like_prose(_t) and not _looks_like_prose(_g):
                return _t, _enc + "(无 BOM,按「像不像中文」判的)"

    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(有坏字节,已替换)"


def analyze(text: str, chunk: int = DEFAULT_CHUNK) -> dict:
    """切章 + 认卷。返回结构给人看,不写盘。"""
    lines = text.splitlines()
    chapters: list[dict] = []
    volumes: list[dict] = []
    cur = None

    for idx, line in enumerate(lines):
        vm = _VOL.match(line)
        if vm and len(line.strip()) <= 30:
            num = cn_to_int(vm.group(1) or vm.group(2) or "")
            name = (vm.group(4) or "").strip()
            if vm.group(3):
                # 「上部/中部/下部」本身不带数字。用「上=1、中=2、下=3」会和「第一卷」撞号,
                # 所以按已认出的卷数往下接:只有上中下的书 → 1,2,3;混着写 → 接着上一卷排。
                num = len(volumes) + 1
                name = name or f"{vm.group(3)}部"
            if num:
                volumes.append({"号": num, "名": name or f"第{num}卷", "起始行": idx + 1})
                continue
        cm = _CH.match(line)
        if cm and len(line.strip()) <= 60:
            num = cn_to_int(cm.group(1) or cm.group(3) or cm.group(4) or "")
            if num and not _looks_like_title(line, cm.group(5) or ""):
                num = 0          # 看着像正文的一句叙述,不当章标题
            if num:
                title = (cm.group(5) or "").strip()
                if cur is not None:
                    chapters.append(cur)
                cur = {"号": num, "标题": title, "起始行": idx + 1, "行": [line]}
                continue
        if cur is not None:
            cur["行"].append(line)
    if cur is not None:
        chapters.append(cur)

    # 楔子/序章:第一个章标记之前的文字。成篇(≥30 字,去掉标题行)才单独成章,
    # 否则书名/作者行也会被当成「第 0 章」(裁定 2026-10-19 B:单独成章,但要真是一篇)。
    prologue = None
    if chapters:          # 只有一章的书也可能有序章(≥1 个章标记就够)
        head = lines[: chapters[0]["起始行"] - 1]
        body_lines = _prologue_body(head)
        # 有「楔子/序章」这种标题的,十来字也认(人已经明说了它是一章);
        # 没标题的要求 ≥30 字,免得书名/作者行/版权页被当成第 0 章。
        _need = 10 if _prologue_title(head) else 30
        _chars = len("".join("".join(body_lines).split()))      # 去掉空白数**字符**(不是词的个数)
        if _chars >= _need:
            prologue = {"号": 0, "标题": _prologue_title(head) or "楔子",
                        "字数": _chars,
                        "行数": len(head), "样例": " ".join(x.strip() for x in body_lines)[:40]}

    by_marker = len(chapters) >= 2
    if not by_marker:
        chapters = _split_by_length(lines, chunk)

    out = []
    for c in chapters:
        body = "\n".join(c["行"]) if isinstance(c.get("行"), list) else c.get("正文", "")
        out.append({"号": c["号"], "标题": c.get("标题", ""),
                    "字数": len("".join(body.split())), "起始行": c["起始行"]})

    nums = [c["号"] for c in out]
    expected = list(range(min(nums), max(nums) + 1)) if nums else []
    missing = [n for n in expected if n not in set(nums)]
    dup = sorted({n for n in nums if nums.count(n) > 1})

    return {"楔子": prologue,
            "章前字数": _prologue_len(text),
            "章前样例": _prologue_sample(text), 
        "切法": "按章标记" if by_marker else f"按长度(每 {chunk} 字一刀)",
        "章数": len(out),
        "总字数": sum(c["字数"] for c in out),
        "章": out,
        "缺号": missing[:50],
        "重号": dup[:20],
        "卷数": len(volumes),
        "卷": volumes,
        "认出卷标记": bool(volumes),
    }


def _split_by_length(lines: list[str], chunk: int) -> list[dict]:
    """认不出章标记时的兜底:按字数切。**明说这是切的,不是原文有的。**"""
    chapters: list[dict] = []
    buf: list[str] = []
    count = 0
    num = 1
    start = 1
    for i, line in enumerate(lines):
        if not buf:                 # 每开新的一段都重新记起始行(原来只认第一段)
            start = i + 1
        buf.append(line)
        count += len("".join(line.split()))
        if count >= chunk:
            chapters.append({"号": num, "标题": "", "起始行": start, "行": buf})
            num += 1
            buf, count = [], 0
    if buf:
        chapters.append({"号": num, "标题": "", "起始行": start, "行": buf})
    return chapters


# 楔子 / 序章 / 引子 / 前言 / 序 —— 有的书写「楔子」有的写「序章」,都算**单独一章**(裁定 2026-10-19 B)
_PRO = re.compile(
    r"^[ \t　]*(?:楔[ \t]*子|序[ \t]*章|引[ \t]*子|前[ \t]*言|序[ \t]*言|序|尾声|后记|"
    r"Prologue|PROLOGUE)[ \t:：、.．]?[ \t]*(.*)$"
)


def _prologue_title(head: list[str]) -> str:
    """章前那几行里,第一行若是「楔子/序章…」就当标题用。"""
    for line in head[:3]:
        t = line.strip()
        if not t:
            continue
        m = _PRO.match(t)
        if m:
            return t[:40]
        return ""      # 第一行不是标题:说明是别的字,别硬编标题
    return ""


def _prologue_body(head: list[str]) -> list[str]:
    """去掉标题行、去掉空行之后的正文行。"""
    out = []
    for i, line in enumerate(head):
        if i < 3 and _PRO.match(line.strip()) and not out:
            continue
        out.append(line)
    return [x for x in out if x.strip()]


def _prologue_lines(txt: str) -> list[str]:
    """第一个章标记**之前**的那些行。成书 TXT 里通常是楔子/序章/作者的话。"""
    out = []
    for line in txt.splitlines():
        if _CH.match(line.strip()):
            break
        out.append(line)
    return out


def _prologue_len(txt: str) -> int:
    return len("".join("".join(_prologue_lines(txt)).split()))


def _prologue_sample(txt: str) -> str:
    t = " ".join(x.strip() for x in _prologue_lines(txt) if x.strip())
    return t[:40]


def chapter_texts(text: str, chunk: int = DEFAULT_CHUNK) -> list[dict]:
    """切完连正文一起给(给下一步「逐章抽章纲」用)。正文与原文逐字一致。"""
    info = analyze(text, chunk=chunk)
    lines = text.splitlines()
    if info["切法"] != "按章标记":
        raw = _split_by_length(lines, chunk)
        return [{"号": c["号"], "标题": c.get("标题", ""), "正文": "\n".join(c["行"]).strip()}
                for c in raw]
    # 按标记切:从每章起始行到下章起始行之前
    starts = [c["起始行"] for c in info["章"]]
    out = []
    if info.get("楔子"):
        head = lines[: starts[0] - 1]
        body = "\n".join(_prologue_body(head)).strip()
        out.append({"号": 0, "标题": info["楔子"]["标题"], "正文": body})
    for i, c in enumerate(info["章"]):
        end = starts[i + 1] - 1 if i + 1 < len(starts) else len(lines)
        body = "\n".join(lines[c["起始行"] - 1:end]).strip()
        out.append({"号": c["号"], "标题": c["标题"], "正文": body})
    return out


def preview(path: Path | str, chunk: int = DEFAULT_CHUNK) -> dict:
    """给 UI 的预览:只报数,不落盘。"""
    text, enc = read_text(path)
    info = analyze(text, chunk=chunk)
    info["编码"] = enc
    info["文件"] = str(path)
    info["字节"] = Path(path).stat().st_size
    return info
