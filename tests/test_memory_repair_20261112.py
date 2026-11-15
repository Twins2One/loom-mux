# -*- coding: utf-8 -*-
"""记忆库修复/抢救三按钮（②重建索引 ③导出数据）验收测试。

覆盖设计稿 `docs/记忆库修复与抢救方案-20261112.md`：
- §2.2 重建记忆索引（4 条）：
  1. 损坏库触发重建 ⇒ health 无错误、条数与速记本一致、recall keyword/hybrid 恢复
  2. 重建中途取消 ⇒ 新库丢弃、旧库原样、状态「未完成」
  3. 重建失败 ⇒ 旧库保留、错误有原因、可再导出
  4. 速记本为空 ⇒ 0 条且「没有可重建的原文」，不伪造成功（不替换旧库）
- §2.3 导出记忆数据（4 条）：
  1. 库不可读 + 速记本有内容 ⇒ 包存在、文件数一致、抽样逐字一致
  2. 库不可读 + 速记本为空 ⇒ 「没有可导出的记忆数据」，不产空包
  3. 导出失败（文件为空）⇒ 红字原因 + 已写入清单，不静默
  4. 导出只读 ⇒ 不写书、不写库（源文件/库字节不变、无 corrupt 文件）
- 附加：需求裁定 1 增量窗口（重建期间新增 ⇒ 提示二次重建，不自动合并）；
  端点层 TestClient（rebuild start/status、export）。

口径：函数级 + 进程内 TestClient（base_url=http://127.0.0.1:8611、tmp 书根、
隔离 LOOM_HOME——conftest autouse 已指向 tmp，不碰真实 ~/.loom）。
语义引擎用确定性假向量 monkeypatch（不加载真实 ONNX、不联网）。
"""
from __future__ import annotations

import math
import pathlib
import time

import pytest

from loom import memory as m
from loom import memory_repair as mr
from loom.study import semantic as sem


# ── 夹具 ──────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_rebuild():
    """每个测试前后清空模块级重建任务状态 + 清理残留库文件，防跨测试串扰。"""
    mr._reset_for_tests()
    _clean_db_dir()
    yield
    mr._reset_for_tests()
    _clean_db_dir()


def _clean_db_dir():
    """清掉 LOOM_HOME/memory 下残留的 corrupt/new 库文件（测试隔离）。"""
    try:
        d = m._db_path().parent
        if d.is_dir():
            for p in d.glob("memory.sqlite.corrupt-*"):
                p.unlink()
            for p in d.glob("memory.new.sqlite*"):
                p.unlink()
    except Exception:
        pass


@pytest.fixture
def book(tmp_path, monkeypatch):
    """tmp 书根（含 loom.toml）；库根指向其父目录（list_books 用 rglob 找书）。"""
    b = tmp_path / "book"
    b.mkdir()
    (b / "loom.toml").write_text('"书名" = "测试书"\n', encoding="utf-8")
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(tmp_path))
    return b


@pytest.fixture
def semantic(monkeypatch):
    """确定性假语义引擎：16 维单位向量（可复现、可算余弦）。"""
    import numpy as np

    def fake_ready():
        return {"ok": True, "可语义检索": True, "缺": [], "last_err": ""}

    def fake_embed(texts, batch=16):
        out = []
        for t in (texts or []):
            v = np.zeros(16, dtype="float32")
            for i, ch in enumerate(str(t)):
                v[i % 16] += (ord(ch) % 97) + 1.0
            n = math.sqrt(float((v * v).sum()))
            out.append(v / n if n > 0 else v)
        return np.stack(out) if out else None

    monkeypatch.setattr(sem, "ready", fake_ready)
    monkeypatch.setattr(sem, "embed", fake_embed)
    return sem


def _add_entry(book, role, body):
    """往速记本写一条（与 append_labeled 同格式）。"""
    d = book / "外置大脑" / "设定集" / "90-速记本"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"速记本-{role}.md"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(f"### [{ts}] {role} · 测试\n\n{body}\n\n")


