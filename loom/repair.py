"""R16 报告两维 + R17 两条修法(AI 修的执行端)。

**报告的两个维度**(早期需求访谈的第三问的落地):

| 维度 | 取值 | 来源 |
|---|---|---|
| 严重度 | 高 / 中 / 低 | `BugItem.stars`(4-5 高,3 中,1-2 低) |
| 能不能自动修 | 能 / 需人工 | 有 `fix` **且**证据能在正文里唯一定位成一句 |

「能不能自动修」不看模型有多自信,只看**锚点能不能唯一定位**——定不到就老实说需人工。
这一条是整个 AI 修的地基:锚错了会改掉读者正在读的那句。

**两条修法**:
① **AI 修** = `plan()` 出方案 → `apply()` 落盘。落盘走 `rewrite.apply_rewrites`(外科式同步 `.原稿`),
   这是防止 learn 把 AI 的改动当成人手改的唯一合法路径;撤销沿用 `snapshot_chapter` + 历史恢复。
② **人工修** = 前端把「修改示例」灌进局部重写面板(桥早就存在),本模块不参与。

**三条纪律**:
- 只改**正文里能唯一定位的那一句**;定位不到/多处命中 → 一律拒绝并说明原因(不猜)。
- 两条建议落在同一句上 → **拒绝整批并点名**,不做「先后替换」的隐式排序(否则改哪句取决于列表顺序)。
- 报告是**纯确定性**的:不调 LLM、不写留痕(除非显式要 `note=True`),两次扫描结果逐字相同。
"""

from __future__ import annotations

import time

import re
from pathlib import Path

from . import paths, rewrite, statebook

# 句界:与 continuity._SENT_SPLIT 同源(那边是编译好的正则,这里按字符集合判界,不重复引入循环依赖)
_SENT_END = "。!?！？…"


def severity_of(stars: int) -> str:
    """严重度三档。4-5 星=高,3 星=中,1-2 星=低(0 星按低处理,不崩)。"""
    if stars >= 4:
        return "高"
    if stars == 3:
        return "中"
    return "低"


def _sentence_bounds(body: str, evidence: str) -> tuple[int, int] | None:
    """把证据短引扩成「所在那一句」的区间。**必须唯一定位**,否则返回 None。"""
    if not evidence:
        return None
    first = body.find(evidence)
    if first < 0:
        return None
    if body.find(evidence, first + 1) >= 0:      # 多处命中 → 歧义
        return None
    start = first
    while start > 0 and body[start - 1] not in _SENT_END and body[start - 1] != "\n":
        start -= 1
    end = first + len(evidence)
    while end < len(body) and body[end] not in _SENT_END and body[end] != "\n":
        end += 1
    if end < len(body) and body[end] in _SENT_END:
        end += 1                                  # 把句号收进来(替换时一起换掉)
    return start, end if end > start else None


def span_of(body: str, evidence: str) -> tuple[int, int] | None:
    """公开版:返回 (start, end)。给自检与 UI 高亮用。"""
    return _sentence_bounds(body, evidence)


def deterministic_items(root: Path | str, chapter: int, body: str):
    """只跑四条确定性检测(不调 LLM)。与 continuity.scan_chapter 的确定性部分同口径。"""
    from . import continuity

    root = Path(root)
    book_p = root / paths.STATEBOOK_REL
    book = statebook.parse_book(book_p.read_text(encoding="utf-8")) if book_p.is_file() else {}
    names = continuity._char_names_from_dir(root)
    det = continuity.merge_items(
        continuity.merge_items(continuity.detect_consumed_reuse(book, chapter, body),
                               continuity.detect_rule_drift(book, chapter, body)),
        continuity.detect_time_mismatch(book, chapter, body))
    det = continuity.merge_items(det, continuity.detect_char_continuity(book, chapter, body, names))
    # R3 知情越界:账本里明写「第 K 章时某人还不知道某事」,而这一章提到了它 → 也进两维报告。
    # 依据是账本里人写的记录(不是猜),所以和上面四条一样是确定性的;没记录就不报。
    from . import facts as _facts

    for b in _facts.bounds_any(root, chapter, body):
        det.append(continuity.BugItem(3, "知情", b["问题"], evidence=b["不该知道"],
                                      prior=f"第{b['记录章']}章记录:{b['人物']} 不知道 {b['不该知道']}",
                                      target="正文",
                                      fix=""))
    return det


REPORT_NOTE_HEAD = "## 审稿留痕 · 报告与修复(R16,两维报告)"   # 与 continuity.REPAIR_NOTE_HEAD 同源


