# -*- coding: utf-8 -*-
"""收尾班(第五班)两项补缺测试:
补1 1:1 当前任务快照恢复(/api/agent/task 接通 + active_only/role 过滤 + tail 挤出模拟);
补2 agentic/流水线收尾后快照无 pending 步骤(completed+skipped 全覆盖)。
"""
from __future__ import annotations

import pathlib
from loom import task_state as ts
from loom.paths import TASKS_DIR


# ── 补1:后端过滤参数 ─────────────────────────────────────────────────────
def test_task快照端点_active_only与role过滤(tmp_path, monkeypatch):
    """active_only=true 只回活动任务;role 非空再按 scope.role 过滤;不带参数=旧行为。"""
    import pathlib
    from fastapi.testclient import TestClient
    from tests.conftest import require_http_transport
    require_http_transport()
    from loom.server import app as _app
    client = TestClient(_app, base_url="http://127.0.0.1:8611")   # 本机 Host 闸要求
    (tmp_path / "loom.toml").write_text("[book]" + chr(10) + "title = \"t\"" + chr(10), encoding="utf-8")
    root = str(tmp_path)
    base = pathlib.Path(root) / TASKS_DIR / "默认"
    base.mkdir(parents=True)
    # 活动任务(设定师) + 终态任务(大纲师,completed) + 活动但别的角色(润色师)
    st_a = ts.TaskState.create({"domain": "chat", "role": "设定师", "conversation_id": "默认"}, "A 的活任务")
    ts.append_task_event(base, st_a.task_id, {"kind": "start", "scope": st_a._d["scope"], "goal": "A 的活任务", "ts": ts.now_ms()})
    ts.save_snapshot(base, st_a)
    st_c = ts.TaskState.create({"domain": "chat", "role": "大纲师", "conversation_id": "默认"}, "旧任务")
    ts.append_task_event(base, st_c.task_id, {"kind": "start", "scope": st_c._d["scope"], "goal": "旧任务", "ts": ts.now_ms()})
    ts.commit(base, st_c, {"kind": "status", "status": "completed", "stop_reason": "已完成"})
    st_b = ts.TaskState.create({"domain": "chat", "role": "润色师", "conversation_id": "默认"}, "B 的活任务")
    ts.append_task_event(base, st_b.task_id, {"kind": "start", "scope": st_b._d["scope"], "goal": "B 的活任务", "ts": ts.now_ms()})
    ts.save_snapshot(base, st_b)

    # 旧行为:不带 active_only → 三个全回(向后兼容)
    r0 = client.get("/api/agent/task", params={"root": root}).json()
    assert r0["ok"] and len(r0["tasks"]) == 3
    # active_only → 只剩两个活动
    r1 = client.get("/api/agent/task", params={"root": root, "active_only": "true"}).json()
    ids1 = {t["task_id"] for t in r1["tasks"]}
    assert st_a.task_id in ids1 and st_b.task_id in ids1
    assert st_c.task_id not in ids1, "completed 任务不得冒充当前任务"
    # active_only+role=设定师 → 只剩 A 的
    r2 = client.get("/api/agent/task", params={"root": root, "active_only": "true",
                                               "role": "设定师"}).json()
    ids2 = {t["task_id"] for t in r2["tasks"]}
    assert ids2 == {st_a.task_id}, f"role 过滤失准: {ids2}"
    # 快照读取零模型:端点本身不调任何后端(读盘即回)


def test_task快照端点_conversation_id清洗沿用旧口径(tmp_path):
    """conversation_id 含路径分隔符 → 回退「默认」,不给越界读取口子(P1-2 口径不变)。

    2026-11-08 分片5（F10）：root 现走 _project_root 口径（必须是真书）——
    测试 root 铺 loom.toml 造书前提（外部任意目录不再可读）。
    """
    import pathlib
    from fastapi.testclient import TestClient
    from tests.conftest import require_http_transport
    require_http_transport()
    from loom.server import app as _app
    client = TestClient(_app, base_url="http://127.0.0.1:8611")
    (tmp_path / "loom.toml").write_text("[book]" + chr(10) + "title = \"t\"" + chr(10), encoding="utf-8")
    root = str(tmp_path)
    r = client.get("/api/agent/task",
                   params={"root": root, "conversation_id": "../外", "active_only": "true"}).json()
    assert r["ok"] and r["tasks"] == []
    assert not (pathlib.Path(root) / "外").exists()


