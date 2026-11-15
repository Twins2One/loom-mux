"""书房 · 知识顾问的最小可用后半段：把一份稿子整理成笔记并落进「个人图书馆」。

：「知识顾问最小可用，取稿到落笔全通」。
四步里前两步（选视频 / 取稿）在界面侧完成——本地文件或粘贴都由浏览器读，
所以这里只做需要花钱的两步：

    ③ 理解：把稿子交给模型，整理成可回查的笔记（**一次调用**）
    ④ 沉淀：写进 `知识库/素材/<科目>/<标题>.md`（重名加后缀，**绝不覆盖**）

边界：
- **只写素材层**，不碰教材/语料库，也不碰任何书的目录。
- **不落对话、不留中间态**：模型返回什么就写什么，失败不写半截文件。
- **一次请求一次调用**，由作者在界面上点头后才发起（调用方负责确认）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from .library import library_root

MAX_TEXT_CHARS = 20000        # 一份稿子最多带这么多字（约 10k token，够一整集）
MAX_TITLE_CHARS = 40
DEFAULT_SUBJECT = "取稿笔记"

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')     # 文件名里不能出现的字符


def _safe_name(value: str, *, limit: int = MAX_TITLE_CHARS, default: str = "未命名") -> str:
    """标题/科目 → 可用作文件名的安全名。非法字符换成 `_`，两头空白去掉，空则用默认名。"""
    s = _BAD.sub("_", str(value or "")).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    if not s:
        s = default
    return s[:limit]


def material_dir(subject: str) -> Path:
    return library_root() / "素材" / _safe_name(subject, default=DEFAULT_SUBJECT)


def free_path(subject: str, title: str) -> Path:
    """挑一个不撞的落点：同名就 `标题(2).md`、`标题(3).md`……**绝不覆盖**。"""
    d = material_dir(subject)
    base = _safe_name(title)
    target = d / f"{base}.md"
    n = 2
    while target.exists():
        target = d / f"{base}({n}).md"
        n += 1
    return target


SYSTEM = """你是资料整理员。作者给你一段视频字幕/转写稿，你把它整理成一份**以后还能用**的笔记。

要求：
1. 只根据这份稿子写，不许补充稿子里没有的事实、书名、链接、时间戳。稿子里没讲清楚的，
   写进「存疑」一节，别猜。
2. 输出 Markdown，结构固定为：
   # <一句话标题>
   ## 一句话
   ## 要点            （3~7 条，每条一行；稿子里给了时间点的，把时间点带上）
   ## 可复用          （以后写小说能直接拿去用的判据/做法，2~4 条）
   ## 存疑            （没讲清或自相矛盾的地方；没有就写「无」）
3. 说人话，别堆术语；不复述原文，要提炼。
4. 不涉及付费、不改任何文件——你只输出这份笔记的正文。"""


def _build_user(title: str, text: str, source: str) -> str:
    head = f"【这份稿子的标题】{title or '未命名'}"
    if source:
        head += f"\n【来源】{source}"
    return f"{head}\n\n【稿子】\n{text}"


from ..request_turn import tool_commit


@tool_commit
def save_note(title: str, subject: str, text: str) -> dict:
    """把整理好的笔记写到素材层。返回 {ok, 路径, 字数}；写不了给 {ok:False, reason}。"""
    body = (text or "").strip()
    if not body:
        return {"ok": False, "reason": "笔记是空的，没写。"}
    target = free_path(subject, title)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "reason": f"写不进素材目录：{e}"}
    return {"ok": True, "路径": str(target), "字数": len(re.sub(r"\s+", "", body))}


def ingest(title: str, text: str, emit: Callable[[dict], None], *,
           subject: str = DEFAULT_SUBJECT, source: str = "",
           backend: Any = None, should_cancel: Callable[[], bool] | None = None) -> None:
    """③ 理解 → ④ 沉淀。事件：status / delta / done / error（与书房伙伴同构）。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    raw = (text or "").strip()
    if not raw:
        emit({"t": "error", "ts": ts, "code": "empty_text", "text": "稿子是空的，先把字幕或转写贴进来。"})
        return
    if len(raw) > MAX_TEXT_CHARS:
        raw = raw[:MAX_TEXT_CHARS]
        emit({"t": "status", "ts": ts, "text": f"稿子太长，只用了前 {MAX_TEXT_CHARS} 字。"})

    if backend is None:
        try:
            from ..backends import get_backend
            from .ask import studio_config
            backend = get_backend(studio_config())
        except Exception as e:      # noqa: BLE001 - 没接模型是常态
            emit({"t": "error", "ts": ts, "code": "no_backend", "text": str(e)})
            return

    emit({"t": "status", "ts": ts, "text": f"正在整理（{len(raw)} 字），一次模型调用…"})
    chunks: list[str] = []

    def _on_chunk(s: str) -> None:
        if should_cancel is not None and should_cancel():
            return
        if s:
            chunks.append(s)
            emit({"t": "delta", "ts": ts, "text": s})

    try:
        note = backend.complete(SYSTEM, _build_user(title, raw, source), on_chunk=_on_chunk)
    except Exception as e:      # noqa: BLE001 - 花钱的调用失败要给人话
        from ..backends import LoomBackendError
        ev = {"t": "error", "ts": ts, "text": str(e) or f"{type(e).__name__}"}
        code = getattr(e, "code", "") if isinstance(e, LoomBackendError) else ""
        if code:
            ev["code"] = code
        emit(ev)
        return

    note = (note or "".join(chunks)).strip()
    if not note:
        emit({"t": "error", "ts": ts, "code": "empty_note", "text": "模型这次没给出内容，什么都没写。"})
        return

    saved = save_note(title, subject, note)
    if not saved.get("ok"):
        emit({"t": "error", "ts": ts, "code": "save_failed", "text": saved.get("reason") or "没写进去。"})
        return
    emit({"t": "done", "ts": ts, "路径": saved["路径"], "字数": saved["字数"],
          "标题": Path(saved["路径"]).stem, "科目": _safe_name(subject, default=DEFAULT_SUBJECT)})
