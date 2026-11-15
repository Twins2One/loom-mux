"""Actual study role loop protocol regressions; no external model calls."""
import importlib

import pytest

ask = importlib.import_module('loom.study.ask')


class Toolset:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def contract(self):
        return 'format'

    def schema(self):
        return []

    def run(self, name, args):
        self.calls.append((name, args))
        return {'ok': not self.fail, 'text': 'denied' if self.fail else 'receipt'}


class TextBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user, on_chunk=None, **kwargs):
        self.calls.append(user)
        result = self.replies.pop(0)
        if on_chunk:
            for ch in result:
                on_chunk(ch)
        return result


@pytest.mark.parametrize('role,name,params', [
    ('知识顾问', '取稿', {'地址': 'https://example.invalid/video'}),
    ('知识顾问', '查知识', {'问题': '器物年代', '范围': '历史'}),
    ('语料分析师', '蒸馏', {'档案': 'demo', '文本': '样本'}),
    ('', '查这本书', {'册': 'demo', '查询': '节奏'}),
])
def test_role_parameter_specs_are_single_source(monkeypatch, role, name, params):
    tools = Toolset()
    monkeypatch.setattr(ask, '_toolset_for', lambda _: tools)
    # Parameter names really declared by this role's current spec.
    declared = ask._text_tool_params((name,), role)[name]
    params = {key: value for key, value in params.items() if key in declared}
    assert params
    raw = '```loom\nTOOL: ' + name + '\n' + '\n'.join(k + ': ' + v for k, v in params.items()) + '\n```'
    backend = TextBackend([raw, '最终回答'])
    events = []
    assert ask._chat_with_tools(backend, 's', 'u', events.append, 't', tools_role=role) == '最终回答'
    assert tools.calls == [(name, params)]
    shown = ''.join(e.get('text', '') for e in events if e['t'] == 'delta')
    assert 'TOOL:' not in shown and '```loom' not in shown


def test_unknown_format_retries_then_exhausts_without_execution(monkeypatch):
    tools = Toolset()
    monkeypatch.setattr(ask, '_toolset_for', lambda _: tools)
    events = []
    backend = TextBackend(['工具:不存在\n内容:demo', '```loom\n不存在\n内容:demo\n```'])
    assert ask._chat_with_tools(backend, 's', 'u', events.append, 't') == ''
    assert tools.calls == []
    assert len(backend.calls) == 2
    assert any(e.get('code') == 'tool_format_retry' for e in events)
    assert any(e.get('code') == 'tool_unparsed' for e in events)
    assert not any(e['t'] == 'delta' for e in events)


def test_bad_format_can_recover_to_valid_tool(monkeypatch):
    tools = Toolset()
    monkeypatch.setattr(ask, '_toolset_for', lambda _: tools)
    backend = TextBackend(['调用:不存在', '用:列书册', '回答'])
    events = []
    assert ask._chat_with_tools(backend, 's', 'u', events.append, 't') == '回答'
    assert tools.calls == [('列书册', {})]
    assert not any(e['t'] == 'error' for e in events)


@pytest.mark.parametrize('native', [False, True])
def test_tool_failure_is_explicit_and_stops(monkeypatch, native):
    tools = Toolset(fail=True)
    monkeypatch.setattr(ask, '_toolset_for', lambda _: tools)
    backend = TextBackend(['用:列书册'])
    if native:
        backend.complete_with_tools = lambda *a, **k: {'tool_calls': [{'id': 'c', 'name': '列书册', 'arguments': '{}'}]}
    events = []
    assert ask._chat_with_tools(backend, 's', 'u', events.append, 't') == ''
    assert len(tools.calls) == 1
    assert any(e.get('code') == 'tool_failed' for e in events)


def test_native_error_after_execution_does_not_restart_text(monkeypatch):
    tools = Toolset()
    monkeypatch.setattr(ask, '_toolset_for', lambda _: tools)
    backend = TextBackend([])
    calls = []
    def native(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return {'tool_calls': [{'id': 'c', 'name': '列书册', 'arguments': '{}'}]}
        raise RuntimeError('native unavailable')
    backend.complete_with_tools = native
    events = []
    assert ask._chat_with_tools(backend, 's', 'u', events.append, 't') == ''
    assert len(tools.calls) == 1 and backend.calls == []
    assert any(e.get('code') == 'native_after_tool_failed' for e in events)


def test_initial_native_failure_can_fallback_with_notice(monkeypatch):
    tools = Toolset()
    monkeypatch.setattr(ask, '_toolset_for', lambda _: tools)
    backend = TextBackend(['回答'])
    def native(*args, **kwargs):
        raise RuntimeError('unsupported')
    backend.complete_with_tools = native
    events = []
    assert ask._chat_with_tools(backend, 's', 'u', events.append, 't') == '回答'
    assert tools.calls == []
    assert any('尚未执行工具' in e.get('text', '') for e in events)


def test_final_round_tool_protocol_is_not_executed_or_shown(monkeypatch):
    tools = Toolset()
    monkeypatch.setattr(ask, '_toolset_for', lambda _: tools)
    backend = TextBackend(['用:列书册', '用:列书册'])
    events = []
    assert ask._chat_with_tools(backend, 's', 'u', events.append, 't', max_rounds=1) == ''
    assert len(tools.calls) == 1
    assert any(e.get('code') == 'tool_round_limit' for e in events)
    assert not any(e['t'] == 'delta' for e in events)


def test_cancel_stops_before_tool_execution(monkeypatch):
    tools = Toolset()
    monkeypatch.setattr(ask, '_toolset_for', lambda _: tools)
    backend = TextBackend(['用:列书册'])
    events = []
    assert ask._chat_with_tools(backend, 's', 'u', events.append, 't', should_cancel=lambda: bool(backend.calls)) == ''
    assert tools.calls == [] and events == []
