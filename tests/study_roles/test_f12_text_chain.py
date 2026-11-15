# -*- coding: utf-8 -*-
"""F12 表征测试（先红）：书房多模态链路三处断链 + 采样时间可见性。

锁定缺陷现场（历史会话 深度审查 F12，2026-11-08）：
1. 文本协议：usr 在第一轮工具后转成 list，下一轮再遇工具行 `usr + str` 必 TypeError
   （:931 组装 / :926 再拼接）；
2. 文本协议：while 循环内的 complete 调用（:872）无 TypeError 兜底——
   兜底写在循环体外永不可达；
3. native 路径：图片仍在 role=tool 的 content list 里（:858-859），
   应改为 tool 纯文字 + 独立 user 消息附图（agent_chat 同款形状）；
4. 「采样时间」作为 image part 的非标准兄弟键（:920）——模型不可见；
   应把时间标注放进相邻 text part。

全部用假后端/假工具面，零网络零模型。
"""
from __future__ import annotations

import pytest

from loom.study import ask as A


class _FakeNative:
    """假原生 FC 后端：先回一次工具调用，再回终答；记录每次请求的 messages。"""
    has_complete = True

    def __init__(self, tool_name="查知识", tool_args="{}", answer="看完了，总结如下。"):
        self.calls: list[list[dict]] = []
        self._tool_name = tool_name
        self._tool_args = tool_args
        self._answer = answer
        self._round = 0

    def complete_with_tools(self, messages, tools, *, max_chars=None):
        self.calls.append([dict(m) for m in messages])
        self._round += 1
        if self._round == 1:
            return {"content": "", "tool_calls": [
                {"id": "c1", "name": self._tool_name, "arguments": self._tool_args}]}
        return {"content": self._answer, "tool_calls": []}


class _FakeText:
    """假文本协议后端：第一轮吐工具行，第二轮吐正文；记录每次 user 参数。"""
    def __init__(self, tool_line=None):
        self.users: list[object] = []
        self._round = 0
        self._tool_line = tool_line or "用：查知识"

    def complete(self, system, user, *, max_chars=None, on_chunk=None, **kw):
        self.users.append(user)
        self._round += 1
        if self._round == 1:
            return self._tool_line
        return "总结完成。"


@pytest.fixture
def role_toolset(monkeypatch):
    """把知识顾问工具面钉死成假工具：第一次返回带图结果，后续纯文字。"""
    class _T:
        @staticmethod
        def schema():
            return []

        @staticmethod
        def contract():
            return "契约"

        @staticmethod
        def run(name, args, *_a, backend=None, **_k):
            if _T.n == 0:
                _T.n = 1
                return {"ok": True, "text": "取到稿了（10 字）。",
                        "images": [{"mime": "image/jpeg", "b64": "QUJD", "times": [1.5, 3.0]}]}
            return {"ok": True, "text": "第二次结果。"}

        n = 0

    monkeypatch.setattr(A, "_toolset_for", lambda role="": _T)
    # 视觉探测恒真（不联网）
    import loom.backends as _B
    monkeypatch.setattr(_B, "supports_vision", lambda *a, **k: True)
    return _T


def test_文本协议两轮工具不炸(role_toolset, monkeypatch):
    """F12-1：第一轮工具带图后 usr 转 list，第二轮模型再吐工具行不得 TypeError。"""
    # 第二轮也吐工具行 → 会走到 usr(list)+str 的旧断链
    class _TwoRounds(_FakeText):
        def complete(self, system, user, *, max_chars=None, on_chunk=None, **kw):
            self.users.append(user)
            self._round += 1
            if self._round <= 2:
                return "用：查知识"
            return "终答。"

    be = _TwoRounds()
    out = []
    A._chat_with_tools(be, "SYS", "问题", out.append, "ts", tools_role="知识顾问")
    # 不抛 TypeError 即过；终答已回
    assert any(e.get("t") == "delta" for e in out) or out


