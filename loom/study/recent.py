# -*- coding: utf-8 -*-
r"""最近读过的书）。

作者要在书房角色栏下方另起一栏，显示最近读过的前三本。

三条设计：
1. **只记路径与时间**，不存正文——正文在知识库里，这里只是一个书签。
2. **按时间倒序、去重**：同一册读两次只留一条，时间取最近那次。
3. **存在 LOOM_HOME/recent.json**：跟配置住一起，不往书稿目录里写东西。

为什么不放浏览器的 localStorage：换个窗口就丢了，
而「最近读过」本该跟着人走。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

KEEP =3 #只留前三本


def _file() -> Path:
    from ..config import user_config_dir
    return user_config_dir() / "recent.json"


def load() -> list:
    """读最近读过的列表（时间倒序）。损坏就当空。"""
    p = _file()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("书") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("路径"):
            out.append(it)
    return out[:KEEP]


def touch(path: str, name: str = "", kind: str = "", subject: str = "") -> bool:
    """记一次阅读。同一册只留一条，时间刷新。"""
    p = str(path or "").strip()
    if not p:
        return False
    items = [x for x in load() if x.get("路径") != p]
    items.insert(0, {
        "路径": p,
        "名": str(name or ""),
        "类": str(kind or ""),
        "科目": str(subject or ""),
        "t": int(time.time()),
    })
    items = items[:KEEP]
    try:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"书": items}, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception:
        return False
