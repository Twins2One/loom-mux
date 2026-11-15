"""P2 recovery: historical revision collisions and the public preparation lock."""
import threading

import pytest

from loom import task_state as ts, turn_prepare as tp


def _paused(base):
    prepared = tp.prepare_turn(base_dir=base, domain="chat", role="设定师", goal="核查世界")
    tid = prepared["task"]["task_id"]
    ts.pause_active_tasks(base)
    return ts.load_snapshot(base, tid)


@pytest.mark.parametrize("decision_kind", ["accepted", "rejected"])
def test_historical_resume_collision_recovers_on_normal_read(tmp_path, decision_kind):
    """Construct the exact persisted history in the 6d finding, without claiming
    that the currently decorated prepare_turn can generate this interleaving.
    """
    stale = _paused(tmp_path)
    tid = stale.task_id
    current = ts.load_snapshot(tmp_path, tid)
    ts.commit_retry(tmp_path, current, {"kind": "decision", "proposal_id": "candidate-1",
                                       "decision_kind": decision_kind})
    resume = {"kind": "status", "status": "ready", "stop_reason": "作者显式恢复", "ts": ts.now_ms()}
    stale.apply(resume)
    ts.append_task_event(tmp_path, tid, {**resume, "applied_revision": stale.revision})
    ts.save_snapshot(tmp_path, stale)

    expected = ts.rebuild_from_log(tmp_path, tid).to_dict()
    assert expected["revision"] == 4
    assert expected["decisions"][0]["kind"] == decision_kind
    for _ in range(2):
        assert ts.load_snapshot(tmp_path, tid).to_dict() == expected
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师",
                               task_id=tid, goal="核查世界")
    assert prepared["task"] == expected
    assert all(event["revision"] == 4 for event in prepared["events"])
    assert ts.load_or_rebuild(tmp_path, tid).to_dict() == expected
    state = ts.load_snapshot(tmp_path, tid)
    ts.commit(tmp_path, state, {"kind": "outcome", "summary": "继续核查"}, expected_revision=4)
    assert state.to_dict() == ts.rebuild_from_log(tmp_path, tid).to_dict()
    assert ts.load_snapshot(tmp_path, tid).to_dict() == state.to_dict()


@pytest.mark.parametrize("with_ticket", [False, True])
def test_public_resume_serializes_decision_after_initial_read(tmp_path, monkeypatch, with_ticket):
    """A competing thread reaches the lock after the paused view was read.
    The public wrapper must keep it out until the complete PreparedTurn exists.
    """
    stale = _paused(tmp_path)
    attempted = threading.Event()
    lock_checked = threading.Event()
    errors = []
    observed = []
    original = ts.load_or_rebuild

    def decision():
        try:
            attempted.set()
            lock = ts._scope_lock(tmp_path)
            acquired = lock.acquire(blocking=False)
            observed.append(acquired)
            if acquired:
                lock.release()
            lock_checked.set()
            ts.commit_retry(tmp_path, stale, {"kind": "decision", "proposal_id": "candidate-1"})
        except BaseException as exc:
            errors.append(exc)
            lock_checked.set()

    worker = threading.Thread(target=decision)

    def read_then_contend(base, tid):
        state = original(base, tid)
        if not attempted.is_set():
            worker.start()
            assert attempted.wait(5)
            assert lock_checked.wait(5)
        return state

    monkeypatch.setattr(ts, "load_or_rebuild", read_then_contend)
    from loom import request_turn
    ticket = request_turn.begin(tmp_path, "设定师", "默认") if with_ticket else None
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师",
                               task_id=stale.task_id, goal="核查世界", request_ticket=ticket)
    worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert observed == [False], "decision entered the preparation critical section"
    assert prepared["task"]["revision"] == 3
    assert prepared["task"]["status"] == "ready"
    records = ts._read_log(tmp_path, stale.task_id)
    assert [r.get("applied_revision") for r in records[1:]] == [2, 3, 4]
    final = ts.load_snapshot(tmp_path, stale.task_id).to_dict()
    assert final == ts.rebuild_from_log(tmp_path, stale.task_id).to_dict()
    assert final["decisions"][0]["proposal_id"] == "candidate-1"


def test_resume_uses_latest_decision_view(tmp_path, monkeypatch):
    """Defensive body contract: simulate a stale locator result, not public concurrency."""
    stale = _paused(tmp_path)
    current = ts.load_snapshot(tmp_path, stale.task_id)
    ts.commit_retry(tmp_path, current, {"kind": "decision", "proposal_id": "before-resume"})
    original = ts.load_or_rebuild
    reads = 0

    def stale_first(base, tid):
        nonlocal reads
        reads += 1
        return stale if reads == 1 else original(base, tid)

    monkeypatch.setattr(ts, "load_or_rebuild", stale_first)
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师",
                               task_id=stale.task_id, goal="核查世界")
    assert prepared["task"]["revision"] == 4
    assert prepared["task"]["decisions"][0]["proposal_id"] == "before-resume"
    assert prepared["task"] == ts.rebuild_from_log(tmp_path, stale.task_id).to_dict()


@pytest.mark.parametrize("change", ["completed", "cancelled", "role", "goal", "ready"])
def test_resume_revalidates_latest_state_before_displacing_other_tasks(tmp_path, monkeypatch, change):
    stale = _paused(tmp_path)
    other = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师", goal="另一任务")
    other_id = other["task"]["task_id"]
    latest = ts.load_snapshot(tmp_path, stale.task_id)
    if change in ("completed", "cancelled", "ready"):
        ts.commit(tmp_path, latest, {"kind": "status", "status": change}, expected_revision=latest.revision)
    else:
        # Synthetic scope/goal drift tests the recovery guard, not a supported mutation API.
        latest._d["scope"]["role"] = "编辑" if change == "role" else "设定师"
        if change == "goal":
            latest._d["goal"] = "已改目标"
        ts.save_snapshot(tmp_path, latest)
    before = ts._log_path(tmp_path, stale.task_id).read_bytes()
    other_before = ts.load_snapshot(tmp_path, other_id).to_dict()
    original = ts.load_or_rebuild
    reads = 0

    def stale_first(base, tid):
        nonlocal reads
        reads += 1
        return stale if reads == 1 else original(base, tid)

    monkeypatch.setattr(ts, "load_or_rebuild", stale_first)
    prepared = tp.prepare_turn(base_dir=tmp_path, domain="chat", role="设定师",
                               task_id=stale.task_id, goal="核查世界")
    assert ts._log_path(tmp_path, stale.task_id).read_bytes() == before
    assert ts.load_snapshot(tmp_path, other_id).to_dict() == other_before
    if change == "ready":
        assert prepared["task"] == latest.to_dict()
    else:
        assert prepared["task"] is None
        assert prepared["task_binding"] == "refused"
        assert prepared["events"] == []


def test_terminal_ignored_status_repeated_revision_preserves_replay_semantics(tmp_path):
    state = _paused(tmp_path)
    ts.commit(tmp_path, state, {"kind": "status", "status": "completed"}, expected_revision=state.revision)
    ts.commit(tmp_path, state, {"kind": "status", "status": "ready"}, expected_revision=state.revision)
    assert ts.load_snapshot(tmp_path, state.task_id).to_dict() == ts.rebuild_from_log(tmp_path, state.task_id).to_dict()
    assert ts.load_snapshot(tmp_path, state.task_id).status == "completed"
