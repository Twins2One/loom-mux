"""6d 日志恢复契约：比较完整状态，不仅比较status。"""
import json
from loom import task_state as ts, turn_prepare as tp


def test_start_replay_preserves_identity_and_times(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "now_ms", lambda: 123456)
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师",
                               goal="整理世界", origin_event_id="origin-1")
    tid = prepared["task"]["task_id"]
    expected = ts.load_snapshot(tmp_path, tid).to_dict()
    monkeypatch.setattr(ts, "now_ms", lambda: 987654)
    assert ts.rebuild_from_log(tmp_path, tid).to_dict() == expected


def test_committed_events_replay_exactly(tmp_path, monkeypatch):
    clock = iter(range(1000, 2000))
    monkeypatch.setattr(ts, "now_ms", lambda: next(clock))
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="study", role="知识顾问", goal="整理材料")
    tid = prepared["task"]["task_id"]
    state = ts.load_snapshot(tmp_path, tid)
    for event in [
        {"kind": "constraint", "content": "保留来源"},
        {"kind": "plan_update", "steps": [{"id": "a", "title": "核查", "status": "completed", "result_ref": "source"}]},
        {"kind": "decision", "proposal_id": "p", "decision_kind": "accepted"},
        {"kind": "outcome", "summary": "已核查"},
    ]:
        ts.commit(tmp_path, state, event, expected_revision=state.revision)
    assert ts.rebuild_from_log(tmp_path, tid).to_dict() == ts.load_snapshot(tmp_path, tid).to_dict()


def test_log_ahead_of_valid_cache_is_recovered(tmp_path):
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="整理世界")
    tid = prepared["task"]["task_id"]
    state = ts.load_snapshot(tmp_path, tid)
    event = {"kind": "outcome", "summary": "日志已提交", "ts": 234567}
    state.apply(event)
    ts.append_task_event(tmp_path, tid, {**event, "applied_revision": state.revision})
    assert ts.load_snapshot(tmp_path, tid).to_dict() == state.to_dict()


def test_log_only_task_is_discovered(tmp_path):
    state = ts.TaskState.create({"domain": "chat", "role": "设定师", "conversation_id": "默认"}, "整理世界")
    ts.append_task_event(tmp_path, state.task_id, {"kind": "start", "scope": state._d["scope"],
                        "goal": "整理世界", "initial_state": state.to_dict(), "ts": state._d["created_ms"]})
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", user_text="继续")
    assert prepared["task"]["task_id"] == state.task_id


def test_paused_task_new_goal_has_no_transient_resume(tmp_path):
    old = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="旧目标")
    tid = old["task"]["task_id"]
    ts.pause_active_tasks(tmp_path)
    before = ts.load_snapshot(tmp_path, tid).revision
    new = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", task_id=tid, goal="新目标")
    assert new["task"]["task_id"] != tid
    assert ts.load_snapshot(tmp_path, tid).revision == before


def test_log_only_tasks_visible_in_real_read_routes(project, tmp_path, monkeypatch):
    from loom.api.agents import agent_task_snapshot_ep
    from loom.study import ask_task_ep, library
    from loom.paths import TASKS_DIR
    monkeypatch.setattr(library, "library_root", lambda: tmp_path)
    for base, role, read in [
        (project / TASKS_DIR / "默认", "设定师", lambda: agent_task_snapshot_ep(str(project))),
        (tmp_path / ".loom-tasks" / "知识顾问" / "默认", "知识顾问", lambda: ask_task_ep(role="知识顾问")),
    ]:
        state = ts.TaskState.create({"domain": "chat", "role": role}, "日志任务")
        ts.append_task_event(base, state.task_id, {"kind": "start", "initial_state": state.to_dict()})
        assert state.task_id in [t["task_id"] for t in read()["tasks"]]


def test_incomplete_tail_is_not_committed(tmp_path, caplog):
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="目标")
    tid = prepared["task"]["task_id"]
    expected = ts.load_snapshot(tmp_path, tid).to_dict()
    with ts._log_path(tmp_path, tid).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "outcome", "summary": "未提交", "applied_revision": 2}))
    assert ts.rebuild_from_log(tmp_path, tid).to_dict() == expected
    assert "尾行未提交" in caplog.text


def test_valid_json_but_invalid_snapshot_recovers(tmp_path):
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="目标")
    tid = prepared["task"]["task_id"]
    expected = ts.load_snapshot(tmp_path, tid).to_dict()
    for invalid in [{}, [], {"task_id": tid, "revision": "坏版本"}]:
        ts._snap_path(tmp_path, tid).write_text(json.dumps(invalid), encoding="utf-8")
        assert ts.load_snapshot(tmp_path, tid).to_dict() == expected


def test_next_commit_after_uncommitted_tail_replays(tmp_path):
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="目标")
    tid = prepared["task"]["task_id"]
    state = ts.load_snapshot(tmp_path, tid)
    with ts._log_path(tmp_path, tid).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "outcome", "summary": "未提交"}))
    ts.commit(tmp_path, state, {"kind": "outcome", "summary": "合法产出"}, expected_revision=state.revision)
    assert ts.rebuild_from_log(tmp_path, tid).to_dict() == state.to_dict()


def test_legacy_event_without_revision_catches_up(tmp_path):
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="目标")
    tid = prepared["task"]["task_id"]
    ts.append_task_event(tmp_path, tid, {"kind": "outcome", "summary": "旧协议提交", "ts": 67890})
    assert ts.load_snapshot(tmp_path, tid)._d["last_outcome"] == "旧协议提交"


def test_invalid_event_cannot_partially_change_replay(tmp_path, caplog):
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="目标")
    tid = prepared["task"]["task_id"]
    expected = ts.load_snapshot(tmp_path, tid).to_dict()
    ts.append_task_event(tmp_path, tid, {"kind": "unknown", "ts": 999999})
    assert ts.rebuild_from_log(tmp_path, tid).to_dict() == expected
    assert "事件未折叠" in caplog.text


def test_same_directory_alias_uses_same_lock(tmp_path):
    assert ts._scope_lock(tmp_path) is ts._scope_lock(tmp_path / "child" / "..")


def test_malformed_metadata_and_foreign_events_are_ignored(tmp_path, caplog):
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="目标")
    tid = prepared["task"]["task_id"]
    expected = ts.load_snapshot(tmp_path, tid).to_dict()
    for event in [
        {"kind": "outcome", "summary": "坏水位", "applied_revision": "invalid"},
        {"kind": "start", "initial_state": [1]},
        {"kind": "outcome", "summary": "其他任务", "task_id": "foreign", "applied_revision": 900},
        {"kind": "outcome", "summary": "其他作用域", "scope": {"task_id": "foreign"}},
    ]:
        ts.append_task_event(tmp_path, tid, event)
    assert ts.load_snapshot(tmp_path, tid).to_dict() == expected
    assert ts.rebuild_from_log(tmp_path, tid).to_dict() == expected
    assert "损坏行" in caplog.text


def test_invalid_initial_state_does_not_fabricate_task(tmp_path, caplog):
    ts.append_task_event(tmp_path, "broken", {"kind": "start", "initial_state": [1], "ts": "invalid"})
    assert ts.load_snapshot(tmp_path, "broken") is None
    assert "损坏行" in caplog.text
