# -*- coding: utf-8 -*-
"""书房角色分支 · 崩溃类表征测试（缺陷 #1 #2 #5 #9）。

依据《优化方案评估-Loom会话-20261108.md》W0 表格。约定：
- 「表征」测试（test_*_当前_*）断言**当前（有缺陷）行为**，现在必须通过——
  锁定缺陷现场；W1 修复后这些测试会转红，届时**同步改写**成对修复后行为的断言
  （或删除、由对应「目标」测试转正接替）。
- 「目标」测试（test_*_修复后_*）标 `@pytest.mark.xfail(reason="W1 修复缺陷#N 后转正")`，
  断言**修复后应有行为**；W1 修完摘掉标记即绿。
- 全部走假后端/桩函数，不出网、不花真钱、不碰真实知识库（conftest 已隔离
  LOOM_HOME / LOOM_LIBRARY_ROOT）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ── 公共桩 ────────────────────────────────────────────────────────────────
class _TextBackend:
    """纯文本假后端：没有 complete_with_tools → 走文本协议。"""

    def __init__(self, reply="先给结论：好的。"):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, on_chunk=None, **kw):
        self.calls.append((system, user))
        if on_chunk and self.reply:
            on_chunk(self.reply)
        return self.reply


def _seed_library(monkeypatch, tmp_path, body="该慢的地方要慢：情绪转折前先给一拍静。"):
    """造一个最小知识库并指向它（表征测试里检索要真翻到东西）。"""
    root = tmp_path / "知识库"
    d = root / "教材" / "01-夜课"
    d.mkdir(parents=True)
    (d / "节奏.md").write_text("# 节奏\n\n## 慢写\n\n" + body + "\n", encoding="utf-8")
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(root))
    from loom.study import ask as A
    A.reset_cache_for_tests()
    return root


# ── #1 _ROLE_SETUPS 缺 tools 键 → ask() 一调即 KeyError ───────────────────
# W1 已修复（2026-11-08）：表征哨兵转正——改写为断言修复后行为（正常路径价值保留）。
@pytest.mark.parametrize("role", ["知识顾问", "语料分析师"])
def test_1_角色ask修复后正常完成且setup声明tools(monkeypatch, tmp_path, role):
    _seed_library(monkeypatch, tmp_path)
    from loom.study import ask as A
    # 修复后：setup 必须带 tools 键，且与 _ROLE_TOOLS 对应角色一致（单一真相）
    setup = A._ROLE_SETUPS[role]
    assert setup.get("tools") == A._ROLE_TOOLS[role]
    ev: list[dict] = []
    A.ask("帮我查证一个年代", ev.append, role=role, backend=_TextBackend())
    kinds = [e.get("t") for e in ev]
    assert "done" in kinds
    assert "error" not in kinds


def test_1_修复后_角色ask应能正常完成(monkeypatch, tmp_path):
    _seed_library(monkeypatch, tmp_path)
    from loom.study import ask as A
    ev: list[dict] = []
    A.ask("帮我查证一个年代", ev.append, role="知识顾问", backend=_TextBackend())
    kinds = [e.get("t") for e in ev]
    assert "done" in kinds
    assert "error" not in kinds


# ── #2 native 协议轮间丢工具结果 ─────────────────────────────────────────
class _NativeBackend:
    """function-calling 假后端：第一轮要工具，第二轮给最终回答。记录每轮收到的 msgs。"""

    def __init__(self):
        self.msgs_seen: list[list[dict]] = []

    def complete_with_tools(self, msgs, tools):
        self.msgs_seen.append([dict(m) for m in msgs])
        if len(self.msgs_seen) == 1:
            return {"content": "", "tool_calls": [{
                "id": "c1",
                "function": {"name": "search_library",
                             "arguments": json.dumps({"查询": "节奏 慢写"}, ensure_ascii=False)},
            }]}
        return {"content": "答完了", "tool_calls": []}


def test_2_native第二轮保留工具结果消息(monkeypatch, tmp_path):
    """W1 修复 #2 后的表征转正：msgs 持久化，第二轮请求里必须能看到上一轮的工具结果。"""
    _seed_library(monkeypatch, tmp_path)
    from loom.study import ask as A
    b = _NativeBackend()
    ev: list[dict] = []
    A._chat_with_tools(b, "系统", "问题", ev.append, "ts", tools_role="知识顾问")
    assert len(b.msgs_seen) == 2
    second = b.msgs_seen[1]
    roles = [m["role"] for m in second]
    assert any(r == "tool" for r in roles), "工具结果必须以 role=tool 消息进入第二轮请求"
    # 标准形状：system → assistant(tool_calls) → role=tool…
    assert roles[0] == "system"
    assert roles.count("assistant") == 1
    assert roles[roles.index("assistant") + 1] == "tool"


