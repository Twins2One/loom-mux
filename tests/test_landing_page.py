# -*- coding: utf-8 -*-
"""启动落地页：0.8.0 起先开书房壳，点某本书才进织布机（作者 2026-10-27 裁定 §12）。

为什么值得一条测试：`desktop.py` 里有**三处**开窗——原生窗口、Edge/Chrome 应用模式兜底、
纯浏览器兜底。三个地方各写一份 URL 时，改的时候必漏一个，而漏掉的那个就是
"某台机器上进不去书房"。这条测试把"三处都用同一个常量"钉死。
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "loom" / "desktop.py"


def _source() -> str:
    return SRC.read_text(encoding="utf-8")


def test_落地页常量指向书房壳():
    src = _source()
    m = re.search(r'^LANDING\s*=\s*"([^"]+)"', src, re.M)
    assert m, "desktop.py 里没有 LANDING 常量"
    assert m.group(1) == "/study.html", f"落地页应是书房壳，实际 {m.group(1)}"


def test_三处开窗都带上落地页():
    src = _source()
    # 所有形如 http://127.0.0.1:{port} 的模板串，后面必须紧跟 {LANDING}
    bad = [ln.strip() for ln in src.splitlines()
           if "127.0.0.1:{port}" in ln and "{LANDING}" not in ln]
    assert not bad, "这些开窗点没带落地页（会直接落到欢迎页，书房进不去）：\n" + "\n".join(bad)


def test_落地页不是裸根路径():
    """散装兜底：`/` 是欢迎页，不是书房。"""
    src = _source()
    m = re.search(r'^LANDING\s*=\s*"([^"]+)"', src, re.M)
    assert m and m.group(1) != "/", "落地页不能是 /（那是原 Loom-mux 的欢迎页）"
