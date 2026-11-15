"""Expert text protocol regressions through the public run_turn path."""
import pytest

from loom import agent_chat, partner_tools


class Backend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None, **kwargs):
        self.calls.append((system, user))
        text = self.replies.pop(0) if self.replies else ''
        if on_chunk:
            for character in text:
                on_chunk(character)
        return text


def run(project, backend):
    events = []
    agent_chat.run_turn(project, '设定师', '请讨论世界观中的一个规则', backend,
                        emit=events.append, ts='expert-protocol', protocol='text')
    return events


def test_flexible_fenced_stream_and_body_reach_real_proposal(project):
    backend = Backend(['说明\n```loom\nTOOL: 提设定\n落点:外置大脑/设定集/00-立项.md#分区\n内容:玄幻\n多行补充证据\n```'])
    events = run(project, backend)
    proposals = [event for event in events if event.get('t') == 'proposal']
    assert len(proposals) == 1
    assert '玄幻' in proposals[0]['content']
    assert '多行补充证据' in proposals[0]['content']
    preview = ''.join(event.get('text', '') for event in events if event.get('t') == 'assistant_delta')
    assert preview == '说明'
    assert 'TOOL:' not in preview and '```' not in preview
    assert not any(event.get('t') == 'error' for event in events)


def test_unknown_tools_stop_after_bounded_format_retry(project):
    backend = Backend(['说明\n工具:不存在甲\n内容:demo', '说明\n```loom\n不存在乙\n内容:demo\n```'])
    events = run(project, backend)
    assert len(backend.calls) == 2
    assert any(event.get('code') == 'tool_format_retry' for event in events)
    assert any(event.get('code') == 'tool_unparsed' and event.get('t') == 'error' for event in events)
    assert not any(event.get('t') in ('tool', 'proposal') for event in events)
    shown = ''.join(event.get('text', '') for event in events if event.get('t') in ('assistant_delta', 'assistant'))
    assert '不存在' not in shown and '内容:demo' not in shown


def test_bad_format_can_recover_to_proposal(project):
    backend = Backend(['调用:不存在\n内容:demo', '用:提设定\n落点:外置大脑/设定集/00-立项.md#分区\n内容:玄幻'])
    events = run(project, backend)
    assert any(event.get('code') == 'tool_format_retry' for event in events)
    assert any(event.get('t') == 'proposal' for event in events)
    assert not any(event.get('code') == 'tool_unparsed' for event in events)


def test_failed_tool_error_is_fed_back_to_next_model_call(project, monkeypatch):
    backend = Backend(['用:读文件\n路径:不存在.md', '下一步：请先检查文件位置。'])
    calls = []
    def failed_tool(*args, **kwargs):
        calls.append((args, kwargs))
        return {'t': 'result', 'error': 'probe denied receipt'}
    monkeypatch.setattr(partner_tools, 'run_tool', failed_tool)
    events = run(project, backend)
    assert len(calls) == 1
    assert len(backend.calls) == 2
    assert 'probe denied receipt' in backend.calls[1][1]
    assert '"ok": false' in backend.calls[1][1]
    assert any(event.get('code') == 'tool_failed' for event in events)
    assert not any(event.get('t') == 'proposal' for event in events)
