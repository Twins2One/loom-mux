# -*- coding: utf-8 -*-
r"""联动提示层（丙 · 固定工作流）的判定与记账。

作者 2026-10-20 裁定的形态：
- **① settings**：设定集变了 → 提醒大纲师
- **② final**：一章写完 → 提醒批评家
- **③ card**：卡章纲变了 → 提醒重算下一章细纲
- **③b outline**：卷纲/全书大纲变了 → 问要不要重排「这一块」章纲

三条硬边界（同 docs/角色联动设计-待审查.md）：
1. **这里不发任何模型请求**。判定全部是「本机文件内容 sha256 比对」，所以在这一层
   「本地模型 function calling 不可靠」根本不适用——模型不参与判断。
2. **只读稿子，只写 `.loom_state.json` 的 `chain_seen` 一个键**。绝不写设定集/正文/大纲。
3. **读端点不写盘**：`check()` 纯只读；要记账只有 `ack()` / `baseline()` 两个显式入口。
   这是对交接文档「提醒过就更新 hash」的一处刻意偏离（理由见设计文档 §九 偏离 1）。

四个键同一形状 `{files: {相对路径: 16 位 sig}, at: ...}`（final 多一个 `chapter`），
所以比对/记账只有一份代码。整键可丢弃：删了只会「安静一会儿」，下次 `need_baseline`
重建基线，不影响 `learned` / `fingerprint_source` / `journey`，更不影响任何稿子。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import paths
from .state import load_state, save_state

#: 四个联动键（顺序 = UI 横条上的建议顺序：设定 → 写完章 → 卡章纲 → 卷纲）
KEYS = ("settings", "final", "card", "outline")

#: 分块单位：与「铺章纲 / 实体词典 / 大纲符合度复核」同一个 50 章，绝不另造一套。
#: （selfcheck-chains.py 会断言它等于 align.BLOCK，防两边漂。）
BLOCK = 50

#: 设定集里**不算「设定变了」**的两类东西（排掉的理由见设计文档 §3.1）：
#: - `90-速记本/`：各角色私有工作记录，不是设定；不排就是「每聊一次天提示一次」
#: - `成长档案.md`：learn 的 [AI补充] 自动落点，每章都变，不代表作者裁断
_EXCLUDE_DIRS = {"90-速记本", "速记本"}
_EXCLUDE_NAMES = {"成长档案.md"}

#: 卡章纲一行：`- 第8章:一句话`（与 align._card_rows / volplan 的落盘形状一致）
_CARD_ROW_RE = re.compile(r"^\s*[-*]\s*第\s*(\d+)\s*章\s*[:：]\s*(.*)$")


# ── 指纹 ──────────────────────────────────────────────────────────────────
def _sig(p: Path) -> str:
    """文件内容指纹：sha256 前 16 位。

    用途是「这份文件还是不是上次那份」，不是密码学，16 位够且让状态文件保持人可读。
    """
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _strip(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _file_map(root: Path, rels: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in rels:
        p = root / rel
        try:
            if p.is_file():
                out[rel] = _sig(p)
        except OSError:      # 读不动就当没有：宁可少提示，不许把整条链搞崩
            continue
    return out


# ── 四个键的「现在长什么样」 ───────────────────────────────────────────────
def settings_map(root: Path | str) -> dict[str, str]:
    """设定集递归 .md → sig。排除 90-速记本 / 成长档案 / 点目录。"""
    root = Path(root)
    base = root / paths.SET_DIR
    if not base.is_dir():
        return {}
    out: dict[str, str] = {}
    try:
        files = sorted(base.rglob("*.md"))
    except OSError:
        return {}
    for p in files:
        try:
            parts = p.relative_to(root).parts
        except ValueError:      # 符号链接指到书外：不算本书设定
            continue
        if any(part in _EXCLUDE_DIRS or part.startswith(".") for part in parts):
            continue
        if p.name in _EXCLUDE_NAMES:
            continue
        try:
            out[_strip(root, p)] = _sig(p)
        except OSError:
            continue
    return out


def card_map(root: Path | str) -> dict[str, str]:
    """卡章纲（一章一行）。文件不存在 → {}。"""
    return _file_map(Path(root), [paths.CARD_REL])


def outline_map(root: Path | str) -> dict[str, str]:
    """卷纲 + 全书大纲（两个一起算，它们的动作都是「重排这一块章纲」）。"""
    return _file_map(Path(root), [paths.VOLUMES_REL, paths.OVERVIEW_REL])


def final_map(root: Path | str) -> dict[str, str]:
    """**最新一章**的正文（只这一个文件，不是整个 正文/ 目录）。"""
    root = Path(root)
    nums = paths.chapter_numbers(root)
    if not nums:
        return {}
    return _file_map(root, [paths.chapter_rel(nums[-1])])


def _entry(root: Path, key: str) -> dict:
    now = time.strftime("%Y-%m-%d %H:%M")
    if key == "settings":
        return {"files": settings_map(root), "at": now}
    if key == "card":
        return {"files": card_map(root), "at": now}
    if key == "outline":
        return {"files": outline_map(root), "at": now}
    if key == "final":
        nums = paths.chapter_numbers(root)
        return {"files": final_map(root), "chapter": nums[-1] if nums else 0, "at": now}
    raise ValueError(f"未知的联动键:{key!r}(只认 {' / '.join(KEYS)})")


def snapshot(root: Path | str) -> dict[str, dict]:
    """四个键的当前状态（baseline / ack 的落盘内容都是它的一个子集）。"""
    root = Path(root)
    return {k: _entry(root, k) for k in KEYS}


# ── 比对 ──────────────────────────────────────────────────────────────────
def _diff(old: dict[str, str], cur: dict[str, str]) -> dict:
    added = sorted(k for k in cur if k not in old)
    removed = sorted(k for k in old if k not in cur)
    modified = sorted(k for k in cur if k in old and cur[k] != old[k])
    changed = sorted(set(added) | set(removed) | set(modified))
    return {"added": added, "modified": modified, "removed": removed, "changed": changed,
            "counts": {"added": len(added), "modified": len(modified), "removed": len(removed)}}


def block_of(chapter_n: int, span: int = BLOCK) -> tuple[int, int]:
    """第 n 章落在哪一块（与 align.block_range 同算法，selfcheck 里有等价性断言）。"""
    n = max(1, int(chapter_n))
    span = max(1, int(span))
    lo = (n - 1) // span * span + 1
    return lo, lo + span - 1


def _card_rows(root: Path) -> dict[int, str]:
    """卡章纲已写的章行（只有**有内容**的算写完；空行算缺）。"""
    p = root / paths.CARD_REL
    if not p.is_file():
        return {}
    out: dict[int, str] = {}
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _CARD_ROW_RE.match(line)
            if m and m.group(2).strip():
                out[int(m.group(1))] = m.group(2).strip()
    except OSError:
        return {}
    return out


def _seen(st: dict) -> dict:
    """`chain_seen` 的容错读：不是 dict 一律当没有（同 journey._journey 的范式）。"""
    s = st.get("chain_seen")
    return s if isinstance(s, dict) else {}


def check(root: Path | str) -> dict:
    """**纯只读**：现在该提示哪些链。绝不写盘。"""
    root = Path(root)
    st = load_state(root)
    seen = _seen(st)
    need_baseline = any(not isinstance(seen.get(k), dict) for k in KEYS)

    hints: list[dict] = []
    if not need_baseline:
        # ① 设定集
        d = _diff((seen.get("settings") or {}).get("files") or {}, settings_map(root))
        if d["changed"]:
            hints.append({"key": "settings", "count": len(d["changed"]),
                          "files": d["changed"][:5], "changed": d["counts"]})

        # ② 最新一章写完（反向闸：删章导致章号变小 → 不提示）
        nums = paths.chapter_numbers(root)
        old_ch = int((seen.get("final") or {}).get("chapter") or 0)
        if nums:
            cur_ch = nums[-1]
            d2 = _diff((seen.get("final") or {}).get("files") or {}, final_map(root))
            if cur_ch > old_ch or (cur_ch == old_ch and d2["changed"]):
                hints.append({"key": "final", "chapter": cur_ch})

        # ③ 卡章纲变了 → 下一章细纲建议重算
        d3 = _diff((seen.get("card") or {}).get("files") or {}, card_map(root))
        if d3["changed"]:
            hints.append({"key": "card", "next_chapter": (nums[-1] + 1) if nums else 1})

        # ③b 卷纲/全书大纲变了 → 问要不要重排「这一块」章纲
        d4 = _diff((seen.get("outline") or {}).get("files") or {}, outline_map(root))
        if d4["changed"]:
            nxt = (nums[-1] + 1) if nums else 1
            lo, hi = block_of(nxt)
            rows = _card_rows(root)
            missing = [n for n in range(lo, hi + 1) if n not in rows]
            hints.append({
                "key": "outline",
                "block": {"起": lo, "止": hi, "正在写": nxt},
                "refill": {"可用": bool(missing), "缺行数": len(missing),
                           "原因": "" if missing else
                           "这块章纲已写满;铺章纲只补空行、不覆盖你写过的行,要整块重来得先手删"},
            })

    order = {k: i for i, k in enumerate(KEYS)}
    hints.sort(key=lambda h: order.get(h["key"], 99))
    checked = {}
    for k in KEYS:
        e = seen.get(k) if isinstance(seen.get(k), dict) else {}
        checked[k] = {"files": len(e.get("files") or {}), "at": e.get("at") or ""}
    return {"ok": True, "need_baseline": need_baseline, "hints": hints, "checked": checked}


# ── 记账（唯一两个写入口）──────────────────────────────────────────────────
def baseline(root: Path | str) -> bool:
    """第一次检查时建基线：**四键缺哪个补哪个**，已有的绝不动。

    返回 True = 这次真写了；False = 四键本来就齐（幂等）。
    """
    root = Path(root)
    st = load_state(root)
    seen = _seen(st)
    if all(isinstance(seen.get(k), dict) for k in KEYS):
        return False
    for k in KEYS:
        seen.setdefault(k, _entry(root, k))
    st["chain_seen"] = seen
    save_state(root, st)
    return True


def ack(root: Path | str, key: str) -> None:
    """作者点了「知道了」/ 点了该链的动作 → 只把那**一个**键记成「现在这样」。

    不抢书写锁（设计文档 §4.5）：抢锁会让「写作中点知道了」直接 409；丢失更新的方向是良性的
    （最坏 = 同一条提示下次再出现一次），绝不会碰 `learned` 或任何稿子。
    """
    if key not in KEYS:
        raise ValueError(f"未知的联动键:{key!r}(只认 {' / '.join(KEYS)})")
    root = Path(root)
    st = load_state(root)
    seen = _seen(st)
    seen[key] = _entry(root, key)
    st["chain_seen"] = seen
    save_state(root, st)