def test_task快照端点_无任务目录向后兼容(tmp_path):
    """旧书没有 .任务状态 目录:端点照常 200 空列表,不炸。"""
    from fastapi.testclient import TestClient
    from tests.conftest import require_http_transport
    require_http_transport()
    from loom.server import app as _app
    (tmp_path / "loom.toml").write_text("[book]" + chr(10) + "title = \"t\"" + chr(10), encoding="utf-8")
    client = TestClient(_app, base_url="http://127.0.0.1:8611")
    r = client.get("/api/agent/task", params={"root": str(tmp_path), "active_only": "true"}).json()
    assert r == {"ok": True, "tasks": []}


def test_task快照端点_非书root被拒(tmp_path):
    """F10 分片5 新增：root 不是 loom 项目（无 loom.toml）→ 400，不任意读。"""
    from fastapi.testclient import TestClient
    from tests.conftest import require_http_transport
    require_http_transport()
    from loom.server import app as _app
    client = TestClient(_app, base_url="http://127.0.0.1:8611")
    r = client.get("/api/agent/task", params={"root": str(tmp_path)})
    assert r.status_code == 400


# ── 补1:前端接线契约 ─────────────────────────────────────────────────────
def test_appjs_loadAgentCenter拉快照恢复任务行():
    """app.js loadAgentCenter 必须拉 /api/agent/task?active_only=true 并按 role 筛活动任务。"""
    import re as _re
    src = (ts.__file__.rsplit("loom", 1)[0] + "loom/webui/app.js")
    body = open(src, encoding="utf-8").read()
    m = _re.search(r"async function loadAgentCenter\(role, lockedSlot\) \{", body)
    assert m, "loadAgentCenter 未找到"
    seg = body[m.start():m.start() + 3000]
    assert "/api/agent/task" in seg, "loadAgentCenter 未接通任务快照端点"
    assert "active_only=true" in seg, "必须按活动任务过滤(旧任务/completed 不冒充)"
    assert "task_prepared" in seg, "命中活动任务应补一行任务行(复用三分支渲染)"


def test_1v1任务行渲染分支仍在():
    """收尾班改动后 renderPartnerEvent 三分支契约保持(最终班 §2e 不回退)。"""
    import re as _re
    src = (ts.__file__.rsplit("loom", 1)[0] + "loom/webui/app.js")
    body = open(src, encoding="utf-8").read()
    m = _re.search(r"function renderPartnerEvent\(ev, opts\) \{", body)
    assert m
    seg = body[m.start():m.start() + 4000]
    for t in ("task_prepared", "plan_updated", "task_status"):
        assert f'"{t}"' in seg
    assert "_taskRev" in seg


# ── 补2:agentic/流水线收尾后无 pending 矛盾 ──────────────────────────────
def test_收尾后快照无pending步骤():
    """§5:收尾后快照 completed+skipped 全覆盖,status=completed 无矛盾。
    直接驱动 _pipeline_task(finish=True) 收尾路径(与 agentic/流水线共用)。"""
    import pathlib
    from loom import agents as _ag
    root = pathlib.Path(_ag.__file__).parent  # 任意存在目录;只用到 tmp 基座
    import tempfile
    proj = pathlib.Path(tempfile.mkdtemp())
    from loom.config import Config
    cfg = Config(title="测试书")
    evs = []
    from loom import events as _ev
    prog = lambda e: evs.append(e)
    _ag._pipeline_task(proj, 3, cfg, prog)          # 开跑
    # 模拟 agentic 投影:只完成了 4 步里的 2 步(其余 pending)
    from loom import artifacts as _art
    names = [a.name for a in _art.ARTIFACTS][:4]
    done = set(names[:2])
    # 直接构造投影计划(同 _agentic_step_done 的投影逻辑)
    from loom import task_state as _ts
    base = proj / TASKS_DIR / "流水线"
    st = _ts.load_snapshot(base, "t-pipe-0003")
    assert st is not None and st.is_active()
    steps_in = [{"id": f"s{i+1}", "title": t, "status": "completed" if t in done else "pending"}
                for i, t in enumerate(names)]
    _ts.commit(base, st, {"kind": "plan_update", "reason": "agentic 产物清单投影",
                          "steps": steps_in}, expected_revision=st.revision)
    _ts.save_snapshot(base, st)
    # 收尾
    _ag._pipeline_task(proj, 3, cfg, prog, finish=True)
    st2 = _ts.load_snapshot(base, "t-pipe-0003")
    assert st2.status == "completed"
    pend = [s for s in st2.plan_steps() if s["status"] == "pending"]
    assert not pend, f"收尾后仍有 pending 步骤(自相矛盾): {pend}"
    statuses = {s["status"] for s in st2.plan_steps()}
    assert statuses <= {"completed", "skipped"}, f"意外状态: {statuses}"
    for s in st2.plan_steps():
        if s["status"] == "skipped":
            assert s.get("skipped_reason") == "未请求的工序不算漏交", s
    assert any(e.get("t") == "task_status" or
               (e.get("type") == "task_status" and e.get("status") == "completed")
               for e in evs), evs


