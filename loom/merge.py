"""R10 的「待合入」三栏:外部改稿时**不覆盖**,给一个能自己合的入口。

**这一支要解决的问题**:作者在编辑器里开着第 3 章,同时(或稍后)在别的编辑器/同步盘里改了盘上的
`正文/第3章.md`。平台如果保存时直接覆盖 = 对方的改动蒸发;如果拒绝保存 = 作者白写。两条都不行。

**所以给三栏**:`盘上版本` / `我这一版` / `合并结果`。合并用三路合并:
- **基线** = `正文/.原稿/第N章.md`(AI 原稿,平台自己写的参照物);没有原稿时退化成两路(基线=盘上)。
- 三路的好处:平台知道「哪部分是我当初给的、哪部分是你后改的」,不把两边都算成「别人的改动」。
- **冲突**(同一段两边都改了且改得不同)不猜、不自动选:标出来,由人定,合并结果里留可见标记。

**纪律**:`preview` 一个字节都不写;`apply_merge` 只在人点了「合入」后写,写前照例留快照(R19 的「可撤销」)。
"""

from __future__ import annotations

import difflib
from pathlib import Path

from . import paths
from .fsutil import atomic_write_text, snapshot_chapter

_MARK_OURS = "<<<<<<< 我这一版"
_MARK_BASE = "||||||| 基线"
_MARK_SPLIT = "======="
_MARK_THEIRS = ">>>>>>> 盘上版本"


def _hunks(base: list[str], other: list[str]) -> list[dict]:
    """other 相对 base 的改动块(坐标在 base 里):base[i1:i2] → other 的这几行。"""
    sm = difflib.SequenceMatcher(a=base, b=other, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append({"i1": i1, "i2": i2, "lines": other[j1:j2], "tag": tag})
    return out


def three_way(base_text: str, mine_text: str, theirs_text: str) -> dict:
    """三路合并。冲突不猜:标记出来,并逐条记下基线/我的/他的。"""
    base = base_text.splitlines(keepends=True)
    hs_mine = _hunks(base, mine_text.splitlines(keepends=True))
    hs_theirs = _hunks(base, theirs_text.splitlines(keepends=True))

    events: list[dict] = []
    for h in hs_mine:
        events.append({"i1": h["i1"], "i2": h["i2"], "mine": h["lines"], "theirs": None})
    for h in hs_theirs:
        events.append({"i1": h["i1"], "i2": h["i2"], "mine": None, "theirs": h["lines"]})
    events.sort(key=lambda e: (e["i1"], e["i2"]))

    merged: list[str] = []
    conflicts: list[dict] = []
    pos = 0
    i = 0
    while i < len(events):
        ev = events[i]
        group = [ev]
        j = i + 1
        while j < len(events) and events[j]["i1"] < max(g["i2"] for g in group):
            group.append(events[j])
            j += 1
        start = min(g["i1"] for g in group)
        end = max(g["i2"] for g in group)
        merged.extend(base[pos:start])
        base_seg = base[start:end]
        mine_e = [g for g in group if g["mine"] is not None]
        theirs_e = [g for g in group if g["theirs"] is not None]
        mine_seg = base[:0] + base_seg
        theirs_seg = base[:0] + base_seg
        if mine_e:
            mine_seg = [ln for g in mine_e for ln in g["mine"]]
        if theirs_e:
            theirs_seg = [ln for g in theirs_e for ln in g["theirs"]]
        if mine_e and theirs_e:
            if mine_seg == theirs_seg:
                merged.extend(mine_seg)                 # 两边改成一模一样:不算冲突
            else:
                conflicts.append({"行": start + 1, "基线": "".join(base_seg),
                                  "我的": "".join(mine_seg), "他的": "".join(theirs_seg)})
                merged.extend([_MARK_OURS + "\n"] + mine_seg
                              + [_MARK_BASE + "\n"] + base_seg
                              + [_MARK_SPLIT + "\n"] + theirs_seg + [_MARK_THEIRS + "\n"])
        elif mine_e:
            merged.extend(mine_seg)
        else:
            merged.extend(theirs_seg)
        pos = end
        i = j
    merged.extend(base[pos:])
    return {"合并": "".join(merged), "冲突": conflicts, "冲突数": len(conflicts)}


def _base_text(root: Path, n: int) -> tuple[str, str]:
    """基线:优先 AI 原稿;没有就退化成「盘上版本」(纯两路)。"""
    snap = paths.snapshot_path(root, n)
    if snap.is_file():
        return snap.read_text(encoding="utf-8"), "AI 原稿"
    disk = paths.chapter_path(root, n)
    if disk.is_file():
        return disk.read_text(encoding="utf-8"), "盘上版本"
    return "", "空"


def preview(root: Path | str, chapter: int, mine: str | None, *, base_text: str = "") -> dict:
    """三栏预览。**一个字节都不写。**

    `mine=None` 表示「我这一版没动」——等价于盘上版本。不能把空串当「把整章删空」,
    否则界面上第一次点「比对」就必出一条假冲突(审查 C 实测过)。
    """
    root = Path(root)
    disk = paths.chapter_path(root, chapter)
    theirs = disk.read_text(encoding="utf-8") if disk.is_file() else ""
    if mine is None:
        mine = theirs
    if base_text:
        base, base_from = base_text, "指定的基线"
    else:
        base, base_from = _base_text(root, chapter)
    r = three_way(base, mine, theirs)
    return {"ok": True, "章": chapter, "落盘": False,
            "基线来源": base_from,
            "盘上版本": theirs, "我这一版": mine, "合并结果": r["合并"],
            "冲突": r["冲突"], "冲突数": r["冲突数"],
            "一样吗": {"盘上=我的": theirs == mine,
                       "我的=合并": mine == r["合并"],
                       "盘上=合并": theirs == r["合并"]},
            "说明": ("两边没有冲突,合并结果就是两边的改动叠起来。" if not r["冲突数"]
                     else "有冲突的那几段没替你选,合并结果里用 <<<<<<< 标出来了,改完再落盘。")}


def apply_merge(root: Path | str, chapter: int, merged: str, *, expect_disk: str = "") -> dict:
    """把合并结果写回正文。**写前留快照**(R19 分类为「可撤销」),并核对盘上没在你眼皮底下又变。"""
    root = Path(root)
    disk = paths.chapter_path(root, chapter)
    if not disk.is_file():
        return {"ok": False, "reason": f"第 {chapter} 章没有正文,没地方合入。"}
    if expect_disk and disk.read_text(encoding="utf-8") != expect_disk:
        return {"ok": False, "code": "drifted",
                "reason": "盘上的正文在你比对之后又被人改过了。请重新比对一次再合," +
                          "现在写下去会把新改动盖掉。"}
    if not merged.strip():
        return {"ok": False, "reason": "合并结果是空的,不写。"}
    snapshot_chapter(root, paths.chapter_rel(chapter))     # 后悔药:写前快照
    atomic_write_text(disk, merged)
    return {"ok": True, "章": chapter, "字节": len(merged.encode("utf-8")),
            "可撤销": True, "说明": "已合入;想退回可在版本历史里恢复上一版。"}
