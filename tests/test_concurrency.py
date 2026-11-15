# -*- coding: utf-8 -*-
"""全局并发闸 + 提案 id 唯一性）。

背景（三档修法里的 ①②③）：
① 提案 id 原来只有秒级 `p-{ts}`，同秒两次会话会撞 id → 拍板可能落错地方（真 bug）；
② 并发上限原来只作用在议事厅，智库一对一/领航员/动作端点全无闸 → 作者"多开查资料"时
   钱按 N 倍烧、还看不见谁在跑；
③ 于是把上限升级成**全局**（2–8），并把"正在跑几个"暴露出来。
"""
from __future__ import annotations

import inspect
import threading
import time

import pytest

from loom import backends, concurrency as conc, config, partner_tools as pt


@pytest.fixture(autouse=True)
def _clean_gate():
    conc.reset_for_tests()
    yield
    conc.reset_for_tests()


# ── ① 提案 id 唯一 ─────────────────────────────────────────────────────────
def test_同一秒出两张卡_id不再相同(project):
    """ts 完全相同（模拟同一秒开两个会话）时，两张卡的 id 必须不同。"""
    a = pt.run_tool(project, "提设定", {"落点": "外置大脑/设定集/00-立项.md#题材", "内容": "重生流"},
                    ts="20261027-120000-1-0")
    b = pt.run_tool(project, "提设定", {"落点": "外置大脑/设定集/00-立项.md#题材", "内容": "废土流"},
                    ts="20261027-120000-1-0")
    assert a["t"] == b["t"] == "proposal"
    assert a["id"] != b["id"], "同秒同 ts 出了同 id —— 拍板会串台"
    assert a["id"].startswith("p-20261027-120000-1-0-")


def test_卡片上记着是谁出的(project):
    from loom import collab

    collab.begin_turn("大纲师")
    ev = pt.run_tool(project, "提设定", {"落点": "外置大脑/设定集/00-立项.md#题材", "内容": "重生流"},
                     ts="t1")
    collab.begin_turn("")
    assert ev["角色"] == "大纲师"


# ── ② 全局闸 ───────────────────────────────────────────────────────────────
def test_上限2时同时最多2个在跑():
    conc.configure(2)
    peak = 0
    lock = threading.Lock()

    @conc.gated
    def call():
        nonlocal peak
        with lock:
            peak = max(peak, conc.stats()["运行中"])
        time.sleep(0.05)

    ths = [threading.Thread(target=call) for _ in range(6)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert peak <= 2, f"上限 2，实测同时 {peak} 个在跑"


def test_调大上限后排队的立刻能进来():
    conc.configure(2)
    started = threading.Event()

    @conc.gated
    def slow():
        started.set()
        time.sleep(0.25)

    ths = [threading.Thread(target=slow) for _ in range(4)]
    for t in ths:
        t.start()
    started.wait(1.0)
    time.sleep(0.05)
    assert conc.stats()["排队"] >= 1          # 两个在跑、至少两个在排
    conc.configure(4)                          # 上限调大 → 排队的被唤醒
    for t in ths:
        t.join()
    assert conc.stats()["运行中"] == 0 and conc.stats()["排队"] == 0


def test_排队超时给明确错误而不是无限挂():
    conc.configure(2)
    hold = threading.Event()

    @conc.gated
    def hold_slot():
        hold.wait(2.0)

    ths = [threading.Thread(target=hold_slot) for _ in range(2)]
    for t in ths:
        t.start()
    time.sleep(0.05)
    with pytest.raises(conc.ConcurrencyBusy) as e:
        with conc.slot(timeout=0.2):
            pass
    assert "并发已满" in str(e.value)
    hold.set()
    for t in ths:
        t.join()


def test_闸装在backends层且不破签名():
    """`accepts_kwarg()` 靠签名探测——装饰器必须用 functools.wraps 保住它。"""
    fn = backends.OpenAICompatBackend.complete
    assert hasattr(fn, "__wrapped__"), "装饰器没保住原函数"
    params = inspect.signature(fn).parameters
    for name in ("system", "user", "max_chars", "on_chunk"):
        assert name in params, f"签名丢了 {name}"
    # 有 tools 能力的后端也要被闸罩住
    assert hasattr(backends.OpenAICompatBackend.complete_with_tools, "__wrapped__")


def test_上限来自每本书的配置(monkeypatch):
    """`get_backend` 是唯一造后端的门（含按用途选模型那条也走它），
    所以把「按本书配置上限」放在它开头，就覆盖了所有通道。用 DEMO 后端避开创 key。"""
    import dataclasses

    monkeypatch.setenv("LOOM_DEMO", "1")
    assert config.clamp_concurrency(3) == 3
    c = config.Config(provider="openai_compat", model="m", base_url="http://127.0.0.1:9/v1")
    backends.get_backend(dataclasses.replace(c, max_concurrency=6))
    assert conc.cap() == 6


# ── ③ 可见性端点 ───────────────────────────────────────────────────────────
def test_并发实况端点(project):
    from fastapi.testclient import TestClient
    from loom import server

    c = TestClient(server.app, base_url="http://127.0.0.1:8611")
    r = c.get("/api/routes/concurrency", params={"root": str(project)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    for k in ("设置", "范围", "上限", "运行中", "排队"):
        assert k in body, body
    assert body["范围"] == [2, 8]


def test_设置端点的范围提示改成2到8(project):
    from fastapi.testclient import TestClient
    from loom import server

    c = TestClient(server.app, base_url="http://127.0.0.1:8611")
    r = c.post("/api/routes/concurrency", json={"root": str(project), "value": 99})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["并发上限"] == 8
    assert "2-8" in body["message"]
