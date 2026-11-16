# -*- coding: utf-8 -*-
"""夜间补缺(P1-2/P1-1 等)测试 · 2026-11-08 第二班。

覆盖:
- P1-2 conversation_id 清洗(路径注入回退「默认」)与 task_id contain 校验;
- P1-1 作者显式约束分句立约 → 下一轮有效约束承接;
- P2 死条件清理等价行为。
"""
from __future__ import annotations

import json

import pytest

from loom import agent_chat, task_state as ts, turn_prepare as tp
from loom.paths import TASKS_DIR


# ── P1-2 conversation_id 清洗 ────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["../逃逸", "..\\逃逸", "..", ".", "/abs", "c:\\x",
                                 "a/b", "a\\b", "a/../..", "", "  "])
def test_sanitize_conversation_id_rejects_traversal(bad):
    assert ts.sanitize_conversation_id(bad) == "默认"


@pytest.mark.parametrize("ok", ["默认", "代际2", "session-abc123", "书房-角色A"])
def test_sanitize_conversation_id_passes_normal(ok):
    assert ts.sanitize_conversation_id(ok) == ok


def test_run_turn_path_injection_falls_back(tmp_path):
    """conversation_id 带 ../ 注入:任务目录仍落书根内,不在书外建目录。"""
    from loom.paths import TASKS_DIR

    class _Backend:
        def complete(self, system, user, max_chars=1000, on_chunk=None):
            return "好的。"

    events = []
    agent_chat.run_turn(tmp_path, "批评家", "分镜是干嘛的？", _Backend(),
                        emit=events.append, conversation_id="../../逃逸")
    tasks_root = tmp_path / TASKS_DIR
    # 书外没有建目录
    outside = tmp_path.parent / "逃逸"
    assert not outside.exists()
    # 注入值被清洗成「默认」
    assert not (tasks_root / "../../逃逸").exists()


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_task_id_contain_check(tmp_path):
    """task_id 带 ../ 注入:写入/读取侧 contain 校验拒绝越界(P2 防御)。"""
    base = tmp_path / "任务"
    base.mkdir()
    with pytest.raises(ValueError):
        ts.append_task_event(base, "../../逃逸", {"kind": "start"})
    with pytest.raises(ValueError):
        ts.load_snapshot(base, "..\\外")
    # 正常 task_id 不受影响
    st = ts.TaskState.create({}, "写好第三章")
    ts.append_task_event(base, st.task_id, {"kind": "start"})
    assert (base / f"{st.task_id}.jsonl").is_file()


# ── P1-1 约束跨轮承接(分句立约)────────────────────────────────────────────
class _Echo:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None):
        self.calls.append((system, user))
        out = self.replies.pop(0) if self.replies else ""
        if on_chunk and out:
            on_chunk(out)
        return out


def test_extract_constraints_prefixes():
    from loom.agent_chat import _extract_constraints
    assert _extract_constraints("记住：力量体系必须带代价") == ["记住：力量体系必须带代价"]
    assert _extract_constraints("别用第一人称。必须金手指有代价。再细化第 2 轮") == \
        ["别用第一人称", "必须金手指有代价"]
    assert _extract_constraints("分镜是干嘛的？") == []       # 疑问句不提
    assert _extract_constraints("今天天气不错") == []
    assert _extract_constraints("") == []


def test_约束分句立约跨轮承接(project):
    """§10 场景1(P1-1):第一轮建任务,第二轮分句立约束,第三轮提示词含该约束。"""
    from loom.paths import TASKS_DIR
    evs = []
    bk = _Echo(["好,先读现有设定。"])
    agent_chat.run_turn(project, "设定师", "重做力量体系", bk, emit=evs.append,
                        ts="t1", protocol="text")
    base = project / TASKS_DIR / "默认"
    snaps = list(base.glob("*.快照.json"))
    assert len(snaps) == 1
    # 第二轮:分句立约束
    bk2 = _Echo(["收到,记下了。"])
    agent_chat.run_turn(project, "设定师", "记住:力量必须带代价。再细化第 2 轮", bk2,
                        emit=evs.append, ts="t2", protocol="text")
    st = ts.load_snapshot(base, snaps[0].name.replace(".快照.json", ""))
    assert any("力量必须带代价" in c["content"] for c in st._d["constraints"]), \
        f"约束应折叠进任务状态: {st._d['constraints']}"
    assert all(c.get("kind") == "显式" for c in st._d["constraints"]), "作者原话=explicit"
    # 第三轮:有效约束进提示词
    bk3 = _Echo(["好的,按约束来。"])
    agent_chat.run_turn(project, "设定师", "继续", bk3, emit=evs.append,
                        ts="t3", protocol="text")
    assert any("力量必须带代价" in s for s, _ in bk3.calls), \
        "下一轮提示词应包含已立约束"
    # 事件流带确认 status
    assert any("约束" in (e.get("text") or "") and e.get("t") == "status" for e in evs)


