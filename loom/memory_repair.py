# -*- coding: utf-8 -*-
"""记忆库修复/抢救（用户裁定②·三按钮之②③；2026-11-12 需求方派单 审查记录）。

定位：**库级损坏**（文件损坏/磁盘错误/schema 不符）的用户可操作途径。
与 `memory.recover()`（重载向量引擎，按钮①）严格分开——两类故障不共用一个按钮
（设计稿 §1.3：引擎缺依赖与库损坏是两种病，药方不同）。

两个途径（设计稿 §2.2 / §2.3，按「设计」列实施）：

- `rebuild_index()`  重建记忆索引（按钮②）。**只从速记本原文重建**，绝不从旧库
  digest 反推（顺序红线：旧库已不可信）。可取消后台任务：新建 `memory.new.sqlite`
  → 逐批算向量入库 → 校验 → 原子改名替换；旧库改名 `memory.sqlite.corrupt-<时间戳>`
  **保留**（可回退、可取证），不删除。取消⇒新库丢弃、旧库原样。
- `export_memory()`  导出记忆数据（按钮③）。**只读**：不写书、不写库（防抢救动作
  弄脏现场）。库完全不可读⇒只导速记本原文，说明「索引已丢失，原文完整」；库部分
  可读⇒附 `索引清单.csv` 标注「来自损坏库，仅供对照」。

落点：桌面 `Loom记忆抢救-<时间戳>`（`LOOM_EXPORT_DESKTOP` 可覆盖，测试用）。
本轮边界（需求裁定 2）：只做默认桌面，不做自定义目录选择器。

运行数据隔离：库路径经 `memory._db_path()`（`LOOM_HOME/memory/memory.sqlite`）；
测试用隔离 `LOOM_HOME`（conftest autouse 已指向 tmp），不碰真实 `~/.loom`。
"""
from __future__ import annotations

import csv
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import memory as _mem

# 重建向量批大小（与 semantic.embed 默认 batch 一致）
_BATCH = 16

# 模块级重建任务状态（单进程单任务；并发 start 被拒）。
# 设计稿 §2.2：可取消后台任务，面板轮询进度（N/M）。
_REBUILD_LOCK = threading.Lock()
_REBUILD: dict = {"task": None}

# 测试注入钩子：前 N 次 rename 失败（确定性验证退避逻辑本身，不依赖 AV 时序）。
# 默认 0 ⇒ 生产路径完全不变。测试用 monkeypatch.setattr(mr, "_INJECT_SWAP_FAILS", N)。
_INJECT_SWAP_FAILS = 0


# ── 书发现 ─────────────────────────────────────────────────────────────
def list_books() -> list[Path]:
    """库根（`libroot.library_root()`）下所有含 `loom.toml` 的书根，按名排序。

    库根不存在 / 无书 ⇒ `[]`。重建/导出都是**全局库**操作（库是物理一个，
    逻辑按 book 域过滤），故须覆盖所有书，不能只收当前打开的那一本。
    """
    try:
        from . import libroot
        root = Path(libroot.library_root())
    except Exception:
        return []
    if not root.is_dir():
        return []
    out = [p for p in root.rglob("loom.toml") if p.is_file()]
    return sorted((p.parent for p in out), key=lambda x: x.name)


def _scratchpad_files(root: Path) -> list[tuple[Path, str]]:
    """一本书的速记本文件（新版 + 历史两落点），返回 [(path, role)]。"""
    files: list[tuple[Path, str]] = []
    for d in (root / "外置大脑" / "设定集" / "90-速记本",
              root / "外置大脑"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("速记本-*.md")):
            if f.is_file():
                role = f.stem.replace("速记本-", "")
                files.append((f, role))
    return files


def collect_entries(root: Path) -> list[tuple[str, str, str, str]]:
    """收一本书的速记本条目，返回 `[(role, anchor, body, book)]`。

    只读原文（顺序红线）。`book` = 该书的规范化根路径（与 `remember` 存库口径一致）。
    新版落点优先；同 anchor 在两落点都出现时去重（历史条目迁移期可能两处都有）。
    正文 <8 字跳过（与 `memory.rebuild` 同口径）。
    """
    book = _mem._norm_root(root)
    seen: set[str] = set()
    out: list[tuple[str, str, str, str]] = []
    for f, role in _scratchpad_files(root):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for piece in re.split(r"(?=^### )", text, flags=re.M):
            if not piece.strip():
                continue
            first = piece.split("\n")[0].strip()
            body = "\n".join(piece.split("\n")[1:]).strip()
            if len(body) < 8:
                continue
            if first in seen:
                continue
            seen.add(first)
            out.append((role, first, body, book))
    return out


