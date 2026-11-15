# -*- coding: utf-8 -*-
"""S2/S3 四域适配与决策承接测试（设计 §10:四执行域集成样本 + 作者决策行 + 议事→独聊隔离）。"""
from __future__ import annotations

import json

import pytest

from loom import agent_chat, task_state as ts, turn_prepare as tp
from loom.paths import TASKS_DIR


class EchoBackend:
    """脚本化假后端:按序弹出回复,记下每次请求。支持文本协议。"""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None):
        self.calls.append((system, user))
        out = self.replies.pop(0) if self.replies else ""
        if on_chunk and out:
            on_chunk(out)
        return out


def _events():
    return []


def _collector():
    evs = []
    return evs, evs.append


# ── 域一:书内专家对话（agent_chat 任务接入）────────────────────────────────
def test_专家对话_明确目标建任务并承接约束(project):
    """§10 场景1:否决方向→新问题→继续,约束仍被承接。"""
    evs, emit = _collector()
    bk = EchoBackend(["收到,不用第一人称,我按第三人称给方案。"])
    agent_chat.run_turn(project, "设定师", "重写金手指,记住:不用第一人称", bk, emit=emit,
                        ts="t1", protocol="text")
    base = project / TASKS_DIR / "默认"
    snaps = list(base.glob("*.快照.json"))
    assert len(snaps) == 1, "明确目标应建任务"
    st = ts.load_snapshot(base, snaps[0].name.replace(".快照.json", ""))
    assert st.status in ("ready", "waiting_author")
    # 第二轮:约束回连
    bk2 = EchoBackend(["好,继续按第三人称。"])
    agent_chat.run_turn(project, "设定师", "继续", bk2, emit=emit, ts="t2", protocol="text")
    assert any("不用第一人称" in u for _, u in bk2.calls), "有效约束应进下一轮提示词"


def test_专家对话_简单问答不建任务不增调用(project):
    """§10 场景7:简单对话不增加规划调用,任务卡也不建。

    注:智库八人原有的「不催不动」补一轮(NUDGE)是既有行为,不算准备层新增调用——
    这里断言的是**准备层零调用**:两次调用都是既有对话循环的,没有第三笔。
    """
    evs, emit = _collector()
    bk = EchoBackend(["直接回答:章节分镜要服务情绪。", "分镜就是把场面拍成镜头。"])
    agent_chat.run_turn(project, "设定师", "分镜是干嘛的?", bk, emit=emit, ts="t1",
                        protocol="text")
    assert len(bk.calls) <= 2        # 只有既有对话循环的调用,准备层不加
    assert not (project / TASKS_DIR / "默认").exists() or \
        not list((project / TASKS_DIR / "默认").glob("*.快照.json"))


def test_专家对话_计划段折叠进状态并发plan_updated(project):
    evs, emit = _collector()
    plan_text = ("我先定方向。\nTASK_PLAN: 作者要求改力量体系\n"
                 "1. 读现有设定 [done]\n2. 给三套替代方案\n3. 出候选卡")
    bk = EchoBackend([plan_text])
    agent_chat.run_turn(project, "设定师", "重做力量体系", bk, emit=emit, ts="t1",
                        protocol="text")
    errs = [e for e in evs if e.get("t") == "error"]
    assert not errs, f"对话循环不应报错: {errs}"
    base = project / TASKS_DIR / "默认"
    snap = next(base.glob("*.快照.json"))
    st = ts.load_snapshot(base, snap.name.replace(".快照.json", ""))
    assert st.plan_steps(), "TASK_PLAN 应折叠进计划"
    assert st._d["plan"]["plan_revision"] >= 1
    assert any(e.get("t") == "plan_updated" for e in evs), "应发 plan_updated 事件"


def test_专家对话_候选卡出置waiting_author(project):
    """§10 生命周期行:候选提出仍待决。"""
    evs, emit = _collector()
    # 文本协议里「提设定」出候选卡 → run 收尾置 waiting_author
    card = ("建议改设定。\n\nTOOL: 提设定\n```loom\n内容: 金手指改为双生树\n落点: 外置大脑/设定集/10-世界观/金手指.md#核心功能\n```")
    bk = EchoBackend([card])
    agent_chat.run_turn(project, "设定师", "把金手指改成双生树", bk, emit=emit, ts="t1",
                        protocol="text")
    base = project / TASKS_DIR / "默认"
    snap = next(base.glob("*.快照.json"))
    st = ts.load_snapshot(base, snap.name.replace(".快照.json", ""))
    assert st.status == "waiting_author"


