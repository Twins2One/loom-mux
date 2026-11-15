"""R18 导出(可做的那一半):TXT / MD × 整书合并 / 分卷 / 分章。

**PDF 这一支没做**:选哪个引擎是作者的裁定(待裁定清单 Q3),我不先斩后奏。
传 `pdf` 会得到一个明确的拒绝,而不是一个用错引擎生成的半成品。

**三条纪律**:
- **只读稿子**:导出只读正文,不改任何原文(自检里比对整棵树)。
- **不覆盖**:同名文件已存在就整批拒绝,不偷偷加序号、不覆盖人以前的导出。
- **分批**:1,200 章按卷切成一份份,不会一次性把全书塞进一个几十兆的文件里。
  卷还没分(`budget.volumes` 为空)时按「整书」导一份,并在结果里说明为什么没分卷。
"""

from __future__ import annotations

from pathlib import Path

from . import budget, paths
from .fsutil import atomic_write_text

FORMATS = ("txt", "md", "pdf")
SPLITS = ("书", "卷", "章")
EXPORT_DIR = "导出"
# 作者 2026-10-18 裁定:「打印成 PDF」。所以不装任何引擎,生成一份专供打印的 HTML,
# 由浏览器自己排版、自己「另存为 PDF」。代价说清楚:页码/字体跟浏览器走,不是印刷级排版。
_PDF_HOWTO = ("已按作者裁定走「浏览器打印成 PDF」。已生成打印稿(见「打印页」):用浏览器打开它,"
              "Ctrl+P →目标选「另存为 PDF」即可。不装引擎,所以页码与字体跟浏览器一致,不是印刷级排版。")
_PRINT_CSS = """
@page { size: A4; margin: 22mm 20mm; }
body { font: 11pt/1.85 "Songti SC", "SimSun", "Noto Serif CJK SC", serif; color:#111; }
h1 { font-size: 20pt; text-align:center; margin: 0 0 8mm; }
h2 { font-size: 13pt; margin: 0 0 4mm; page-break-after: avoid; }
p  { margin: 0 0 .55em; text-indent: 2em; }
.chapter { page-break-before: always; }
.chapter:first-of-type { page-break-before: auto; }
.meta { color:#666; text-align:center; font-size: 9pt; margin-bottom: 10mm; }
"""


def _chapter_files(root: Path) -> list[tuple[int, Path]]:
    out = []
    for n in paths.chapter_numbers(root):
        p = paths.chapter_path(root, n)
        if p.is_file():
            out.append((n, p))
    return out


def _split_title(text: str, n: int) -> tuple[str, str]:
    """拆成 (标题, 正文)。正文通常自带 H1:标题取出来**当导出文件的章节标题用**,
    既不会重复贴两次,也不会把作者写的标题丢掉。"""
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("#").strip() or f"第{n}章"
        return title, "\n".join(lines[1:]).lstrip("\n")
    return f"第{n}章", text


def _block(n: int, text: str, fmt: str) -> str:
    title, body = _split_title(text, n)
    if fmt == "md":
        return f"## {title}\n\n{body.strip()}\n"
    return f"\n\n{title}\n\n{body.strip()}\n"


def plan(root: Path | str, *, fmt: str = "txt", split: str = "书") -> dict:
    """先出方案(不写盘):会生成几个文件、各含哪些章。

    **每个文件都带一份显式章号清单 `章号`**(2026-10-21 修)。
    以前只记 `起/止` 区间,而「卷外章节」那种**不连续**的集合一旦跨过某个卷的区间,
    导出时按区间取章就会把别人的章也塞进来 —— 真机复现:全书 10 章导出合计 12 章,
    `卷外章节.txt` 里 5、6 两章重复(它们本来在第 2 卷)。
    现在写盘只认 `章号`;`起/止` 仅作展示用(以及给老调用方兜底)。
    """
    root = Path(root)
    if fmt == "pdf":
        p = plan(root, fmt="md", split=split)     # 打印稿按 md 的切分走,只是渲染成 HTML
        if not p.get("ok"):
            return p
        for f in p["文件"]:
            f["文件名"] = f["文件名"].rsplit(".", 1)[0] + ".打印稿.html"
        p["格式"] = "pdf"
        p["方式"] = "打印"
        p["怎么变 PDF"] = _PDF_HOWTO
        return p
    if fmt not in FORMATS:
        return {"ok": False, "reason": f"不认识的格式:{fmt}(只认 {' / '.join(FORMATS)})"}
    if split not in SPLITS:
        return {"ok": False, "reason": f"不认识的切分:{split}(只认 {' / '.join(SPLITS)})"}
    chs = _chapter_files(root)
    if not chs:
        return {"ok": False, "reason": "这本书还没有正文,没什么可导的。"}
    vols = budget.volumes(root) if split == "卷" else []
    files: list[dict] = []
    if split == "章":
        for n, _p in chs:
            files.append({"文件名": f"第{n}章.{fmt}", "起": n, "止": n, "章号": [n]})
    elif split == "卷" and vols:
        used: set[int] = set()          # 卷起止重叠时,同一章只进第一份(否则合计会比实际章数多)
        overlapped = 0
        for i, (start, end, label) in enumerate(vols, start=1):
            span = [n for n, _ in chs if start <= n <= end]
            overlapped += len([n for n in span if n in used])
            picked = [n for n in span if n not in used]
            used.update(picked)
            if not picked:
                continue
            name = (label or f"第{i}卷").replace("/", "_").replace("\\", "_")
            files.append({"文件名": f"第{i}卷-{name}.{fmt}", "起": min(picked), "止": max(picked),
                          "章号": picked})
        covered = used
        rest = [n for n, _ in chs if n not in covered]
        if rest:      # 卷纲没盖到的章另存一份,绝不悄悄丢掉
            files.append({"文件名": f"卷外章节.{fmt}", "起": min(rest), "止": max(rest),
                          "章号": rest,
                          "说明": "这些章不在任何一卷的起止范围内"})
    else:
        allnums = [n for n, _ in chs]
        files.append({"文件名": f"全书.{fmt}", "起": allnums[0], "止": allnums[-1],
                      "章号": allnums})
    note = ""
    if split == "卷" and not vols:
        note = "卷纲里还没有能切分的卷,已按整书导出一份。"
    elif split == "卷" and overlapped:
        note = (f"卷纲里有 {overlapped} 章被两卷同时覆盖,已只放进靠前那一卷"
                "(免得同一章出现两次)。")
    return {"ok": True, "格式": fmt, "切分": split, "文件": files, "章数": len(chs),
            "说明": note, "落盘": False}


