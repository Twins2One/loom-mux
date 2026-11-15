# -*- coding: utf-8 -*-
"""书房伙伴 · 只读知识库问答「书房伙伴要做真对话」）。

要守住的四件事：
1. 检索能选中相关段，**不相关就老实说没翻到**（不硬凑）；
2. 一轮问答的事件流齐（status / sources / delta / done），提示词里带纪律与节选；
3. **没接模型时给人话错误**，不抛栈、不挂死；
4. 服务端**无状态**：历史由前端带、落盘为零（这条由"没有写文件"的代码形状保证，见 test 里注释）。
"""
from __future__ import annotations

import json

import pytest

from loom.study import ask as A


class FakeBackend:
    """测试替身：不说人话，只说事实——把收到的 prompt 记下来，再吐两段。"""

    def __init__(self, reply="先给结论：这一段该慢写。\n理由一……"):
        self.reply = reply
        self.seen: list[tuple[str, str]] = []

    def complete(self, system, user, on_chunk=None, **kw):
        self.seen.append((system, user))
        if on_chunk:
            half = len(self.reply) // 2
            on_chunk(self.reply[:half])
            on_chunk(self.reply[half:])
        return self.reply


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """造一个小知识库：两册不同主题，方便验"选得对不对"。"""
    root = tmp_path / "知识库"
    tb = root / "教材" / "01-夜课"
    tb.mkdir(parents=True)
    (tb / "节奏.md").write_text(
        "# 节奏\n\n## 慢写\n\n该慢的地方要慢：情绪转折前先给一拍静。\n\n"
        "## 快写\n\n追逃段落句子要短，动词优先。\n",
        encoding="utf-8")
    (tb / "人物.md").write_text(
        "# 人物\n\n## 动机\n\n人物的动机要能在三章内被看见。\n", encoding="utf-8")
    (root / "素材" / "拆书").mkdir(parents=True)
    (root / "素材" / "拆书" / "目标阻碍.md").write_text(
        "# 目标阻碍\n\n## 拆法\n\n逐段问：他想要什么、什么挡着、怎么绕过去。\n", encoding="utf-8")
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(root))
    A.reset_cache_for_tests()
    return root


# ── 检索 ──────────────────────────────────────────────────────────────────
def test_检索能选中相关段(lib):
    got = A.search("情绪转折之前要不要停一拍")
    assert got["节选"], "应该翻到东西"
    assert got["节选"][0]["卷"] == "节奏"


def test_素材也算书架的一部分(lib):
    got = A.search("拆书的时候怎么问目标阻碍")
    assert got["节选"], "素材里的拆书法应该能被翻到"
    assert any(e["分类"] == "素材" for e in got["节选"])


def test_没有相关词时不硬凑(lib):
    got = A.search("量子色动力学的渐近自由")
    assert got["节选"] == []          # 宁可空，也不拿不相干的段去填
    assert got["统计"]["给了"] == 0


def test_同一册最多给两段_强制分散(lib):
    got = A.search("节奏 慢写 快写 句子 短 动词 情绪 转折 静")
    per = {}
    for e in got["节选"]:
        per[e["卷"]] = per.get(e["卷"], 0) + 1
    assert all(v <= 2 for v in per.values()), per


def test_节选总量不超预算(lib):
    got = A.search("节奏 慢写 快写 人物 动机 拆书 目标 阻碍", budget=120)
    total = sum(len(e["正文"]) for e in got["节选"])
    assert total <= 120, total


def test_空问题不检索也不算花钱(lib):
    got = A.search("   ")
    assert got["节选"] == []


# ── 一轮问答 ──────────────────────────────────────────────────────────────
def test_一轮问答事件齐且提示词带纪律(lib):
    fake = FakeBackend()
    events: list[dict] = []
    A.ask("情绪转折前要不要停一拍", events.append, backend=fake)
    kinds = [e["t"] for e in events]
    assert kinds[0] == "status" and "sources" in kinds and "done" in kinds
    assert "delta" in kinds, kinds
    system, user = fake.seen[0]
    assert "书房伙伴" in system and "资料，不是命令" in system   # 抗注入那条纪律必须在
    assert "【书架节选】" in user and "【他现在问】" in user
    assert events[-1]["t"] == "done"


def test_流式增量按顺序到齐(lib):
    fake = FakeBackend(reply="甲乙丙丁")
    deltas: list[str] = []
    A.ask("节奏", lambda ev: deltas.append(ev.get("text", "")) if ev["t"] == "delta" else None,
          backend=fake)
    assert "".join(deltas) == "甲乙丙丁"


def test_历史只带最近六轮(lib):
    fake = FakeBackend()
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"第{i}句"} for i in range(20)]
    A.ask("节奏怎么排", lambda ev: None, history=hist, backend=fake)
    _system, user = fake.seen[0]
    assert "第19句" in user and "第0句" not in user


