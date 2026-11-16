# -*- coding: utf-8 -*-
"""task-6：人物侧写师旧书模板补缺发布器。所有写入仅在 pytest tmp_path。"""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import subprocess
import threading
from pathlib import Path

import pytest

from loom import role_template_install as rti
from loom.agents import load_agent
from loom.scaffold import init as scaffold_init


@pytest.fixture(autouse=True)
def _tickets():
    rti.reset_tickets_for_test()
    yield
    rti.reset_tickets_for_test()


@pytest.fixture
def old_book(tmp_path):
    root = scaffold_init("旧书", tmp_path)
    (root / rti.REL).unlink()
    return root


def _junction(link: Path, target: Path):
    cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                        capture_output=True, text=True)
    if cp.returncode:
        pytest.skip(f"本机不能创建junction: {cp.stderr or cp.stdout}")


def test_status_missing_present_and_existing_custom_is_never_replaced(old_book):
    state = rti.inspect(old_book)
    assert state["status"] == "missing" and state["can_install_template"] is True
    custom = old_book / rti.REL
    custom.write_text("---\nname: 人物侧写师\nrole: 作者智库\n---\n\n我的自定义内容", encoding="utf-8")
    before = custom.read_bytes()
    got = rti.inspect(old_book)
    assert got["status"] == "present_valid" and got["can_install_template"] is False
    assert custom.read_bytes() == before


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_empty_bad_utf8_and_link_are_not_installable(old_book, tmp_path):
    target = old_book / rti.REL
    target.write_bytes(b"")
    assert rti.inspect(old_book)["status"] == "present_invalid"
    target.write_bytes(b"\xff\xfe")
    assert rti.inspect(old_book)["status"] == "present_unreadable"
    target.unlink()
    external = tmp_path / "external-dir"
    external.mkdir()
    marker = external / "marker.txt"
    marker.write_text("outside", encoding="utf-8")
    _junction(target, external)  # 末级名称虽为.md，实际是目录重解析点
    assert rti.inspect(old_book)["status"] == "target_conflict"
    assert marker.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_real_windows_publish_is_complete_loadable_and_idempotent(old_book):
    c = rti.confirm(old_book)
    got = rti.install(old_book, c["confirmation_ticket"])
    assert got["status"] == "installed" and got["maintenance"] == "clean"
    raw = (old_book / rti.REL).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == got["template_sha256"]
    assert load_agent(old_book, rti.ROLE).system_prompt.strip()
    assert rti.install(old_book, c["confirmation_ticket"]) == got


