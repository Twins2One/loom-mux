# -*- coding: utf-8 -*-
"""S4/S5 前端契约与中断恢复测试（设计 §10:前端行 / 中断恢复行 / 旧数据与签名行 / 预算与降级行）。"""
from __future__ import annotations

import json

import pytest

from loom import task_state as ts, turn_prepare as tp
from loom.paths import TASKS_DIR


# ── 前端契约:新旧消费者共存（§9 契约测试锁）────────────────────────────────
def test_前端handleEvent消费三个任务事件():
    """app.js handleEvent 必须有 task_prepared/plan_updated/task_status 分支(§9)。"""
    src = (ts.__file__.rsplit("loom", 1)[0] + "loom/webui/app.js")
    body = open(src, encoding="utf-8").read()
    start = body.index("function handleEvent(ev)")
    seg = body[start:start + 60000]
    for t in ("task_prepared", "plan_updated", "task_status"):
        assert f'"{t}"' in seg, f"handleEvent 缺 {t} 分支"
    assert "_taskEvent" in seg


def test_书房agentsjs消费任务事件():
    src = (ts.__file__.rsplit("loom", 1)[0] + "loom/webui/study/agents.js")
    body = open(src, encoding="utf-8").read()
    for t in ("task_prepared", "task_status", "plan_updated"):
        assert t in body
    assert "conversation_id" in body      # 任务子区隔离参数随请求带上


def test_人物侧写师进入作者智库前端名册():
    src = ts.__file__.rsplit("loom", 1)[0] + "loom/webui/app.js"
    body = open(src, encoding="utf-8").read()
    author_line = next(line for line in body.splitlines() if line.startswith("const AUTHOR_AGENTS"))
    assert '"人物侧写师"' in author_line
    assert author_line.count('"') // 2 == 8
    assert '"人物侧写师":   { slug: "character-profiler"' in body
    assert "和你一起细化角色设计与人物卡" in body
    assert "人物与设定集互动细节" in body
    assert '"人物侧写师": ["正在梳理人物动机与关系…"' in body
    # 所有角色继续走同名 PNG + 回退；人物侧写师有无同名 PNG 都不影响名册。
    assert "`/agents/${encodeURIComponent(name)}.png?v=${AVATAR_VER}`" in body
    assert "fb.textContent = AGENT_AVATAR_FALLBACK[name] || name.slice(0, 1)" in body


def test_聊天气泡按当前角色取头像():
    """pcBubble 曾把 assistant 固定成领航员：作者智库任何角色回复都顶领航员头像。"""
    src = ts.__file__.rsplit("loom", 1)[0] + "loom/webui/app.js"
    body = open(src, encoding="utf-8").read()
    assert "function pcBubble(role, text, live, speakerRole)" in body
    assert "const who = speakerRole || \"领航员\";" in body
    assert "av.appendChild(agentAvatar(who, \"jc-ava\", \"jc-fallback\"))" in body
    # 智库对话把当前角色传进渲染链，流式增量同样带上。
    assert "role: _navRole || \"领航员\"," in body
    assert "pcBubble(\"assistant\", ev.text, false, (opts && opts.role) || \"领航员\")" in body
    assert "pcBubble(\"assistant\", ac._delta, true, role)" in body
    # 人物侧写师没有正式 PNG 时用专属占位，而不是通用首字「人」。
    assert '"人物侧写师": "侧"' in body
    assert "正式头像待本地生成" in body


def test_角色卡与共用卡片有窄栏安全规则():
    base = ts.__file__.rsplit("loom", 1)[0] + "loom/webui/"
    css = open(base + "style.css", encoding="utf-8").read()
    assert ".agent-info { flex: 1 1 auto; min-width: 0; padding-right: 12px; }" in css
    assert ".agent-desc { margin-top: 2px; min-width: 0;" in css
    assert "overflow-wrap: anywhere; word-break: break-word; }" in css
    assert "max-width: calc(100vw - 24px)" in css
    study = open(base + "study.html", encoding="utf-8").read()
    assert ".route-card-head { display:flex; align-items:baseline; gap:10px; min-width:0; flex-wrap:wrap; }" in study
    assert ".route-card-foot { display:flex; align-items:center; gap:10px; margin-top:2px; min-width:0; flex-wrap:wrap; }" in study
    # 窄窗保底：不重排三栏，保底宽允许横向滚动，避免文字被压成竖排。
    assert "@media (max-width: 720px)" in css
    assert ".main { min-width: 720px; }" in css
    assert "#proj-title { min-width: 0; max-width: 40vw;" in css


