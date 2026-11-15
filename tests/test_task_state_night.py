# -*- coding: utf-8 -*-
"""S1 最小状态层测试（设计 §10:生命周期行 / revision 冲突 / 迟到结果 / 简单问答无计划即结束）。"""
from __future__ import annotations

import pytest

from loom import task_state as ts
from loom import turn_prepare as tp


# ── TaskState 生命周期 ────────────────────────────────────────────────────
def _state(goal="写好第三章") -> ts.TaskState:
    return ts.TaskState.create({"domain": "chat", "role": "设定师", "conversation_id": "c1"},
                               goal)


def test_lifecycle_ready_to_running_to_completed():
    st = _state()
    assert st.status == "ready"
    st.apply({"kind": "status", "status": "running"})
    assert st.status == "running"
    st.apply({"kind": "status", "status": "completed", "stop_reason": "交付完成"})
    assert st.status == "completed"


def test_late_result_does_not_reactivate_completed():
    """§5:迟到结果不激活已完成任务。"""
    st = _state()
    st.apply({"kind": "status", "status": "completed"})
    r = st.apply({"kind": "status", "status": "running"})
    assert st.status == "completed"
    assert "ignored" in r


def test_revision_conflict_raises_no_overwrite():
    st = _state()
    with pytest.raises(ts.RevisionConflict):
        st.apply({"kind": "outcome", "summary": "旧的一版"}, expected_revision=st.revision + 5)
    # 冲突后状态未被污染
    assert st._d["last_outcome"] == ""
    assert st.revision == 1


def test_waiting_author_and_pause_resume():
    st = _state()
    st.apply({"kind": "status", "status": "waiting_author", "stop_reason": "等作者拍板"})
    assert st.status == "waiting_author"
    st.apply({"kind": "status", "status": "ready"})
    st.apply({"kind": "status", "status": "paused", "stop_reason": "轮数耗尽"})
    assert st.status == "paused"
    st.apply({"kind": "status", "status": "ready"})     # 作者恢复
    assert st.status == "ready"


def test_simple_qa_no_plan_finishes_without_plan():
    """简单问答:plan 为空也能以 completed 结束(不强制计划)。"""
    st = _state(goal="问一句")
    assert st.plan_steps() == []
    st.apply({"kind": "status", "status": "completed"})
    assert st.status == "completed"


def test_plan_update_keeps_completed_refs_and_marks_stale():
    """计划修订保留已完成步骤结果引用;失效步骤标回 pending(不靠重编号抹进度)。"""
    st = _state()
    st.apply({"kind": "plan_update", "steps": [
        {"id": "s1", "title": "取材"}, {"id": "s2", "title": "成稿"}]})
    st.set_step("s1", "completed", result_ref="doc:v3")
    rev = st._d["plan"]["plan_revision"]
    st.apply({"kind": "plan_update", "reason": "作者改方向", "steps": [
        {"id": "s1", "title": "重新取材"}, {"id": "s2", "title": "成稿"}]})
    steps = {s["id"]: s for s in st.plan_steps()}
    assert st._d["plan"]["plan_revision"] == rev + 1
    assert steps["s1"]["status"] == "pending"       # 被新版改掉 → 待重做
    assert steps["s2"]["status"] == "pending"


def test_cycle_deps_are_filtered():
    st = _state()
    st.apply({"kind": "plan_update", "steps": [
        {"id": "a", "title": "A", "deps": ["b"]}, {"id": "b", "title": "B", "deps": ["a"]}]})
    for s in st.plan_steps():
        assert s["deps"] == []      # 循环引用被清洗,不炸


def test_constraint_supersede():
    st = _state()
    st.apply({"kind": "constraint", "content": "男主不许用枪"})
    st.apply({"kind": "constraint", "content": "男主可以用枪(作者 11-08 改)"})
    assert len(st._d["constraints"]) == 2
    st.apply({"kind": "supersede_constraint", "index": 0})
    assert st.active_constraints() == ["男主可以用枪(作者 11-08 改)"]


