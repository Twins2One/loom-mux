# -*- coding: utf-8 -*-
"""F1 表征测试（先红）：显式 task_id 绕过 role 校验 + paused 不隐式复活。

锁定缺陷现场（历史会话 深度审查 F1，2026-11-08）：
1. prepare_turn 显式 task_id 路径不校验 scope.role——B 角色传 A 的 task_id
   能把 A 的任务（goal/约束/计划）整个拿去喂给 B 的模型；
2. is_active() 含 paused → A 任务被「新任务让位」暂停后，A 的普通「继续」
   （无 goal）仍会把 A 的上下文喂给模型（读旧任务指导新答复），另起对话后
   的旧请求同理——「不能写」但「仍能读」。
"""
from __future__ import annotations

from loom import task_state as ts
from loom import turn_prepare as tp


def test_显式task_id跨角色被拒(tmp_path):
    """F1-1：B 角色显式传 A 的 task_id → 不得把 A 任务给 B（当新任务或空处理）。"""
    base = tmp_path / ".任务状态" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="设定师任务,这是任务")
    tid_a = p1["task"]["task_id"]
    # B（批评家）显式带 A 的 task_id
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="批评家",
                         conversation_id="默认", task_id=tid_a, user_text="继续")
    t2 = p2.get("task")
    if t2 is not None:
        role2 = (t2.get("scope") or {}).get("role")
        assert role2 in ("批评家", ""), \
            f"显式 task_id 绕过 role 校验：B 拿到了 A 的任务（scope.role={role2}）"
    # 分片2自查补：B 显式带 A 的 task_id + B 自己的 goal → B 的新任务不被吞
    p3 = tp.prepare_turn(base_dir=base, domain="chat", role="批评家",
                         conversation_id="默认", task_id=tid_a,
                         goal="批评家自己的任务,这是任务", user_text="做这个")
    t3 = p3.get("task")
    assert t3 is not None and (t3.get("scope") or {}).get("role") == "批评家", \
        "B 自带明确 goal 的新任务不得因 task_id 跨角色被吞"


def test_显式task_id暂停任务不被普通继续复活(tmp_path):
    """F1-2：paused 任务显式 task_id + 普通 user_text（非明确恢复指令）不得承接。"""
    base = tmp_path / ".任务状态" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="任务甲,这是任务")
    tid_a = p1["task"]["task_id"]
    # 同角色开新任务 → 旧任务让位 paused
    tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                    conversation_id="默认", goal="任务乙,这是任务")
    st = ts.load_snapshot(base, tid_a)
    assert st.status == "paused"
    # 显式带 paused task_id + 普通继续 → 不应把它当活动任务承接（喂给模型）
    p3 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", task_id=tid_a, user_text="继续")
    t3 = p3.get("task")
    if t3 is not None:
        assert t3["task_id"] != tid_a or t3.get("status") not in ("ready", "running"), \
            "paused 任务被普通「继续」显式承接（上下文会喂给模型）"


def test_隐式扫描不认paused任务(tmp_path):
    """F1-3：隐式扫描（无 task_id 无 goal 的「继续」）不得定位 paused 任务。"""
    base = tmp_path / ".任务状态" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="任务甲,这是任务")
    tid_a = p1["task"]["task_id"]
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="任务乙,这是任务")
    # 甲已 paused；「继续」不带 goal —— 不得把甲翻出来喂模型
    p3 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", user_text="继续")
    if p3.get("task") is not None:
        assert p3["task"]["task_id"] == p2["task"]["task_id"], \
            "隐式扫描认领了 paused 任务（应为当前活动任务乙）"


def test_显式恢复动作仍可接paused(tmp_path):
    """F1-4（保护既有行为）：明确恢复语义（goal 非空匹配原 goal）仍能承接自己的任务。

    设计 §5：显式 task_id 是「恢复沿用」通道。恢复 = 作者明确要继续这个任务
    ——以 goal 等值（沿用）或显式恢复标记为判据；普通「继续」不该复活，
    但明确指名（task_id+同 goal / task_id+恢复语）应该能接。
    这里测：paused 任务 + 显式 task_id + goal=原 goal → 承接并恢复 ready。
    """
    base = tmp_path / ".任务状态" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="任务甲,这是任务")
    tid_a = p1["task"]["task_id"]
    # 让甲暂停（开新任务乙）
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="任务乙,这是任务")
    assert ts.load_snapshot(base, tid_a).status == "paused"
    # 显式指名甲 + 原样 goal（沿用不转向）
    p3 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", task_id=tid_a, goal="任务甲,这是任务",
                         user_text="继续任务甲")
    t3 = p3.get("task")
    assert t3 is not None and t3["task_id"] == tid_a, \
        "显式指名+同 goal 应承接（明确恢复语义）"
    assert ts.load_snapshot(base, tid_a).status in ("ready", "running"), \
        "明确恢复应把 paused 拉回 ready（旧任务乙应让位）"


def test_拒绝绑定时收尾不认领别人的任务(tmp_path):
    """历史回执 回执 P1（审查记录）：A（设定师）旧任务 paused 后 B（设定师
    新任务）在跑；A 的显式请求被 prepare 拒绝 → agent_chat 收尾不得按 role
    重扫认领 B（B.last_outcome 被 A 输出污染——探针实跑复现过）。"""
    base = tmp_path / ".任务状态" / "默认"
    pA = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="旧任务A,这是任务")
    tid_a = pA["task"]["task_id"]
    pB = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", goal="新任务B,这是任务")
    tid_b = pB["task"]["task_id"]
    assert ts.load_snapshot(base, tid_a).status == "paused"
    # A 显式带自己（已 paused）的 task_id + 普通「继续」→ 拒绝
    pR = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", task_id=tid_a, user_text="继续")
    assert pR.get("task") is None
    assert pR.get("task_binding") == "refused", \
        f"拒绝绑定必须显式标记（task_binding），实际={pR.get('task_binding')}"
    # 模拟 agent_chat 收尾：bound_task_id=refused 哨兵 → 不得动 B
    from loom import agent_chat as ac
    seen: list[dict] = []
    ac._finish_turn(tmp_path, "设定师", "默认", "A 请求的输出", seen.append, "t",
                    had_proposal=False, bound_task_id="__refused__")
    st_b = ts.load_snapshot(base, tid_b)
    assert not (st_b._d.get("last_outcome") or {}).get("summary"), \
        "A 的被拒请求污染了 B 的 outcome（空绑定回退扫描仍在跑）"
    # 对照：正常空绑定（无任务轮）仍允许扫描——这是旧行为，不该被本修破坏
    pN = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         conversation_id="默认", user_text="继续")
    assert pN.get("task_binding") in ("none", "bound"), \
        "普通无任务轮的 task_binding 应为 none（不抑制回退）"
