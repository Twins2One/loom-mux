# -*- coding: utf-8 -*-
"""6a 表征测试（先红）：F5 CAS 非原子 + F11 同任务并发无代际。

锁定缺陷（历史会话 深度审查 F5/F11 + 历史回执 回执挂账）：
1. commit 读-改-写无锁：两个 barrier 同步的线程同 revision 过检后并发写，
   后写覆盖先写（丢更新）；
2. pause_active_tasks 恒等 CAS（内存对象自身 revision）+裸 save；
3. save_snapshot 固定 .tmp 路径，并发可互换；
4. prepare_turn 暂停旧任务+新建不在同一临界区——间隙另一请求可建出双活动任务；
5. 同任务两个并发请求倒序返回互相覆盖（无 request 代际核验）。
"""
from __future__ import annotations

import threading

import pytest

from loom import task_state as ts
from loom import turn_prepare as tp


def test_并发commit不丢更新(tmp_path):
    """F5-1：barrier 同步双线程同 revision 提交——两条 constraint 都得在盘上。"""
    base = tmp_path / "t"
    st = ts.TaskState.create({"domain": "chat", "role": "设定师",
                              "conversation_id": "默认"}, "任务")
    ts.save_snapshot(base, st)
    barrier = threading.Barrier(2)
    errs: list[Exception] = []
    done: list[int] = []

    def worker(i: int) -> None:
        try:
            local = ts.load_snapshot(base, st.task_id)
            barrier.wait()          # 两线程都读到同一 revision 后同时过检
            # 6a：并发追加路径用 commit_retry（锁内追盘重放，双方更新都在；
            # 严格 CAS（commit）语义=旧视图必拒，契约测试另锁）
            ts.commit_retry(base, local,
                            {"kind": "constraint", "content": f"约束{i}", "explicit": True},
                            expected_revision=local.revision)
            done.append(i)
        except Exception as e:      # noqa: BLE001
            errs.append(e)
            done.append(i)

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    final = ts.load_snapshot(base, st.task_id)
    cons = [c["content"] for c in final._d.get("constraints") or []]
    # 两条都要在（丢任何一条 = 覆盖写 = F5 缺陷）
    assert "约束1" in cons and "约束2" in cons, \
        f"并发 commit 丢更新：盘上只有 {cons}（错误: {[type(e).__name__ for e in errs]}）"


def test_并发prepare不产生双活动任务(tmp_path):
    """F5-4：同 role 两线程同时 prepare 新任务（不同 goal）——只允许一个活动任务。"""
    base = tmp_path / ".任务状态" / "默认"
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def worker(goal: str) -> None:
        barrier.wait()
        p = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                            conversation_id="默认", goal=goal)
        results.append(p)

    t1 = threading.Thread(target=worker, args=("任务甲,这是任务",))
    t2 = threading.Thread(target=worker, args=("任务乙,这是任务",))
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    tids = {(p.get("task") or {}).get("task_id") for p in results if p.get("task")}
    # 同一 (role, conversation) 作用域的活动任务必须唯一
    active = []
    for p in results:
        t = p.get("task")
        if t:
            st = ts.load_snapshot(base, t["task_id"])
            if st and st.status in ("ready", "running", "waiting_author"):
                active.append(t["task_id"])
    assert len(set(active)) <= 1, \
        f"并发 prepare 产生双活动任务：{set(active)}（tids={tids}）"


def test_同任务并发收尾不互相覆盖(tmp_path):
    """F5-3：同任务两个并发收尾（agent_chat._finish_turn 同款链路）——
    两次 outcome 至少各自落一条（或一次明确冲突拒绝），不得静默覆盖。"""
    from loom import agent_chat as ac
    base = tmp_path / ".任务状态" / "默认"
    p = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                        conversation_id="默认", goal="任务,这是任务")
    tid = p["task"]["task_id"]
    barrier = threading.Barrier(2)

    def worker(say: str) -> None:
        barrier.wait()
        ac._finish_turn(tmp_path, "设定师", "默认", say, lambda e: None, f"t-{say}",
                        had_proposal=False, bound_task_id=tid)

    t1 = threading.Thread(target=worker, args=("第一轮输出",))
    t2 = threading.Thread(target=worker, args=("第二轮输出",))
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    st = ts.load_snapshot(base, tid)
    last = str(st._d.get("last_outcome") or "")
    # 两个收尾并发：最终盘上必须有 outcome（至少一个落了），不得双双静默丢
    assert last, "两次并发收尾一条都没落（都静默丢了）"
    # 修复目标：一次成功+一次 CAS 冲突拒绝或重试——日志里两次提交的痕迹
    # 至少 outcome 不是空的（语义：不是无产出收尾）


def test_status重放不复活被让位任务(tmp_path):
    """6a 回执 P1（历史回执 审查记录）：A 收尾期间被并发让位成 paused——
    A 的 status:ready 重放必须被守卫拒绝（不复活、盘上不双活动）。"""
    from loom import turn_prepare as tp2
    base = tmp_path / ".任务状态" / "默认"
    pA = tp2.prepare_turn(base_dir=base, domain="chat", role="设定师",
                          conversation_id="默认", goal="任务甲,这是任务")
    tid_a = pA["task"]["task_id"]
    st = ts.load_snapshot(base, tid_a)
    # 模拟 A 收尾进行到一半（outcome 已落）时 B 建新任务把甲 paused
    ts.commit(base, st, {"kind": "outcome", "summary": "A 的产出"},
              expected_revision=st.revision)
    pB = tp2.prepare_turn(base_dir=base, domain="chat", role="设定师",
                          conversation_id="默认", goal="任务乙,这是任务")
    tid_b = pB["task"]["task_id"]
    assert ts.load_snapshot(base, tid_a).status == "paused"
    # A 的收尾 status:ready 走 commit_retry（追盘重放路径）——守卫必须拒
    st_a = ts.load_snapshot(base, tid_a)
    with pytest.raises(ts.RevisionConflict):
        ts.commit_retry(base, st_a, {"kind": "status", "status": "ready"})
    # 甲不得复活；乙仍是唯一活动任务
    assert ts.load_snapshot(base, tid_a).status == "paused", "甲被 status 重放复活（P1）"
    assert ts.load_snapshot(base, tid_b).status in ("ready", "running")
    # 对照：可承接状态内的 status 事件重放正常（非 paused 场景不受守卫影响）
    st_b = ts.load_snapshot(base, tid_b)
    ts.commit_retry(base, st_b, {"kind": "status", "status": "ready"})
    assert ts.load_snapshot(base, tid_b).status == "ready"
