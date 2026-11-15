"""Dialog protocol filtering: flexible opt-in, strict callers unchanged."""
import pytest

from loom import parse


@pytest.mark.parametrize("prefix", ["用", "调用", "工具", "tool", "Tool", "TOOL", "invoke", "Invoke"])
def test_unknown_flexible_attempt_is_botched(prefix):
    raw = f"说明\n{prefix}:不存在\n内容:demo"
    say, tools = parse.parse_tool_blocks(raw, {"提设定"}, flexible=True)
    assert tools == []
    assert parse.strip_protocol_lines(say, flexible=True) == ("说明", True)


@pytest.mark.parametrize("first", ["提设定", "TOOL: 提设定", "调用:提设定"])
def test_valid_fences_parse_without_stream_leak(first):
    raw = f"说明\n```loom\n{first}\n内容:demo\n```"
    say, tools = parse.parse_tool_blocks(raw, {"提设定"}, {"提设定": ("内容",)}, flexible=True)
    assert say == "说明"
    assert [t["name"] for t in tools] == ["提设定"]
    for split in range(len(raw) + 1):
        emitted = []
        chunk, flush = parse.stream_line_relay(emitted.append, flexible=True)
        chunk(raw[:split])
        chunk(raw[split:])
        flush()
        assert emitted == ["说明"]


@pytest.mark.parametrize("body", ["不存在\n内容:demo", "TOOL: 不存在\n内容:demo", "调用:不存在\n内容:demo"])
def test_unknown_fence_remains_detectable(body):
    raw = f"说明\n```tool\n{body}\n```\n后话"
    say, tools = parse.parse_tool_blocks(raw, {"提设定"}, flexible=True)
    assert tools == []
    assert parse.strip_protocol_lines(say, flexible=True) == ("说明\n后话", True)


@pytest.mark.parametrize("prefix", ["TOOL", "用", "调用"])
def test_legacy_external_header_parameter_fence(prefix):
    raw = f"说明\n\n{prefix}: 提设定\n```loom\n内容: 金手指改为双生树\n落点: 示例#核心功能\n```"
    say, tools = parse.parse_tool_blocks(raw, {"提设定"}, {"提设定": ("内容", "落点")}, flexible=True)
    assert say == "说明"
    assert tools == [{"name": "提设定", "params": {"内容": "金手指改为双生树", "落点": "示例#核心功能"}, "body": ""}]


def test_unknown_external_header_is_not_promoted():
    raw = "TOOL: 不存在\n```loom\n内容: demo\n```"
    say, tools = parse.parse_tool_blocks(raw, {"提设定"}, flexible=True)
    assert not tools
    assert parse.strip_protocol_lines(say, flexible=True) == ("", True)


def test_unclosed_fence_is_hidden_and_botched():
    raw = "说明\n```loom\n不存在\n内容:demo"
    say, tools = parse.parse_tool_blocks(raw, {"提设定"}, flexible=True)
    assert not tools
    assert parse.strip_protocol_lines(say, flexible=True) == ("说明", True)
    emitted = []
    chunk, flush = parse.stream_line_relay(emitted.append, flexible=True)
    for character in raw:
        chunk(character)
    flush()
    assert emitted == ["说明"]


def test_strict_default_and_ordinary_code_are_unchanged():
    raw = "工具:锤子\n用途:说明"
    assert not parse.is_trigger_line("工具:锤子")
    assert parse.strip_protocol_lines(raw) == (raw, False)
    emitted = []
    chunk, flush = parse.stream_line_relay(emitted.append)
    chunk(raw)
    flush()
    assert emitted == raw.splitlines()
    ordinary = "```python\nprint('demo')\n```"
    assert parse.strip_protocol_lines(ordinary, flexible=True) == (ordinary, False)
    assert parse.is_trigger_line("用:不存在")
    assert parse.strip_protocol_lines("用:不存在\n内容:demo") == ("", True)


def test_flexible_trigger_and_flush_without_newline():
    assert parse.is_trigger_line("调用:不存在", flexible=True)
    assert parse.is_trigger_line("```loom", flexible=True)
    assert not parse.is_trigger_line("```loom")
    for raw in ["调用:不存在", "```loom"]:
        emitted = []
        chunk, flush = parse.stream_line_relay(emitted.append, flexible=True)
        chunk(raw)
        flush()
        assert emitted == []