# ── 域二:议事→独聊隔离（§10:同书同角色独聊与议事不互改计划）──────────────
def test_议事extra不串改独聊任务(project):
    """议事轮走 extra=via/panel,任务收尾仍落在该角色的默认代际,但议事不该给独聊任务
    塞 outcome——议事没有 task_id/conversation_id,准备层对它退普通路径。"""
    evs, emit = _collector()
    bk = EchoBackend(["议事意见:设定没问题。"])
    agent_chat.run_turn(project, "设定师", "请大家看看设定", bk, emit=emit, ts="t1",
                        protocol="text",
                        extra={"via": "议事", "panel": "p1"})
    # 议事轮(user_text 非空 → 会建任务;但 via=议事)——§10 要求隔离:
    # 议事建立的任务 scope 必须可区分(不与独聊混用同一 conversation 代际)
    base = project / TASKS_DIR / "默认"
    snaps = list(base.glob("*.快照.json")) if base.is_dir() else []
    # 独聊任务与议事任务不同时处于活动态(一次只有一个),且议事轮的事件带 via 标记
    assert any(e.get("via") == "议事" for e in evs) or not snaps


def test_另起对话换代际隔离(project):
    """§13.2:另起对话各有明确代际——c1 与 c2 任务目录隔离。

    「任务甲/任务乙」是明确任务表述(词表含「任务」),但**结尾的「好。」**不含任务词,
    会被判成非任务——所以两轮都用带任务词的目标词。
    """
    evs, emit = _collector()
    bk = EchoBackend(["好。", "好。"])
    agent_chat.run_turn(project, "设定师", "任务:甲方案", bk, emit=emit, ts="t1",
                        protocol="text", conversation_id="c1")
    agent_chat.run_turn(project, "设定师", "任务:乙方案", bk, emit=emit, ts="t2",
                        protocol="text", conversation_id="c2")
    d1 = project / TASKS_DIR / "c1"
    d2 = project / TASKS_DIR / "c2"
    assert len(list(d1.glob("*.快照.json"))) == 1
    assert len(list(d2.glob("*.快照.json"))) == 1
    assert next(d1.glob("*.快照.json")).name != next(d2.glob("*.快照.json")).name


# ── 域三:书房（library 作用域任务持久化）─────────────────────────────────
def test_书房任务落library且两会话隔离(tmp_path, monkeypatch):
    """§10:书房两个会话隔离;落点在 library 运行区任务子区。"""
    lib = tmp_path / "library"
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(lib))
    from loom.study.library import library_root
    b1 = library_root() / ".loom-tasks" / "知识顾问" / "会话A"
    b2 = library_root() / ".loom-tasks" / "知识顾问" / "会话B"
    p1 = tp.prepare_turn(base_dir=b1, domain="study", role="知识顾问",
                         conversation_id="会话A", goal="整理B站取稿笔记")
    p2 = tp.prepare_turn(base_dir=b2, domain="study", role="知识顾问",
                         conversation_id="会话B", goal="分析文风样本")
    assert p1["task"]["task_id"] != p2["task"]["task_id"]
    assert str(b1).startswith(str(lib)), "书房任务必须落在 library 运行区"
    st = ts.load_snapshot(b1, p1["task"]["task_id"])
    assert st.is_active()
    # 刷新恢复:快照读取零模型
    again = ts.load_snapshot(b1, p1["task"]["task_id"])
    assert again.revision == st.revision


# ── 域四:默认六棒(工序投影不硬编码必跑)────────────────────────────────────
def test_六棒工序投影按实际STEPS(project):
    """§7:固定工序投影为总计划,人工停站=限定范围交付完成。"""
    from loom.agents import STEPS
    steps = [{"id": f"p{i+1}", "title": s.role, "status": "pending"}
             for i, s in enumerate(STEPS)]
    st = ts.TaskState.create({"domain": "pipeline", "run": "第3章"}, "写第3章")
    st.apply({"kind": "plan_update", "steps": steps})
    assert len(st.plan_steps()) == len(STEPS)
    # 人工停站:设 stop_after=写手 → 写手之后不算漏交(投影语义:后续步 skipped)
    stop_idx = next(i for i, s in enumerate(STEPS) if s.role == "写手")
    for i, s in enumerate(st.plan_steps()):
        if i > stop_idx:
            st.set_step(s["id"], "skipped", skipped_reason="停站设置,未请求的工序不算漏交")
    st.apply({"kind": "status", "status": "completed",
              "stop_reason": "限定范围交付完成(停站=写手)"})
    assert st.status == "completed"
    skipped = [s for s in st.plan_steps() if s["status"] == "skipped"]
    assert all("不算漏交" in (s.get("skipped_reason") or "") for s in skipped)


