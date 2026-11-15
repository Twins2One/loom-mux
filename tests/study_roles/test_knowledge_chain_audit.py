"""知识与多模态首轮审计：刻画当前调用边界，不代表目标行为。全离线。"""
from copy import deepcopy

import pytest
from loom.study import ask as A, video as V

# 一像素 PNG；只验证传输结构，不据此推断视觉理解质量。
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(A, "_auto_memory", lambda *a, **k: "")
    monkeypatch.setattr(A, "read_current", lambda *a: None)
    hit = {"科目": "审计", "卷": "样本册", "节": "证据", "路径": "sample.md",
           "正文": "LOCAL_EVIDENCE_MARKER"}
    monkeypatch.setattr(A, "search", lambda *a, **k: {
        "节选": [hit], "统计": {"段": 1, "给了": 1}})


def test_professional_sources_event_is_not_initial_model_evidence(quiet):
    class Backend:
        def complete(self, system, user, **kwargs):
            self.user = user
            return "答复"
    for role in ("知识顾问", "语料分析师"):
        be, events = Backend(), []
        A.ask("请说明样本", events.append, backend=be, role=role)
        assert any(e["t"] == "sources" and e["引用"] for e in events)
        assert "LOCAL_EVIDENCE_MARKER" not in be.user
    be = Backend()
    A.ask("请说明样本", lambda e: None, backend=be)
    assert "LOCAL_EVIDENCE_MARKER" in be.user


def test_task_outcome_and_step_titles_are_not_projected():
    task = {"task_id": "t-audit", "goal": "核对资料", "status": "waiting_author",
            "revision": 4, "last_outcome": "OUTCOME_MARKER"}
    block = A._task_facts_block(task, [], {
        "plan_revision": 1,
        "steps": [{"id": "s1", "title": "STEP_TITLE_MARKER", "status": "completed"}]}, [])
    assert "核对资料" in block and "已完成 1" in block
    assert "OUTCOME_MARKER" not in block
    assert "STEP_TITLE_MARKER" not in block


@pytest.fixture
def image_tool(monkeypatch):
    class Tools:
        def schema(self):
            return []
        def contract(self):
            return "查询工具"
        def run(self, *a, **k):
            return {"ok": True, "text": "TOOL_EVIDENCE_MARKER",
                    "images": [{"mime": "image/png", "b64": PNG, "times": [1.5]}]}
    monkeypatch.setattr(A, "_toolset_for", lambda *a: Tools())


def test_native_failure_after_tool_stops_without_losing_receipt_or_repeating(image_tool, monkeypatch):
    """旧坏行为：已有工具结果后降 text 丢回执；目标为中止且不重复执行。"""
    toolset = A._toolset_for('知识顾问')
    tool_calls = []
    original_run = toolset.run
    def tracked_run(*args, **kwargs):
        tool_calls.append((args, kwargs))
        return original_run(*args, **kwargs)
    toolset.run = tracked_run
    monkeypatch.setattr(A, '_toolset_for', lambda *a: toolset)
    class Backend:
        def __init__(self):
            self.n = 0
            self.users = []
        def complete_with_tools(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return {"content": "", "tool_calls": [
                    {"id": "c1", "name": "lookup_knowledge", "arguments": "{}"}]}
            raise RuntimeError("offline protocol rejection")
        def complete(self, system, user, **kwargs):
            self.users.append(deepcopy(user))
            return "终答"
    be, events = Backend(), []
    A._chat_with_tools(be, "SYS", "问题", events.append, "ts", tools_role="知识顾问")
    assert any(e.get('t') == 'error' and e.get('code') == 'native_after_tool_failed' for e in events)
    assert not any("改用文本协议" in e.get("text", "") for e in events)
    assert be.users == []
    assert len(tool_calls) == 1
    assert be.n == 2


def test_text_third_call_does_not_retain_previous_image(image_tool):
    class Backend:
        def __init__(self):
            self.users = []
        def complete(self, system, user, **kwargs):
            self.users.append(deepcopy(user))
            return "用：查知识" if len(self.users) <= 2 else "终答"
    be = Backend()
    A._chat_with_tools(be, "SYS", "问题", lambda e: None, "ts", tools_role="知识顾问")
    assert len(be.users) == 3
    assert isinstance(be.users[1], list) and "image_url" in str(be.users[1])
    assert isinstance(be.users[2], str) and "image_url" not in be.users[2]
    assert "TOOL_EVIDENCE_MARKER" in be.users[2]


def test_image_typeerror_fallback_has_no_coverage_event(image_tool):
    class Backend:
        def __init__(self):
            self.n = 0
        def complete(self, system, user, **kwargs):
            if isinstance(user, list):
                raise TypeError("offline text-only interface")
            self.n += 1
            return "用：查知识" if self.n == 1 else "终答"
    events = []
    out = A._chat_with_tools(Backend(), "SYS", "问题", events.append, "ts", tools_role="知识顾问")
    assert out == "终答"
    assert not any("降级" in e.get("text", "") or "仅文字" in e.get("text", "") for e in events)


def test_evidence_text_drops_top_level_ocr_coverage_and_grid_times():
    out = V.evidence_text({"text": "字幕样本", "画面文字": "OCR_MARKER",
                           "画面覆盖": "部分", "coverage": {"note": "COVERAGE_MARKER"},
                           "images": [{"path": "local.png", "times": [1.5, 3.0]}]})
    assert "字幕样本" in out
    assert "OCR_MARKER" not in out and "COVERAGE_MARKER" not in out
    assert "1.5" not in out
