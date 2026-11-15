# -*- coding: utf-8 -*-
"""议事例会:一句需求 → 领航员点名 → 几位同事并发分析。

需求背景:「我对领航员说『我想要 A 在前期装杯,后期被打脸』,领航员自动喊来大纲师、
情绪节奏设计师甚至设定师、批评家,分别分析……」
"""
from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from loom import collab, config, roundtable as rt
from loom.scaffold import init as scaffold_init


class _Recorder:
    """假后端:记下每次调用,可控制耗时,用于验证「真的并发」与「上限真的生效」。"""

    def __init__(self, reply="先给一句判断。\n下一步:你把第 3 章的爆点往前挪一格。", delay=0.0):
        self.reply = reply
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def complete(self, system, user, *, max_chars=None, on_chunk=None):
        with self._lock:
            self.calls.append((system, user))
            self.now += 1
            self.peak = max(self.peak, self.now)
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.now -= 1
        if on_chunk and self.reply:
            on_chunk(self.reply)
        return self.reply


# ── 并发上限这一格成 2–8）──────────────────────────────
@pytest.mark.parametrize("raw,want", [(99, 8), (8, 8), (9, 8), (0, 2), (1, 2), (-3, 2),
                                      ("abc", 2), (3, 3), ("5", 5)])
def test_并发上限收敛到2到8(raw, want):
    assert config.clamp_concurrency(raw) == want


def test_并发范围是2到8():
    assert (config.CONCURRENCY_MIN, config.CONCURRENCY_MAX) == (2, 8)


def test_并发上限默认是2(project):
    cfg = config.load_config(project)
    assert cfg.max_concurrency == 2


def test_并发上限写进toml再读回来(project):
    cfg = config.load_config(project)
    config.save_config(project, dataclasses.replace(cfg, max_concurrency=3))
    assert config.load_config(project).max_concurrency == 3
    assert "并发上限" in (project / "loom.toml").read_text(encoding="utf-8")


def test_默认值不往toml里写多余的行(project):
    """默认 2 时不要在别人的 toml 里多塞一行——老项目的文件保持原样。"""
    cfg = config.load_config(project)
    config.save_config(project, cfg)
    assert "并发上限" not in (project / "loom.toml").read_text(encoding="utf-8")


# ── 点名 ───────────────────────────────────────────────────────────────────
def test_点名只认作者智库八人():
    assert "写手" not in rt.PANEL_ROLES and "章节细纲师" not in rt.PANEL_ROLES
    assert "大纲师" in rt.PANEL_ROLES and "批评家" in rt.PANEL_ROLES


def test_点名解析_认竖线也认冒号_不认的人丢掉():
    raw = ("大纲师|这条改线牵动多少章的纲\n"
           "- 情绪节奏设计师：前期爽点与后期打脸的落差怎么铺\n"
           "写手|不该参会\n"
           "批评家:会不会中心偏离\n"
           "隔壁老王|不是角色")
    picks = rt._parse_picks(raw)
    assert [p["role"] for p in picks] == ["大纲师", "情绪节奏设计师", "批评家"]
    assert picks[0]["why"].startswith("这条改线")
    assert "写手" not in [p["role"] for p in picks]


def test_点名解析_人数封顶():
    raw = "\n".join(f"{r}|理由" for r in rt.PANEL_ROLES)
    assert len(rt._parse_picks(raw)) <= rt.MAX_ROLES


def test_点名_模型乱答时退回关键词(project):
    be = _Recorder(reply="我觉得这个问题很复杂,要具体看。")
    out = rt.pick(project, "我想改大纲,把爽点提前", be)
    assert out["source"] == "fallback"
    assert out["roles"], out
    assert "关键词" in out["note"]
    assert all(p["role"] in rt.PANEL_ROLES for p in out["roles"])


def test_点名_空需求不花钱():
    called = []

    class Boom:
        def complete(self, *a, **k):
            called.append(1)
            return "大纲师|x"

    out = rt.pick("/nowhere", "   ", Boom())
    assert out["roles"] == [] and called == []


def test_点名_模型失败也不堵死(project):
    class Boom:
        def complete(self, *a, **k):
            raise RuntimeError("网关 502")

    out = rt.pick(project, "改大纲", Boom())
    assert out["roles"] and out["source"] == "fallback"
    assert "502" in out["note"] or "点名调用失败" in out["note"]


# ── 派发 ───────────────────────────────────────────────────────────────────
def test_派发_每个角色各跑一轮_事件带角色名(project):
    be = _Recorder()
    seen: list[dict] = []
    cfg = config.load_config(project)
    rid = rt.run_panel(project, "想让 A 前期装杯后期被打脸", ["大纲师", "批评家"], cfg,
                       get_backend_for=lambda c, p: be, emit=seen.append, ts="20261025-101010")

    assert rid == "20261025-101010"
    kinds = [e["t"] for e in seen]
    assert kinds[0] == "panel_begin" and kinds[-1] == "panel_end"
    assert seen[0]["roles"] == ["大纲师", "批评家"]
    # 两个角色都真的说了话,并且事件上标了是谁说的
    said = {e.get("role") for e in seen if e["t"] == "assistant"}
    assert said == {"大纲师", "批评家"}
    # 作者那句话只记一次(不能因为三个人就刷三遍)
    assert sum(1 for e in seen if e["t"] == "user") == 0     # 走的是 panel_begin 的 text
    assert seen[0]["text"] == "想让 A 前期装杯后期被打脸"