def export(root: Path | str, *, fmt: str = "txt", split: str = "书",
           out_dir: str = "") -> dict:
    """真导:写进 导出/ 目录(或指定目录)。**同名就整批拒绝**,不覆盖。"""
    root = Path(root)
    pl = plan(root, fmt=fmt, split=split)
    if not pl.get("ok"):
        return pl
    dest = (root / out_dir) if out_dir else (root / EXPORT_DIR)
    # out_dir 不许把文件写书外面去(绝对路径 / .. 都拦);不然既会写到别人的盘,还会在
    # relative_to() 上抛 ValueError 把整个请求打成 500(审查 B 的 P1)。
    try:
        dest.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return {"ok": False, "code": "out_of_book",
                "reason": f"导出目录「{out_dir}」跑到书外面去了。请填书内的相对路径(例如 导出-上卷)。"}
    # 也不许写进受保护的位置(.原稿 / .历史 / .回收站 / 写作指纹):往 .原稿 里放导出文件,
    # 等于伪造一份「AI 原稿」快照,learn 的基线会被污染(交叉审查 P1)。
    from . import guardrail as _gr
    _canon = dest.resolve().relative_to(Path(root).resolve()).as_posix()
    _why = _gr.protected(_canon)
    if _why:
        return {"ok": False, "code": "protected",
                "reason": f"导出目录「{out_dir}」是受保护位置,不能往那里写。" + _why}
    targets = [dest / f["文件名"] for f in pl["文件"]]
    clash = [t.name for t in targets if t.exists()]
    if clash:
        return {"ok": False, "code": "exists",
                "reason": "这些文件已经存在,不覆盖:" + "、".join(clash[:3])
                          + "。请换个导出目录,或自己先把旧的挪走。"}
    chs = dict(_chapter_files(root))
    dest.mkdir(parents=True, exist_ok=True)
    if fmt == "pdf":
        return _export_print(root, pl, chs, dest)
    written = []
    for f, t in zip(pl["文件"], targets):
        # 只认 plan 给的**显式章号清单**;区间仅作兜底(老调用方/手写方案才没有 章号)。
        nums = f.get("章号") or [n for n in chs if f["起"] <= n <= f["止"]]
        if not nums:
            continue
        if pl["切分"] == "章":
            text = _block(nums[0], chs[nums[0]].read_text(encoding="utf-8"), fmt).strip() + "\n"
        elif fmt == "md":
            text = "".join(_block(n, chs[n].read_text(encoding="utf-8"), fmt) for n in nums)
        else:
            text = "".join(_block(n, chs[n].read_text(encoding="utf-8"), fmt) for n in nums).lstrip()
        atomic_write_text(t, text)
        written.append({"文件": t.name, "章数": len(nums), "字节": t.stat().st_size})
    return {"ok": True, "目录": str(dest.relative_to(root)), "写出": written,
            "合计章数": sum(w["章数"] for w in written), "说明": pl["说明"]}


def _html_escape(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _print_html(title: str, blocks: list[tuple[int, str, str]]) -> str:
    """把 (章号, 标题, 正文) 拼成一页页的打印稿。正文按空行分段,段落首行缩进由 CSS 管。"""
    out = [f"<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
           f"<title>{_html_escape(title)}</title><style>{_PRINT_CSS}</style></head><body>",
           f"<h1>{_html_escape(title)}</h1>"]
    for n, head, body in blocks:
        out.append(f'<section class="chapter"><h2>{_html_escape(head or f"第{n}章")}</h2>')
        for para in [p for p in body.split("\n") if p.strip()]:
            out.append(f"<p>{_html_escape(para.strip())}</p>")
        out.append("</section>")
    out.append("</body></html>")
    return "\n".join(out)


def _export_print(root: Path, pl: dict, chs: dict, dest: Path) -> dict:
    """打印稿:每份 HTML 由人用浏览器打印成 PDF。**只写目标文件**,不碰稿子。"""
    title = "全书" if pl["切分"] == "书" else dest.name
    written = []
    for f in pl["文件"]:
        # 同 export():只认显式章号清单,区间仅兜底 —— 否则「卷外章节」那种不连续集合会重复收章。
        nums = f.get("章号") or [n for n in chs if f["起"] <= n <= f["止"]]
        if not nums:
            continue
        blocks = []
        for n in nums:
            head, body = _split_title(chs[n].read_text(encoding="utf-8"), n)
            blocks.append((n, head, body))
        t = dest / f["文件名"]
        atomic_write_text(t, _print_html(title, blocks))
        written.append({"文件": t.name, "章数": len(nums), "字节": t.stat().st_size,
                        "怎么变 PDF": "浏览器打开 → Ctrl+P → 另存为 PDF"})
    return {"ok": True, "目录": str(dest.relative_to(root)), "写出": written,
            "合计章数": sum(w["章数"] for w in written),
            "方式": "打印", "说明": _PDF_HOWTO}
