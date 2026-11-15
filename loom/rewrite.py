"""局部重写:选中一段,按写作指纹的嗓音 + 作者指令只重写这一段(整章作上下文)。

铁律(见 docs/design/局部重写.md):局部重写产出的是 AI 文本。learn 只学你的手改、绝不学 AI(ADR 0001)。
所以应用一次重写时,必须把这段 AI 改动【外科式同步进 .原稿 快照】,否则下次 learn 会把它当文风学进指纹。
不变量:快照永远 = "若全程只有 AI 生成、没人手打字" 的那一版。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import events
from .backends import Backend
from .fsutil import atomic_write_text, snapshot_chapter
from .paths import FINGERPRINT_REL, chapter_path, chapter_rel, snapshot_path

Progress = Callable[[dict], None]


def _noop(event: dict) -> None:
    pass


_REWRITE_SYSTEM = """你是作者本人的写手。我给你整章正文作上下文、作者选中要重写的一段、以及作者的指令。
按【写作指纹】的嗓音重写【选中段】,满足指令。只输出重写后的那一段本身,不要整章、不要任何解释或引号包裹。
默认不改剧情/人物/设定(除非指令明确要求);长度与原段相当;与前后文衔接自然。"""


def rewrite_span(project_root: Path, chapter_n: int, full_text: str, span: str,
                 instruction: str, backend: Backend, progress: Progress = _noop) -> str:
    if not span.strip():
        raise ValueError("没选中要重写的段落。先在正文里选一段。")
    fp_path = project_root / FINGERPRINT_REL
    fp = fp_path.read_text(encoding="utf-8") if fp_path.exists() else "(还没有写作指纹)"
    progress(events.info("正在按你的嗓音重写选中段…"))
    user = (
        f"## 写作指纹\n{fp}\n\n"
        f"## 整章上下文(供衔接,别整章输出)\n{full_text}\n\n"
        f"## 要重写的选中段\n{span}\n\n"
        f"## 作者指令\n{instruction.strip() or '(按你的嗓音改得更顺,别改剧情)'}"
    )
    return backend.complete(_REWRITE_SYSTEM, user, max_chars=len(span) + 300).strip()


def apply_rewrites(project_root: Path, chapter_n: int, new_content: str,
                   pairs: list[tuple[str, str]]) -> dict:
    """一次落盘**多条**修正(一键修多条:材料层的错 → 正文落地)。

    为什么必须批量(平台需求细化-v0.2.md §3.1 第二行短板):单个修正条目只覆盖一处,
    修 12 条就得 12 次「落盘 + 快照同步」——中间任何一次失败留下半修状态,而快照被同步
    12 次也更容易把 learn 的信号搅乱。这里改成:留一版快照 → 一次性写正文 → 一次性同步快照。

    返回 `{"applied": n, "missed": [...], "snapshot": "surgical"|"rebased"}`:
    - `missed` 是「正文里找不到的旧段」。**调用方必须把它摆给作者看**——它意味着这条修没落地
      (通常是因为上游 LLM 把原句改过一个标点)。静默忽略就是第二个「裸吞异常」。
    - `snapshot="rebased"` 表示旧段已不在快照里(作者手改过那段),这版快照退化为整章基线:
      只丢 learn 信号,不污染(ADR-0001)。
    """
    out = chapter_path(project_root, chapter_n)
    snap = snapshot_path(project_root, chapter_n)

    seen: set[str] = set()
    todo: list[tuple[str, str]] = []
    for old, new in pairs:
        if not old or old == new or old in seen:
            continue
        seen.add(old)
        todo.append((old, new))
    missed: list[str] = []
    applied = 0
    body = new_content
    for old, new in todo:
        if old in body:
            body = body.replace(old, new, 1)
            applied += 1
        else:
            missed.append(old)

    snapshot_chapter(project_root, chapter_rel(chapter_n))  # 应用前留一版,误替换可整章回滚
    atomic_write_text(out, body.rstrip() + "\n")
    snap_state = "surgical"
    if snap.exists():
        s = snap.read_text(encoding="utf-8")
        # 全部旧段都还在快照里 → 逐条精准替换(其余手改部分仍是原 AI 版,learn 照常看见你的手改);
        # 有任何一条不在(你刚手改过这段)→ 退化为整章作新 AI 基线(只丢信号,不污染)。
        if todo and all(old in s for old, _ in todo):
            s2 = s
            for old, new in todo:
                s2 = s2.replace(old, new, 1)
        else:
            s2 = body.rstrip() + "\n"
            snap_state = "rebased"
        atomic_write_text(snap, s2)
    return {"applied": applied, "missed": missed, "snapshot": snap_state}


def apply_rewrite(project_root: Path, chapter_n: int, new_content: str,
                  old_span: str, new_span: str) -> None:
    """落盘正文 + 外科式同步快照(单条形态;批量请用 `apply_rewrites`)。守住 learn 不被 AI 重写污染。"""
    apply_rewrites(project_root, chapter_n, new_content, [(old_span, new_span)])