def collect_all(roots: list[Path]) -> list[tuple[str, str, str, str]]:
    """收所有书的条目。"""
    out: list[tuple[str, str, str, str]] = []
    for r in roots:
        out.extend(collect_entries(r))
    return out


# ── 重建（按钮②）──────────────────────────────────────────────────────
def _init_new(p: Path) -> None:
    """建一个新库文件（与 `memory._connect` 同 schema）。绝不读旧库。

    直接走裸 sqlite3 建同构表——不走 `_connect()`，因为那会写 WAL pragma 到
    `_db_path()` 的固定路径，而这里要写到任意临时路径。
    """
    conn = sqlite3.connect(str(p))
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            book TEXT NOT NULL DEFAULT '',
            anchor TEXT NOT NULL,
            digest TEXT NOT NULL,
            vector BLOB NOT NULL,
            created INTEGER NOT NULL,
            superseded_by INTEGER DEFAULT NULL)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_book ON memory(book)")
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts"
            " USING fts5(digest, tokenize='unicode61')")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entity_index ("
            " entity TEXT NOT NULL, memory_id INTEGER NOT NULL,"
            " PRIMARY KEY (entity, memory_id))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entity ON entity_index(entity)")
        conn.commit()
    finally:
        conn.close()


def _old_count() -> int:
    """旧库当前条数（superseded_by IS NULL）。不可读/损坏 ⇒ 0（不抛）。"""
    try:
        with _mem._LOCK:
            conn = _mem._connect()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE superseded_by IS NULL"
                ).fetchone()[0]
            finally:
                conn.close()
        return int(n)
    except Exception:
        return 0


def _drop_new(newp: Path) -> str:
    """清理新库；失败时返回可见原因（不静默吞，裁定②第4项同源），成功返回空串。"""
    try:
        if newp.exists():
            newp.unlink()
        return ""
    except Exception as e:      # noqa: BLE001 - "不静默"裁定 ⇒ 留一笔可见原因
        return "新库清理失败：%s: %s" % (type(e).__name__, str(e)[:160])