# ── 持久化:日志+快照+幂等恢复 ─────────────────────────────────────────────
def test_persist_snapshot_and_rebuild(tmp_path):
    base = tmp_path / ".任务状态" / "c1"
    st = _state()
    ts.append_task_event(base, st.task_id,
                         {"kind": "start", "scope": st._d["scope"], "goal": st._d["goal"]})
    ts.commit(base, st, {"kind": "plan_update", "steps": [{"id": "s1", "title": "取材"}]})
    ts.commit(base, st, {"kind": "status", "status": "running"})

    loaded = ts.load_snapshot(base, st.task_id)
    assert loaded is not None and loaded.revision == st.revision
    rebuilt = ts.rebuild_from_log(base, st.task_id)
    assert rebuilt is not None
    assert rebuilt.revision == st.revision
    assert rebuilt.status == "running"
    assert rebuilt.plan_steps()[0]["title"] == "取材"


def test_bad_snapshot_falls_back_to_log(tmp_path):
    base = tmp_path / "c1"
    st = _state()
    ts.append_task_event(base, st.task_id,
                         {"kind": "start", "scope": st._d["scope"], "goal": st._d["goal"]})
    ts.commit(base, st, {"kind": "outcome", "summary": "做了一半"})
    # 把快照写坏
    ts._snap_path(base, st.task_id).write_text("{坏掉的json", encoding="utf-8")
    loaded = ts.load_or_rebuild(base, st.task_id)
    assert loaded is not None and loaded._d["last_outcome"] == "做了一半"


def test_partial_last_line_is_not_completed(tmp_path):
    """末尾不完整事件不算已完成(§8)。"""
    base = tmp_path
    st = _state()
    ts.append_task_event(base, st.task_id,
                         {"kind": "start", "scope": st._d["scope"], "goal": st._d["goal"]})
    p = ts._log_path(base, st.task_id)
    with p.open("a", encoding="utf-8") as f:
        f.write('{"kind":"status","status":"compl')       # 半行
    st2 = ts.rebuild_from_log(base, st.task_id)
    assert st2 is not None and st2.status == "ready"


def test_pause_active_tasks_single_activity(tmp_path):
    """§5:每个作用域同一时间只有一个活动任务。"""
    base = tmp_path
    st1 = _state("任务一")
    ts.append_task_event(base, st1.task_id,
                         {"kind": "start", "scope": st1._d["scope"], "goal": "任务一"})
    ts.save_snapshot(base, st1)
    paused = ts.pause_active_tasks(base)
    assert paused == [st1.task_id]
    st2 = _state("任务二")
    ts.append_task_event(base, st2.task_id,
                         {"kind": "start", "scope": st2._d["scope"], "goal": "任务二"})
    ts.save_snapshot(base, st2)
    paused2 = ts.pause_active_tasks(base)
    assert paused2 == [st2.task_id]     # 1 已 paused 不再动
    assert ts.load_snapshot(base, st1.task_id).status == "paused"


def test_commit_revision_flow(tmp_path):
    base = tmp_path
    st = _state()
    ts.append_task_event(base, st.task_id,
                         {"kind": "start", "scope": st._d["scope"], "goal": st._d["goal"]})
    ts.save_snapshot(base, st)
    other = ts.load_snapshot(base, st.task_id)
    ts.commit(base, st, {"kind": "outcome", "summary": "A 线提交"})
    with pytest.raises(ts.RevisionConflict):
        ts.commit(base, other, {"kind": "outcome", "summary": "B 线旧视图"},
                  expected_revision=other.revision)
    # B 重读后再提交成功
    other = ts.load_snapshot(base, st.task_id)
    ts.commit(base, other, {"kind": "outcome", "summary": "B 线重读后"}, expected_revision=other.revision)


