"""R-05：`load_or_rebuild` 的兜底重放必须与 append/save 在同一临界区内。

背景：`load_snapshot` 自身已在 `_scope_lock` 内重放（水位领先/重复水位/无有效缓存），
但 `load_or_rebuild` 的 `or rebuild_from_log(...)` 兜底分支在锁外。该分支一旦执行，
读路径就脱离了「同一临界区里的读-改-写」语义：与并发 `commit` 交错时可能返回
「日志已有事件、快照尚未落盘」的前缀状态。
"""
from __future__ import annotations

from pathlib import Path

from loom import task_state as ts


def _make_task(root: Path, tid: str = "t-r05") -> ts.TaskState:
    st = ts.TaskState.create({"role": "设定师", "conversation_id": "默认"}, "R05 探针", task_id=tid)
    ts.append_task_event(root, tid, {"kind": "start", "ts": ts.now_ms(),
                                     "scope": st._d.get("scope"), "goal": "R05 探针",
                                     "applied_revision": st.revision})
    ts.save_snapshot(root, st)
    return st


def test_fallback_rebuild_runs_inside_scope_lock(tmp_path, monkeypatch):
    """兜底分支必须持作用域锁；否则它与并发 commit 之间没有互斥。"""
    _make_task(tmp_path)
    # 强制走兜底分支：让 load_snapshot 报「读不出状态」（等价于无有效缓存）。
    monkeypatch.setattr(ts, "load_snapshot", lambda base, tid: None)

    observed: dict = {}
    real = ts.rebuild_from_log

    def spy(base, tid):
        observed["locked"] = ts._scope_lock(base)._is_owned()
        return real(base, tid)

    monkeypatch.setattr(ts, "rebuild_from_log", spy)
    result = ts.load_or_rebuild(tmp_path, "t-r05")

    assert observed.get("locked") is True, "兜底重放跑在锁外：与并发 append/save 无互斥"
    assert result is not None and result.task_id == "t-r05"


def test_load_or_rebuild_matches_snapshot_when_cache_is_current(tmp_path):
    """缓存有效时不得触发任何重放（读路径保持只读快照语义）。"""
    st = _make_task(tmp_path)
    assert ts.load_or_rebuild(tmp_path, "t-r05").revision == st.revision


def test_load_or_rebuild_recovers_from_log_without_snapshot(tmp_path):
    """无快照但日志可判定 → 交给锁内的 load_snapshot 重建，结果与日志一致。"""
    st = _make_task(tmp_path)
    ts._snap_path(tmp_path, "t-r05").unlink()
    rebuilt = ts.load_or_rebuild(tmp_path, "t-r05")
    assert rebuilt is not None and rebuilt.revision == st.revision


def test_load_or_rebuild_returns_none_for_empty_log(tmp_path):
    """无快照且日志无可判定事件 → None（不伪造状态）。"""
    assert ts.load_or_rebuild(tmp_path, "t-nothing") is None
