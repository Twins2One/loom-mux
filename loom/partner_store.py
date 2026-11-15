"""伙伴对话存储:一条对话一个 当前.jsonl,单行 append、坏行跳过。

上下文不是状态:删整个 .伙伴对话/ 目录,书完好无损(门禁/完成度从书文件推导)。
读回带 errors="replace" 兜 GBK(人可改文件);坏行 try/except 跳过(同 ledger「坏就当无」)。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import paths

_HEAD_RE = re.compile(r"^#{1,6}[ \t]*", re.M)      # Markdown 标题记号
_QUOTE_RE = re.compile(r"^[ \t]*(?:>[ \t]*)+", re.M)  # 行首引用记号（`> `，可嵌套 `> > `）
_HR_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)  # 整行分隔线
_MARK_RE = re.compile(r"\*\*|__|`+|^[ \t]*[-*+][ \t]+", re.M)   # 粗体/行内码/列表符
_WS_RE = re.compile(r"\s+")


def brief(text: str, limit: int = 40) -> str:
    """提案正文 → 单行摘要（2026-10-27 对方复核提出）。

    `content` 是 Markdown 长文，直接截前 40 字会把 `##`、`###`、空行一起截进来，
    角标卡片上就变成「## 卷一情感线（三轴·六段）」。所以先剥记号、压成单行再截。

    剥的记号：标题 `#`、引用 `>`、分隔线 `---`、粗体 `**`、行内码 `` ` ``、列表符。
    只剥**行首/成对**记号，不碰正文里的 `3 > 2` 这类正常字符。
    **只影响显示，不动落盘内容。**
    """
    s = str(text or "")
    s = _HEAD_RE.sub("", s)
    s = _QUOTE_RE.sub("", s)
    s = _HR_RE.sub("", s)
    s = _MARK_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _cur(root: Path) -> Path:
    return root / paths.PARTNER_CUR_REL


def append_event(root: Path, event: dict) -> None:
    p = _cur(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(root: Path, *, tail: int | None = None) -> list[dict]:
    p = _cur(root)
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue   # 坏行跳过,坏就当无
        if isinstance(ev, dict):
            out.append(ev)
    return out[-tail:] if tail else out


def find_proposal(root: Path, pid: str) -> dict | None:
    """按提案 id 找 proposal 事件。

    两个地方都要找（作者 2026-10-20 裁定 4）：
    - 书房伙伴：`.伙伴对话/当前.jsonl`
    - 作者智库对话：`.伙伴对话/agent-<角色>.jsonl`（每个角色一份）
    曾只找前者，于是角色对话出的候选卡一点「落库」就报「提案已过期」。
    """
    for ev in reversed(read_events(root)):   # 从近到远找
        if ev.get("t") == "proposal" and ev.get("id") == pid:
            return ev
    d = root / paths.PARTNER_DIR
    if d.is_dir():
        files = sorted(d.glob("agent-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(ev, dict) and ev.get("t") == "proposal" and ev.get("id") == pid:
                    return ev
    return None


def archive_current(root: Path, stamp: str) -> None:
    p = _cur(root)
    if p.is_file():
        p.rename(p.with_name(f"归档-{stamp}.jsonl"))


def all_events(root: Path | str) -> list[dict]:
    """**全部**事件：`当前.jsonl`（领航员）+ 每个 `agent-<角色>.jsonl`（智库一对一）。

    2026-10-27 为「书房伙伴角标」而加：待拍板的卡可能出自任何一条通道，
    只读 `当前.jsonl` 会漏掉角色对话里出的卡。坏行一律跳过（同 read_events 口径）。
    """
    root = Path(root)
    out: list[dict] = list(read_events(root))
    d = root / paths.PARTNER_DIR
    if not d.is_dir():
        return out
    files = sorted(d.glob("agent-*.jsonl"), key=lambda p: p.stat().st_mtime)
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(ev, dict):
                out.append(ev)
    return out


def pending_proposals(root: Path | str) -> list[dict]:
    """还没被拍板的候选卡，**新→旧**。

    判据只有一条：有 proposal、且没有**同 id** 的 confirm 事件。
    （已落库/已作废的卡不该继续挂在那里；归档走 `归档-*.jsonl`，不在这里。）
    """
    evs = all_events(root)
    done = {e.get("id") for e in evs if e.get("t") == "confirm"}
    props = [e for e in evs
             if e.get("t") == "proposal" and e.get("id") and str(e["id"]) not in done]
    props.reverse()
    return props