def test_派发_合并流水真的落盘了(project):
    be = _Recorder()
    rt.run_panel(project, "改大纲", ["大纲师"], config.load_config(project),
                 get_backend_for=lambda c, p: be, emit=lambda e: None, ts="20261025-101011")
    p = project / rt.PANEL_DIR / "20261025-101011.jsonl"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "panel_begin" in text and "助手" not in text
    assert "大纲师" in text
    back = rt.read_round(project, "20261025-101011")
    assert back and back[0]["t"] == "panel_begin"


def test_派发_同时跑的人数不超过上限(project):
    be = _Recorder(delay=0.12)
    cfg = dataclasses.replace(config.load_config(project), max_concurrency=2)
    rt.run_panel(project, "改大纲", ["大纲师", "设定师", "批评家", "编辑总监"], cfg,
                 get_backend_for=lambda c, p: be, emit=lambda e: None, ts="20261025-101012")
    assert be.peak <= 2, f"上限 2,实测同时 {be.peak} 个在跑"


def test_派发_上限1会被抬到2_下限抬高了(project):
    """2026-10-27 起并发下限是 2：填 1 会被 `clamp_concurrency` 抬到 2，
    所以两个角色是**并行**的（旧行为"一个一个来"已经不存在了）。"""
    be = _Recorder(delay=0.05)
    cfg = dataclasses.replace(config.load_config(project), max_concurrency=1)
    rt.run_panel(project, "改大纲", ["大纲师", "设定师"], cfg,
                 get_backend_for=lambda c, p: be, emit=lambda e: None, ts="20261025-101013")
    assert be.peak == 2


def test_派发_一个角色的后端挂了不拖垮别人(project):
    good = _Recorder()

    def factory(cfg, purpose):
        raise RuntimeError("没有可用的 key")

    seen: list[dict] = []
    rt.run_panel(project, "改大纲", ["大纲师"], config.load_config(project),
                 get_backend_for=factory, emit=seen.append, ts="20261025-101014")
    states = [e.get("state") for e in seen if e["t"] == "panel_state"]
    assert "error" in states
    assert any(e["t"] == "panel_end" for e in seen)


def test_派发_不认识的角色被过滤掉(project):
    seen: list[dict] = []
    rt.run_panel(project, "改大纲", ["写手", "隔壁老王"], config.load_config(project),
                 get_backend_for=lambda c, p: _Recorder(), emit=seen.append, ts="20261025-101015")
    assert seen[0]["t"] == "error"          # 一个可参会的都没有 → 说人话,别静默空跑


# ── 收口 ───────────────────────────────────────────────────────────────────
def test_收口_产出领航员的汇总并落盘(project):
    rt.run_panel(project, "改大纲", ["大纲师", "批评家"], config.load_config(project),
                 get_backend_for=lambda c, p: _Recorder(), emit=lambda e: None,
                 ts="20261025-101016")
    be = _Recorder(reply="**一致的地方**:方向没问题。\n\n**分歧点**:大纲师和批评家相反。\n\n**可以怎么走**:\n1. 保守改\n推荐:1。")
    seen: list[dict] = []
    out = rt.wrapup(project, "20261025-101016", be, seen.append, ts="20261025-101017")
    assert "分歧点" in out
    assert seen and seen[-1]["t"] == "assistant" and seen[-1]["role"] == "领航员"
    assert seen[-1]["wrap"] is True
    # 材料里必须真的有两位同事说过的话,不能空口收口
    assert "大纲师" in be.calls[0][1] and "批评家" in be.calls[0][1]
    back = rt.read_round(project, "20261025-101016")
    assert any(e.get("wrap") for e in back)


def test_收口_空的一场会说清楚而不是瞎编(project):
    seen: list[dict] = []
    assert rt.wrapup(project, "不存在的轮次", _Recorder(), seen.append) == ""
    assert seen and seen[0]["t"] == "error"


# ── 并发安全(这是并发功能的前置修复,必须锁死)────────────────────────────
def test_问同事的预算按线程隔离(project):
    """四个角色并发时,后开跑的那个不能把前一个的计数清零。

    真出过的隐患:原来是进程级字典 —— A 问了一次、B 一开始 begin_turn 就把它清成 0,
    于是 A 还能再问两次。上限形同虚设,钱花掉了作者还不知道。
    """
    results: dict[str, str] = {}

    def worker(role: str) -> None:
        collab.begin_turn(role)
        for _ in range(collab.MAX_CONSULTS_PER_TURN):
            collab._consume()
        try:
            collab._consume()
            results[role] = "没拦住"
        except ValueError:
            results[role] = "拦住了"

    ts = [threading.Thread(target=worker, args=(r,)) for r in ("大纲师", "批评家", "设定师")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(results.values()) == {"拦住了"}, results