def test_收尾幂等_已终态不重复动步骤():
    """二次收尾(如 stop_after 后再跑)对终态任务 no-op,不重复 bump revision。"""
    import pathlib, tempfile
    from loom import agents as _ag
    from loom.config import Config
    proj = pathlib.Path(tempfile.mkdtemp())
    cfg = Config(title="测试书")
    evs = []
    prog = lambda e: evs.append(e)
    _ag._pipeline_task(proj, 5, cfg, prog, finish=True)   # 无既有任务:建+立即收尾
    from loom import task_state as _ts
    base = proj / TASKS_DIR / "流水线"
    st = _ts.load_snapshot(base, "t-pipe-0005")
    rev1 = st.revision
    assert st.status == "completed"
    _ag._pipeline_task(proj, 5, cfg, prog, finish=True)   # 二次收尾:终态不接受 status
    st2 = _ts.load_snapshot(base, "t-pipe-0005")
    assert st2.revision == rev1, "终态任务不得被二次收尾改写"


def test_agentic_step_done投影计划本身全completed不误标skipped():
    """正常走完的产物:投影建计划时当前产物 completed,收尾才把剩余标 skipped。"""
    import pathlib, tempfile
    from loom import agents as _ag, artifacts as _art, task_state as _ts
    proj = pathlib.Path(tempfile.mkdtemp())
    from loom.config import Config
    cfg = Config(title="测试书")
    evs = []
    prog = lambda e: evs.append(e)
    _ag._pipeline_task(proj, 2, cfg, prog)
    name = _art.ARTIFACTS[0].name
    _ag._agentic_step_done(proj, 2, name, prog)
    base = proj / TASKS_DIR / "流水线"
    st = _ts.load_snapshot(base, "t-pipe-0002")
    step = next(s for s in st.plan_steps() if s["title"] == name)
    assert step["status"] == "completed"
    assert st.status != "completed"      # 中途不冒充收尾
    _ag._pipeline_task(proj, 2, cfg, prog, finish=True)
    st2 = _ts.load_snapshot(base, "t-pipe-0002")
    assert not [s for s in st2.plan_steps() if s["status"] == "pending"]



def test_另起对话暂停旧任务且不回显(tmp_path):
    """复审方行为用例(审查记录):A 建立(非终态)→agent/chat/new→A 不被 active_only 返回;
    盘上 paused 可追溯;直接构造快照驱动,不依赖模型。"""
    from fastapi.testclient import TestClient
    from tests.conftest import require_http_transport
    require_http_transport()
    from loom.server import app as _app
    client = TestClient(_app, base_url="http://127.0.0.1:8611")
    (tmp_path / "loom.toml").write_text("[book]" + chr(10) + "title = \"t\"" + chr(10), encoding="utf-8")
    root = str(tmp_path)
    base = pathlib.Path(root) / TASKS_DIR / "默认"
    base.mkdir(parents=True)
    st_a = ts.TaskState.create({"domain": "chat", "role": "设定师", "conversation_id": "默认"}, "A 的活任务")
    ts.append_task_event(base, st_a.task_id, {"kind": "start", "scope": st_a._d["scope"], "goal": "A", "ts": ts.now_ms()})
    ts.save_snapshot(base, st_a)
    # 另起对话(会 pause 掉 A)
    r2 = client.post("/api/agent/chat/new", json={"root": root, "role": "设定师"})
    assert r2.status_code == 200
    q = client.get("/api/agent/task", params={"root": root, "role": "设定师",
                                              "conversation_id": "默认", "active_only": "true"}).json()
    assert q.get("tasks") == [], f"另起后旧任务不得回显: {q.get('tasks')}"
    # 盘上 paused 可追溯
    after = [(_json_snap_taskid(s), _json_snap_status(s)) for s in base.glob("*.快照.json")]
    assert any(st == "paused" for _, st in after), after
    # 新对话建新任务(直接构造,ready)→ active_only 只回 B
    st_b = ts.TaskState.create({"domain": "chat", "role": "设定师", "conversation_id": "默认"}, "B 的新任务")
    ts.append_task_event(base, st_b.task_id, {"kind": "start", "scope": st_b._d["scope"], "goal": "B", "ts": ts.now_ms()})
    ts.save_snapshot(base, st_b)
    q2 = client.get("/api/agent/task", params={"root": root, "role": "设定师",
                                               "conversation_id": "默认", "active_only": "true"}).json()
    ids = [t["task_id"] for t in q2.get("tasks", [])]
    assert ids == [st_b.task_id], ids


def _json_snap_taskid(p):
    import json
    return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))["task_id"][-6:]


def _json_snap_status(p):
    import json
    return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))["status"]