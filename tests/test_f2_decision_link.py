# -*- coding: utf-8 -*-
"""F2/F3 表征测试（先红）：决策回连按「最近 updated_ms」猜来源。

锁定缺陷现场（历史会话 深度审查 F2/F3，2026-11-08）：
- proposal 发出时不携带任务身份（task_id/conversation/panel）；
- _link_decision_to_task 扫全部代际按 role+is_active+最大 updated_ms 选目标——
  旧卡 A 的接受会被记到更新的任务 C 头上（A/B/C 同角色不同代际）；
- task_reject 的 recorded=True 只证明记了原话，不证明拒绝了正确任务；
  proposal 找不到时退 {"role": role} 仍走「猜最近」。
"""
from __future__ import annotations

import time

from loom import task_state as ts
from loom import turn_prepare as tp
from loom import usecases as uc


def _mk(root, conv, role, goal):
    base = root / ".任务状态" / conv
    p = tp.prepare_turn(base_dir=base, domain="chat", role=role,
                        conversation_id=conv, goal=goal)
    return p["task"]["task_id"]


def test_旧卡接受不得记到新任务头上(tmp_path):
    """F2：A 卡（默认代际、旧）→ 建了更新的 C（议事代际）→ 接受 A 的卡，
    decision 必须记在 A 上（或明确未关联），不得按最近 updated_ms 记到 C。"""
    root = tmp_path / "book"
    tid_a = _mk(root, "默认", "批评家", "旧任务A,这是任务")
    time.sleep(0.05)
    tid_c = _mk(root, "议事-1", "批评家", "议事任务C,这是任务")
    # 接受「A 的卡」——proposal 只有 role，无任务归属（现状形状）
    uc._link_decision_to_task(root, {"role": "批评家", "id": "p-old"}, "p-old",
                              accepted=True, ts="t")
    st_a = ts.load_snapshot(root / ".任务状态" / "默认", tid_a)
    st_c = ts.load_snapshot(root / ".任务状态" / "议事-1", tid_c)
    dec_a = [d for d in st_a._d.get("decisions") or [] if d.get("proposal_id") == "p-old"]
    dec_c = [d for d in st_c._d.get("decisions") or [] if d.get("proposal_id") == "p-old"]
    if dec_c:
        assert dec_a, "旧卡 p-old 的决策记到了更新的任务 C（F2 误关联）且 A 上没有"


def test_无角色卡不得跨角色猜最近(tmp_path):
    """F2：无 role 的领航员卡不得在所有角色里选最近任务。"""
    root = tmp_path / "book"
    tid_a = _mk(root, "默认", "设定师", "设定任务,这是任务")
    time.sleep(0.05)
    tid_b = _mk(root, "默认", "批评家", "批评任务,这是任务")
    uc._link_decision_to_task(root, {"id": "p-nav"}, "p-nav", accepted=True, ts="t")
    st_a = ts.load_snapshot(root / ".任务状态" / "默认", tid_a)
    st_b = ts.load_snapshot(root / ".任务状态" / "默认", tid_b)
    assert not (st_a._d.get("decisions") or st_b._d.get("decisions")), \
        "无 role 卡被猜到某角色的任务上（应明确未关联）"


def test_proposal带task_id时精准回连(tmp_path):
    """F2 正路径：proposal 带服务端绑定的 task_id → 只回连该任务。"""
    root = tmp_path / "book"
    tid_a = _mk(root, "默认", "批评家", "任务A,这是任务")
    time.sleep(0.05)
    tid_c = _mk(root, "议事-1", "批评家", "任务C,这是任务")
    # proposal 携带归属（修复后应支持的形状）
    uc._link_decision_to_task(root, {"role": "批评家", "id": "p-a", "task_id": tid_a,
                                     "conversation_id": "默认"},
                              "p-a", accepted=True, ts="t")
    st_a = ts.load_snapshot(root / ".任务状态" / "默认", tid_a)
    st_c = ts.load_snapshot(root / ".任务状态" / "议事-1", tid_c)
    assert any(d.get("proposal_id") == "p-a" for d in st_a._d.get("decisions") or []), \
        "带 task_id 的卡必须回连原任务"
    assert not any(d.get("proposal_id") == "p-a" for d in st_c._d.get("decisions") or []), \
        "带 task_id 的卡不得记到别的任务"


def test_task_reject返回真实关联(tmp_path):
    """F3：reject 的返回应如实反映关联结果（linked/task_id），recorded ≠ 承接成功。"""
    root = tmp_path / "book"
    tid_a = _mk(root, "默认", "批评家", "任务A,这是任务")
    # 拒绝一张不存在的卡（无归属信息）→ recorded=True 但 linked 必须如实为 False
    rec = uc.task_reject(root, "p-不存在", "批评家", original_text="不要了")
    assert rec.get("ok") is True and rec.get("recorded") is True
    assert rec.get("linked") is False, \
        "未关联到任务时 linked 必须显式 False（recorded 只是记了原话）"