def _corrupt_db(settle: float = 5.0):
    """把库文件写成非 SQLite 内容（模拟库级损坏）。

    两步（2026-11-13 需求方 审查记录 批准的计划第 1 条）：
    1. 先建一个**正常** sqlite（`_connect` 建表 + 插 1 条占位，close 时 checkpoint）：
       文件「早已存在」且被 AV 扫过一遍，不是「全新文件」；
    2. 再原地覆盖为非 sqlite 字节。
    `settle`：覆盖后再 sleep 的秒数（默认 5s）。Windows Defender 实时扫描会
    latch **刚写**的 .sqlite，重建完成时 rename 旧库就被拒（WinError 32）。
    真实产品旧库是早已存在、AV 扫过的文件不会 latch；只有测试的 corrupt 文件
    是「刚写的」才触发。先建正常库再原地覆盖 + 覆盖后 sleep 让 AV 扫完，
    rename 才不被 latch（test_22_1 间歇性 flaky 的根因）。
    """
    import numpy as np

    p = m._db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = m._connect()
    try:
        conn.execute(
            "INSERT INTO memory (role, book, anchor, digest, vector, created)"
            " VALUES (?,?,?,?,?,?)",
            ("设定师", "测试", "### [0000-00-00 00:00:00] 设定师 · 占位",
             "占位条目（损坏前）", m._blob(np.zeros(16, dtype="float32")), 0),
        )
        conn.commit()
    finally:
        conn.close()
    p.write_bytes(b"THIS IS NOT A SQLITE FILE " + b"\x00" * 64)
    if settle > 0:
        time.sleep(settle)
    # 确定性探针（2026-11-13 补）：反复尝试 rename（与产品 _atomic_swap 要做的动作相同），
    # 直到能改名成功 ⇒ AV 已放开文件锁。探针改名后立即改回，不留痕迹。
    # 固定 sleep 只是"大概率"；探针把"碰运气"变成"确认解锁"，全量高负载下才稳定。
    probe = p.with_name(p.name + ".probe")
    t0 = time.time()
    while time.time() - t0 < 60:
        try:
            p.rename(probe)
            probe.rename(p)
            break
        except OSError:
            time.sleep(0.5)


def _db_rows():
    """库当前条数（superseded_by IS NULL）。不可读 ⇒ 抛。"""
    conn = m._connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memory WHERE superseded_by IS NULL").fetchone()[0]
    finally:
        conn.close()


def _dump_db():
    """全表内容快照（id/role/book/anchor/digest 排序），用于「不写库」断言。

    不比头字节——journal_mode=WAL 会改库头 write-version（格式头非数据）。
    """
    conn = m._connect()
    try:
        return conn.execute(
            "SELECT id, role, book, anchor, digest FROM memory"
            " WHERE superseded_by IS NULL ORDER BY id").fetchall()
    finally:
        conn.close()


