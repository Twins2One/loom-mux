"""R14 建议:带锚点的**只读**诊断(早期需求访谈的第三问「给出建议但不改稿」的落地)。

**它和 R16/R17 的分工**:

| | 报告(R16) | 建议(R14) | AI 修(R17) |
|---|---|---|---|
| 会不会调模型 | 不调(纯确定性) | **调一次**(复审员) | 调(回炉) |
| 会不会改盘 | 不会 | **绝不会** | 会(要人挑) |
| 条目的锚 | 证据句 → 正文区间 | 同左,外加 `(章, 版本, 选区hash)` | 同左 |

**每条建议带三样东西**:章号、版本号(正文体归一后的 sha12)、选区 hash。
版本一变 → 这条建议立刻 stale 并**禁止应用**——否则人改了那段之后,旧建议会把别处的字改坏。

**为什么不提供「应用建议」**:建议是复审员的一次性判断,不是可回放的补丁。
要落地只有两条路:走 R17 的 AI 修(带冲突检测与外科式同步),或人工修。这个口子不开口子。

**本模块一个字都不写盘**:没有 atomic_write、没有 snapshot、没有 apply_rewrites(自检里有源码断言)。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from . import gates, paths
from .chaptertext import body_key


def _noop(event: dict) -> None:
    pass


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def version_of(root: Path | str, chapter: int) -> str:
    """这一章的版本号 = 正文体(去掉标题)的 sha12。**改标题不算新版本**(与 learn 同口径)。"""
    p = paths.chapter_path(root, chapter)
    if not p.is_file():
        return ""
    return _h(body_key(p.read_text(encoding="utf-8")))


def anchor(body: str, evidence: str):
    """证据短引 → 正文里的句子区间。与 R17 用同一把尺(repair.span_of),唯一定位才算数。"""
    from . import repair

    return repair.span_of(body, evidence)


def build(items: list, body: str, chapter: int, version: str) -> dict:
    """把复审员的 issue 清单变成带锚点的建议。纯函数,不碰盘。"""
    rows = []
    for i, it in enumerate(items, start=1):
        span = anchor(body, getattr(it, "evidence", ""))
        rows.append({
            "序号": i,
            "章": chapter,
            "版本": version,
            "类别": getattr(it, "kind", ""),
            "问题": getattr(it, "desc", ""),
            "证据": getattr(it, "evidence", ""),
            # 诊断台按这几个键渲染(审查 C:原来只给「证据」,前端读不到 → 每条都渲染成 "[]")
            "本章证据": getattr(it, "evidence", ""),
            "严重度": str(getattr(it, "severity", "") or getattr(it, "星", "") or ""),
            "能自动修": False,        # 建议永不自动应用;要改走 AI 修或人工修
            "修改示例": getattr(it, "hint", "") or "",
            "锚点": list(span) if span else None,
            "选区": _h(body[span[0]:span[1]]) if span else "",
            "能应用": False,          # 建议永远不能直接应用(见模块头)
        })
    return {"ok": True, "章": chapter, "版本": version, "条目": rows, "合计": len(rows),
            "落盘": False,
            "说明": "建议只诊断、不落盘。要改请走 AI 修(要先挑条目)或人工改。"}


def stale(item: dict, version: str) -> bool:
    """版本变了 → 这条建议作废。**宁严不宽**:版本拿不到(空)也算作废。"""
    return (not version) or item.get("版本") != version


def annotate(items: list[dict], version: str) -> list[dict]:
    """给每条建议盖「是否已作废」的章。UI 拿这个决定灰不灰。"""
    out = []
    for it in items:
        row = dict(it)
        row["stale"] = stale(it, version)
        out.append(row)
    return out

def _gate_of(role: str) -> tuple[str, str, str, list[str], bool]:
    """借「编辑」那道闸的复审员来做诊断(同一份提示词,同一套判据)。"""
    from .agents import _GATES

    for key in (role, "编辑"):
        if key in _GATES:
            return _GATES[key]
    raise KeyError(f"没有可用的复审闸:{role}")


def advise(root: Path | str, chapter: int, backend, *, body: str = "",
           progress: Callable[[dict], None] = _noop, rounds: int = 1,
           knowledge: str = "") -> dict:
    """跑一次只诊断的建议。`rounds=1` = 只要清单,不回炉(gates 的既定语义)。

    **废弃 gres.text**:run_gate 会返回一份(可能被回炉过的)稿,这里**绝不**把它写盘。
    """
    root = Path(root)
    if not body:
        p = paths.chapter_path(root, chapter)
        if not p.is_file():
            return {"ok": False, "reason": f"第 {chapter} 章还没有正文。", "条目": []}
        body = p.read_text(encoding="utf-8")

    label, critic, revise, reads, wants_prev = _gate_of("编辑")
    from . import agents as _agents

    gk = _agents._read_files(root, reads, _noop) if reads else ""
    if wants_prev and chapter > 1:
        prev = paths.chapter_path(root, chapter - 1)
        if prev.is_file():
            gk += "\n\n【上一章章末】\n" + prev.read_text(encoding="utf-8")[-800:]
    if knowledge:
        gk += "\n\n" + knowledge

    # R23:有平台合规模板才拼带;没有就是一个空串,提示词与从前逐字节相同。
    from . import review_template as _rt

    _extra = _rt.extra_critic(root)
    if _extra:
        critic = critic.rstrip() + "\n\n" + _extra
    gres = gates.run_gate(backend, label=label, owner_role="编辑", critic_system=critic,
                          revise_system=revise, draft=body, knowledge=gk, produces="正文",
                          rounds=max(1, rounds), max_chars=len(body) or 2000,
                          progress=progress, critic_backend=None, detector=None)
    rep = build(list(gres.remaining), body, chapter, version_of(root, chapter))
    rep["闸"] = label
    rep["轮"] = gres.rounds
    return rep
