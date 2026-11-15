# -*- coding: utf-8 -*-
"""M1a context_len 前端契约入口；动态行为由 Node vm 测试，不需要浏览器。

2026-11-16（开源准备）：
本测试依赖两样**只有开发机才有**的东西 —— Node 运行时，以及仓库外的
`tools/check_context_len_frontend_20261115.cjs`（`tools/` 不在发布白名单内）。
原来把某个本机绝对路径写死当兜底，发布副本的 A-5 扫描会命中用户名；
而且副本里 `parents[2]` 指到仓库外，脚本必然找不到 ⇒ 测试在副本里必红。
现在：环境变量 → PATH 查找；两样缺一就跳过，不把任何一台机器的路径带进仓库。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "check_context_len_frontend_20261115.cjs"


def _find_node() -> Path | None:
    """Node 运行时：先看环境变量 DSH_NODE_EXE，再走 PATH。"""
    env = os.environ.get("DSH_NODE_EXE")
    if env:
        return Path(env)
    which = shutil.which("node")
    return Path(which) if which else None


def test_context_len_frontend_contract():
    node = _find_node()
    if node is None:
        pytest.skip("没有 Node 运行时（设 DSH_NODE_EXE，或把 node 放进 PATH）")
    if not SCRIPT.is_file():
        pytest.skip(f"契约脚本不在（tools/ 不进发布副本）：{SCRIPT}")
    result = subprocess.run(
        [str(node), str(SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "context_len frontend contract checks passed" in result.stdout
