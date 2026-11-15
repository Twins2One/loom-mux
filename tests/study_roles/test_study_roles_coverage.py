# -*- coding: utf-8 -*-
"""书房角色分支 · 覆盖率自查（W0 缺口证明）。

证明评审的判断「1483 全绿却漏掉 11 个实锤 = 书房角色分支零覆盖」成立：
对既有测试面做结构性断言，防止未来"假绿"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent.parent


def test_0_既有测试面在补齐前没有任何文件引用书房角色分支的工具入口():
    """结构性断言：除本目录（study_roles）外，tests/ 里没有任何既有测试文件
    调用书房角色分支的三个关键入口（_toolset_for / _ROLE_SETUPS / role=）。
    W1 修复后若有人在这些入口上动刀而只跑老测试，这条保证老测试面仍拦不住——
    必须配合本目录的表征/目标测试一起看。"""
    hits: list[str] = []
    # 2026-11-08 夜间任务准备架构:书房域任务持久化测试（test_task_*_night.py）以
    # role="知识顾问" 走书房角色分支做集成样本（设计 §10"书房两个会话隔离"）——
    # 这正是 W0/W1 想要的补齐覆盖,豁免出"零覆盖"哨兵;表征防线仍由本目录测试承担。
    # 6c 新增真实 ask_route 双 worker 倒序完成覆盖，同属补齐后的集成测试。
    # 2026-11-12 协议修复专项覆盖真实角色工具参数/失败/流式，同属补齐后覆盖。
    _NIGHT_COVERAGE = {"test_task_domains_night.py", "test_task_recovery_night.py",
                       "test_6c_nav_gate.py", "test_6d_boundaries.py", "test_6d_replay.py",
                       "test_study_protocol_repair.py"}
    for f in sorted(TESTS_DIR.rglob("test_*.py")):
        if f.resolve().parent.name == "study_roles":
            continue
        if f.name in _NIGHT_COVERAGE:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for needle in ("_toolset_for", "_ROLE_SETUPS", "tools_role=",
                       "role='知识顾问'", 'role="知识顾问"',
                       "role='语料分析师'", 'role="语料分析师"'):
            if needle in text:
                hits.append(f"{f.name}:{needle}")
                break
    assert not hits, f"书房角色分支被这些既有测试引用了（预期为零覆盖）：{hits}"