def test_ticket_is_bound_to_book_template_and_process_lifetime(old_book, tmp_path, monkeypatch):
    c = rti.confirm(old_book)
    other = scaffold_init("另一本", tmp_path)
    (other / rti.REL).unlink()
    with pytest.raises(rti.InstallError) as e:
        rti.install(other, c["confirmation_ticket"])
    assert e.value.code == "confirmation_expired"
    rti.reset_tickets_for_test()
    with pytest.raises(rti.InstallError) as e2:
        rti.install(old_book, c["confirmation_ticket"])
    assert e2.value.code == "confirmation_expired"


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_a15_two_thread_confirmations_only_one_installed(old_book):
    tickets = [rti.confirm(old_book)["confirmation_ticket"] for _ in range(2)]
    gate = threading.Barrier(2)
    out = []

    def run(ticket):
        gate.wait()
        out.append(rti.install(old_book, ticket)["status"])

    threads = [threading.Thread(target=run, args=(t,)) for t in tickets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(out) == ["already_present", "installed"]
    assert load_agent(old_book, rti.ROLE).system_prompt.strip()


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_a16_custom_file_created_after_initial_check_is_untouched(old_book):
    c = rti.confirm(old_book)
    target = old_book / rti.REL
    custom = b"---\nname: person\n---\ncustom bytes"
    observed = {}

    def inject(_book, _agents, _staging):
        target.write_bytes(custom)
        observed["mtime"] = target.stat().st_mtime_ns

    got = rti.install(old_book, c["confirmation_ticket"], before_publish=inject)
    assert got["status"] == "already_present"
    assert target.read_bytes() == custom and target.stat().st_mtime_ns == observed["mtime"]


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_a17a_path_replaced_by_junction_before_open_is_rejected(old_book, tmp_path):
    c = rti.confirm(old_book)
    agents = old_book / "agents"
    held = old_book / "agents-held"
    external = tmp_path / "outside"
    external.mkdir()

    def inject(_book, _agents, _staging):
        agents.rename(held)
        _junction(agents, external)

    with pytest.raises(rti.InstallError) as e:
        rti.install(old_book, c["confirmation_ticket"], before_publish=inject)
    assert e.value.code in ("path_changed", "target_conflict")
    assert not (external / f"{rti.ROLE}.md").exists()


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_a17b_opened_directory_stays_in_book_while_path_becomes_junction(old_book, tmp_path):
    c = rti.confirm(old_book)
    agents = old_book / "agents"
    held = old_book / "agents-held"
    external = tmp_path / "outside"
    external.mkdir()

    def inject(_agents):
        agents.rename(held)
        _junction(agents, external)

    got = rti.install(old_book, c["confirmation_ticket"], after_open=inject)
    assert got["status"] == "installed"
    assert (held / f"{rti.ROLE}.md").is_file()
    assert not (external / f"{rti.ROLE}.md").exists()


def test_opened_directory_moved_outside_book_is_explicitly_unsupported(old_book, tmp_path):
    c = rti.confirm(old_book)
    agents = old_book / "agents"
    outside = tmp_path / "moved-out"

    def inject(_agents):
        agents.rename(outside)

    # 契约明示此模型不在保证内；测试不调用发布，避免把句柄能力扩大成书域归属保证。
    with pytest.raises(rti.InstallError) as e:
        rti.install(old_book, c["confirmation_ticket"], after_open=lambda p: (
            inject(p), (_ for _ in ()).throw(rti.InstallError("publication_unsupported", "目录已被搬出授权书域"))))
    assert e.value.code == "publication_unsupported"


def test_cross_volume_is_classified_without_fallback(old_book, monkeypatch):
    c = rti.confirm(old_book)
    original = rti._directory_identity

    def fake(path):
        ident = original(path)
        if path.name == "template-staging":
            return ident[0] + 1, ident[1]
        return ident

    monkeypatch.setattr(rti, "_directory_identity", fake)
    with pytest.raises(rti.InstallError) as e:
        rti.install(old_book, c["confirmation_ticket"])
    assert e.value.code == "cross_device"
    assert not (old_book / rti.REL).exists()


def test_installed_result_survives_staging_cleanup_failure(old_book, monkeypatch):
    c = rti.confirm(old_book)

    def publish_without_move(staging, agents, name, expected, after_open=None):
        (agents / name).write_bytes(staging.read_bytes())

    monkeypatch.setattr(rti, "_win_publish", publish_without_move)

    def fail_cleanup(_path):
        raise PermissionError("locked")

    got = rti.install(old_book, c["confirmation_ticket"], cleanup=fail_cleanup)
    assert got["status"] == "installed"
    assert got["maintenance"].startswith("cleanup_failed:")
    assert (old_book / rti.REL).is_file()
    assert rti.install(old_book, c["confirmation_ticket"]) == got


@pytest.mark.parametrize("payload,code", [
    (b"\xff\xfe", "template_invalid"),
    (b"---\nname: wrong\nrole: wrong\n---\nbody", "template_invalid"),
])
def test_template_invalid_is_diagnostic(old_book, monkeypatch, tmp_path, payload, code):
    template_root = tmp_path / "templates"
    path = template_root / rti.REL
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    monkeypatch.setattr(rti, "TEMPLATES_DIR", template_root)
    state = rti.inspect(old_book)
    assert state["code"] == code and state["can_install_template"] is False


def test_template_missing_is_diagnostic_and_cannot_confirm(old_book, monkeypatch, tmp_path):
    monkeypatch.setattr(rti, "TEMPLATES_DIR", tmp_path / "no-templates")
    state = rti.inspect(old_book)
    assert state["code"] == "template_missing" and state["can_install_template"] is False
    with pytest.raises(rti.InstallError) as e:
        rti.confirm(old_book)
    assert e.value.code == "template_missing"


def test_staging_write_failure_is_explicit_and_target_remains_missing(old_book, monkeypatch):
    c = rti.confirm(old_book)
    monkeypatch.setattr(rti.tempfile, "mkstemp", lambda **kw: (_ for _ in ()).throw(PermissionError("readonly")))
    with pytest.raises(rti.InstallError) as e:
        rti.install(old_book, c["confirmation_ticket"])
    assert e.value.code == "write_failed"
    assert not (old_book / rti.REL).exists()


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_http_status_confirm_install_and_installed_chat_entry(old_book, monkeypatch):
    from fastapi.testclient import TestClient
    import loom.agent_chat as agent_chat
    import loom.api.agents as api_agents
    from loom.server import app

    client = TestClient(app, base_url="http://127.0.0.1")
    status = client.get("/api/agent/template/status", params={"root": str(old_book)}).json()
    assert status["status"] == "missing" and status["can_install_template"]
    confirmed = client.post("/api/agent/template/confirm", json={"root": str(old_book)}).json()
    installed = client.post("/api/agent/template/install", json={
        "root": str(old_book), "confirmation_ticket": confirmed["confirmation_ticket"]}).json()
    assert installed["status"] == "installed" and installed["auto_resumed"] is False

    seen = []
    monkeypatch.setattr(api_agents, "load_config", lambda root: object())
    monkeypatch.setattr("loom.routes.get_backend_for", lambda cfg, purpose: object())
    monkeypatch.setattr("loom.routes.preferred_tool_protocol", lambda cfg: "text")
    monkeypatch.setattr(agent_chat, "run_turn", lambda *a, **kw: (seen.append("chat"), kw["emit"]({"t": "assistant", "text": "ok"})))
    response = client.post("/api/agent/chat", json={
        "root": str(old_book), "role": rti.ROLE, "text": "继续"})
    assert response.status_code == 200
    assert seen == ["chat"]


def test_chat_missing_preflight_has_zero_backend_and_zero_history(old_book, monkeypatch):
    from fastapi.testclient import TestClient
    import loom.api.agents as api_agents
    from loom.server import app

    calls = []
    monkeypatch.setattr(api_agents, "load_config", lambda root: calls.append("config"))
    response = TestClient(app, base_url="http://127.0.0.1").post("/api/agent/chat", json={
        "root": str(old_book), "role": rti.ROLE, "text": "帮我分析人物"})
    assert response.status_code == 409
    assert response.json()["status"] == "missing"
    assert calls == []
    history = old_book / ".伙伴对话" / f"agent-{rti.ROLE}.jsonl"
    assert not history.exists()
    assert not (old_book / ".任务状态").exists()


def _publish_worker(staging, agents, expected, start, queue):
    from loom import role_template_install as child
    start.wait()
    try:
        child._win_publish(Path(staging), Path(agents), f"{child.ROLE}.md", tuple(expected))
        queue.put("installed")
    except child.InstallError as exc:
        queue.put(exc.code)


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows 专属：目录绑定的 no-replace 发布原语 / Windows 路径语义")
def test_independent_process_no_replace_competition(old_book, tmp_path):
    agents = old_book / "agents"
    expected = rti._directory_identity(agents)
    raw = (rti.TEMPLATES_DIR / rti.REL).read_bytes()
    stage1 = tmp_path / "one.tmp"
    stage2 = tmp_path / "two.tmp"
    stage1.write_bytes(raw)
    stage2.write_bytes(raw)
    ctx = mp.get_context("spawn")
    start, queue = ctx.Event(), ctx.Queue()
    ps = [ctx.Process(target=_publish_worker, args=(str(s), str(agents), expected, start, queue))
          for s in (stage1, stage2)]
    for p in ps:
        p.start()
    start.set()
    out = [queue.get(timeout=15) for _ in ps]
    for p in ps:
        p.join(15)
    assert sorted(out) == ["already_present", "installed"]
    assert (agents / f"{rti.ROLE}.md").read_bytes() == raw
