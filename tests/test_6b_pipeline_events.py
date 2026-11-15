# -*- coding: utf-8 -*-
"""6b 表征测试（先红）：F7 流水线步骤完成绕过事件折叠。

锁定缺陷（历史会话 深度审查 F7）：
agents._pipeline_step_done / _agentic_step_done / finish 的 skipped 都走
set_step + save_snapshot 裸写——revision 不涨、无日志、恢复重建丢完成状态；
TaskState.apply 的 plan_update 也不保留 result_ref、deps 未复制。
"""
from __future__ import annotations

from loom import task_state as ts


def _task_with_plan(base):
    st = ts.TaskState.create({"domain": "pipeline", "role": "大纲师",
                              "conversation_id": "默认"}, "写章任务")
    ts.append_task_event(base, st.task_id,
                         {"kind": "start", "scope": st._d["scope"], "goal": "写章任务",
                          "ts": ts.now_ms()})
    ts.save_snapshot(base, st)
    st.apply({"kind": "plan_update", "reason": "初版",
              "steps": [{"id": "s1", "title": "提纲"}, {"id": "s2", "title": "细纲"}]})
    ts.commit(base, st, {"kind": "plan_update", "reason": "初版",
                         "steps": [{"id": "s1", "title": "提纲"},
                                   {"id": "s2", "title": "细纲"}]},
              expected_revision=st.revision - 1)
    return st


def test_步骤完成走事件折叠涨revision(tmp_path):
    """F7-1：步骤完成必须以 plan_update 事件折叠（涨 revision、进日志）。"""
    base = tmp_path
    st = _task_with_plan(base)
    rev0 = ts.load_snapshot(base, st.task_id).revision
    # 现网 agents._pipeline_step_done 的做法（裸写）——修复后应改走事件：
    # 这里模拟修复后语义：plan_update 事件
    st2 = ts.load_snapshot(base, st.task_id)
    st2.apply({"kind": "plan_update", "reason": "s1 完成",
               "steps": [{"id": "s1", "title": "提纲", "status": "completed"},
                         {"id": "s2", "title": "细纲"}]})
    ts.commit_retry(base, st2, {"kind": "plan_update", "reason": "s1 完成",
                         "steps": [{"id": "s1", "title": "提纲", "status": "completed"},
                                   {"id": "s2", "title": "细纲"}]})
    snap = ts.load_snapshot(base, st.task_id)
    assert snap.revision > rev0, "事件折叠应涨 revision"
    log = (base / (st.task_id + ".jsonl")).read_text(encoding="utf-8")
    assert "s1 完成" in log, "步骤完成必须进事件日志（恢复可重建）"


def test_result_ref在事件折叠中保留(tmp_path):
    """F7-2：plan_update 事件里的 result_ref 要折进状态（apply 现在丢弃——
    步骤的「干成了什么」证据在重建后丢失）。"""
    base = tmp_path
    st = _task_with_plan(base)
    st2 = ts.load_snapshot(base, st.task_id)
    st2.apply({"kind": "plan_update", "reason": "s1 完成",
               "steps": [{"id": "s1", "title": "提纲", "status": "completed",
                          "result_ref": "提纲已入工作区"},
                         {"id": "s2", "title": "细纲"}]})
    ts.commit_retry(base, st2, {"kind": "plan_update", "reason": "s1 完成",
                         "steps": [{"id": "s1", "title": "提纲", "status": "completed",
                                    "result_ref": "提纲已入工作区"},
                                   {"id": "s2", "title": "细纲"}]})
    snap = ts.load_snapshot(base, st.task_id)
    s1 = next(s for s in snap.plan_steps() if s["id"] == "s1")
    assert s1.get("result_ref") == "提纲已入工作区", \
        f"result_ref 被折叠丢弃（F7-2）：{s1}"


def test_日志重建恢复步骤完成状态(tmp_path):
    """F7-3：删快照只剩日志 → rebuild 后步骤完成状态等值。"""
    base = tmp_path
    st = _task_with_plan(base)
    st2 = ts.load_snapshot(base, st.task_id)
    ev = {"kind": "plan_update", "reason": "s1 完成",
          "steps": [{"id": "s1", "title": "提纲", "status": "completed",
                     "result_ref": "提纲已入工作区"},
                    {"id": "s2", "title": "细纲"}]}
    st2.apply(ev)
    ts.commit_retry(base, st2, ev)
    snap = ts.load_snapshot(base, st.task_id)
    (base / (st.task_id + ".快照.json")).unlink()
    rebuilt = ts.rebuild_from_log(base, st.task_id)
    assert rebuilt is not None
    r_s1 = next((s for s in rebuilt.plan_steps() if s["id"] == "s1"), None)
    assert r_s1 and r_s1["status"] == "completed", \
        f"日志重建丢步骤完成状态（F7-3）：{rebuilt.plan_steps()}"
    assert r_s1.get("result_ref") == "提纲已入工作区"
