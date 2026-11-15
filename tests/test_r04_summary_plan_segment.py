"""R-04：答复里的 TASK_PLAN 协议段不得挤占任务摘要预算。

裁定（需求方 审查记录）：
① 在 agent_chat / study 两个 outcome 写入点，先剥离从首个 `TASK_PLAN:` 行起的整块
   （含其后编号步骤）再交 outcome_summary；
② 不做整段丢弃式截断（结论常紧邻协议段之前）；
③ plan_update 折叠路径与 agents.py 的固定短句不动。
"""
from __future__ import annotations

from loom import task_state as ts
from loom import turn_prepare as tp


BODY = "结论：本章把主角推到了矿难现场。" + "铺垫细节。" * 40
PROTOCOL = "\nTASK_PLAN: 因为设定变了\n1. 重写第一场 [done]\n2. 补第二场钩子\n3. 收束"


def test_outcome_summary_currently_keeps_protocol_text():
    """现状刻画（修复前后都成立）：直接交原文时，协议段会进摘要并挤占尾部预算。"""
    summary = ts.outcome_summary(BODY + PROTOCOL)
    assert "TASK_PLAN" in summary
    assert "收束" in summary          # 尾部被协议段占据


def test_strip_plan_segment_drops_protocol_and_keeps_conclusion():
    stripped = tp.strip_plan_segment(BODY + PROTOCOL)
    assert "TASK_PLAN" not in stripped
    assert "[done]" not in stripped
    assert stripped.startswith("结论：本章把主角推到了矿难现场。")
    summary = ts.outcome_summary(stripped)
    assert "TASK_PLAN" not in summary
    assert summary.startswith("结论：本章把主角推到了矿难现场。")


def test_strip_plan_segment_preserves_conclusion_next_to_protocol():
    """结论紧邻协议段之前时不能被整段丢弃式截断带走。"""
    text = "结论在协议段正上方，必须留下。" + "\nTASK_PLAN: 原因\n1. 甲\n2. 乙"
    assert tp.strip_plan_segment(text) == "结论在协议段正上方，必须留下。"


def test_strip_plan_segment_without_protocol_is_unchanged():
    assert tp.strip_plan_segment("纯正文，没有协议段。") == "纯正文，没有协议段。"
    assert tp.strip_plan_segment("") == ""


def test_strip_plan_segment_only_protocol_yields_empty():
    """只有协议段、无正文 —— 先测现状：剥离后为空（本轮没有可摘要正文）。"""
    assert tp.strip_plan_segment("TASK_PLAN: 只有协议\n1. 甲\n2. 乙") == ""


def test_committed_outcome_excludes_protocol(tmp_path):
    """集成：剥离后写进任务状态摘要，盘上不再出现协议文本。"""
    st = ts.TaskState.create({"role": "设定师", "conversation_id": "默认"}, "R04", task_id="t-r04")
    ts.append_task_event(tmp_path, "t-r04", {"kind": "start", "ts": ts.now_ms(),
                                             "scope": st._d.get("scope"), "goal": "R04",
                                             "applied_revision": st.revision})
    ts.save_snapshot(tmp_path, st)
    ts.commit(tmp_path, st, {"kind": "outcome", "summary": tp.strip_plan_segment(BODY + PROTOCOL)})
    assert "TASK_PLAN" not in st._d["last_outcome"]
    assert st._d["last_outcome"].startswith("结论：本章把主角推到了矿难现场。")