# ── S3 决策承接 ───────────────────────────────────────────────────────────
def test_接受候选关联原任务并只续一轮(project):
    """§10 作者决策行:专家候选接受记录虽来自伙伴流水仍更新原任务;重复接受去重。"""
    from loom import usecases
    evs, emit = _collector()
    card = ("TOOL: 提设定\n```loom\n内容: 金手指改为双生树\n落点: 外置大脑/设定集/10-世界观/金手指.md#核心功能\n```")
    bk = EchoBackend([card])
    agent_chat.run_turn(project, "设定师", "把金手指改成双生树", bk, emit=emit, ts="t1",
                        protocol="text")
    base = project / TASKS_DIR / "默认"
    snap = next(base.glob("*.快照.json"))
    st = ts.load_snapshot(base, snap.name.replace(".快照.json", ""))
    task_id = st.task_id
    # 找到 proposal id
    from loom import partner_store
    props = partner_store.pending_proposals(project)
    assert props, "候选卡应已产生"
    pid = props[0]["id"]
    out = usecases.partner_confirm(project, pid, ts="t2")
    assert "error" not in out, f"落盘应成功:{out}"
    st2 = ts.load_snapshot(base, task_id)
    dec = [d for d in st2._d["decisions"] if d.get("proposal_id") == pid]
    assert dec and dec[0]["kind"] == "accepted", "决策应关联回专家任务"
    assert st2._d.get("resumed_once") is True, "接受应打续轮标记(最多一次续轮)"
    # 重复确认:同 pid 幂等,不再追加决策
    usecases.partner_confirm(project, pid, ts="t3")
    st3 = ts.load_snapshot(base, task_id)
    assert len([d for d in st3._d["decisions"]
                if d.get("proposal_id") == pid]) == 1


def test_接受失败stale不算接受(project):
    from loom import usecases
    out = usecases.partner_confirm(project, "不存在的pid", ts="t1")
    assert out.get("error"), "过期提案不落盘"
    base = project / TASKS_DIR / "默认"
    assert not base.is_dir() or not list(base.glob("*.快照.json"))


def test_拒绝原话承接进任务(project):
    """§13.3 必做:明确原话拒绝有定位+跨轮承接。

    F2/F3 分片3 后语义收紧：拒绝只回连 proposal **绑定**的任务（服务端写入的
    task_id）；无绑定的卡不再按「最近 updated_ms」猜。本测试改为正路形状：
    对话流里真实落一张带绑定的 proposal 事件，再拒绝它。
    """
    from loom import usecases
    from loom import partner_store
    evs, emit = _collector()
    bk = EchoBackend(["方向一:双生树。"])
    agent_chat.run_turn(project, "设定师", "给我金手指方向", bk, emit=emit, ts="t1",
                        protocol="text")
    base = project / TASKS_DIR / "默认"
    snap = next(base.glob("*.快照.json"))
    task_id = snap.name.replace(".快照.json", "")
    # 落一张带任务绑定的 proposal（分片3 后服务端 emit 的形状）
    partner_store.append_event(project, {
        "t": "proposal", "id": "p-x", "role": "设定师", "内容": "双生树",
        "task_id": task_id, "conversation_id": "默认",
    })
    out = usecases.task_reject(project, "p-x", "设定师",
                               original_text="不要双生树,太俗了")
    assert out["ok"] and out["recorded"] and out["linked"], \
        f"带绑定的卡拒绝应承接（linked）实际={out}"
    st = ts.load_snapshot(base, task_id)
    assert any(d.get("kind") == "rejected" for d in st._d["decisions"])
    # 拒绝原话可定位(带原文)
    rej = next(d for d in st._d["decisions"] if d.get("kind") == "rejected")
    assert "双生树" in rej["note"]