# ── PreparedTurn ──────────────────────────────────────────────────────────
def test_prepare_simple_qa_no_task(tmp_path):
    """简单问答:无 goal 无任务 → 不建任务不建文件,events 为空。"""
    p = tp.prepare_turn(base_dir=tmp_path / ".任务状态" / "c1", domain="chat", role="书房伙伴",
                        user_text="今天天气怎么样", conversation_tail=[
                            {"role": "user", "text": "你好"}, {"role": "assistant", "text": "你好!"}])
    assert p["task"] is None and p["plan_progress"] is None and p["events"] == []
    assert p["history_tail"][-1]["text"] == "你好!"
    assert not (tmp_path / ".任务状态").exists()


def test_prepare_creates_task_and_reuses(tmp_path):
    base = tmp_path / ".任务状态" / "c1"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="重设金手指",
                         user_text="帮我重设金手指", request_id="r1")
    assert p1["task"] is not None and p1["task"]["status"] == "ready"
    assert any(e["t"] == "task_prepared" for e in p1["events"])
    # 重试同一请求(相同 conversation)不重复建任务——幂等
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="重设金手指",
                         user_text="帮我重设金手指", request_id="r1")
    assert p2["task"]["task_id"] == p1["task"]["task_id"]
    # 快照文件只有一份
    assert len(list(base.glob("*.快照.json"))) == 1


def test_prepare_new_goal_pauses_old(tmp_path):
    """任务转向:旧任务 paused,新任务建立。"""
    base = tmp_path / ".任务状态" / "c1"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="任务甲")
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="任务乙")
    assert p1["task"]["task_id"] != p2["task"]["task_id"]
    old = ts.load_snapshot(base, p1["task"]["task_id"])
    assert old.status == "paused"
    # 转向事件里有 task_status paused 通告
    assert any(e["t"] == "task_status" and e.get("status") == "paused" for e in p2["events"])


def test_prepare_ignores_terminal_task_id(tmp_path):
    """显式给已完成任务的 task_id:不开计划,退普通答复。"""
    base = tmp_path / ".任务状态" / "c1"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="旧的")
    st = ts.load_snapshot(base, p1["task"]["task_id"])
    st.apply({"kind": "status", "status": "completed"})
    ts.save_snapshot(base, st)
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师",
                         task_id=p1["task"]["task_id"], goal="续")
    assert p2["task"] is None


def test_prepare_budget_and_constraints(tmp_path):
    base = tmp_path / ".任务状态" / "c1"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="任务")
    st = ts.load_snapshot(base, p1["task"]["task_id"])
    st.apply({"kind": "constraint", "content": "不用第一人称"})
    st.apply({"kind": "constraint", "content": "猜的规则", "explicit": False})
    ts.save_snapshot(base, st)
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师")
    cons = {c["content"]: c for c in p2["constraints"]}
    assert cons["不用第一人称"]["kind"] == "显式"
    assert cons["猜的规则"].get("待核") is True
    assert p2["budget"]["chars"] > 0


# ── 计划更新解析(文本协议等价路径)────────────────────────────────────────
def test_parse_plan_update_valid():
    text = "前言。\nTASK_PLAN: 作者要求加支线\n1. 取材 [done]\n2. 写支线\n3. 收尾 [skip: 主线压缩]\n后记。"
    r = tp.parse_plan_update(text)
    assert r is not None and r["reason"] == "作者要求加支线"
    assert [s["status"] for s in r["steps"]] == ["completed", "pending", "skipped"]
    assert "主线压缩" in r["steps"][2]["skipped_reason"]


def test_parse_plan_update_invalid_keeps_previous():
    """无步骤 = 无效更新,返回 None(保留上一份有效计划,不当任务完成)。"""
    assert tp.parse_plan_update("TASK_PLAN: 只有标题没步骤") is None
    assert tp.parse_plan_update("完全普通答复") is None


def test_parse_plan_update_step_cap():
    text = "TASK_PLAN: 太多\n" + "\n".join(f"{i}. 步{i}" for i in range(1, 20))
    r = tp.parse_plan_update(text)
    assert r is not None and len(r["steps"]) <= 8       # §13.4 结构验证:步骤数量