def report(root: Path | str, chapter: int, *, body: str = "", note: bool = False) -> dict:
    """生成报告(两维 + 证据 + 锚点)。**不调模型、不改正文**;
    `note=True` 时把报告落一份到 `.审稿留痕`(R16 —— 报告不阻断不等于不留痕)。"""
    root = Path(root)
    if not body:
        p = paths.chapter_path(root, chapter)
        if not p.is_file():
            return {"ok": False, "reason": f"第 {chapter} 章还没有正文。", "条目": []}
        body = p.read_text(encoding="utf-8")

    rows: list[dict] = []
    for i, it in enumerate(deterministic_items(root, chapter, body), start=1):
        span = _sentence_bounds(body, it.evidence)
        auto = bool(it.fix) and span is not None
        why = ""
        if not it.fix:
            why = "没有修改示例,只能人工决定"
        elif span is None:
            why = "证据句在正文里定位不到唯一一句(可能已改过,或这串字出现多次)"
        rows.append({
            "序号": i,
            "严重度": severity_of(it.stars),
            "星": it.stars,
            "类别": it.kind,
            "问题": it.desc,
            "本章证据": it.evidence,
            "前情证据": it.prior,
            "落点": it.target,
            "修改示例": it.fix,
            "能自动修": auto,
            "修不了的原因": why,
            "锚点": list(span) if span else None,
            "旧句": body[span[0]:span[1]] if span else "",
        })
    high = sum(1 for r in rows if r["严重度"] == "高")
    out = {"ok": True, "章": chapter, "落盘": False, "条目": rows,
           "合计": len(rows), "高": high, "可自动修": sum(1 for r in rows if r["能自动修"]),
           "说明": "确定性扫描:不调模型、不改正文。同一条在两次扫描里判定一致。",
           "生成于": time.strftime("%Y-%m-%d %H:%M:%S")}
    if note:
        try:
            from .continuity import note_repair_report
            p = note_repair_report(root, chapter, out)
            out["落盘"] = True
            out["留痕"] = str(p)
            out["说明"] += " 本次报告已落 .审稿留痕。"
        except Exception as e:       # noqa: BLE001 - 留痕失败不该让报告本身失败
            out["留痕失败"] = f"{type(e).__name__}: {e}"
            out["说明"] += " 留痕没写成功(见 留痕失败),报告内容本身有效。"
    return out


def plan(root: Path | str, chapter: int) -> dict:
    """AI 修的方案:每条挑出一条「旧句 → 新句」。与报告同源,只是多带可执行性。"""
    rep = report(root, chapter)
    if not rep.get("ok"):
        return rep
    steps = []
    for r in rep["条目"]:
        if not r["能自动修"]:
            continue
        steps.append({"序号": r["序号"], "旧句": r["旧句"], "新句": r["修改示例"],
                      "严重度": r["严重度"], "锚点": r["锚点"], "问题": r["问题"]})
    return {"ok": True, "章": chapter, "步骤": steps,
            "需人工": [r["序号"] for r in rep["条目"] if not r["能自动修"]],
            "落盘": False,
            "说明": "这是方案,还没改一个字。要落地请调 apply。"}


def _conflicts(pairs: list[tuple[str, str]]) -> list[str]:
    """同一批里有没有两条落在同一句上(旧句相同/有包含关系)。"""
    out: list[str] = []
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            a, b = pairs[i][0], pairs[j][0]
            if a == b or (a and b and (a in b or b in a)):
                out.append(f"第 {i + 1} 条与第 {j + 1} 条落在同一句上")
    return out


def apply(root: Path | str, chapter: int, picks: list | None = None) -> dict:
    """落地 AI 修。`picks` 是序号列表(用方案里的「修改示例」),或 [{序号, 新句}] 覆盖。

    落盘走 `rewrite.apply_rewrites`:一次写正文 + 一次外科式同步 `.原稿`(防 learn 污染)。
    **任何一条不成立就整批拒绝**——半修状态比不修更难收拾。
    """
    if not picks:
        return {"ok": False, "reason": "没挑选要修哪几条(picks 为空)。"}
    root = Path(root)
    body_p = paths.chapter_path(root, chapter)
    if not body_p.is_file():
        return {"ok": False, "reason": f"第 {chapter} 章还没有正文。"}
    body = body_p.read_text(encoding="utf-8")
    pl = plan(root, chapter)
    if not pl.get("ok"):
        return pl

    by_no = {s["序号"]: s for s in pl["步骤"]}
    chosen: list[dict] = []
    for pick in picks:
        if isinstance(pick, dict):
            no = pick.get("序号")
            override = (pick.get("新句") or "").strip()
        else:
            no, override = pick, ""
        step = by_no.get(no)
        if step is None:
            return {"ok": False, "reason": f"第 {no} 条不在可自动修的清单里(它需要人工修)。",
                    "可自动修": sorted(by_no)}
        chosen.append({"序号": no, "旧句": step["旧句"],
                       "新句": override or step["新句"], "锚点": step["锚点"]})

    pairs = [(c["旧句"], c["新句"]) for c in chosen if c["旧句"] != c["新句"]]
    if not pairs:
        return {"ok": False, "reason": "挑的这几条新旧一样,没什么可改的。"}
    clash = _conflicts(pairs)
    if clash:
        return {"ok": False, "reason": "；".join(clash) + "。请一批只改同一句的一处,或分两批。",
                "冲突": clash}

    for old, _new in pairs:
        if old not in body:
            return {"ok": False, "reason": f"旧句已不在正文里(可能刚被人改过):「{old[:20]}…」",
                    "rebase": True}
    # 替换交给 apply_rewrites 做:它边替换边统计 applied/missed,并同步 .原稿。
    # 以前这里先自己替换一遍,再把它传过去 —— 那边找不到旧句,回报就成了
    # 「applied=0、missed=全部」,与「正文确实改对了」互相矛盾(交叉审查 B-P1-1)。
    res = rewrite.apply_rewrites(root, chapter, body, pairs)
    return {"ok": True, "章": chapter, "改了": len(pairs),
            "applied": res.get("applied"), "missed": res.get("missed"),
            "snapshot": res.get("snapshot"),
            "可撤销": "用版本历史回滚(snapshot_chapter 已留旧版)"}