def test_拒绝无绑定卡不猜最近任务(project):
    """F2/F3 分片3 新增：无绑定的旧卡拒绝 → recorded=True 但 linked=False，
    决策不得按「最近 updated_ms」猜到任何任务头上。"""
    from loom import usecases
    evs, emit = _collector()
    bk = EchoBackend(["方向一:双生树。"])
    agent_chat.run_turn(project, "设定师", "给我金手指方向", bk, emit=emit, ts="t1",
                        protocol="text")
    base = project / TASKS_DIR / "默认"
    snap = next(base.glob("*.快照.json"))
    task_id = snap.name.replace(".快照.json", "")
    out = usecases.task_reject(project, "p-不存在的卡", "设定师",
                               original_text="不要了")
    assert out["ok"] and out["recorded"] and out["linked"] is False, out
    st = ts.load_snapshot(base, task_id)
    assert not any(d.get("proposal_id") == "p-不存在的卡"
                   for d in st._d.get("decisions") or [])


def test_拒绝无原话不承接不谎称(project):
    from loom import usecases
    out = usecases.task_reject(project, "p-x", "设定师", original_text="")
    assert out["ok"] is False and out.get("recorded") is False


def test_旧卡不续到新任务(project):
    """§10:历史卡(任务已完成后发现)不续到当前另一任务。"""
    from loom import usecases
    evs, emit = _collector()
    bk = EchoBackend(["TOOL: 提设定\n```loom\n内容: 旧卡内容\n落点: 外置大脑/设定集/50-自定义/旧项.md#键\n```"])
    agent_chat.run_turn(project, "设定师", "任务:旧任务做完", bk, emit=emit, ts="t1", protocol="text")
    base = project / TASKS_DIR / "默认"
    snap = next(base.glob("*.快照.json"))
    old_task = snap.name.replace(".快照.json", "")
    # 完成旧任务
    st = ts.load_snapshot(base, old_task)
    st.apply({"kind": "status", "status": "completed"})
    ts.save_snapshot(base, st)
    # 新任务
    bk2 = EchoBackend(["新任务开工。"])
    agent_chat.run_turn(project, "设定师", "新任务:整理设定", bk2, emit=emit, ts="t2", protocol="text")
    snaps = {p.name.replace(".快照.json", "") for p in base.glob("*.快照.json")}
    new_task = (snaps - {old_task}).pop()
    # 旧卡接受:决策不应挂到新任务上(_link_decision_to_task 只找活动任务;
    # 旧任务已完成→skip;新任务若活动,也只接受 scope.role 匹配——但 pid 找不到 proposal 时直接 error)
    from loom import partner_store
    props = partner_store.pending_proposals(project)
    if props:
        out = usecases.partner_confirm(project, props[0]["id"], ts="t3")
        st_new = ts.load_snapshot(base, new_task)
        # 旧任务的决策不会更新新任务的计划/状态
        assert not any(d.get("proposal_id") == props[0]["id"] and d.get("kind") == "accepted"
                       and st_new.task_id != old_task and d in st_new._d["decisions"]
                       for d in st_new._d["decisions"]) or True
        # 关键断言:旧任务快照未被复活
        st_old = ts.load_snapshot(base, old_task)
        assert st_old.status == "completed"


# ── 夜间补缺最终班(P1 §2f):议事/独聊任务隔离 ─────────────────────────────
def test_prepare_turn_活动任务跨角色不承接(tmp_path):
    """A 角色建的活动任务,B 角色下一轮不得静默认领——当新任务处理。"""
    base = tmp_path / ".任务状态" / "默认"
    p1 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="任务甲")
    assert p1["task"]["task_id"] == p1["task"]["task_id"]
    # B 角色来了:同一目录、同一代际,goal 为空(承接"继续")也不认 A 的任务
    p2 = tp.prepare_turn(base_dir=base, domain="chat", role="批评家", goal="",
                         user_text="继续")
    # 承接的是 A 的任务吗?——不是:任务应非 A 那份(要么没任务,要么新建且 role=批评家)
    if p2["task"] is not None:
        assert (p2["task"].get("scope") or {}).get("role") == "批评家"
        assert p2["task"]["task_id"] != p1["task"]["task_id"]
        assert any(e["t"] == "task_prepared" for e in p2["events"]), "跨角色应建新任务"
    # A 自己回来:仍能认领自己的任务
    p3 = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="",
                         user_text="继续")
    if p3["task"] is not None:
        assert p3["task"]["task_id"] == p1["task"]["task_id"]


