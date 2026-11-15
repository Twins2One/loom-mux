"""High context guard receipts reflect the actual scratchpad outcome."""
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from conftest import ScriptedBackend
from loom import context_guard, partner, scratchpad


@pytest.mark.parametrize("save_fails", [False, True], ids=["saved", "save_failed"])
def test_high_guard_uses_real_append_signature_and_truthful_receipt(project, monkeypatch, save_fails):
    raw = "原始模型草稿标记：这是尚未核验的讨论内容。"
    backend = ScriptedBackend([raw])
    monkeypatch.setattr(context_guard, "check", lambda *_: SimpleNamespace(level="high"))
    # Autospec enforces append's actual keyword-only author_note signature.
    spy = create_autospec(scratchpad.append, spec_set=True)
    if save_fails:
        spy.side_effect = OSError("simulated scratchpad failure")
    monkeypatch.setattr(scratchpad, "append", spy)
    events = []

    partner.run_turn(project, "讨论一个写作问题", backend, emit=events.append, ts="guard-receipt")

    spy.assert_called_once()
    args, kwargs = spy.call_args
    assert len(args) == 3
    assert args[0] == "领航员"
    assert args[1] == project
    assert kwargs == {"author_note": "上下文守卫·高阈值自动生成"}
    summary = args[2]
    assert raw in summary
    assert "尚未解析执行的模型草稿" in summary
    assert "不构成工具或保存回执" in summary
    summaries = [event["text"] for event in events if event.get("t") == "summary"]
    assert summaries == [summary]
    statements = [event["text"] for event in events if event.get("t") == "assistant"]
    assert len(statements) == 1
    if save_fails:
        assert "速记本未保存成功" in statements[0]
        assert "已保存" not in statements[0]
    else:
        assert "已保存未验证草稿到速记本" in statements[0]
        assert "未保存成功" not in statements[0]
    assert not any(event.get("t") in {"proposal", "tool", "confirm"} for event in events)
    assert len(backend.calls) == 1