def test_2_修复后_native第二轮应能看到工具结果(monkeypatch, tmp_path):
    _seed_library(monkeypatch, tmp_path)
    from loom.study import ask as A
    b = _NativeBackend()
    ev: list[dict] = []
    A._chat_with_tools(b, "系统", "问题", ev.append, "ts", tools_role="知识顾问")
    second = b.msgs_seen[1]
    assert any(m.get("role") == "tool" for m in second), \
        "assistant(tool_calls) 之后必须跟 role=tool 的结果消息，模型才能接着材料回答"


# ── #5 _LAST_INJECTION 未定义 → NameError 被裸 except 吞，注入永远失败 ────
def test_5_自动回忆命中时注入正常且统计落账(monkeypatch, tmp_path):
    """W1 修复 #5 后的表征转正：检索命中即注入成功，LAST_INJECTION 统计如实落账。"""
    _seed_library(monkeypatch, tmp_path)
    from loom.study import ask as A
    import loom.memory as m

    def fake_recall(root, question, scope="book", k=3):
        return {"ok": True, "条": [{"book": "某书", "digest": "摘要甲"},
                                   {"book": "", "digest": "摘要乙"}]}

    monkeypatch.setattr(m, "recall", fake_recall)
    out = A._auto_memory("知识顾问", "问题", Path(os.environ["LOOM_LIBRARY_ROOT"]))
    assert "记忆注入" in out and "摘要甲" in out and "摘要乙" in out
    assert A.LAST_INJECTION.get("条数") == 2
    assert A.LAST_INJECTION.get("scope") == "all"


def test_5_修复后_自动回忆命中时应注入记忆(monkeypatch, tmp_path):
    _seed_library(monkeypatch, tmp_path)
    from loom.study import ask as A
    import loom.memory as m

    def fake_recall(root, question, scope="book", k=3):
        return {"ok": True, "条": [{"book": "某书", "digest": "摘要甲"}]}

    monkeypatch.setattr(m, "recall", fake_recall)
    out = A._auto_memory("知识顾问", "问题", Path(os.environ["LOOM_LIBRARY_ROOT"]))
    assert "记忆注入" in out
    assert "摘要甲" in out


# ── #9 落笔记 mutates=True 返 str → run_tool 事件组装 TypeError（成功后崩）──
class _NoteBackend:
    """material.ingest 的假模型：吐一份标准笔记。"""

    def complete(self, system, user, on_chunk=None, **kw):
        return "# 标题\n\n## 一句话\n好\n\n## 要点\n- 甲\n\n## 存疑\n无"


def test_9_落笔记成功落盘且事件组装正常(monkeypatch, tmp_path):
    """W1 修复 #9 后的表征转正：mutates 契约对齐后，落盘成功且事件正常组装。"""
    import loom.backends as backends
    monkeypatch.setattr(backends, "get_backend", lambda cfg: _NoteBackend())
    from loom import partner_tools as pt
    root = Path(os.environ["LOOM_LIBRARY_ROOT"])
    out = pt.run_tool(root, "落笔记",
                      {"标题": "回归笔记甲", "稿子": "内容一二三四五六七八九十"}, ts="t1")
    assert out.get("t") == "result"
    assert not out.get("error")
    # 关键：笔记确实落盘且内容非空（原缺陷就是"落盘成功、事件组装崩"）
    hits = list(root.rglob("*回归笔记甲*"))
    assert hits and hits[0].is_file() and hits[0].read_text(encoding="utf-8").strip()


def test_9_修复后_落笔记应返回正常结果事件(monkeypatch, tmp_path):
    import loom.backends as backends
    monkeypatch.setattr(backends, "get_backend", lambda cfg: _NoteBackend())
    from loom import partner_tools as pt
    root = Path(os.environ["LOOM_LIBRARY_ROOT"])
    out = pt.run_tool(root, "落笔记",
                      {"标题": "回归笔记乙", "稿子": "内容一二三四五六七八九十"}, ts="t2")
    assert out.get("t") == "result"
    assert "落好了" in str(out.get("text") or "")
