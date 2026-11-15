# -*- coding: utf-8 -*-
r"""阅读笔记）。

需求背景：「增加笔记功能，类似起点阅读页，可选定某一段落（或页），在后面紧跟标记，
标记点开就是记的笔记。笔记内容可检索，可删除。」

三条设计：
1. **定位用「段序号」**，不用字符偏移：正文是按段落渲染的，段号稳、偏移会被标点改动弄歪。
2. **按册存一份 json**，放 `LOOM_HOME/notes/`，文件名是册路径的 sha1。
   放自己目录、不塞书稿目录（书稿是作者的，工具不该往里面加东西）。
3. **永不写字稿**：笔记与原文完全分离，删笔记不碰原文一个字节。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


def _dir() -> Path:
    from ..config import user_config_dir
    d = user_config_dir() / "notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(path: str) -> Path:
    key = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
    return _dir() / (key + ".json")


def load(path: str) -> dict:
    """读一册的笔记。没有就返回空结构（不建文件）。"""
    p = _file(path)
    if not p.is_file():
        return {"册": "", "路径": str(path), "笔记": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"册": "", "路径": str(path), "笔记": [], "损坏": True}


def save(path: str, book: str, items: list) -> bool:
    p = _file(path)
    data = {"册": book, "路径": str(path), "笔记": items}
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception:
        return False


def add(path: str, book: str, para: int, text: str) -> dict:
    """加一条。返回新那条（含 id）。"""
    body = str(text or "").strip()
    if not body:
        return {}
    items = load(path)["笔记"]
    item = {
        "id": "n-" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex(),
        "段": int(para),
        "文": body[:2000],
        "时": int(time.time()),
    }
    items.append(item)
    save(path, book, items)
    return item


def remove(path: str, note_id: str) -> bool:
    data = load(path)
    items = data["笔记"]
    left = [x for x in items if x.get("id") != note_id]
    if len(left) == len(items):
        return False
    return save(path, data.get("册") or "", left)


def search(word: str) -> list:
    """全库搜笔记。返回 [{路径, 册, 段, 文, id, 时}]。"""
    q = str(word or "").strip().lower()
    out = []
    for p in sorted(_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in data.get("笔记") or []:
            if not q or q in str(it.get("文") or "").lower():
                out.append({
                    "路径": data.get("路径") or "", "册": data.get("册") or "",
                    "段": it.get("段"), "文": it.get("文") or "", "id": it.get("id"),
                    "时": it.get("时")})
    out.sort(key=lambda x: -(x.get("时") or 0))
    return out