def test_缓存戳已bump():
    """引用戳一律带文件名断言（`/app.js?v=…`），别用裸 `?v=…` 子串——
    study.html 里 style.css 的戳会让裸子串断言**假通过**。"""
    base = ts.__file__.rsplit("loom", 1)[0] + "loom/webui/"
    body = open(base + "index.html", encoding="utf-8").read()
    # 2026-11-12b：写作交互与书房输入/目录能力，保留 a 版候选卡标记隐藏。
    # app/style/core/browse 与书房对应资源精确戳同步。
    # 2026-11-16f = 设置文案清理 + AI思考强度 + 单会话上下文上限 + 记忆库位置面板三行；
    # app 与本轮复审修过的 workbench/library 都动过内容，戳一并前移到 f（style.css 仍是 e）。
    assert "/app.js?v=20261116f" in body, "index.html 的 app.js 缓存戳未 bump 到 20261116f"
    # 2026-11-11c = v7 收口：本轮改过的 core.js / app.js / workbench.js / library.js 统一前移，
    # 避免"改了内容却让浏览器继续吃旧缓存"（需求方 审查记录）。
    assert "/study/core.js?v=20261116d" in body, "index.html 的 core.js 缓存戳未 bump 到 20261116d"
    # 写死具体戳而不是裸 `/style.css?v=`：本文件开头那条纪律——裸子串会让 study.html 的戳假通过。
    assert "/style.css?v=20261116e" in body, "index.html 的 style.css 缓存戳未 bump 到 20261116e"
    # study.html：2026-11-10a 同批（核心 sameRoot / workbench 移除清键 / partner·agents 记忆反馈）；
    # 2026-11-11a = F2 记忆库面板（core.js 新增 memoryPanel、workbench 挂载、index/study 加承载位）。
    # 同一批顺带把几处**文件改了戳没动**的历史错配补齐（archive/roles/finder/study.css/style.css），
    # 判据=文件 mtime 日期不得晚于戳日期（见 docs/H01最小修复候选-20261110.md §缓存戳核对）。
    study = base + "study.html"
    study_body = open(study, encoding="utf-8").read()
    # 2026-11-11c = v7 收口：core / workbench / library 三件本轮都动过，戳一并前移。
    # 特别记过一笔：library.js 从 20261111a（首版）起内容改到 v6 却**没人动戳** ——
    # 这正是"改了内容忘了前移戳"的实例，现在由下面的日期守卫 + 精确断言双重把关。
    # 2026-11-16e：core / workbench / library 三件本轮都动过（三档小字、prefs 失败路径、
    # 迁移弹层与位置面板），戳一并前移；partner / agents 本轮未动，仍留在 20261112b。
    for ref in ("/study/core.js?v=20261116d", "/study/workbench.js?v=20261116f",
                "/study/library.js?v=20261116f",
                "/study/partner.js?v=20261112b", "/study/agents.js?v=20261112b"):
        assert ref in study_body, ("study.html 缓存戳未写清:", ref)
    assert "/style.css?v=20261116e" in study_body, "study.html 的 style.css 缓存戳未与 index.html 对齐"
    assert "/study.css?v=20261112b" in study_body, "study.html 的 study.css 缓存戳未 bump 到 20261112b"
    assert "/browse.js?v=20261112b" in body
    assert "/browse.js?v=20261112b" in study_body


def test_缓存版本策略_戳日期不得早于文件改动():
    """缓存版本策略的机械守卫（v7 交付边界）。

    规则：每个 `资源?v=YYYYMMDDx` 的**日期部分**不得早于该资源的 mtime 日期。
    戳早于内容 ⇒ 老浏览器会一直用带缺陷的缓存副本，且从界面上看不出来。
    这条把"改内容必须前移戳"从口头约定变成可执行断言：以后谁改了文件忘了前移戳，
    这里会点名是哪个文件。
    """
    import re
    from datetime import date
    from pathlib import Path

    webui = Path(ts.__file__.rsplit("loom", 1)[0]) / "loom" / "webui"
    pat = re.compile(r'(?:src|href)="(/[^"?]+)\?v=(\d{8})[a-z]?"')
    problems = []
    for page in ("index.html", "study.html"):
        body = (webui / page).read_text(encoding="utf-8")
        for ref, stamp in pat.findall(body):
            target = webui / ref.lstrip("/")
            if not target.is_file():
                continue                       # 外部/CDN 引用不在此规则内
            changed = date.fromtimestamp(target.stat().st_mtime)
            stamped = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
            if stamped < changed:
                problems.append(f"{page}: {ref} 戳={stamp} 但文件改于 {changed.isoformat()}")
    assert not problems, "缓存戳早于文件改动（会让老浏览器继续用旧副本）：\n" + "\n".join(problems)


