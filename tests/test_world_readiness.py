"""世界信息充足度软门（2026-11-11）：world_readiness / world_block / 三入口 / 确认记录。

验收判据对应（规格第六节）：1 硬门先于软门；2 A 级缺→409 world_incomplete；3 确认→放行+落盘；
4 只有 B→ok=True 但 draft_only 非空；5 只有 C/D→未达标；6 账本冲突→conflicts；7 异常→ok=True+error
照常放行；8 章2 不介入；9 不调模型；10 四键恒在。
"""
from __future__ import annotations

import dataclasses
import json
import types

import pytest

from conftest import FakeBackend, require_http_transport

from loom import config as C
from loom import facts_ledger, navigator, paths

KEYS = ("applies", "ok", "missing", "draft_only", "conflicts", "error")
DOCS = tuple({rel for d in navigator.WORLD_A_DIMENSIONS for rel in d["docs"]})
LONG = "这是一段成篇的设定正文，写清了规则、代价与边界，也写清了谁能用、用多了会怎样。" * 3


def _fill(root, *, draft=(), thin=(), skip=()):
    for rel in DOCS:
        if rel in skip:
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        body = "一句话。" if rel in thin else (("（草案）" if rel in draft else "") + LONG)
        p.write_text(f"# 标题\n\n{body}\n", encoding="utf-8")


def _base_ready(root):
    """地基齐全（硬门放行），世界信息保持模板占位。"""
    cfg = C.load_config(root)
    C.save_config(root, dataclasses.replace(cfg, idea="一句话设定", chapter_chars=800))


# ── 判据 5 / 2：模板占位 → D → missing ────────────────────────────────

def test_template_placeholders_are_missing(project):
    _base_ready(project)
    r = navigator.world_readiness(project, 1)
    assert for_keys(r)
    assert r["applies"] is True and r["ok"] is False
    assert r["missing"], "模板占位应判 D 并进 missing"
    assert all(m["level"] == "A" and m["role"] and m["field"] for m in r["missing"])
    # 模板明确带占位示例的那份必须判 D
    assert "核心约束与代价" in {m["field"] for m in r["missing"]}
    # 情绪节奏设计师的两项 A 级维度都要登记（需求方 2026-11-11 补「爽点/痛点位置」）
    all_fields = {g["field"] for g in navigator._world_dimension_grades(project)[0]}
    assert "全书情绪基调" in all_fields and "爽点/痛点的大致位置" in all_fields


def test_single_step_is_not_gated(project):
    """软门只对生成类生效：单棒（含审稿）跳过，不问「确定开始织第一章吗」。"""
    _base_ready(project)
    from loom.api._common import _world_gate
    body = types.SimpleNamespace(chapter=1, step="责任编辑", confirm_world=False)
    assert _world_gate(project, body) is None
    body2 = types.SimpleNamespace(chapter=1, step="", confirm_world=False)
    assert _world_gate(project, body2) is not None, "空 step=整章生成，必须评估软门"


def for_keys(r):
    return all(k in r for k in KEYS)


# ── 判据 4 / 5：B 只进 draft_only，C/D 进 missing ────────────────────

def test_filled_world_is_ok(project):
    _base_ready(project)
    _fill(project)
    r = navigator.world_readiness(project, 1)
    assert r["ok"] is True and r["missing"] == [] and r["draft_only"] == []
    assert navigator.world_block(project, 1) is None


def test_draft_doc_is_b_grade_not_blocking(project):
    _base_ready(project)
    _fill(project, draft=("外置大脑/卷纲.md",))
    r = navigator.world_readiness(project, 1)
    assert r["ok"] is True, "只有 B 级草案不该判未达标"
    assert any(d["grade"] == "B" for d in r["draft_only"])
    assert "分卷走向" in {d["field"] for d in r["draft_only"]}


def test_thin_doc_is_c_grade_missing(project):
    _base_ready(project)
    _fill(project, thin=("外置大脑/设定集/10-世界观/力量体系.md",))
    r = navigator.world_readiness(project, 1)
    assert r["ok"] is False
    assert "核心约束与代价" in {m["field"] for m in r["missing"]}