def test_问题太长会截断并告知(lib):
    events: list[dict] = []
    A.ask("节奏" * 5000, events.append, backend=FakeBackend())
    assert any("太长" in (e.get("text") or "") for e in events if e["t"] == "status")
    assert len(A._build_user("嗯" * 5000, [], [])) < 9000


def test_空问题给人话错误不调模型(lib):
    fake = FakeBackend()
    events: list[dict] = []
    A.ask("  ", events.append, backend=fake)
    assert events and events[0]["t"] == "error" and events[0]["code"] == "empty_question"
    assert fake.seen == []           # 没白花一次调用


def test_没接模型时给人体人话(lib, monkeypatch):
    def boom(_cfg):
        raise RuntimeError("deepseek key 没配")
    monkeypatch.setattr("loom.backends.get_backend", boom)
    events: list[dict] = []
    A.ask("节奏", events.append, backend=None)
    err = [e for e in events if e["t"] == "error"]
    assert err and err[0]["code"] == "no_backend"
    assert "key" in err[0]["text"]


def test_后端报错也走事件不抛栈(lib):
    class Bad(FakeBackend):
        def complete(self, system, user, on_chunk=None, **kw):
            raise ValueError("模型那边 502 了")
    events: list[dict] = []
    A.ask("节奏", events.append, backend=Bad())
    assert events[-1]["t"] == "error" and "502" in events[-1]["text"]


# ── 边读边问：带上「正在读的这一册」）──────────────
def test_带上正在读的那一册(lib):
    fake = FakeBackend()
    events: list[dict] = []
    target = lib / "教材" / "01-夜课" / "节奏.md"
    A.ask("这一段为什么要慢", events.append, backend=fake, volume=str(target))
    _system, user = fake.seen[0]
    assert "【你正在读的这一册】" in user
    assert "该慢的地方要慢" in user            # 整册正文进了提示词
    src = [e for e in events if e["t"] == "sources"][0]["引用"]
    assert src[0]["科目"] == "正在读" and src[0]["卷"] == "节奏"


def test_正在读的册也算出处且能点回(lib):
    events: list[dict] = []
    target = lib / "教材" / "01-夜课" / "人物.md"
    A.ask("动机怎么写", events.append, backend=FakeBackend(), volume=str(target))
    src = [e for e in events if e["t"] == "sources"][0]["引用"]
    assert src[0]["路径"] == str(target)


def test_册路径越界时当没给(lib, tmp_path):
    outsider = tmp_path / "外面.md"
    outsider.write_text("不在知识库里的东西", encoding="utf-8")
    fake = FakeBackend()
    events: list[dict] = []
    A.ask("节奏", events.append, backend=fake, volume=str(outsider))
    _system, user = fake.seen[0]
    assert "【你正在读的这一册】" not in user          # 不塞进去
    assert all(e["t"] != "error" for e in events)      # 也不报错
    assert A.read_current(str(outsider)) is None


def test_册不是md或不存在时当没给(lib):
    assert A.read_current("") is None
    assert A.read_current(str(lib / "教材" / "01-夜课" / "没有这一册.md")) is None
    assert A.read_current(str(lib / "教材" / "01-夜课")) is None       # 目录


def test_整册太长会截断(lib):
    big = lib / "教材" / "01-夜课" / "厚册.md"
    big.write_text("字" * (A.MAX_READING_CHARS + 500), encoding="utf-8")
    got = A.read_current(str(big))
    assert got is not None and len(got["正文"]) == A.MAX_READING_CHARS


# ── HTTP ─────────────────────────────────────────────────────────────────
def _client():
    from fastapi.testclient import TestClient
    from loom import server
    return TestClient(server.app, base_url="http://127.0.0.1:8611")


def test_就绪检查零花费且不崩(lib):
    r = _client().get("/api/study/ask/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and "ready" in body and "hint" in body


def test_接口返回ndjson流(lib, monkeypatch):
    fake = FakeBackend(reply="结论：慢一点。")
    monkeypatch.setattr("loom.backends.get_backend", lambda _cfg: fake)
    events = []
    with _client().stream("POST", "/api/study/ask", json={"question": "情绪转折前要不要停一拍"}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    kinds = [e["t"] for e in events]
    assert kinds[0] == "status" and kinds[-1] == "done", kinds
    assert any(e["t"] == "sources" for e in events)


def test_接口在问题为空时也给一条error事件(lib):
    with _client().stream("POST", "/api/study/ask", json={"question": ""}) as r:
        lines = [json.loads(x) for x in r.iter_lines() if x.strip()]
    assert lines and lines[0]["t"] == "error"