def test_无活动任务时约束不入状态(project):
    """简单问答轮里说的约束话:不入任务状态(没有任务可挂),不报错。"""
    from loom.paths import TASKS_DIR
    evs = []
    bk = _Echo(["好的。"])
    agent_chat.run_turn(project, "设定师", "记住:必须用短句。顺便聊聊节奏", bk,
                        emit=evs.append, ts="t1", protocol="text")
    assert not list((project / TASKS_DIR / "默认").glob("*.快照.json"))
    errs = [e for e in evs if e.get("t") == "error"]
    assert not errs


# ── 缺口2/3:deliberate/proactive/force_block 与新计划冲突抽查 ─────────────
def test_force_block_yields_to_structured_plan(project):
    """设计 §5 末条:活动任务有结构化计划时,旧 deliberate 刹车不得再叠加。"""
    from loom import deliberate as _delib
    from loom.paths import TASKS_DIR
    # 先让对话流水有 4 条 assistant(触发谈满刹车),再带活动任务跑一轮
    base = project / TASKS_DIR / "默认"
    base.mkdir(parents=True, exist_ok=True)
    st = ts.TaskState.create({"domain": "chat", "role": "设定师", "conversation_id": "默认"},
                             "重做力量体系")
    ts.append_task_event(base, st.task_id, {"kind": "start", "goal": st._d["goal"]})
    ts.save_snapshot(base, st)
    ts.commit(base, st, {"kind": "plan_update", "reason": "初版",
                         "steps": [{"id": "s1", "title": "读设定", "status": "pending"}]},
              expected_revision=st.revision)
    st = ts.load_snapshot(base, st.task_id)   # 重读盘上最新(含 plan+revision)
    ts.save_snapshot(base, st)
    evs = []
    bk = _Echo(["按计划推进第一步。"])
    # 把对话流水预置成谈满 4 轮(旧刹车生效条件)
    from loom import agent_chat as _ac
    for i in range(4):
        _ac._append_event(project, "设定师", {"t": "assistant", "text": f"谈谈第{i}轮,下一步:继续谈"})
    agent_chat.run_turn(project, "设定师", "继续", bk, emit=evs.append,
                        ts="t1", protocol="text")
    system, user = bk.calls[0]
    assert "刹车" not in user, "有结构化计划时旧 force_block 不应出现"
    # 反向对照:没有任务时刹车照旧
    bk2 = _Echo(["那我们继续谈。"])
    base2 = project / TASKS_DIR / "默认"
    for f in base2.glob("*.快照.json"):
        f.unlink()
    agent_chat.run_turn(project, "设定师", "继续", bk2, emit=evs.append,
                        ts="t2", protocol="text")
    _, user2 = bk2.calls[0]
    assert "刹车" in user2, "无结构化计划时旧刹车保留"



# ── 缺口1:run_pipeline 任务事件 ──────────────────────────────────────────
def test_pipeline_emits_task_events(project):
    """六棒流水线跑章:事件流带 task_prepared / plan_updated(每棒)/ task_status(收尾)。"""
    from loom import agents as _agents, events as _events
    from loom.config import Config as _Cfg

    class _FakeBackend:
        provider = "fake"
        model = "fake"
        loom_purpose = ""

        def complete(self, system, user, *, max_chars=None, on_chunk=None):
            # 每棒给一段足够长的非空输出(过非空闸)
            return "## 章节\n" + "内容。" * 200

    cfg = _Cfg()
    cfg.title = "测试书"
    evs = []
    _agents.run_pipeline(project, 1, _FakeBackend(), cfg, evs.append, resume=False)
    types = [e.get("type") for e in evs]
    assert "task_prepared" in types, f"开跑应发 task_prepared: {types[:6]}"
    assert types.count("plan_updated") >= 2, "每棒 done 应发 plan_updated"
    assert types[-1] == "task_status" or "task_status" in types, "收尾应发 task_status"
    st_done = [e for e in evs if e.get("type") == "task_status"]
    assert st_done and st_done[-1].get("status") == "completed"

# ── P2 死条件清理后的等价行为 ─────────────────────────────────────────────
def test_parse_plan_update_empty_and_invalid():
    assert tp.parse_plan_update("") is None
    assert tp.parse_plan_update("没有计划段的一段话") is None
    upd = tp.parse_plan_update("TASK_PLAN: 收束\n1. 定稿 [done]")
    assert upd and upd["steps"][0]["status"] == "completed"