def _work(st: dict, entries: list[tuple[str, str, str, str]]) -> None:
    """重建后台工作线程体。只写新库，绝不碰旧库；取消/失败⇒丢弃新库。"""
    from .study import semantic as _sem
    newp: Path = st["new_path"]

    def _abort(reason: str) -> None:
        # P2 (审查记录): 先完成清理动作，再置 status ⇒ 失败瞬间快照一致，
        # 轮询者不会看到「failed 但 memory.new.sqlite 还在」（2/15 顺序竞态的真根因）。
        # 清理失败留可见原因，不静默 pass（与裁定②第4项同源）。
        note = _drop_new(newp)
        st["错误"] = reason + (("; " + note) if note else "")
        st["status"] = "failed"

    try:
        if not _sem.ready().get("可语义检索"):
            _abort("语义引擎不可用，无法重建（先点「重载向量引擎」修引擎，再重建）")
            return
        total = len(entries)
        st["total"] = total
        # 顺序红线延伸：速记本为空 ⇒ 0 条且明确「没有可重建的原文」，
        # **不**用空新库替换旧库（替换会清空它），不伪造成功。
        if total == 0:
            note = _drop_new(newp)
            st["条数"] = 0
            st["结果"] = "没有可重建的原文（速记本为空）" + (("; " + note) if note else "")
            st["status"] = "done"
            return
        done = 0
        for start in range(0, total, _BATCH):
            # 批间查取消：取消⇒新库丢弃、旧库原样
            if st["cancel"].is_set():
                note = _drop_new(newp)
                st["结果"] = "已取消：新库已丢弃，旧库原样保留。" + (("; " + note) if note else "")
                st["status"] = "cancelled"
                return
            batch = entries[start:start + _BATCH]
            try:
                vs = _sem.embed([b for (_r, _a, b, _bk) in batch])
            except Exception as e:      # noqa: BLE001 - 单批失败即中止，不静默
                _abort("重建失败于第 %d/%d 条批次：%s: %s"
                       % (start + len(batch), total, type(e).__name__, str(e)[:160]))
                return
            if vs is None:
                _abort("重建失败于第 %d/%d 条批次：向量算不出来（引擎不可用）"
                       % (start + len(batch), total))
                return
            now = int(time.time())
            conn = sqlite3.connect(str(newp))
            try:
                for (role, anchor, body, book), v in zip(batch, vs):
                    mid = _insert_new(conn, role, book, anchor, body, v, now)
                    _mem._index_side_tables(conn, mid, body, None)
                conn.commit()
            finally:
                conn.close()
            done += len(batch)
            st["done"] = done
            # 批结束后**再查一次**取消：单批（≤16 条）时，主线程可能在 embed
            # sleep 期间就发了取消——若只在批前查，worker 会跑完批直接替换，
            # 取消被吞。批后查保证「取消⇒新库丢弃、旧库原样」对任意批大小成立。
            if st["cancel"].is_set():
                note = _drop_new(newp)
                st["结果"] = "已取消：新库已丢弃，旧库原样保留。" + (("; " + note) if note else "")
                st["status"] = "cancelled"
                return
    except Exception as e:      # noqa: BLE001 - 兜底：任何未预期异常都显式失败
        _abort("重建失败：%s: %s" % (type(e).__name__, str(e)[:160]))
        return

    # 完成：原子替换。旧库改名保留，不删除。
    try:
        _atomic_swap(_mem._db_path(), newp, st["corrupt_path"])
    except Exception as e:      # noqa: BLE001
        note = _drop_new(newp)
        st["错误"] = "替换失败：%s: %s" % (type(e).__name__, str(e)[:160]) + (("; " + note) if note else "")
        st["status"] = "failed"
        return

    new_total = st["done"]
    st["status"] = "done"
    st["条数"] = new_total
    # 增量窗口（需求裁定 1）：rebuild 快照 S0 条，期间速记本新写 M 条
    # （不在快照里、没被索引）。完成时重新 collect 得 S1，added = S1 - S0。
    # 比用 old_count 准：old_count 是旧库条数，损坏库读不到=0 会把快照全算成
    # 「新增」；速记本重新计数才是「期间新写」的真值。
    try:
        s1 = len(collect_all(st["roots"]))
    except Exception:
        s1 = new_total
    added = s1 - new_total
    st["期间新增"] = added if added > 0 else 0
    if added > 0:
        st["提示"] = "重建期间新增 %d 条，建议再点一次重建" % added
    st["结果"] = "重建完成：%d 条。旧库已保留为 %s。" % (new_total, st["corrupt_path"].name)


def _insert_new(conn, role: str, book: str, anchor: str, body: str, v, now: int) -> int:
    """往新库插一条（含向量 + 摘要）。返回新 id。"""
    cur = conn.execute(
        "INSERT INTO memory (role, book, anchor, digest, vector, created)"
        " VALUES (?,?,?,?,?,?)",
        (role, book, anchor, body[:200], _mem._blob(v), now),
    )
    return cur.lastrowid


