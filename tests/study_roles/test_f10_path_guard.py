# -*- coding: utf-8 -*-
"""F10 表征测试（先红）：书房 role 路径注入 + 书内快照 root 未校验。

锁定缺陷现场（3c678607 深度审查 F10）：
- study/__init__.py 把客户端可控的 b.role 直接拼进路径
  （library/.loom-tasks/<role>/<conversation>）——绝对路径/分隔符可逃出任务子区；
- api/agents.py 的任务快照端点直接 Path(root)，未走 _project_root 口径。
本机登录服务内不据此宣称远程可利用，但边界应收紧。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def night_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "library").mkdir(parents=True)
    monkeypatch.setenv("LOOM_HOME", str(home))
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(home / "library"))
    yield home


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_role路径逃逸被拒(night_home):
    """F10-1：sanitize_role 清洗后 base 不得逃出 .loom-tasks 子区（路由形状）。"""
    from loom import task_state as ts
    from loom.study.library import library_root
    from loom import turn_prepare as tp
    for evil in ["C:\\Temp\\evil", "../../evil", "a/b", "..\\..\\x",
                 "知识顾问/../../..", "知识顾问:默认"]:
        role = ts.sanitize_role(evil)
        assert role == "书房伙伴", f"evil role 未被清洗: {evil!r} → {role!r}"
        base = library_root() / ".loom-tasks" / role / ts.sanitize_conversation_id("c1")
        assert str(base).startswith(str(library_root()) + "\\.loom-tasks"), \
            f"清洗后仍逃逸：{evil!r} → {base}"
        p = tp.prepare_turn(base_dir=base, domain="study", role=role,
                            conversation_id="c1", goal="任务,这是任务")
        _ = p


def test_任务根定位不落子区外的盘(night_home):
    """F10-1 附：路由层 evil role 实跑后，library/.loom-tasks 之外无新目录。"""
    from pathlib import Path
    from loom import task_state as ts
    from loom.study.library import library_root
    from loom import turn_prepare as tp
    before = {p for p in night_home.rglob("*") if p.is_dir()}
    # 模拟路由层形状（study/__init__.py:138 同款清洗链）
    for evil in ["C:\\Temp\\evil2", "../../evil2"]:
        base = library_root() / ".loom-tasks" / ts.sanitize_role(evil) / \
            ts.sanitize_conversation_id("c1")
        tp.prepare_turn(base_dir=base, domain="study", role="知识顾问",
                        conversation_id="c1", goal="任务,这是任务")
    after = {p for p in night_home.rglob("*") if p.is_dir()}
    new = after - before
    for p in new:
        assert ".loom-tasks" in str(p), f"任务目录写到子区外: {p}"
    # home 之外零写入
    assert not (Path("C:\\Temp\\evil2")).exists()


def test_书内快照端点root走项目入口校验(night_home, tmp_path, monkeypatch):
    """F10-2：/api/agent/task 的 root 参数与 chat/new 的 _project_root 口径一致。"""
    from loom.api import agents as ag
    fn = getattr(ag, "_project_root", None)
    assert callable(fn), "api/agents.py 应有 _project_root 入口校验"
    # 已注册书目录之外的 root 应被拒/收口（不任意读已知任务目录）
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".任务状态" / "默认").mkdir(parents=True)
    from loom import task_state as ts
    st = ts.TaskState.create({"domain": "chat", "role": "设定师",
                              "conversation_id": "默认"}, "外部任务")
    ts.save_snapshot(outside / ".任务状态" / "默认", st)
    try:
        r = fn(str(outside)) if fn else None
    except Exception:
        r = None
    # 校验语义：外部路径要么抛、要么被归一到注册书根（不得原样放行）
    if r is not None:
        assert str(r) != str(outside), "外部 root 原样放行（F10-2）"