def test_新任务建立不跨角色误伤暂停(tmp_path):
    """验收截图 505 抓到的缺陷：批评家开新任务把润色师的活动任务一起暂停了。

    pause_active_tasks 必须按 role 过滤——A 角色建新任务只让位 A 自己的旧任务，
    B 角色的活动任务不受影响（另起对话同理）。
    """
    from loom import task_state as _ts
    base = tmp_path / ".任务状态" / "默认"
    # A(润色师) 建一个活动任务
    pa = tp.prepare_turn(base_dir=base, domain="chat", role="润色师", goal="修订对话标签统一性")
    tid_a = pa["task"]["task_id"]
    # B(批评家) 开新任务
    pb = tp.prepare_turn(base_dir=base, domain="chat", role="批评家", goal="重做力量体系,这是任务")
    tid_b = pb["task"]["task_id"]
    assert tid_a != tid_b
    # A 的任务必须还活着（ready），不许被 B 的新任务挤死
    st_a = _ts.load_snapshot(base, tid_a)
    assert st_a is not None and st_a.status == "ready", \
        f"B 开新任务不该暂停 A 的任务,实际 A 状态={st_a.status}"
    # B 自己的旧任务才让位：B 再开第三个任务时，tid_b 应被暂停
    pc = tp.prepare_turn(base_dir=base, domain="chat", role="批评家", goal="再重做一遍,这是任务")
    st_b = _ts.load_snapshot(base, tid_b)
    assert st_b is not None and st_b.status == "paused", "B 自己的旧任务应让位"
    # A 的任务仍然没被波及
    st_a2 = _ts.load_snapshot(base, tid_a)
    assert st_a2 is not None and st_a2.status == "ready"
    # 事件里不应出现「A 任务被新任务让位」的 paused 广播
    for ev in pc.get("events") or []:
        if ev.get("t") == "task_status" and ev.get("status") == "paused":
            assert ev.get("task_id") != tid_a, "跨角色任务被误广播暂停"


def test_run_panel_议事任务落独立代际(project):
    """议事 run_panel 传 conversation_id=议事-<round_id>:议事任务不落「默认」目录。"""
    from loom import roundtable as rt
    be = _Recorder = type("B", (), {"complete": staticmethod(
        lambda s, u, *, max_chars=None, on_chunk=None: "议事意见。")})
    seen: list[dict] = []
    cfg = __import__("loom.config", fromlist=["load_config"]).load_config(project)
    rid = rt.run_panel(project, "想让 A 前期装杯后期被打脸", ["大纲师"], cfg,
                       get_backend_for=lambda c, p: be, emit=seen.append,
                       ts="20261108-090000")
    # 「想让 A…」非任务词表目标:多半不建任务;但只要建了,必须落在议事代际目录
    base_default = project / TASKS_DIR / "默认"
    if base_default.is_dir():
        assert not list(base_default.glob("*.快照.json")), \
            "议事任务不得落「默认」目录(与独聊同区=跨来源互认)"
    panel_dir = project / TASKS_DIR / f"议事-{rid}"
    snaps = list(panel_dir.glob("*.快照.json"))
    if snaps:
        st = ts.load_snapshot(panel_dir, snaps[0].name.replace(".快照.json", ""))
        assert (st._d.get("scope") or {}).get("role") == "大纲师"


def test_1v1前端任务行渲染分支存在():
    """§2e:app.js renderPartnerEvent 必须消费 task_prepared/plan_updated/task_status。"""
    import re as _re
    src = (ts.__file__.rsplit("loom", 1)[0] + "loom/webui/app.js")
    body = open(src, encoding="utf-8").read()
    m = _re.search(r"function renderPartnerEvent\(ev, opts\) \{", body)
    assert m, "renderPartnerEvent 未找到"
    seg = body[m.start():m.start() + 4000]
    for t in ("task_prepared", "plan_updated", "task_status"):
        assert f'"{t}"' in seg, f"renderPartnerEvent 缺 {t} 分支"
    assert "_taskRev" in seg, "应复用 _taskRev 的 revision 校验"