# ── 判据 6：账本冲突 → conflicts, ok=False ───────────────────────────

def test_ledger_conflict_marks_not_ok(project):
    _base_ready(project)
    _fill(project)
    facts_ledger.record(project, [
        {"实体": "矿脉", "属性": "品级", "取值": "甲", "状态": "确认"},
        {"实体": "矿脉", "属性": "品级", "取值": "乙", "状态": "确认"},
    ])
    r = navigator.world_readiness(project, 1)
    assert r["conflicts"], "同实体同属性不同取值应报冲突"
    assert r["ok"] is False
    assert {"entity", "attr", "old", "new", "status_pair"} <= set(r["conflicts"][0])


# ── 判据 7 / 10：异常不阻塞、四键恒在 ────────────────────────────────

def test_exception_does_not_block(project, monkeypatch):
    _base_ready(project)
    def boom(root):
        raise RuntimeError("账本坏了")
    monkeypatch.setattr(navigator, "_world_dimension_grades", boom)
    r = navigator.world_readiness(project, 1)
    assert for_keys(r), "异常路径也不许缺键"
    assert r["ok"] is True, "判定自身出问题不得演化成写不了第一章"
    assert "RuntimeError" in r["error"]


# ── 判据 8：章 2 不介入 ──────────────────────────────────────────────

def test_chapter_two_not_applicable(project):
    r = navigator.world_readiness(project, 2)
    assert r["applies"] is False and r["ok"] is True
    assert navigator.world_block(project, 2) is None


# ── 判据 9：不调模型、不写盘 ─────────────────────────────────────────

def test_readiness_has_no_side_effects(project):
    _base_ready(project)
    before = {p: p.stat().st_mtime_ns for p in (project / "外置大脑").rglob("*") if p.is_file()}
    navigator.world_readiness(project, 1)
    after = {p: p.stat().st_mtime_ns for p in (project / "外置大脑").rglob("*") if p.is_file()}
    assert before == after, "判定必须只读：不写盘、不落确认记录"


# ── 判据 3：确认放行 + 落盘记录 ──────────────────────────────────────

def test_confirm_releases_and_records(project):
    _base_ready(project)
    from loom.api._common import _world_gate
    body = types.SimpleNamespace(chapter=1, confirm_world=False)
    assert _world_gate(project, body) is not None, "未确认应返回 409 提示体"

    body2 = types.SimpleNamespace(chapter=1, confirm_world=True)
    assert _world_gate(project, body2) is None, "确认后必须放行"
    rec = project / paths.SET_DIR / "开织确认记录.jsonl"
    assert rec.is_file()
    row = json.loads(rec.read_text(encoding="utf-8").splitlines()[-1])
    assert row["author_choice"] == "continue" and row["chapter"] == 1
    assert row["world_ok"] is False and row["foundation"] == "ok"


# ── 判据 1：硬门先于软门 ─────────────────────────────────────────────

def _client():
    require_http_transport()
    from starlette.testclient import TestClient
    import loom.server as server
    return TestClient(server.app, base_url="http://127.0.0.1")


def test_hard_gate_wins_over_world_gate(project):
    cfg = C.load_config(project)
    C.save_config(project, dataclasses.replace(cfg, idea=""))   # 地基不全 + 世界也缺
    r = _client().post("/api/chapter/step",
                       json={"root": str(project), "chapter": 1, "step": "责任编辑"})
    assert r.status_code == 409
    assert r.json()["code"] == "foundation_incomplete"
    assert "world_incomplete" not in r.text


def test_http_world_incomplete_when_hard_gate_passes(project):
    _base_ready(project)
    r = _client().post("/api/chapter/step", json={"root": str(project), "chapter": 1, "step": ""})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "world_incomplete"
    assert body["missing"] and body["问"]
    assert not r.headers.get("content-type", "").startswith("application/x-ndjson"), \
        "软门必须在开流之前"