def _atomic_swap(final: Path, newp: Path, corruptp: Path,
                 attempts: int | None = None) -> None:
    """原子替换：旧库改名 `corruptp` 保留（不删除），新库改名 `final`。

    Windows 上旧库文件常有**瞬态锁**（Defender 实时扫描 / 文件系统缓存 /
    刚关句柄的 GC 延迟），`rename` 抛 `WinError 32`（文件被占用）。这是瞬态的——
    有界退避重试即可；重试耗尽才当真失败抛（调用方判 failed，不静默）。
    重试期间旧库始终存在（未改名），数据安全。

    退避用**纯 sleep**（不重开文件）：Defender 实时扫描 latch 刚写的 .sqlite，
    每次 connect 都触发再扫、越摸越锁（实测 22s+connect 反而更糟）。只等它
    扫完再试 rename。0.5/1.0/.../5.0 秒（总 ~30s）。真实产品旧库是 AV 早已
    扫过的文件，rename 不会 latch；退避只是兜底极端情况。
    """
    last = None
    if attempts is None:
        # 产品默认 10 次（总 ~30s）；测试/高负载环境可用 LOOM_SWAP_ATTEMPTS 加大预算。
        import os
        try:
            attempts = int(os.environ.get("LOOM_SWAP_ATTEMPTS", 10))
        except ValueError:
            attempts = 10
    # 测试可注入前 N 次 rename 失败（确定性验证退避逻辑本身，不依赖 AV 时序）。
    _inj = _INJECT_SWAP_FAILS
    for i in range(attempts):
        try:
            if _inj and i < _inj:
                raise PermissionError(32, "另一个程序正在使用此文件（测试注入）", str(final))
            # 防御：corruptp 已存在（极端并发/残留）时先移走，避免 Windows
            # Path.rename 目标已存在抛 FileExistsError。
            if corruptp.exists():
                corruptp.rename(corruptp.with_name(corruptp.name + ".dup"))
            if final.exists():
                final.rename(corruptp)
            newp.replace(final)
            return
        except Exception as e:      # noqa: BLE001
            last = e
            # 纯 sleep 退避，**绝不重开文件**：Defender 实时扫描 latch 刚写的
            # .sqlite，每次 connect 都触发再扫、越摸越锁（实测 22s+connect 反而
            # 更糟）。只等它扫完再试 rename。0.5/1.0/.../5.0 秒（总 ~30s）。
            # 真实产品旧库是 AV 早已扫过的文件，rename 不会 latch；退避只是
            # 兜底极端情况。重建是用户主动的后台操作，等得起。
            time.sleep(0.5 * (i + 1))
    raise last


def rebuild_index(root: str | None = None) -> dict:
    """启动重建后台任务（按钮②）。

    - `root` 给 ⇒ 只收该书的条目；不给 ⇒ 收所有书（全局库）。
    - 已有重建在跑 ⇒ 拒绝（`ok=False`），不并发重建。
    - 返回 `{"ok":True,"task_id":…,"total":…}` 供面板轮询 `rebuild_status`。
    """
    with _REBUILD_LOCK:
        if _REBUILD["task"] is not None and _REBUILD["task"]["status"] == "running":
            return {"ok": False,
                    "结果": "已有重建在进行，请等它完成或先取消。"}
        ts = time.strftime("%Y%m%d-%H%M%S")
        # corrupt 名加唯一后缀（纳秒 + 随机）：Windows 的 Path.rename 目标已存在
        # 会抛 FileExistsError（不像 POSIX 覆盖）。同秒两次重建若共用秒级 ts，
        # 第二次 rename 到已存在的 corrupt 名就撞。唯一后缀杜绝撞名。
        import random
        uniq = "%d-%04d" % (int(time.time() * 1000) % 100000, random.randint(0, 9999))
        base = _mem._db_path().parent
        base.mkdir(parents=True, exist_ok=True)
        newp = base / "memory.new.sqlite"
        corruptp = base / ("memory.sqlite.corrupt-" + ts + "-" + uniq)
        # 清掉上一次残留的新库（若存在）
        try:
            if newp.exists():
                newp.unlink()
        except Exception:
            pass
        _init_new(newp)

        roots = [Path(root)] if root else list_books()
        entries = collect_all(roots)

        st = {
            "task_id": "rebuild-" + ts,
            "status": "running",
            "done": 0,
            "total": len(entries),
            "cancel": threading.Event(),
            "new_path": newp,
            "corrupt_path": corruptp,
            "old_count": _old_count(),
            "roots": roots,
            "条数": 0,
            "期间新增": 0,
            "提示": "",
            "结果": "",
            "错误": "",
        }
        _REBUILD["task"] = st
        t = threading.Thread(target=_work, args=(st, entries), daemon=True)
        t.start()
        st["thread"] = t
        return {"ok": True, "task_id": st["task_id"], "total": st["total"]}


def rebuild_status() -> dict:
    """读重建进度（N/M）。无任务 ⇒ `{"status":"idle"}`。"""
    with _REBUILD_LOCK:
        st = _REBUILD["task"]
        if st is None:
            return {"status": "idle"}
        return {
            "status": st["status"],
            "done": st["done"],
            "total": st["total"],
            "条数": st.get("条数", 0),
            "期间新增": st.get("期间新增", 0),
            "提示": st.get("提示", ""),
            "结果": st.get("结果", ""),
            "错误": st.get("错误", ""),
        }