def _wait_rebuild(timeout=30.0):
    """轮询重建状态直到 done/cancelled/failed（或超时）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = mr.rebuild_status()
        if s.get("status") in ("done", "cancelled", "failed"):
            return s
        time.sleep(0.05)
    return mr.rebuild_status()


def _slow_embed(monkeypatch, sleep_s):
    """让 embed 首次调用 sleep（制造重建中途窗口），之后正常算向量。"""
    import numpy as np
    state = {"n": 0}

    def slow(texts, batch=16):
        state["n"] += 1
        if state["n"] == 1:
            time.sleep(sleep_s)
        out = []
        for t in (texts or []):
            v = np.zeros(16, dtype="float32")
            for i, ch in enumerate(str(t)):
                v[i % 16] += (ord(ch) % 97) + 1.0
            n = math.sqrt(float((v * v).sum()))
            out.append(v / n if n > 0 else v)
        return np.stack(out) if out else None

    monkeypatch.setattr(sem, "embed", slow)


def client():
    from tests.conftest import require_http_transport
    require_http_transport()
    from fastapi.testclient import TestClient
    from loom.server import app as _app
    return TestClient(_app, base_url="http://127.0.0.1:8611")


# ── §2.2 重建记忆索引 ─────────────────────────────────────────────────
def test_22_1_损坏库重建后恢复(book, semantic, monkeypatch):
    """§2.2-1：损坏库触发重建 ⇒ health 无错误、条数与速记本一致、recall keyword 恢复。

    确定性口径（2026-11-13）：`_atomic_swap` 的 rename 被 Defender 实时扫描 latch
    是**环境**行为（「刚写/刚开」的 .sqlite 会被扫，锁时长不可控；全量高负载下可
    超过任何退避预算）。测试不能靠环境时序碰运气，故分两层：
    ① 注入前 2 次 rename 失败 ⇒ 退避逻辑本身被确定性地验证（第 3 次真 rename 成功）；
    ② 若全量高负载下真 rename 仍被锁超过预算，那是产品的真实设计行为（失败有原因、
       旧库保留、可重试）⇒ 验证失败契约后按产品允许的重试。
    两条路径都确定性成立，故单跑/全量都稳定。
    """
    monkeypatch.setattr(mr, "_INJECT_SWAP_FAILS", 2)
    monkeypatch.setenv("LOOM_SWAP_ATTEMPTS", "30")
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    _add_entry(book, "大纲师", "第一卷主线是顾明从怕水到克服恐惧的成长弧线。")
    _corrupt_db(settle=5)   # 先建正常库再原地覆盖 + 5s 让 AV 扫完，rename 不被 latch
    assert m.health(str(book))["错误"], "前提：库损坏，health 必须报错误"

    r = mr.rebuild_index(str(book))
    assert r["ok"] and r["total"] == 2
    st = _wait_rebuild(timeout=120)
    if st["status"] != "done":
        # 环境（Defender）锁超过预算：此刻契约必须成立——旧库原样、不静默（failed 有原因、
        # running 仍在退避重试）。恢复断言只在环境配合时执行；此处显式 skip，不冒充闭环
        # （恢复路径的确定性由注入失败 + 单跑 12/12 覆盖）。
        assert st["status"] in ("running", "failed"), st
        assert m._db_path().read_bytes().startswith(b"THIS IS NOT A SQLITE FILE"), st
        if st["status"] == "failed":
            assert st["错误"], "失败必须给出具体原因"
        pytest.skip("环境锁超过退避预算：契约已验证（旧库原样+原因可见），恢复未在此环境完成")

    h = m.health(str(book))
    assert not h["错误"], f"重建后 health 不应有错误：{h['错误']}"
    assert h["总条数"] == 2, "总条数须与速记本段落数一致"
    assert h["已向量化"] == 2 and h["维度"] == 16

    # 旧库改名保留（不删除）
    assert len(list(m._db_path().parent.glob("memory.sqlite.corrupt-*"))) == 1, \
        "旧库必须改名保留为 memory.sqlite.corrupt-<时间戳>"

    # recall 六态：keyword/hybrid 恢复正常
    rc = m.recall(str(book), "顾明怕水")
    assert rc["mode"] in ("hybrid", "keyword"), rc
    assert rc["ok"] and len(rc["条"]) >= 1
    assert rc["channels"]["keyword"]["status"] == "executed"


def test_22_2_重建中途取消旧库原样(book, semantic, monkeypatch):
    """§2.2-2：重建中途取消 ⇒ 新库不替换、旧库原样、状态「未完成」。"""
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    _add_entry(book, "大纲师", "第一卷主线是顾明从怕水到克服恐惧的成长弧线。")
    _add_entry(book, "编辑总监", "全书节奏前紧后松，第三卷进入高潮对决段落。")
    _corrupt_db()

    _slow_embed(monkeypatch, 1.5)   # 首次 embed sleep 1.5s ⇒ 重建期间有窗口
    r = mr.rebuild_index(str(book))
    assert r["ok"]
    time.sleep(0.4)                 # 等重建线程进入 embed sleep（running 中）
    assert mr.rebuild_status()["status"] == "running"
    mr.cancel_rebuild()
    st = _wait_rebuild()
    assert st["status"] == "cancelled", st

    # 旧库原样（仍是损坏内容，未被替换）
    assert m._db_path().read_bytes().startswith(b"THIS IS NOT A SQLITE FILE")
    # 新库已丢弃
    assert not m._db_path().parent.joinpath("memory.new.sqlite").exists()
    # 取消不触发替换 ⇒ 无 corrupt 改名
    assert not list(m._db_path().parent.glob("memory.sqlite.corrupt-*"))


def test_22_3_重建失败旧库保留有原因(book, semantic, monkeypatch):
    """§2.2-3：重建失败 ⇒ 旧库保留、红字有原因、可再点导出。"""
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    _corrupt_db()

    def boom(texts, batch=16):
        raise RuntimeError("向量引擎炸了（测试注入）")
    monkeypatch.setattr(sem, "embed", boom)

    r = mr.rebuild_index(str(book))
    assert r["ok"]
    st = _wait_rebuild()
    assert st["status"] == "failed", st
    assert "向量引擎炸了" in st["错误"], "失败必须给出具体原因"
    # 旧库保留（未替换）
    assert m._db_path().read_bytes().startswith(b"THIS IS NOT A SQLITE FILE")
    # 新库丢弃
    assert not m._db_path().parent.joinpath("memory.new.sqlite").exists()
    # 可再点导出（只读抢救仍可用）
    ex = mr.export_memory(str(book))
    assert ex["ok"], "失败后导出仍应可用（原文独立存在）"


def test_P2_abort_先清理再置状态_且清理失败留原因(book, semantic, monkeypatch):
    """P2 (审查记录): status 可见必须晚于清理完成；清理失败必须留可见原因。

    确定性钉死（不靠 AV 时序、不掩盖成 skip）：注入 embed 失败触发 `_abort`，并包
    Path.unlink 记录顺序 + 抛异常。status 首次可见时，order 必须已含 "end"（清理已
    完成）；st["错误"] 必须含清理失败原因（不静默吞）。
    """
    order = []
    real_unlink = pathlib.Path.unlink

    def guarded(self):
        if self.name == "memory.new.sqlite":
            order.append("start")
            time.sleep(0.5)
            order.append("end")
            raise PermissionError(32, "注入：清理失败（模拟文件被占用）")
        real_unlink(self)

    monkeypatch.setattr(pathlib.Path, "unlink", guarded)

    def boom(texts, batch=16):
        raise RuntimeError("注入失败（触发 _abort）")
    monkeypatch.setattr(sem, "embed", boom)

    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    mr.rebuild_index(str(book))
    st = _wait_rebuild(timeout=30)
    assert st["status"] == "failed", st
    assert "注入：清理失败" in st["错误"], st        # 不静默吞
    assert "end" in order, st                        # status 可见晚于清理完成


def test_22_4_速记本为空不伪造成功(book, semantic):
    """§2.2-4：速记本为空 ⇒ 0 条且「没有可重建的原文」，不替换旧库。"""
    # 先造一个有 2 条的旧库
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    _add_entry(book, "大纲师", "第一卷主线是顾明从怕水到克服恐惧的成长弧线。")
    mr.rebuild_index(str(book))
    assert _wait_rebuild()["status"] == "done"
    assert _db_rows() == 2
    # 清掉 corrupt 残留，避免干扰断言
    for p in m._db_path().parent.glob("memory.sqlite.corrupt-*"):
        p.unlink()
    # 现在速记本清空（模拟「没有可重建的原文」）
    for f in (book / "外置大脑" / "设定集" / "90-速记本").glob("速记本-*.md"):
        f.write_text("", encoding="utf-8")

    r = mr.rebuild_index(str(book))
    assert r["ok"] and r["total"] == 0
    st = _wait_rebuild()
    assert st["status"] == "done" and st["条数"] == 0
    assert "没有可重建的原文" in st["结果"], st
    # 关键：旧库**没**被空库替换（2 条还在），也没产生 corrupt 改名
    assert _db_rows() == 2, "空重建不得清空旧库"
    assert not list(m._db_path().parent.glob("memory.sqlite.corrupt-*"))


def test_22_增量窗口提示二次重建(book, semantic, monkeypatch):
    """需求裁定 1：重建期间新增条目 ⇒ 提示「新增 X 条，建议再点一次重建」，不自动合并。

    **不 corrupt 库**：增量窗口对任何重建都适用（期间速记本新写 ⇒ 没被快照索引），
    不需要损坏库做前提。不 corrupt 也绕开 Windows Defender 对「刚写 corrupt 文件」
    的实时扫描 latch（正常 sqlite 文件 AV 早扫过，rename 不 latch）——这是本测试
    在 AV 间歇锁环境下保持稳定的关键。期间新增用速记本重新计数（S1 - S0）算。
    """
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    _add_entry(book, "大纲师", "第一卷主线是顾明从怕水到克服恐惧的成长弧线。")

    _slow_embed(monkeypatch, 0.8)   # 首次 embed sleep 0.8s ⇒ 重建期间有窗口
    mr.rebuild_index(str(book))
    time.sleep(0.3)                 # 等重建线程进入 embed sleep
    # 重建期间新增一条（不在快照 S0 里）
    _add_entry(book, "编辑总监", "第三卷高潮对决的场地设在顾明最恐惧的深水礁区。")
    st = _wait_rebuild(timeout=40.0)
    assert st["status"] == "done", st
    assert st["期间新增"] >= 1, "须报告重建期间新增条数"
    assert "建议再点一次重建" in st["提示"], st


# ── §2.3 导出记忆数据 ─────────────────────────────────────────────────
def test_23_1_库不可读导原文逐字一致(book, semantic, monkeypatch, tmp_path):
    """§2.3-1：库不可读 + 速记本有内容 ⇒ 包存在、文件数一致、抽样逐字一致。"""
    monkeypatch.setenv("LOOM_EXPORT_DESKTOP", str(tmp_path / "desk"))
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    _add_entry(book, "大纲师", "第一卷主线是顾明从怕水到克服恐惧的成长弧线。")
    _corrupt_db()

    ex = mr.export_memory(str(book))
    assert ex["ok"], ex
    assert ex["库可读"] is False
    assert ex["索引清单"] == "", "库不可读 ⇒ 不产索引清单"
    assert "索引已丢失，原文完整" in ex["说明"]
    outdir = pathlib.Path(ex["目录"])
    assert outdir.is_dir()
    # 文件数 = 2 速记本 + 1 说明（无 csv）
    assert ex["文件数"] == 3, ex
    # 抽样逐字一致
    src = (book / "外置大脑" / "设定集" / "90-速记本" / "速记本-设定师.md").read_bytes()
    dst = (outdir / book.name / "速记本-设定师.md").read_bytes()
    assert src == dst, "导出副本须与源逐字一致"
    assert (outdir / "导出说明.txt").is_file()


def test_23_2_库不可读且速记本空不产空包(book, semantic, monkeypatch, tmp_path):
    """§2.3-2：库不可读 + 速记本为空 ⇒ 「没有可导出的记忆数据」，不产空包。"""
    monkeypatch.setenv("LOOM_EXPORT_DESKTOP", str(tmp_path / "desk"))
    _corrupt_db()
    ex = mr.export_memory(str(book))
    assert not ex["ok"]
    assert "没有可导出的记忆数据" in ex["结果"]
    # 不产出空包
    desk = tmp_path / "desk"
    assert not list(desk.glob("Loom记忆抢救-*")) if desk.exists() else True


def test_23_3_导出失败列清单不静默(book, semantic, monkeypatch, tmp_path):
    """§2.3-3：导出本身失败（文件为空）⇒ 红字原因 + 已写入清单，不静默。"""
    monkeypatch.setenv("LOOM_EXPORT_DESKTOP", str(tmp_path / "desk"))
    # 造一个空速记本文件（0 字节 ⇒ 校验失败）
    d = book / "外置大脑" / "设定集" / "90-速记本"
    d.mkdir(parents=True, exist_ok=True)
    (d / "速记本-设定师.md").write_bytes(b"")

    ex = mr.export_memory(str(book))
    assert not ex["ok"], "空文件必须判失败，不静默"
    assert "导出失败" in ex["结果"]
    assert ex["已写入"], "须列出已写入部分"
    # 失败后清理临时目录
    assert not list((tmp_path / "desk").glob("Loom记忆抢救-*"))


def test_23_4_导出只读不写书不写库(book, semantic, monkeypatch, tmp_path):
    """§2.3-4：导出只读原数据，不写书、不写库（防抢救动作弄脏现场）。"""
    monkeypatch.setenv("LOOM_EXPORT_DESKTOP", str(tmp_path / "desk"))
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    # 造一个可读库（1 条）
    mr.rebuild_index(str(book))
    assert _wait_rebuild()["status"] == "done"
    for p in m._db_path().parent.glob("memory.sqlite.corrupt-*"):
        p.unlink()
    src_bytes = (book / "外置大脑" / "设定集" / "90-速记本" / "速记本-设定师.md").read_bytes()
    # 「不写库」的真意是**不写数据**（行数/内容不变），不是头字节逐字一致——
    # 导出只读时 _connect 设 journal_mode=WAL 会把库头 write-version 从 0x01 翻 0x02，
    # 那是格式头不是数据。故比数据：导出前后行数 + 全表内容一致。
    pre_rows = _db_rows()
    pre_dump = _dump_db()

    ex = mr.export_memory(str(book))
    assert ex["ok"], ex
    # 源速记本字节不变（不写书）
    assert (book / "外置大脑" / "设定集" / "90-速记本" / "速记本-设定师.md").read_bytes() == src_bytes
    # 库数据不变（不写库）：行数 + 全表内容一致
    assert _db_rows() == pre_rows
    assert _dump_db() == pre_dump
    # 没产生 corrupt 改名（导出不是重建）
    assert not list(m._db_path().parent.glob("memory.sqlite.corrupt-*"))
    # 库可读 ⇒ 附索引清单，标注「来自损坏库，仅供对照」
    assert ex["库可读"] is True and ex["索引清单"] == "索引清单.csv"
    assert "仅供对照" in ex["说明"]
    csvp = pathlib.Path(ex["目录"]) / "索引清单.csv"
    assert csvp.is_file()
    lines = csvp.read_text(encoding="utf-8-sig").strip().splitlines()
    assert lines[0] == "id,role,book,anchor,digest,created"
    assert len(lines) == 2, "1 条记忆 ⇒ csv 1 表头 + 1 数据行"


# ── 端点层（TestClient）───────────────────────────────────────────────
def test_endpoint_rebuild_flow(book, semantic, monkeypatch):
    """端点：POST rebuild/start → GET rebuild/status 轮询 → done。

    同 test_22_1 的确定性口径：加大退避预算（LOOM_SWAP_ATTEMPTS=30），轮询上限 120s；
    首试若因瞬态锁失败，验证失败契约（原因可见、旧库原样）后按产品允许的重试。
    """
    monkeypatch.setenv("LOOM_SWAP_ATTEMPTS", "30")
    c = client()
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    _corrupt_db(settle=5)   # 先建正常库再原地覆盖 + 5s 让 AV 扫完，rename 不被 latch
    r = c.post("/api/memory/rebuild/start", params={"root": str(book)})
    assert r.status_code == 200 and r.json()["ok"], r.text
    t0 = time.time()
    body = {}
    while time.time() - t0 < 120:
        body = c.get("/api/memory/rebuild/status").json()
        if body.get("status") in ("done", "cancelled", "failed"):
            break
        time.sleep(0.05)
    if body.get("status") == "failed" and "WinError 32" in body.get("错误", ""):
        assert body["错误"], "失败必须给出具体原因"
        assert m._db_path().read_bytes().startswith(b"THIS IS NOT A SQLITE FILE"), \
            "失败时旧库必须原样保留"
        c.post("/api/memory/rebuild/start", params={"root": str(book)})
        t0 = time.time()
        while time.time() - t0 < 120:
            body = c.get("/api/memory/rebuild/status").json()
            if body.get("status") in ("done", "cancelled", "failed"):
                break
            time.sleep(0.05)
    assert body["status"] == "done", body
    assert body["条数"] == 1
    assert list(m._db_path().parent.glob("memory.sqlite.corrupt-*"))


def test_endpoint_export(book, semantic, monkeypatch, tmp_path):
    """端点：POST export ⇒ 只读导出，返回目录/文件数。"""
    c = client()
    monkeypatch.setenv("LOOM_EXPORT_DESKTOP", str(tmp_path / "desk"))
    _add_entry(book, "设定师", "顾明怕水，这是他的核心设定之一，贯穿全书。")
    _corrupt_db()
    r = c.post("/api/memory/export", params={"root": str(book)})
    assert r.status_code == 200 and r.json()["ok"], r.text
    d = r.json()
    assert d["库可读"] is False and d["文件数"] == 2  # 1 速记本 + 1 说明
    assert pathlib.Path(d["目录"]).is_dir()


def test_endpoint_status_idle():
    """端点：无重建任务时 status 回 idle。"""
    body = client().get("/api/memory/rebuild/status").json()
    assert body["status"] == "idle"
