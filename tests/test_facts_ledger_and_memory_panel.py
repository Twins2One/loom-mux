"""T3 结构化事实账本 + T7 记忆库面板的离线证据。

全部本地：账本用 tmp 书目录，记忆面板用隔离 sqlite + 假语义引擎。
不联网、不加载真实 ONNX。
"""
from __future__ import annotations

import json

import pytest

from loom import facts_ledger as fl


@pytest.fixture
def book(tmp_path):
    (tmp_path / "loom.toml").write_text('"书名" = "测试书"\n', encoding="utf-8")
    return tmp_path


def _entry(ent, attr, val, status="草案"):
    return {"实体": ent, "属性": attr, "取值": val, "状态": status}


# ── T3 ────────────────────────────────────────────────────────────────
def test_ledger_is_separate_file_and_append_only(book):
    n = fl.record(book, [fl.normalize_entry(_entry("顾明", "怕水", "是"))])
    assert n == 1
    p = book / "外置大脑/设定集/结构化事实.jsonl"
    assert p.is_file()
    assert not (book / "外置大脑/设定集/00-立项.md").exists()   # 没塞进设定集 md
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["实体"] == "顾明" and rows[0]["状态"] == "草案"
    assert set(rows[0]) == {"实体", "属性", "取值", "状态", "来源", "更新时间"}
    # 完全重复不重复写
    assert fl.record(book, [fl.normalize_entry(_entry("顾明", "怕水", "是"))]) == 0


def test_conflict_detected_and_status_pairs_are_distinct(book):
    fl.record(book, [fl.normalize_entry(_entry("顾明", "怕水", "是", "确认"))])
    got = fl.check(book, entries=[_entry(" 顾明 ", "怕水", "否", "草案")])
    assert got["冲突"], "同实体同属性不同取值必须报冲突（归一化后仍是同一键）"
    assert got["冲突"][0]["类型"] == "确认vs草案"
    assert got["冲突"][0]["既有"]["取值"] == "是"


def test_same_value_is_not_a_conflict(book):
    fl.record(book, [fl.normalize_entry(_entry("顾明", "怕水", "是", "确认"))])
    assert fl.check(book, entries=[_entry("顾明", "怕水", "是", "确认")])["冲突"] == []
    # 同值的草案登记同样不算冲突（只是重复）
    assert fl.check(book, entries=[_entry("顾明", "怕水", "是", "草案")])["冲突"] == []


def test_no_reasoning_based_contradiction(book):
    """只报可保证的：同键不同值。不同属性/不同实体一律不报（需要推理的一概不承诺）。"""
    fl.record(book, [fl.normalize_entry(_entry("顾明", "怕水", "是"))])
    assert fl.check(book, entries=[_entry("顾明", "喜欢雨", "否")])["冲突"] == []
    assert fl.check(book, entries=[_entry("小明", "怕水", "否")])["冲突"] == []


def test_extraction_failure_never_raises_nor_writes(book):
    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("模型挂了")
    got = fl.check(book, text="顾明怕水。", backend=Broken())
    assert got == {"冲突": [], "登记": 0, "抽取": 0}
    assert not (book / "外置大脑/设定集/结构化事实.jsonl").exists()


def test_parse_tolerates_fence_and_validates_fields():
    raw = '好的：\n```json\n[{"实体":"顾明","属性":"怕水","取值":"是"},{"实体":"","属性":"x","取值":"y"}]\n```'
    got = fl.parse_entries(raw)
    assert len(got) == 1 and got[0]["实体"] == "顾明"
    assert fl.parse_entries("不是 JSON") == []


def test_books_are_isolated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
    fl.record(a, [fl.normalize_entry(_entry("顾明", "怕水", "是"))])
    assert fl.load(b) == []


def test_corrupt_line_does_not_kill_ledger(book):
    p = book / "外置大脑/设定集/结构化事实.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"实体":"顾明","属性":"怕水","取值":"是","状态":"草案"}\n坏行{{{}\n',
                 encoding="utf-8")
    assert len(fl.load(book)) == 1


# ── T7 ────────────────────────────────────────────────────────────────
def test_memory_health_has_no_fake_zero(tmp_path, monkeypatch):
    from loom import memory as m

    monkeypatch.setattr(m, "_db_path", lambda: tmp_path / "memory.sqlite")
    conn = m._connect()
    conn.execute(
        "INSERT INTO memory(role,book,anchor,digest,vector,created) VALUES(?,?,?,?,?,?)",
        ("角色", "/book", "a", "顾明怕水", b"\x00" * 16, 1))
    conn.commit()
    conn.close()

    got = m.health("/book")
    assert got["总条数"] == 1 and got["已向量化"] == 1
    assert got["维度"] == 4                      # float32 × 4 = 16 字节
    assert got["置顶"] is None and got["软删除"] is None
    assert "置顶" in got["unsupported"] and "软删除" in got["unsupported"]
    assert isinstance(got["引擎"], dict) and got["引擎"]["形态"].startswith("进程内 ONNX")


def test_memory_health_db_error_is_visible_not_crash(monkeypatch):
    from loom import memory as m

    def broken():
        raise OSError("磁盘没了")
    monkeypatch.setattr(m, "_connect", broken)
    got = m.health("/book")
    assert got["总条数"] is None and "磁盘没了" in got["错误"]
    assert got["置顶"] is None


def test_memory_recover_reloads_model(monkeypatch):
    from loom import memory as m
    from loom.study import semantic as sem

    calls = []

    def fake_reload():
        calls.append(1)
        return {"可语义检索": True, "缺": [], "last_err": "", "attempted": True, "loaded": True}

    monkeypatch.setattr(sem, "reload", fake_reload)
    got = m.recover()
    assert calls == [1] and got["ok"] is True and got["loaded"] is True
    assert got["缺"] == []