def test_旧事件契约不变():
    """既有 t 信封事件(user/assistant/tool/result/proposal/error)键未变——旧消费者不破。"""
    from loom import partner_store
    assert callable(partner_store.append_event)
    # type 信封注册表仍含全部旧事件
    from loom import events
    for t in ("pipeline_start", "agent_start", "agent_chunk", "agent_done",
              "chapter_done", "gate_start", "error", "info", "warn", "cancelled"):
        assert t in events.EVENT_TYPES


# ── 中断恢复（§10 表:三窗口中断;书房刷新/服务重启恢复）────────────────────
def test_工具结果确认前中断_重建不伪造完成(tmp_path):
    """工具结果确认前断:日志只有 start,重建=ready,不伪造 completed。"""
    base = tmp_path
    st = ts.TaskState.create({"domain": "chat", "role": "设定师"}, "任务")
    ts.append_task_event(base, st.task_id,
                         {"kind": "start", "scope": st._d["scope"], "goal": "任务"})
    r = ts.rebuild_from_log(base, st.task_id)
    assert r.status == "ready"      # 未确认的执行不算成功


def test_状态确认后展示前中断_快照与日志一致(tmp_path):
    base = tmp_path
    st = ts.TaskState.create({"domain": "chat", "role": "设定师"}, "任务")
    ts.append_task_event(base, st.task_id,
                         {"kind": "start", "scope": st._d["scope"], "goal": "任务"})
    ts.commit(base, st, {"kind": "status", "status": "waiting_author",
                         "stop_reason": "等作者"})
    loaded = ts.load_snapshot(base, st.task_id)
    rebuilt = ts.rebuild_from_log(base, st.task_id)
    assert loaded.status == rebuilt.status == "waiting_author"


def test_running遇重启转待恢复(tmp_path):
    """§8:running 遇进程重启后转为待恢复,不把在途步骤视为成功。"""
    base = tmp_path
    st = ts.TaskState.create({"domain": "chat", "role": "设定师"}, "任务")
    ts.append_task_event(base, st.task_id,
                         {"kind": "start", "scope": st._d["scope"], "goal": "任务"})
    ts.commit(base, st, {"kind": "status", "status": "running"})
    ts.commit(base, st, {"kind": "plan_update", "steps": [{"id": "s1", "title": "在途"}]})
    # 模拟重启:只读盘
    r = ts.load_or_rebuild(base, st.task_id)
    assert r.status == "running"
    # §8 口径:重启后 running 任务可显式转 paused 待核——pause_active_tasks 只动非终态
    ts.pause_active_tasks(base, reason="进程重启,待恢复核对")
    r2 = ts.load_snapshot(base, st.task_id)
    assert r2.status == "paused"
    assert r2.plan_steps()[0]["status"] == "pending"    # 在途步骤不算成功


def test_书房刷新恢复_快照零模型(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(lib))
    from loom.study.library import library_root
    base = library_root() / ".loom-tasks" / "知识顾问" / "s1"
    p = tp.prepare_turn(base_dir=base, domain="study", role="知识顾问",
                        conversation_id="s1", goal="取稿整理")
    # 服务重启:重建路由从盘读,无模型
    st = ts.load_or_rebuild(base, p["task"]["task_id"])
    assert st is not None and st.is_active()
    assert st._d["goal"] == "取稿整理"


