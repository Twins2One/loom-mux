# -*- coding: utf-8 -*-
"""对话压缩 + 现行三级上下文守卫。"""
from __future__ import annotations

from loom import compact, context_guard as cg


def test_三道阈值边界():
    assert cg.check(10, 100).level is None
    assert cg.check(35, 100).level == "prep"
    assert cg.check(39, 100).level == "prep"
    assert cg.check(40, 100).level == "low"
    assert cg.check(59, 100).level == "low"
    assert cg.check(60, 100).level == "high"
    assert cg.check(0, 0).level is None


def test_预备阈值那句要人真去写速记():
    msg = cg.PREP_WARNING
    assert "速记本" in msg
    assert "摘要" in msg


def _tail(n=40):
    ev = []
    for i in range(n):
        ev.append({"t": "user", "text": "作者%d:这个方向对不对" % i})
        ev.append({"t": "assistant", "text": "回%d:建议这样改" % i})
        ev.append({"t": "tool_result", "text": "x" * 200})
    return ev


def test_压缩保留近段且旧段变摘要():
    tail = _tail()
    out, summary = compact.compress(tail, keep_chars=600, role="领航员")
    assert summary
    assert out[0]["t"] == "summary"
    assert out[0]["text"] == summary
    assert len(out) < len(tail)
    assert "作者第0句" not in summary
    assert "tool_result" not in str(out)


def test_压缩不编_摘要每行都能在原事件里找到():
    tail = [
        {"t": "user", "text": "要写一个反转"},
        {"t": "assistant", "text": "建议在第三章埋伏笔"},
        {"t": "candidate", "slot": "主角", "content": "顾明"},
    ]
    text = compact.summarize(tail, role="领航员")
    hay = " ".join(str(ev.get("text", "")) + str(ev.get("slot", "")) + " " + str(ev.get("content", "")) for ev in tail)
    items = [ln[2:] for ln in text.splitlines() if ln.startswith("- ")]
    assert items
    for it in items:
        assert it in hay, it


def test_没东西可压时原样返回():
    tail = [{"t": "user", "text": "短"}]
    out, s = compact.compress(tail, keep_chars=100000)
    assert s == ""
    assert out == tail
    out2, s2 = compact.compress([{"t": "tool_result", "text": "x" * 500}], keep_chars=10)
    assert s2 == ""


def test_摘要渲染成一行给模型看():
    from loom.partner_context import _render_event
    got = _render_event({"t": "summary", "text": "作者要反转"})
    assert "摘要" in got
    assert "作者要反转" in got
