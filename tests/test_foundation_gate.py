"""第一章地基硬门：三 HTTP 入口与流水线在开 worker/模型/任务前统一阻断。

范围（需求方 审查记录 裁定）：
- 章 1 任何当前可能进入生成流水线的调用一律 foundation_incomplete；
  不做“已有正文就豁免”（现有入口仍可能从 0 跑细纲/写手）。
- title/idea 纯空白算缺失；chapter_chars 沿现有有效配置判据。
- 旧 brain 门保留；章 2 不受新增门影响。
- 改变 run_pipeline 起跑以支持真正纯编辑不在本块。
"""
from __future__ import annotations

import dataclasses

import pytest

from conftest import require_http_transport, FakeBackend

from loom import config as C
from loom import navigator, paths
from loom.backends import LoomBackendError


def _blank_title(root):
    cfg = C.load_config(root)
    C.save_config(root, dataclasses.replace(cfg, title="   "))


def _fill_foundation(root):
    navigator.save_foundation(root, {"idea": "一句话设定：地基齐备用例", "chapter_chars": "800"})


def _clear_foundation(root):
    """把夹具书退回「地基未完成」（conftest 的 project 夹具默认已补齐）。"""
    cfg = C.load_config(root)
    C.save_config(root, dataclasses.replace(cfg, idea=""))


# ── 判据本身 ────────────────────────────────────────────────────────────

def test_blank_required_fields_are_missing(project):
    _fill_foundation(project)
    assert navigator.foundation_progress(project)["done"] is True
    _blank_title(project)
    progress = navigator.foundation_progress(project)
    assert progress["done"] is False
    assert "title" in progress["缺失项"]


def test_blank_only_whitespace_required_field_is_missing(project):
    _fill_foundation(project)
    cfg = C.load_config(project)
    C.save_config(project, dataclasses.replace(cfg, idea=" \t "))
    assert "idea" in navigator.foundation_progress(project)["缺失项"]


def test_foundation_block_gates_only_chapter_one(project):
    _clear_foundation(project)
    block = navigator.foundation_block(project, 1)
    assert block is not None and block["code"] == "foundation_incomplete"
    assert "idea" in block["missing"]
    assert block["missing_details"]
    assert navigator.foundation_block(project, 2) is None
    _fill_foundation(project)
    assert navigator.foundation_block(project, 1) is None


# ── 三个 HTTP 入口 ──────────────────────────────────────────────────────

def _client():
    require_http_transport()
    from starlette.testclient import TestClient
    import loom.server as server
    return TestClient(server.app, base_url="http://127.0.0.1")


def test_write_endpoint_blocks_chapter_one_before_worker(project):
    _clear_foundation(project)
    r = _client().post("/api/write", json={"root": str(project), "chapter": 1})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "foundation_incomplete"
    assert "idea" in body["missing"]


def test_run_step_endpoint_blocks_chapter_one(project):
    _clear_foundation(project)
    r = _client().post("/api/chapter/run-step",
                       json={"root": str(project), "chapter": 1, "step": "责任编辑"})
    assert r.status_code == 409
    assert r.json()["code"] == "foundation_incomplete"


def test_chapter_step_endpoint_blocks_chapter_one_even_with_empty_step(project):
    _clear_foundation(project)
    for step in ("责任编辑", ""):
        r = _client().post("/api/chapter/step",
                           json={"root": str(project), "chapter": 1, "step": step})
        assert r.status_code == 409, step
        assert r.json()["code"] == "foundation_incomplete", step


def test_existing_chapter_one_body_does_not_exempt_full_write(project):
    """T1（2026-11-11）覆盖旧口径：有正文的**编辑类单棒**现在算审稿、放行（见
    tests/test_t1_review_single_step.py）；但**整章生成**（/api/write）仍不豁免。"""
    _clear_foundation(project)
    body = paths.chapter_path(project, 1)
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text("# 第1章\n\n已有正文\n", encoding="utf-8")
    r = _client().post("/api/write", json={"root": str(project), "chapter": 1})
    assert r.status_code == 409
    assert r.json()["code"] == "foundation_incomplete"


def test_chapter_two_keeps_original_semantics(project):
    """地基不全也只门章 1；章 2 仍走原有规则（本例缺上章正文 → prev_unfinished）。"""
    _clear_foundation(project)
    r = _client().post("/api/chapter/step",
                       json={"root": str(project), "chapter": 2, "step": "写手"})
    assert r.status_code == 409
    assert r.json()["code"] == "prev_unfinished"


# ── 流水线内部（普通与 agentic 分流前）──────────────────────────────────

def _run(root, agentic=False):
    _clear_foundation(root)
    cfg = C.load_config(root)
    if agentic:
        cfg = dataclasses.replace(cfg, agentic=True)
    from loom import agents
    backend = FakeBackend(lambda system, user: "不应被调用")
    with pytest.raises(LoomBackendError) as exc:
        agents.run_pipeline(root, 1, backend, cfg, progress=lambda ev: None)
    assert getattr(exc.value, "code", "") == "foundation_incomplete"
    assert backend.calls == []


def test_pipeline_blocks_chapter_one_plain(project):
    _run(project, agentic=False)


def test_pipeline_blocks_chapter_one_agentic_before_branch(project):
    """agentic 忽略 from_step/stop_after，不能按“传入审查棒”豁免。"""
    _run(project, agentic=True)


def test_complete_foundation_passes_all_gates(project):
    _fill_foundation(project)
    assert navigator.foundation_block(project, 1) is None
    assert navigator.foundation_progress(project)["done"] is True
