"""R5 材料冲突仲裁:设定 > 章纲 > 正文,但**裁定不外推**。

**作者 2026-10-18 的裁定(原文)**:
「设定大于章纲大于正文,但当人作出不同裁定后,其他所有冲突项必须请示每处是否更改。」

这句话里有两半,缺一半都会出事:
- **前半**给默认优先级:同一件事三处写得不一样时,以设定为准,其次章纲,最后正文。
- **后半**给**边界**:人一旦对某处判了「这次以正文为准」,平台**不得**把这条判定外推到别的冲突项上
  ——每一处都要**单独问**。否则一次例外会把整本书的口径悄悄改掉,而作者根本不知道自己批准了什么。

**所以本模块分两步**:
1. `arbitrate(conflicts)`——只算「按默认优先级以谁为准」+ 指向**具体文件与行**,不写任何稿子。
2. `plan(root, conflicts)`——查「这本书有没有人作过不同裁定」:有 → 其它冲突项一律标「需请示」,
   一次也不自动改;要改必须逐项带上确认。

落盘只有一处:`外置大脑/裁定记录.md`(人作的例外裁定),`arbitrate` 与 `plan` 都不写。
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .fsutil import atomic_write_text

OVERRIDE_REL = paths.brain_rel("裁定记录")
LAYERS = ("设定", "章纲", "正文")
_RANK = {"设定": 1, "章纲": 2, "正文": 3}

_HEAD = """# 裁定记录

> 材料互相矛盾时,默认以 **设定 > 章纲 > 正文** 为准(作者 2026-10-18 裁定)。
> 人一旦在这里记下一条「这一处以 …
为准」的例外,**其它冲突项不会被自动外推**,平台会逐处请示。
>
> | 项 | 以谁为准 | 理由 | 记于 |
> |---|---|---|---|
"""


def _cell(v: str) -> str:
    return (v or "").replace("|", "／").replace("\n", " ").strip()


def load_overrides(root: Path | str) -> list[dict]:
    """读人作过的例外裁定。坏行跳过。"""
    p = Path(root) / OVERRIDE_REL
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("项",) or set(cells[0]) <= {"-"}:
            continue
        out.append({"项": cells[0], "以谁为准": cells[1], "理由": cells[2], "记于": cells[3]})
    return out


def add_override(root: Path | str, item: str, layer: str, *, reason: str = "") -> dict:
    """记一条例外裁定。**只记不执行**:记完由 plan 逐处请示。"""
    if layer not in LAYERS:
        return {"ok": False, "reason": f"不认识的材料层:{layer}(只认 {' / '.join(LAYERS)})"}
    if not (item or "").strip():
        return {"ok": False, "reason": "得说是哪一项(例如「主角姓名」)。"}
    rows = load_overrides(root)
    if any(r["项"] == item and r["以谁为准"] == layer for r in rows):
        return {"ok": True, "已存在": True, "条数": len(rows)}
    import time

    rows.append({"项": item.strip(), "以谁为准": layer, "理由": _cell(reason),
                 "记于": time.strftime("%Y-%m-%d %H:%M")})
    p = Path(root) / OVERRIDE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, _HEAD + "".join(
        f"| {_cell(r['项'])} | {r['以谁为准']} | {_cell(r['理由'])} | {r['记于']} |\n" for r in rows))
    return {"ok": True, "项": item, "以谁为准": layer, "条数": len(rows)}


def _where(entry: dict) -> str:
    """把「具体行」拼成人能去点的坐标:文件:行。"""
    pos = entry.get("位置") or ""
    line = entry.get("行")
    if pos and line:
        return f"{pos}:{line}"
    return pos or (f"第{line}行" if line else "未给位置")


def arbitrate(conflict: dict) -> dict:
    """按默认优先级算「以谁为准」。**纯函数、不写盘、不改稿。**"""
    item = (conflict.get("项") or "").strip() or "未命名冲突"
    layers = [L for L in LAYERS if isinstance(conflict.get(L), dict) and conflict.get(L)]
    if not layers:
        return {"项": item, "以谁为准": None, "各处": [], "要改的层": [],
                "reason": "这条冲突没给任何一层的具体内容,我判断不了。"}
    if len(layers) == 1:
        return {"项": item, "以谁为准": layers[0], "各处": [{"层": layers[0],
                "值": conflict[layers[0]].get("值", ""), "位置": _where(conflict[layers[0]])}],
                "要改的层": [], "reason": "只有一处写了这件事,没有冲突。"}
    winner = min(layers, key=lambda L: _RANK[L])
    values = {L: (conflict[L].get("值") or "").strip() for L in layers}
    agree = len(set(values.values())) == 1
    return {"项": item, "以谁为准": winner,
            "各处": [{"层": L, "值": values[L], "位置": _where(conflict[L])} for L in layers],
            # 只改「与胜出层不一致」的那些层:章纲本来就和设定一样,就不该把它列进待改
            "要改的层": [] if agree else [L for L in layers
                                        if L != winner and values[L] != values[winner]],
            "一致": agree,
            "reason": ("三处一致,不用仲裁。" if agree else
                       f"按「设定 > 章纲 > 正文」,以{winner}为准;要改的是其余 "
                       f"{len([L for L in layers if L != winner])} 层。")}


def plan(root: Path | str, conflicts: list[dict]) -> dict:
    """逐条给「以谁为准 / 要改哪层 / 要不要请示」。**落盘=False,一个字都不改。**

    关键:书里只要有过**任一**例外裁定,其它冲突项就全部标「需请示」(作者裁定的后半句)。
    """
    root = Path(root)
    ovs = load_overrides(root)
    over_by_item = {r["项"]: r for r in ovs}
    items = []
    for c in conflicts or []:
        base = arbitrate(c)
        item = base["项"]
        need_ask = False
        note = ""
        if item in over_by_item:
            ov = over_by_item[item]
            base["以谁为准"] = ov["以谁为准"]
            base["要改的层"] = [x["层"] for x in base["各处"] if x["层"] != ov["以谁为准"]]
            base["依据"] = f"人对这一项作过例外裁定(以{ov['以谁为准']}为准):{ov['理由'] or '未写理由'}"
            note = "这一项已有例外裁定,按裁定执行。"
        elif ovs:
            need_ask = True
            note = ("这本书已有例外裁定,所以这一处**不自动改**:请逐处确认是否按默认优先级"
                    "(设定 > 章纲 > 正文)改。")
        items.append({**base, "需请示": need_ask, "说明": note})
    return {"ok": True, "落盘": False, "条数": len(items), "条目": items,
            "有例外裁定": bool(ovs), "例外裁定": ovs,
            "口径": "默认 设定 > 章纲 > 正文;有例外裁定后,其余冲突逐处请示,不外推。"}