def test_书房显式goal才发task_prepared简单问答不发(tmp_path, monkeypatch):
    """§9:简单问答无冗余任务卡——kinds[0] 仍是 status;显式 goal 才建任务。"""
    import json as _json
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "教材").mkdir()
    (lib / "教材" / "节奏.md").write_text("# 节奏\n\n## 慢写\n\n该慢的地方要慢。\n",
                                          encoding="utf-8")
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(lib))
    from fastapi.testclient import TestClient
    from tests.conftest import require_http_transport, FakeBackend
    require_http_transport()
    monkeypatch.setattr("loom.backends.get_backend",
                        lambda _cfg: FakeBackend(responder=lambda s, u: "好。"))
    from loom.server import app as _app
    client = TestClient(_app, base_url="http://127.0.0.1:8611")   # 本机 Host 闸要求
    # 简单问答:不建任务
    evs = []
    def _read(stream):
        out = []
        for line in stream.iter_lines():
            line = (line or "").strip()
            if not line or not line.startswith("{"):
                continue
            try:
                out.append(_json.loads(line))
            except ValueError:
                continue
        return out
    with client.stream("POST", "/api/study/ask", json={"question": "情绪转折前要不要停一拍"}) as r:
        evs = _read(r)
    assert evs[0]["t"] == "status" and evs[-1]["t"] == "done", evs   # 旧契约不变
    assert not any(e["t"] == "task_prepared" for e in evs)
    # 显式 goal:建任务并先发 task_prepared
    with client.stream("POST", "/api/study/ask",
                       json={"question": "把这段整理成笔记", "role": "知识顾问",
                             "goal": "整理便条成笔记", "conversation_id": "g1"}) as r:
        evs2 = _read(r)
    assert evs2[0]["t"] == "task_prepared"
    assert evs2[0].get("task_id") and "goal" in evs2[0]
    from loom.study.library import library_root
    base = library_root() / ".loom-tasks" / "知识顾问" / "g1"
    assert list(base.glob("*.快照.json")), "任务应持久化在 library 任务子区"


# ── 旧数据与签名（§10 表）────────────────────────────────────────────────
def test_无任务身份旧记录只兼容读取(project):
    """旧书没有 .任务状态 目录:history 读取/对话照常,不炸不自动合并。"""
    from loom import agent_chat
    assert agent_chat.history(project, "设定师") == []
    evs = []
    class B:
        def complete(self, s, u, *, max_chars=None, on_chunk=None):
            return "旧书兼容路径正常。"
    agent_chat.run_turn(project, "设定师", "这是什么?", B(), emit=evs.append,
                        ts="t", protocol="text")
    assert not (project / TASKS_DIR / "默认").exists() or \
        not list((project / TASKS_DIR / "默认").glob("*.快照.json"))
    assert any(e.get("t") == "assistant" for e in evs)


def test_旧deliberate文本不生成作者决定(project):
    """旧 deliberate 文本提取只是低置信显示兼容;不写进 decisions。"""
    from loom import deliberate
    prog = deliberate.progress(project, "设定师")
    assert isinstance(prog, dict)
    base = project / TASKS_DIR / "默认"
    assert not base.is_dir() or not list(base.glob("*.快照.json")), \
        "旧文本提取不得新建任务"


def test_任务元数据不进产物签名():
    """§13.5:状态 revision/时间戳不参与 resume 签名——sig_v2 输入不含任务元数据。"""
    import inspect
    from loom import resume
    src = inspect.getsource(resume.sig_v2)
    assert "task_id" not in src and "TASKS_DIR" not in src


# ── 预算与降级（§10 表）──────────────────────────────────────────────────
def test_长历史裁剪保留最近轮():
    tail = [{"role": "user", "text": f"第{i}句" + "长" * 100} for i in range(20)]
    out = tp._tail(tail, max_items=6, max_chars=100)
    assert len(out) == 6
    assert out[-1]["text"].startswith("第19句")
    assert all(len(o["text"]) <= 100 for o in out)


def test_准备层异常回退诊断():
    """prepare_turn 抛异常可被调用方 catch——base_dir 是文件时路径不可建,应抛。"""
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    blocker = d / "占用"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(Exception):
        tp.prepare_turn(base_dir=blocker / "子", domain="chat", role="设定师", goal="任务")


def test_耗尽状态含原因不判完成(tmp_path):
    base = tmp_path
    st = ts.TaskState.create({"domain": "chat", "role": "设定师"}, "任务")
    st.apply({"kind": "plan_update", "steps": [{"id": "s1", "title": "A"}, {"id": "s2", "title": "B"}]})
    st.apply({"kind": "status", "status": "paused", "stop_reason": "轮数耗尽:已完成 0/2,剩余 A、B"})
    assert st.status == "paused"
    assert "轮数耗尽" in st._d["stop_reason"]
    remaining = [s for s in st.plan_steps() if s["status"] == "pending"]
    assert len(remaining) == 2      # 未完成步骤保留,不伪造完成
