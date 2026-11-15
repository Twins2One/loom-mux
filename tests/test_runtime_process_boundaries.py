"""R-02 support characterization. All roots and lock files are isolated.

These tests describe current limits, not a promise of multi-process task support.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from loom import request_turn as rt, task_state as ts
from loom.writelock import FileLock


def _child(code, *args):
    env = dict(os.environ, PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8", env=env,
                            timeout=15, check=True)
    return json.loads(result.stdout)


_LOCK_CHILD = '''
import json, os, sys
os.environ["LOOM_HOME"] = sys.argv[1]
from loom.usecases import try_lock
from loom.writelock import lock_path
handle = try_lock(sys.argv[2])
print(json.dumps({"acquired": handle is not None, "path": str(lock_path(sys.argv[2]))}))
if handle is not None:
    handle.release()
'''


def test_os_write_lock_blocks_same_home_but_not_different_home(tmp_path, monkeypatch):
    from loom import writelock
    root = tmp_path / "book"
    home = tmp_path / "home-a"
    monkeypatch.setenv("LOOM_HOME", str(home))
    monkeypatch.setattr(writelock, "_BASE_CACHE", [])
    path = writelock.lock_path(root)
    holder = FileLock(path)
    assert holder.acquire() and holder.held
    try:
        same = _child(_LOCK_CHILD, home, root)
        other = _child(_LOCK_CHILD, tmp_path / "home-b", root)
        assert same["acquired"] is False
        assert Path(same["path"]) == path
        assert other["acquired"] is True
        assert Path(other["path"]) != path
    finally:
        holder.release()
    assert _child(_LOCK_CHILD, home, root)["acquired"] is True


def test_unavailable_lock_file_does_not_claim_cross_process_protection(tmp_path):
    obstacle = tmp_path / "not-a-directory"
    obstacle.write_text("isolated fixture", encoding="utf-8")
    lock = FileLock(obstacle / "scope.lock")
    assert lock.acquire() is False
    assert lock.held is False
    lock.release()


def test_failed_file_lock_releases_usecase_thread_lock(tmp_path, monkeypatch):
    from loom import usecases
    monkeypatch.setattr(usecases, "lock_path_for", lambda root, kind: tmp_path / "blocked" / "scope.lock")
    (tmp_path / "blocked").write_text("fixture", encoding="utf-8")
    assert usecases.try_lock(tmp_path / "book") is None
    assert usecases.try_lock(tmp_path / "book") is None
    monkeypatch.setattr(usecases, "lock_path_for", lambda root, kind: tmp_path / "valid" / "scope.lock")
    held = usecases.try_lock(tmp_path / "book")
    assert held is not None
    held.release()


def test_task_directory_thread_lock_does_not_block_other_process(tmp_path):
    prepared = __import__("loom.turn_prepare", fromlist=["prepare_turn"]).prepare_turn(
        base_dir=tmp_path, domain="chat", role="设定师", goal="边界核查")
    tid = prepared["task"]["task_id"]
    code = '''
import json, sys
from pathlib import Path
from loom import task_state as ts
base = Path(sys.argv[1])
state = ts.load_snapshot(base, sys.argv[2])
ts.commit(base, state, {"kind": "constraint", "content": "child process"}, expected_revision=state.revision)
print(json.dumps({"revision": state.revision}))
'''
    with ts._scope_lock(tmp_path):
        result = _child(code, tmp_path, tid)
    assert result["revision"] == 2
    assert ts.load_snapshot(tmp_path, tid).active_constraints() == ["child process"]


def test_request_generation_in_other_process_does_not_retire_parent(tmp_path):
    ticket = rt.begin(tmp_path, "设定师", "默认")
    code = '''
import json, sys
from loom import request_turn as rt
key, token = rt.begin(sys.argv[1], "设定师", "默认")
print(json.dumps({"token": token, "current": rt.current((key, token))}))
'''
    child = _child(code, tmp_path)
    assert child["current"] is True
    assert child["token"] != ticket[1]
    assert rt.current(ticket) is True
