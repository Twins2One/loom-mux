"""T1（2026-11-11）：编辑类单棒只在「正文已落盘且非空」时算审稿，且真正从该棒起跑。

用户裁定：放行的话，作者还没落盘审稿信息，编辑拿什么审？⇒ 无现稿仍拦。
判据：点责任编辑只跑该棒（零生成调用）；无现稿仍 409；空 step / /api/write 不绕过。
"""
from __future__ import annotations

import dataclasses

import pytest

from conftest import FakeBackend, require_http_transport

from loom import agents, config as C, paths
from loom.backends import LoomBackendError

EDIT_OUT = ("第1章改好的正文。" + "这是一段用于满足篇幅校验的正文内容，" * 10
            + "\n<LOOM:EDIT-NOTE>\n- 修了一处口误\n</LOOM:EDIT-NOTE>")


def _prep(root, *, body: str = "第1章现稿正文，写得足够长可以审。"):
    cfg = C.load_config(root)
    C.save_config(root, dataclasses.replace(cfg, gate_rounds=0, continuity_scan=False, idea=""))
    if body is not None:
        p = paths.chapter_path(root, 1)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def test_review_from_step_runs_only_that_step(project):
    """本体：从责任编辑起跑，以现稿为上游 —— 零生成调用。"""
    _prep(project)
    be = FakeBackend(lambda s, u: EDIT_OUT)
    agents.run_pipeline(project, 1, be, C.load_config(project), progress=lambda ev: None,
                        from_step="责任编辑", stop_after="责任编辑")
    # 允许「责任编辑 + 起标题」最多两次；关键是没有任何生成棒被调用。
    assert len(be.calls) <= 2, f"调用数过多（可能重跑了生成棒）：{len(be.calls)}"
    assert all(("你是「写手」" not in s and "你是「章节细纲师」" not in s) for s, _ in be.calls), \
        "生成棒（写手/细纲师）不得被调用（零生成）"
    assert "责任编辑" in be.calls[0][0], "第一调用应是责任编辑本棒"
    assert "现稿正文" in be.calls[0][1], "编辑应拿盘上现稿当上游工作区"


def test_review_without_body_still_blocked(project):
    _prep(project, body=None)
    be = FakeBackend(lambda s, u: EDIT_OUT)
    with pytest.raises(LoomBackendError) as exc:
        agents.run_pipeline(project, 1, be, C.load_config(project), progress=lambda ev: None,
                            from_step="责任编辑")
    assert getattr(exc.value, "code", "") == "foundation_incomplete"
    assert be.calls == [], "无现稿时不得有任何模型调用"


def test_agentic_config_does_not_hijack_review_step(project):
    """P1（审查 审查记录）：agentic=True 时编辑单棒曾被分流进整章写作 agent。"""
    _prep(project)
    be = FakeBackend(lambda s, u: EDIT_OUT)
    cfg = dataclasses.replace(C.load_config(project), agentic=True)
    agents.run_pipeline(project, 1, be, cfg, progress=lambda ev: None,
                        from_step="责任编辑", stop_after="责任编辑")
    assert all("你在为一本网文写一章" not in s for s, _ in be.calls), \
        "agentic 不得把编辑单棒当整章写作"
    assert "责任编辑" in be.calls[0][0], "第一调用应是责任编辑本棒"
    assert "现稿正文" in be.calls[0][1]


def test_agentic_chapter_two_review_not_hijacked(project):
    """起跑谓词不限章号：章 2 现稿 + 责任编辑 也不得进整章 agent（需求方/审查）。"""
    _prep(project)
    p2 = paths.chapter_path(project, 2)
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_text("第2章现稿正文，写得足够长可以审。", encoding="utf-8")
    be = FakeBackend(lambda s, u: EDIT_OUT)
    cfg = dataclasses.replace(C.load_config(project), agentic=True)
    agents.run_pipeline(project, 2, be, cfg, progress=lambda ev: None,
                        from_step="责任编辑", stop_after="责任编辑")
    assert all("你在为一本网文写一章" not in s for s, _ in be.calls), \
        "章 2 编辑单棒不得被 agentic 当整章写作"
    assert "责任编辑" in be.calls[0][0]
    assert "第2章现稿正文" in be.calls[0][1]


def _client():
    require_http_transport()
    from starlette.testclient import TestClient
    import loom.server as server
    from loom import routes
    fake = lambda *a, **k: FakeBackend(lambda s, u: EDIT_OUT)  # noqa: E731
    routes.get_backend_for = fake
    server.get_backend = lambda cfg: FakeBackend(lambda s, u: EDIT_OUT)
    return TestClient(server.app, base_url="http://127.0.0.1")


def test_chapter_step_endpoint_allows_review_when_body_exists(project):
    _prep(project)
    r = _client().post("/api/chapter/step",
                       json={"root": str(project), "chapter": 1, "step": "责任编辑"})
    assert r.status_code == 200, r.text[:300]
    assert "foundation_incomplete" not in r.text


def test_empty_step_and_full_write_still_blocked(project):
    _prep(project)
    client = _client()
    r1 = client.post("/api/chapter/step", json={"root": str(project), "chapter": 1, "step": ""})
    assert r1.status_code == 409 and r1.json()["code"] == "foundation_incomplete"
    r2 = client.post("/api/write", json={"root": str(project), "chapter": 1})
    assert r2.status_code == 409 and r2.json()["code"] == "foundation_incomplete"
