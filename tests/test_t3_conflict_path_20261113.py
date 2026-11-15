# -*- coding: utf-8 -*-
"""T3 残余：partner_confirm 冲突路径端到端。

场景：账本里已有一条「顾明·怕水=是(确认)」，提案正文里模型抽出
「顾明·怕水=否」→ confirm 落盘后事件必须携带 事实冲突 字段。

验证：
- confirm 事件有 事实冲突 列表（非空）
- confirm 事件有 事实冲突提示（人话列表）
- 落盘本身成功（landed 非空）
- 账本里新条目已登记（record 被调用）

不联网：backend 用桩，返回固定 JSON。
"""
from __future__ import annotations

import json

import pytest

from loom import facts_ledger as fl
from loom import partner_store
from loom import usecases
from loom import journey as journey_mod


class _StubBackend:
    """桩后端：complete 返回固定 JSON 数组（模拟模型抽取）。"""

    def __init__(self, entries_json: str):
        self._entries_json = entries_json

    def complete(self, system, user_prompt, **kw):
        return self._entries_json

    def complete_with_tools(self, messages, tools, **kw):
        return {"content": self._entries_json, "tool_calls": []}


@pytest.fixture
def book(tmp_path):
    (tmp_path / "loom.toml").write_text('"书名" = "测试书"\n', encoding="utf-8")
    return tmp_path


def _seed_conflict(book, ent="顾明", attr="怕水", val="是", status="确认"):
    """预置一条已确认的事实到账本。"""
    fl.record(book, [fl.normalize_entry(
        {"实体": ent, "属性": attr, "取值": val, "状态": status},
        source="预置")])


def _seed_proposal(book, pid, slot, content, before=""):
    """在伙伴流水里写一条 proposal 事件。"""
    ev = {"t": "proposal", "id": pid, "slot": slot, "content": content}
    if before:
        ev["before"] = before
    partner_store.append_event(book, ev)


def _patch_landing(monkeypatch):
    """让 _land_slot 不校验槽位存在性（tmp 书没有设定集文件）。
    冲突检查在落盘**之前**，所以只需落盘不抛即可。"""
    monkeypatch.setattr(journey_mod, "_land_slot",
                        lambda root, slot_id, content: f"外置大脑/设定集/{slot_id.split('#')[0]}")


def test_confirm_with_conflict_carries_fact_conflict(book, monkeypatch):
    """预置冲突条目 → confirm 落盘 → 事件携带 事实冲突。"""
    _patch_landing(monkeypatch)

    # 1. 预置账本：顾明·怕水=是（确认）
    _seed_conflict(book)

    # 2. 桩后端：模型从提案正文抽出 顾明·怕水=否（与预置冲突）
    stub = _StubBackend(json.dumps(
        [{"实体": "顾明", "属性": "怕水", "取值": "否"}], ensure_ascii=False))
    monkeypatch.setattr(usecases, "cheap_backend", lambda cfg: stub)
    monkeypatch.setattr(usecases, "get_backend", lambda cfg: stub)

    # 3. 写一条 proposal（slot 用设定集里存在的槽位）
    pid = "test-pid-001"
    slot = "外置大脑/设定集/00-立项.md#主角"
    content = "顾明其实不怕水，他喜欢游泳。"
    _seed_proposal(book, pid, slot, content)

    # 4. 调 partner_confirm
    result = usecases.partner_confirm(book, pid, ts="2026-11-13T00:00:00")

    # 5. 断言
    # 落盘成功（不因为冲突而阻断）
    assert "landed" in result, f"confirm 应落盘，实际: {result}"
    assert "error" not in result, f"不应有 error: {result}"

    # 事件携带 事实冲突
    events = partner_store.read_events(book)
    confirm_ev = next((e for e in events if e.get("t") == "confirm" and e.get("id") == pid), None)
    assert confirm_ev is not None, "应有 confirm 事件"
    assert "事实冲突" in confirm_ev, f"confirm 事件应携带 事实冲突: {confirm_ev}"
    assert len(confirm_ev["事实冲突"]) >= 1, "冲突列表非空"
    assert confirm_ev["事实冲突"][0]["实体"] == "顾明"
    assert confirm_ev["事实冲突"][0]["属性"] == "怕水"
    assert confirm_ev["事实冲突"][0]["类型"] == "确认vs确认"

    # 人话提示
    assert "事实冲突提示" in confirm_ev
    assert any("顾明" in line for line in confirm_ev["事实冲突提示"])

    # 账本里新条目已登记（record 被调用）
    entries = fl.load(book)
    new_entries = [e for e in entries if e["取值"] == "否"]
    assert len(new_entries) >= 1, "新条目（取值=否）应已登记到账本"


def test_confirm_without_conflict_no_fact_field(book, monkeypatch):
    """无冲突时 confirm 事件不应有 事实冲突 字段。"""
    _patch_landing(monkeypatch)

    # 桩后端：模型抽出的事实与账本一致（同值不算冲突）
    stub = _StubBackend(json.dumps(
        [{"实体": "顾明", "属性": "怕水", "取值": "是"}], ensure_ascii=False))
    monkeypatch.setattr(usecases, "cheap_backend", lambda cfg: stub)
    monkeypatch.setattr(usecases, "get_backend", lambda cfg: stub)

    # 预置同值（不冲突）
    _seed_conflict(book, val="是")

    pid = "test-pid-002"
    slot = "外置大脑/设定集/00-立项.md#主角"
    content = "顾明确实怕水。"
    _seed_proposal(book, pid, slot, content)

    result = usecases.partner_confirm(book, pid, ts="2026-11-13T00:00:00")
    assert "landed" in result

    events = partner_store.read_events(book)
    confirm_ev = next((e for e in events if e.get("t") == "confirm" and e.get("id") == pid), None)
    assert confirm_ev is not None
    assert "事实冲突" not in confirm_ev, f"无冲突时不应有 事实冲突 字段: {confirm_ev}"


def test_confirm_extraction_failure_no_conflict_no_crash(book, monkeypatch):
    """抽取后端挂了 → 不崩、不报冲突、落盘照常。"""
    _patch_landing(monkeypatch)

    class _Broken:
        def complete(self, *a, **k):
            raise RuntimeError("模型挂了")
        def complete_with_tools(self, *a, **k):
            raise RuntimeError("模型挂了")

    monkeypatch.setattr(usecases, "cheap_backend", lambda cfg: _Broken())
    monkeypatch.setattr(usecases, "get_backend", lambda cfg: _Broken())

    pid = "test-pid-003"
    slot = "外置大脑/设定集/00-立项.md#主角"
    content = "随便一些文字。"
    _seed_proposal(book, pid, slot, content)

    result = usecases.partner_confirm(book, pid, ts="2026-11-13T00:00:00")
    assert "landed" in result, f"抽取失败不应阻断落盘: {result}"
    assert "error" not in result

    events = partner_store.read_events(book)
    confirm_ev = next((e for e in events if e.get("t") == "confirm" and e.get("id") == pid), None)
    assert confirm_ev is not None
    assert "事实冲突" not in confirm_ev, "抽取失败时不应有 事实冲突"