def test_循环内complete有TypeError兜底(role_toolset, monkeypatch):
    """F12-2：多模态 user 传给不认 list 的后端，必须在调用点降级重试而非死循环外兜底。"""
    class _StrOnly:
        def __init__(self):
            self.users = []

        def complete(self, system, user, *, max_chars=None, on_chunk=None, **kw):
            if isinstance(user, list):
                raise TypeError("user must be str")
            self.users.append(user)
            if len(self.users) == 1:
                return "用：查知识"
            return "文字终答。"

    be = _StrOnly()
    out = []
    A._chat_with_tools(be, "SYS", "问题", out.append, "ts", tools_role="知识顾问")
    assert all(isinstance(u, str) for u in be.users)


def test_native路径tool消息不带图_图片走user消息(role_toolset, monkeypatch):
    """F12-3：role=tool 的 content 必须是纯文字；图片作为 user 消息的 image_url 部分。"""
    be = _FakeNative()
    out = []
    A._chat_with_tools(be, "SYS", "问题", out.append, "ts", tools_role="知识顾问")
    assert len(be.calls) == 2, "应有两轮 native 请求"
    second = be.calls[1]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs, "应有 tool 消息"
    for m in tool_msgs:
        assert isinstance(m.get("content"), str), \
            f"tool content 不得是 list（F12-3）：{type(m.get('content'))}"
    user_msgs = [m for m in second if m.get("role") == "user"]
    img_user = [m for m in user_msgs if isinstance(m.get("content"), list)
                and any(p.get("type") == "image_url" for p in m["content"]
                        if isinstance(p, dict))]
    assert img_user, "图片必须走独立 user 消息的 image_url 部分"


def test_采样时间进文字而非图片part键(role_toolset, monkeypatch):
    """F12-4：时间标注必须出现在 text part 里（模型可见），不得做 image part 的兄弟键。"""
    be = _FakeNative()
    out = []
    A._chat_with_tools(be, "SYS", "问题", out.append, "ts", tools_role="知识顾问")
    second = be.calls[1]
    for m in second:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for p in content:
            if isinstance(p, dict) and p.get("type") == "image_url":
                assert "采样时间" not in p, \
                    "「采样时间」不得作为 image part 的非标准键（部分网关会拒整个请求）"
    # 时间信息应出现在某条 text part 里
    all_text = ""
    for m in second:
        content = m.get("content")
        if isinstance(content, list):
            all_text += "".join(p.get("text", "") for p in content
                                if isinstance(p, dict) and p.get("type") == "text")
        elif isinstance(content, str):
            all_text += content
    assert "1.5" in all_text or "3.0" in all_text or "采样" in all_text, \
        "采样时间标注应出现在模型可见文字里"


def test_工具到终答两轮内图就附上(role_toolset, monkeypatch):
    """历史回执 回执（审查记录）P1 根修验证：常见「工具→终答」路径——
    首轮工具带图、第二轮模型直接终答（无工具行，循环提前 return）——
    图必须在第二轮请求里已附上（原实现只在 while 耗尽后兜底附，早退丢图）。"""
    class _ToolThenAnswer(_FakeText):
        def complete(self, system, user, *, max_chars=None, on_chunk=None, **kw):
            self.users.append(user)
            self._round += 1
            if self._round == 1:
                return "用：查知识"
            return "终答。"

    be = _ToolThenAnswer()
    out = []
    A._chat_with_tools(be, "SYS", "问题", out.append, "ts", tools_role="知识顾问")
    assert len(be.users) == 2
    second = be.users[1]
    assert isinstance(second, list), \
        "第二轮请求应为 content list（带图）——「工具→终答」不得丢图"
    assert any(p.get("type") == "image_url" for p in second if isinstance(p, dict)), \
        "第二轮请求缺少 image_url 部件"
    txt = "".join(p.get("text", "") for p in second
                  if isinstance(p, dict) and p.get("type") == "text")
    assert "采样" in txt or "1.5" in txt, f"采样时间未进文字: {txt!r}"