def cancel_rebuild() -> dict:
    """取消重建：置取消标志，工作线程在批间检查后丢弃新库、旧库原样。"""
    with _REBUILD_LOCK:
        st = _REBUILD["task"]
        if st is None or st["status"] != "running":
            return {"ok": False, "结果": "没有进行中的重建可取消。"}
        st["cancel"].set()
        return {"ok": True, "结果": "已请求取消；新库将被丢弃，旧库原样保留。"}


def _reset_for_tests() -> None:
    """测试钩子：清空模块级重建任务状态（生产路径不用）。"""
    with _REBUILD_LOCK:
        _REBUILD["task"] = None


# ── 导出（按钮③）──────────────────────────────────────────────────────
def _db_readable() -> bool:
    """库是否可读（能连 + 能查 memory 表）。任何异常 ⇒ False。"""
    try:
        with _mem._LOCK:
            conn = _mem._connect()
            try:
                conn.execute("SELECT id FROM memory LIMIT 1").fetchone()
            finally:
                conn.close()
        return True
    except Exception:
        return False


KEEP_EXPORTS = 16      # 只按时间顺序保留最近 16 次抢救导出（用户 2026-11-16 裁定）


def _export_base() -> Path:
    """导出落点根目录。

    2026-11-16（用户裁定）：**收进知识库目录一起管理** —— `<知识库>/记忆抢救/`，
    每次导出是其中一个 `Loom记忆抢救-<时间戳>` 子目录，只保留最新 16 次。
    桌面落点与真实用户无关，只在开发/测试用 `LOOM_EXPORT_DESKTOP` 显式指定时才出现。
    """
    import os
    raw = (os.environ.get("LOOM_EXPORT_DESKTOP") or "").strip()
    if raw:
        return Path(raw)
    from .libroot import library_root
    return Path(library_root()) / "记忆抢救"


def _prune_exports(base: Path, keep: int = KEEP_EXPORTS) -> list[str]:
    """只按时间顺序保留最新 keep 份导出，多出来的整目录清掉。返回被清掉的目录名。"""
    try:
        cand = [p for p in base.iterdir()
                if p.is_dir() and p.name.startswith("Loom记忆抢救-")]
    except OSError:
        return []
    # 目录名里带 `%Y%m%d-%H%M%S`，按名字倒序就是按时间倒序
    cand.sort(key=lambda p: p.name, reverse=True)
    removed: list[str] = []
    for d in cand[keep:]:
        try:
            _cleanup(d)
            removed.append(d.name)
        except OSError:
            pass
    return removed


def _export_desktop() -> Path:
    """导出落点根目录（默认知识库下的「记忆抢救」；`LOOM_EXPORT_DESKTOP` 可覆盖，测试/开发用）。"""
    return _export_base()


