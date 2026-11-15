from loom.partner import run_turn
from conftest import ScriptedBackend


def test_speech_does_not_mask_unparsed_tool(project):
    backend = ScriptedBackend([
        "我来提一张卡。\n用:不存在的提案工具\n内容:一个候选",
        "用:提设定\n落点:外置大脑/设定集/00-立项.md#分区\n内容:玄幻",
    ])
    events = []
    run_turn(project, "请给候选卡", backend, emit=events.append, ts="format-test")
    # 提案处理还可做一次内容分离检查；主对话必须确实进入第二次。
    assert len(backend.calls) >= 2
    assert any(e.get("t") == "proposal" for e in events)
    assert any(e.get("code") == "tool_format_retry" for e in events)


def test_repeated_unparsed_tools_report_visible_failure(project):
    events = []
    backend = ScriptedBackend(["我来处理。\n用:未知甲", "再试一次。\n用:未知乙"])
    run_turn(project, "请给候选卡", backend, emit=events.append, ts="bad-format")
    assert any(e.get("t") == "error" and e.get("code") == "tool_unparsed" for e in events)
    assert not any(e.get("t") == "proposal" for e in events)
