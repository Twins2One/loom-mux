"""6d 项目及角色读取边界。"""
import pytest
from fastapi import HTTPException
from loom.api import agents


@pytest.mark.parametrize("reader", [
    lambda root: agents.scratchpads_list_ep(root),
    lambda root: agents.scratchpad_load_ep("设定师", root),
    lambda root: agents.scratchpad_append_ep("设定师", agents.ScratchpadAppendBody(root=root, content="记录")),
    lambda root: agents.agent_chat_history_ep(root, "设定师"),
])
def test_nonproject_root_rejected(tmp_path, reader):
    with pytest.raises(HTTPException):
        reader(str(tmp_path))


def test_chat_role_must_be_known():
    from loom.agent_chat import _chat_rel
    with pytest.raises(ValueError):
        _chat_rel("陌生角色")
    assert "设定师" in _chat_rel("设定师")


def test_study_constraints_and_plan_reach_next_turn(tmp_path, monkeypatch):
    import asyncio
    import json
    from loom.study import AskBody, ask_route, library, ask
    monkeypatch.setattr(library, "library_root", lambda: tmp_path)
    captured = []

    def fake(question, emit, **kwargs):
        captured.append(kwargs)
        emit({"t": "delta", "text": "背景说明" + "材料" * 300 + "最终结论：来源已核实。\nTASK_PLAN:\n1. [done] 核查材料\n2. 整理结果"})

    monkeypatch.setattr(ask, "ask", fake)

    async def collect(response):
        return [json.loads(line) async for line in response.body_iterator]

    first = asyncio.run(collect(ask_route(AskBody(question="必须保留来源", role="知识顾问", goal="核查资料"))))
    assert any(e.get("t") == "plan_updated" for e in first)
    asyncio.run(collect(ask_route(AskBody(question="继续", role="知识顾问"))))
    assert any("保留来源" in c["content"] for c in captured[1]["constraints"])
    assert len(captured[1]["plan_progress"]["steps"]) == 2
    assert captured[1]["task"]["status"] == "waiting_author"
    from loom.study import ask_task_ep
    outcome = ask_task_ep(role="知识顾问")["tasks"][0]["last_outcome"]
    assert outcome.startswith("背景说明") and "最终结论：来源已核实。" in outcome
    assert len(outcome) <= 300


@pytest.mark.parametrize("failure", ["error_event", "exception", "retired"])
def test_study_failed_or_retired_answer_does_not_finish(tmp_path, monkeypatch, failure):
    import asyncio
    import json
    from loom.study import AskBody, ask_route, ask_task_ep, library, ask
    from loom import request_turn
    monkeypatch.setattr(library, "library_root", lambda: tmp_path)

    def fake(question, emit, **kwargs):
        emit({"t": "delta", "text": "部分答复"})
        if failure == "error_event":
            emit({"t": "error", "text": "后端失败"})
        elif failure == "exception":
            raise RuntimeError("后端失败")
        else:
            request_turn.begin(tmp_path / ".loom-tasks", "知识顾问", "默认", scoped_conversation=True)
        emit({"t": "delta", "text": "迟到尾部"})

    monkeypatch.setattr(ask, "ask", fake)

    async def collect(response):
        return [json.loads(line) async for line in response.body_iterator]

    events = asyncio.run(collect(ask_route(AskBody(question="核查", role="知识顾问", goal="核查资料"))))
    state = ask_task_ep(role="知识顾问")["tasks"][0]
    assert state["last_outcome"] == "" and state["status"] == "ready"
    assert not any(e.get("t") == "task_status" for e in events)
    if failure == "retired":
        assert not any(e.get("text") == "迟到尾部" for e in events)


def test_study_prepare_failure_has_diagnostic_and_answer(tmp_path, monkeypatch):
    import asyncio
    import json
    from loom.study import AskBody, ask_route, library, ask
    from loom import turn_prepare
    monkeypatch.setattr(library, "library_root", lambda: tmp_path)

    def broken(**kwargs):
        raise RuntimeError("准备失败")

    monkeypatch.setattr(turn_prepare, "prepare_turn", broken)
    monkeypatch.setattr(ask, "ask", lambda question, emit, **kwargs: emit({"t": "delta", "text": "普通答复"}))

    async def collect(response):
        return [json.loads(line) async for line in response.body_iterator]

    events = asyncio.run(collect(ask_route(AskBody(question="普通问题"))))
    assert any("任务准备暂不可用" in e.get("text", "") for e in events)
    assert any(e.get("text") == "普通答复" for e in events)


def test_retarget_unknown_role_rejected_before_lookup(project):
    with pytest.raises(HTTPException):
        agents.proposal_retarget_ep(agents.RetargetBody(root=str(project), role="陌生角色",
                                                      id="missing", new_slot="设定集#项目"))