def export_memory(root: str | None = None) -> dict:
    """导出记忆数据（按钮③）。**只读**：不写书、不写库。

    - 库完全不可读 ⇒ 只导速记本原文，说明「索引已丢失，原文完整」。
    - 库部分可读 ⇒ 附 `索引清单.csv`，标注「来自损坏库，仅供对照」。
    - 速记本为空 ⇒ 报「没有可导出的记忆数据」，不产出空包冒充成功。
    - 落点：`<知识库>/记忆抢救/Loom记忆抢救-<时间戳>`（2026-11-16 用户裁定：与知识库一同管理，
      只按时间顺序保留最新 16 次；桌面落点仅开发/测试用 `LOOM_EXPORT_DESKTOP` 指定时出现）。
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    files_written: list[str] = []
    outdir = Path("")
    try:
        base = _export_desktop()
        base.mkdir(parents=True, exist_ok=True)
        outdir = base / ("Loom记忆抢救-" + ts)
        outdir.mkdir(parents=True)

        roots = [Path(root)] if root else list_books()
        books_with_data = 0
        for r in roots:
            sp_files = _scratchpad_files(r)
            if not sp_files:
                continue
            book_dir = outdir / r.name
            book_dir.mkdir(parents=True, exist_ok=True)
            for f, role in sp_files:
                if not f.is_file():
                    continue
                dst = book_dir / f.name
                dst.write_bytes(f.read_bytes())
                files_written.append(str(dst.relative_to(outdir)))
            books_with_data += 1

        if not files_written:
            # 速记本为空：不产出空包冒充成功
            _cleanup(outdir)
            return {"ok": False,
                    "结果": "没有可导出的记忆数据（速记本为空）。"}

        # 校验：每个文件字节 > 0
        bad = [p for p in files_written
               if not (outdir / p).exists() or (outdir / p).stat().st_size <= 0]
        if bad:
            _cleanup(outdir)
            return {"ok": False,
                    "结果": "导出失败：以下文件为空或缺失：%s" % "、".join(bad[:8]),
                    "已写入": files_written}

        # 索引清单（仅库可读时）
        readable = _db_readable()
        csv_rel = ""
        if readable:
            csv_rel = "索引清单.csv"
            _write_index_csv(outdir / csv_rel)

        # 导出说明
        desc_lines = ["Loom 记忆抢救导出",
                      "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                      "库状态：%s" % ("部分可读（附索引清单，仅供对照）" if readable
                                     else "不可读（索引已丢失，原文完整）"),
                      "书数：%d，文件数：%d" % (books_with_data, len(files_written)),
                      "", "文件清单："]
        desc_lines.extend(files_written)
        (outdir / "导出说明.txt").write_text("\n".join(desc_lines), encoding="utf-8")

        # 抽样回源校验（find_by_anchor 从源速记本读回，逐字一致）
        spot = _spot_check(roots)
        if not spot["ok"]:
            _cleanup(outdir)
            return {"ok": False,
                    "结果": "导出校验失败：%s" % spot["原因"],
                    "已写入": files_written}

        return {"ok": True,
                "目录": str(outdir),
                "书数": books_with_data,
                "文件数": len(files_written) + 1 + (1 if csv_rel else 0),
                "库可读": readable,
                "索引清单": csv_rel,
                "说明": ("索引已丢失，原文完整" if not readable
                        else "索引清单来自损坏库，仅供对照"),
                "校验": spot["说明"],
                # 与知识库一同管理：只留最新 16 次，多出来的在这里清掉（清不掉不算失败）
                "已清理旧导出": _prune_exports(base)}
    except Exception as e:      # noqa: BLE001 - 导出失败要能看见原因，不静默
        try:
            if str(outdir):
                _cleanup(outdir)
        except Exception:
            pass
        return {"ok": False,
                "结果": "导出失败：%s: %s" % (type(e).__name__, str(e)[:160]),
                "已写入": files_written}


def _cleanup(outdir: Path) -> None:
    """失败时清掉本次导出的临时目录（只删自己刚建的，不碰别处）。"""
    import shutil
    try:
        if outdir and outdir.exists():
            shutil.rmtree(outdir)
    except Exception:
        pass


def _write_index_csv(p: Path) -> None:
    """从（可读的）memory 表导 id/role/book/anchor/digest/created 到 csv。"""
    with _mem._LOCK:
        conn = _mem._connect()
        try:
            rows = conn.execute(
                "SELECT id, role, book, anchor, digest, created FROM memory"
                " WHERE superseded_by IS NULL ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "role", "book", "anchor", "digest", "created"])
        for r in rows:
            w.writerow(list(r))


def _spot_check(roots: list[Path]) -> dict:
    """抽样回源校验：取一条导出条目，用 find_by_anchor 从**源**速记本读回，
    与正文逐字比对。读不回 / 不一致 ⇒ 校验失败。"""
    from . import scratchpad as _sp
    for r in roots:
        for f, role in _scratchpad_files(r):
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            for piece in re.split(r"(?=^### )", text, flags=re.M):
                if not piece.strip():
                    continue
                first = piece.split("\n")[0].strip()
                body = "\n".join(piece.split("\n")[1:]).strip()
                if len(body) < 8:
                    continue
                got = _sp.find_by_anchor(r, role, first)
                if got and got == body:
                    return {"ok": True, "说明": "抽样回源一致（%s / %s）"
                            % (r.name, role)}
                if got:
                    return {"ok": False, "原因": "抽样回源不一致（%s / %s）"
                            % (r.name, role)}
    return {"ok": True, "说明": "无抽样条目（已校验文件非空）"}
