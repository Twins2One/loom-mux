# -*- coding: utf-8 -*-
"""向量记忆库（0.8.7，作者 2026-10-31 裁定）。

定位一句话：**速记本的检索索引层**——不存第二份正文，只存
「哪条记录在哪个文件哪一段（anchor）+ 摘要 + 向量」。

作者已裁定的四条：
1. agent 自动写（随想也记，`author_note` 必须标来源）；管理（删/重建）只归书房伙伴与领航员。
2. 物理一个库，逻辑按域过滤：`book` 字段存**项目根路径**（规范化的）——
   两本书可能同名（实测：E:\\Book 与 tmp-selftest 下各有一本「基金会档案」），
   存书名会串域，存路径天然区分。检索查 `book IN (本书, '')`，全局条目跨书可召回。
3. 速记本是唯一事实源：检索命中后用 anchor 回速记本取**原文段落**给模型，
   摘要只用于排序。作者手改速记本，检索拿到的是改后的。
4. 草稿层边界：记忆不能直接进正文，升格必须走「提设定」「改指纹」候选卡。

运行数据隔离：使用独立的 Loom 记忆文件、代码与包内嵌入模型。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()

# 相对召回阈值：最高分的 65% 作为尾部过滤线。
# 库越大距离越小，固定绝对阈值会形同虚设——按「最高分 × 0.65」砍尾，绝对值只做兜底。
RECALL_RATIO = 0.65
RECALL_FLOOR = 0.30

# 行动模式 → 「记要点」每轮上限（作者 2026-10-31 裁定三档）
MODE_LIMITS = {"主动思考": 4, "默认": 2, "安静": 1}


def _db_path() -> Path:
    from .config import user_config_dir

    d = user_config_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "memory.sqlite"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    # 2026-11-03（外部 AI 审阅 Q2 结论）：WAL + 调大 autocheckpoint + synchronous=NORMAL
    # —— 逐条事务的写放大被摊平，维护进程在 BELOW_NORMAL 优先级下对磁盘的冲击约等于零。
    # 每次连接都设一遍是刻意保守：PRAGMA 是连接级的，多入口（对话/维护/CLI）都得生效。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=500")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass  # 只读盘/老文件格式等极端情况：退回默认，不让连接失败
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
    # ── 2026-11-03（批 B｜外部 AI 审阅 Q6）：双通道检索的两张辅助表 ──
    # memory_fts：字面通道。digest 必须先过 seg_for_fts 按字切开（unicode61 会把整串
    # 汉字当一个 token，"顾明"就永远搜不到——本机实测过）。rowid 手动对齐 memory.id。
    # entity_index：实体精确匹配通道——专名走这里，精度 100%，绕开中文分词。
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(digest, tokenize='unicode61')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entity_index ("
            " entity TEXT NOT NULL, memory_id INTEGER NOT NULL,"
            " PRIMARY KEY (entity, memory_id))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entity ON entity_index(entity)")
    except sqlite3.Error:
        pass  # 极老 sqlite 无 FTS5：字面/实体通道自动降级（向量通道仍工作）
    return conn


def _norm_root(root: Path | str) -> str:
    """项目根的规范化（同 workspace 闸的口径）：正斜杠、去尾斜杠、小写。"""
    return str(root or "").replace("\\", "/").rstrip("/").lower()


def _blob(vec) -> bytes:
    import numpy as np

    return np.asarray(vec, dtype="float32").tobytes()


def _unblob(blob: bytes):
    import numpy as np

    return np.frombuffer(blob, dtype="float32")


def _embed_one(text: str):
    """单条文本 → 单位向量（一维，形状 (d,)）。引擎不可用返回 None。"""
    from .study import semantic as _sem

    if not _sem.ready().get("可语义检索"):
        return None
    v = _sem.embed([text[:400]])
    if v is None:
        return None
    import numpy as np

    return np.asarray(v).reshape(-1)   # embed 返回 (1, d)，摊平成 (d,)


def _append_fact(root: Path, role: str, body: str, author_note: str) -> str:
    """把原文落到事实源，返回 anchor（label 串）。

    ALL_ROLES 内的角色走 scratchpad.append_labeled；
    领航员/书房伙伴不在名册里（主持人不是智库成员），但**它们的记忆同样要有
    事实源**——原文落到 外置大脑/速记本-<role>.md（同结构）。端到端实测踩到：
    没有落点的话，检索命中后原文是空的。
    """
    from . import scratchpad as _sp

    try:
        return _sp.append_labeled(role, root, body, author_note=author_note)
    except ValueError:
        import time as _t

        ts = _t.strftime("%Y-%m-%d %H:%M:%S")
        label = f"### [{ts}] {role}"
        if author_note:
            label += f" · {author_note}"
        base = Path(root) / "外置大脑"
        base.mkdir(parents=True, exist_ok=True)
        f = base / f"速记本-{role}.md"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(label + "\n\n" + body.strip() + "\n\n")
        return label


def _fact_by_anchor(root: Path, role: str, anchor: str) -> str:
    """按 anchor 回事实源取原文段落。取不到返回空串（调用方降级显示 anchor）。"""
    from . import scratchpad as _sp

    try:
        return _sp.find_by_anchor(root, role, anchor)
    except Exception:      # noqa: BLE001 - 回不了源不该让检索整个失败
        return ""


def remember(root: Path | str, role: str, book: str, digest: str,
             author_note: str = "") -> dict:
    """写一条：原文落速记本（事实源），摘要算向量入库（索引层）。

    返回 {"ok": True, "id": …, "anchor": …} 或 {"ok": False, "reason": …}。
    来源标注（author_note）是作者裁定的硬要求：作者原话 / 已拍板 / 随想 / 联想，
    检索时跟着给模型看，让它自己判断可信度。
    """
    body = str(digest or "").strip()
    if not body:
        return {"ok": False, "reason": "记忆内容是空的。"}
    vec = _embed_one(body)
    if vec is None:
        return {"ok": False, "reason": "语义引擎不可用，现在记不了（可以稍后再试）。"}
    from .request_turn import commit_effect
    with commit_effect():
        try:
            anchor = _append_fact(Path(root), role, body, author_note or "工作记录")
        except Exception as e:      # noqa: BLE001 - 事实源失败时不建立索引
            return {"ok": False, "reason": f"原文落不了速记本：{e}"}
        with _LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO memory (role, book, anchor, digest, vector, created)"
                    " VALUES (?,?,?,?,?,?)",
                    (role, _norm_root(book), anchor, body[:200], _blob(vec), int(time.time())),
                )
                conn.commit()
                mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                _index_side_tables(conn, mid, body, _known_entities_locked(conn))
                conn.commit()
            finally:
                conn.close()
    return {"ok": True, "id": mid, "anchor": anchor}


def _index_side_tables(conn, mid: int, digest: str, known: list[str] | None = None) -> None:
    """把一条记忆同步进字面通道（FTS5）与实体通道（entity_index）。

    ⚠️ **只吃调用方给的 conn，绝不自己开连接/拿 _LOCK**——2026-11-03 实测：
    初版在里面调 _known_entities()（它会 with _LOCK），而 remember 已经持着
    同一把非重入锁 ⇒ 进程直接死锁（测试跑了 5 分钟无输出）。

    known 由调用方传（同连接查出来的已知专名）；传 None 则退化成保守抽取
    （只认书名号/引号内），不会乱猜。失败一律吞掉：检索增强是加分项。
    """
    try:
        from . import memory_index as _mi

        seg = _mi.seg_for_fts(digest)
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (int(mid),))
        conn.execute("INSERT INTO memory_fts(rowid, digest) VALUES (?,?)", (int(mid), seg))
    except Exception:
        pass
    try:
        from . import memory_index as _mi

        for e in _mi.extract_entities(digest, known):
            conn.execute(
                "INSERT OR IGNORE INTO entity_index(entity, memory_id) VALUES (?,?)",
                (e, int(mid)),
            )
    except Exception:
        pass


def _known_entities_locked(conn, limit: int = 400) -> list[str]:
    """已知专名（实体索引里出现过的），**用调用方已持有的连接**。

    与 _known_entities() 的区别：这个不拿锁、不开连接，专供"已经持锁的写入路径"用。
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT entity FROM entity_index LIMIT ?", (limit,)
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _known_entities(limit: int = 400) -> list[str]:
    """已知专名（给检索侧用：抽 query 实体时需要它提高精度）。

    自己拿锁开连接 —— **只许在"没持锁"的路径调用**（recall/检索侧）。
    """
    try:
        with _LOCK:
            conn = _connect()
            try:
                return _known_entities_locked(conn, limit)
            finally:
                conn.close()
    except Exception:
        return []

def _rrf_fuse_ids(channels: list[list[int]], weights: list[float] | None = None,
                 k: int = 60) -> list[tuple[int, float]]:
    """RRF 融合（薄包装：memory_index.rrf_fuse 失败时退回按通道顺序拼接）。
    """
    try:
        from . import memory_index as _mi

        return _mi.rrf_fuse(channels, k=k, weights=weights)
    except Exception:
        seen: list[int] = []
        for ch in channels:
            for mid in ch:
                if mid not in seen:
                    seen.append(mid)
        return [(m, 0.0) for m in seen]

def recall(root: Path | str, query: str, book: str = "", k: int = 5,
           scope: str = "book") -> dict:
    try:
        return _recall(root, query, book=book, k=k, scope=scope)
    except Exception:
        channels = {name: {"status": "failed", "count": 0}
                    for name in ("vector", "keyword", "entity")}
        return _recall_result([], channels)


def _recall(root: Path | str, query: str, book: str = "", k: int = 5,
            scope: str = "book") -> dict:
    """检索：向量余弦 top-k，域过滤（本书 + 全局），相对阈值砍尾。

    scope⑤）：默认 "book"=本书+全局（织布机用，防书间污染）；
    "all"=跨全部书（书房三角色用，结果带 book 标注来源）。
    返回 {"ok": True, "条": [{anchor, digest, role, book, score, 原文}]}。
    原文按 anchor 回事实源取——摘要只是排序用的。
    """
    q = str(query or "").strip()
    channels = {name: {"status": "unavailable", "count": 0}
                for name in ("vector", "keyword", "entity")}
    if not q:
        return _recall_result([], channels, empty=True)
    try:
        vec = _embed_one(q)
    except Exception:
        vec = None
        channels["vector"]["status"] = "failed"
    norm = _norm_root(root)
    with _LOCK:
        conn = _connect()
        try:
            if scope == "all":
                # 跨书可读（书房三角色）：不加 book 过滤，结果带 book 标注来源
                rows = conn.execute(
                    "SELECT id, role, book, anchor, digest, vector FROM memory"
                    " WHERE superseded_by IS NULL",
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, role, book, anchor, digest, vector FROM memory"
                    " WHERE superseded_by IS NULL AND (book = ? OR book = '')",
                    (norm,),
                ).fetchall()
            # ── 2026-11-03（批 B）：三通道召回 ───────────────────────────────
            # ① 向量语义（宽召回，有 gate 砍尾）② FTS5 字面（专名/精确短语）
            # ③ 实体精确（人名地名，权重加倍）。三路各自 top-N 交给 RRF 融合。
            fts_ids: list[int] = []
            ent_ids: list[int] = []
            try:
                from . import memory_index as _mi

                fq = _mi.fts_query(q)
                if fq:
                    fts_ids = [
                        int(r[0])
                        for r in conn.execute(
                            "SELECT memory_fts.rowid FROM memory_fts JOIN memory m"
                            " ON m.id = memory_fts.rowid WHERE memory_fts MATCH ?"
                            " AND m.superseded_by IS NULL"
                            + ("" if scope == "all" else " AND (m.book = ? OR m.book = '')")
                            + " ORDER BY bm25(memory_fts), m.id LIMIT 15",
                            (fq,) if scope == "all" else (fq, norm),
                        ).fetchall()
                    ]
                if fq:
                    channels["keyword"] = {"status": "executed", "count": len(fts_ids)}
            except Exception:
                channels["keyword"]["status"] = "failed"
                fts_ids = []
            try:
                from . import memory_index as _mi

                ents = _mi.extract_entities(q, _known_entities_locked(conn))
                if ents:
                    ph = ",".join("?" * len(ents))
                    ent_ids = [
                        int(r[0])
                        for r in conn.execute(
                            "SELECT DISTINCT e.memory_id FROM entity_index e JOIN memory m"
                            " ON m.id = e.memory_id WHERE e.entity IN (%s)" % ph
                            + " AND m.superseded_by IS NULL"
                            + ("" if scope == "all" else " AND (m.book = ? OR m.book = '')")
                            + " ORDER BY e.memory_id LIMIT 15",
                            tuple(ents) if scope == "all" else (*ents, norm),
                        ).fetchall()
                    ]
                # Probe the real table even when entity extraction yields no terms.
                # No terms is an executed zero-hit query, not an engine failure.
                if not ents:
                    conn.execute("SELECT memory_id FROM entity_index WHERE 0").fetchall()
                channels["entity"] = {"status": "executed", "count": len(ent_ids)}
            except Exception:
                channels["entity"]["status"] = "failed"
                ent_ids = []
        finally:
            conn.close()
    # 向量通道：算完全表余弦，取过 gate 的 top-15（作为 RRF 的排名输入）
    vec_ids: list[int] = []
    vec_scores: dict[int, float] = {}
    if vec is not None:
        try:
            import numpy as np
            if rows:
                mat = np.stack([_unblob(r[5]) for r in rows])
                sims = mat @ vec
                if not np.isfinite(sims).all():
                    raise ValueError("non-finite similarity")
                order = sorted(range(len(rows)), key=lambda i: -float(sims[i]))
                top = float(sims[order[0]]) if order else 0.0
                gate = max(RECALL_FLOOR, top * RECALL_RATIO)
                for i in order:
                    if float(sims[i]) < gate:
                        break
                    mid = int(rows[i][0])
                    vec_ids.append(mid)
                    vec_scores[mid] = float(sims[i])
                    if len(vec_ids) >= 15:
                        break
            channels["vector"] = {"status": "executed", "count": len(vec_ids)}
        except Exception:
            channels["vector"]["status"] = "failed"
            vec_ids, vec_scores = [], {}

    # RRF 融合：字面/实体通道不受语义 gate 约束（它们是精确命中，不该被阈值砍掉）
    weights = [1.0, 1.0, 2.0]   # 实体通道精度最高 → 权重加倍
    fused = _rrf_fuse_ids([vec_ids, fts_ids, ent_ids], weights=weights)
    if not fused:
        return _recall_result([], channels)

    root_p = Path(root)
    rows_by_id = {int(r[0]): r for r in rows}
    out = []
    for mid, rrf_score in fused:
        r = rows_by_id.get(mid)
        if r is None:
            continue   # 跨 scope 之外的条目（不该出现，防御性跳过）
        item = {"anchor": r[3], "digest": r[4], "role": r[1],
                "book": r[2], "score": round(vec_scores.get(mid, 0.0), 3),
                "rrf": round(rrf_score, 4)}
        body = _fact_by_anchor(Path(r[2]) if r[2] else root_p, r[1], r[3])
        if body:
            item["原文"] = body
        out.append(item)
        if len(out) >= max(1, k):
            break
    return _recall_result(out, channels)


def health(root: Path | str = "") -> dict:
    """设置栏记忆库面板的**真实**读数（T7）。

    规矩（用户裁定）：**不许显示虚构的 0**。某值拿不到 / 本版没有对应字段 ⇒ None，
    并在 unsupported 里点名原因。本版表里没有"置顶"字段，"软删除"也不是本表概念
    （superseded_by 是**过时**，语义不同，另列「过时」）。
    """
    out = {"总条数": None, "已向量化": None, "维度": None, "过时": None,
           "置顶": None, "软删除": None, "范围": {"本书": None, "全局": None},
           "unsupported": {"置顶": "当前表无置顶字段",
                           "软删除": "当前表无软删除字段（过时≠删除，另列「过时」）"},
           "错误": ""}
    try:
        norm = _norm_root(root) if str(root or "").strip() else ""
        with _LOCK:
            conn = _connect()
            try:
                out["总条数"] = conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE superseded_by IS NULL").fetchone()[0]
                out["已向量化"] = conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE superseded_by IS NULL"
                    " AND length(vector) > 0").fetchone()[0]
                row = conn.execute(
                    "SELECT length(vector) FROM memory WHERE length(vector) > 0 LIMIT 1").fetchone()
                # 向量是 float32 裸字节：维度 = 字节数 / 4。取不到给 None，不编 0。
                out["维度"] = (int(row[0]) // 4) if row and row[0] else None
                out["过时"] = conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE superseded_by IS NOT NULL").fetchone()[0]
                out["范围"]["全局"] = conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE superseded_by IS NULL AND book = ''"
                ).fetchone()[0]
                if norm:
                    out["范围"]["本书"] = conn.execute(
                        "SELECT COUNT(*) FROM memory WHERE superseded_by IS NULL AND book = ?",
                        (norm,)).fetchone()[0]
            finally:
                conn.close()
    except Exception as e:      # noqa: BLE001 - 面板读数失败要能看见原因，但不能崩
        out["错误"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    try:
        from .study import semantic as _sem
        rdy = _sem.ready()
        out["引擎"] = {"可用": bool(rdy.get("可语义检索")), "缺": list(rdy.get("缺") or []),
                       "last_err": str(rdy.get("last_err") or ""),
                       "已加载": bool(getattr(_sem, "_SESS", None) is not None),
                       "形态": "进程内 ONNX"}
    except Exception as e:      # noqa: BLE001
        out["引擎"] = {"可用": None, "缺": None, "last_err": "%s: %s" % (type(e).__name__, e),
                       "已加载": None, "形态": "进程内 ONNX"}
    return out


def recover() -> dict:
    """手动「修复」：重新加载向量模型 + 重新探测依赖。绝不抛。"""
    try:
        from .study import semantic as _sem
        got = _sem.reload()
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "可用": False, "attempted": False, "loaded": False,
                "last_err": "%s: %s" % (type(e).__name__, str(e)[:200])}
    return {"ok": bool(got.get("loaded")), "可用": bool(got.get("可语义检索")),
            "attempted": bool(got.get("attempted")), "loaded": bool(got.get("loaded")),
            "缺": got.get("缺") or [], "last_err": got.get("last_err") or ""}


def _recall_result(rows, channels, *, empty=False):
    vector_ok = channels["vector"]["status"] == "executed"
    keyword_ok = any(channels[c]["status"] == "executed" for c in ("keyword", "entity"))
    mode = "empty_query" if empty else "hybrid" if vector_ok else "keyword" if keyword_ok else "unavailable"
    degraded = not empty and not vector_ok
    reason = "" if not degraded else "vector_unavailable" if keyword_ok else "retrieval_unavailable"
    notice = ("向量记忆不可用，已关键词兜底，请检查设置。结果只覆盖字面或实体命中，零命中不代表事实不存在。"
              if mode == "keyword" else
              "记忆检索暂不可用，关键词兜底也未完成，请检查设置；不能据此判断没有相关事实。"
              if mode == "unavailable" else "")
    result = {"ok": mode != "unavailable", "条": rows, "mode": mode,
              "degraded": degraded, "reason_code": reason, "channels": channels, "notice": notice}
    if not result["ok"]:
        result["reason"] = notice
    return result


def retrieval_event(result: dict, *, scope: str, origin: str = "automatic") -> dict:
    """Same result feeds model and stream; no global state or cross-turn dedupe."""
    return {"t": "memory_retrieval", "origin": origin, "scope": scope,
            "mode": result.get("mode", "unavailable"),
            "degraded": bool(result.get("degraded")),
            "reason_code": result.get("reason_code", ""),
            "notice": result.get("notice", ""), "count": len(result.get("条") or []),
            "channels": result.get("channels", {})}


def forget(root: Path | str, mid: int, by: str) -> bool:
    """软删（superseded_by 记触发者）。**只归书房伙伴与领航员**。"""
    if by not in ("书房伙伴", "领航员"):
        return False
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE memory SET superseded_by = ? WHERE id = ? AND superseded_by IS NULL",
                (f"{by}@{int(time.time())}", int(mid)),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def _fingerprint(root: Path | str) -> dict:
    """速记本文件的 mtime+大小 指纹——作者手改过的判据。"""
    out = {}
    base = Path(root) / "外置大脑"
    if not base.is_dir():
        return out
    for f in sorted(base.glob("速记本-*.md")):
        st = f.stat()
        out[f.name] = f"{int(st.st_mtime)}|{st.st_size}"
    return out


def stale(root: Path | str) -> bool:
    """速记本与上次重建相比有没有变（变了就该提示重建）。"""
    fp = _fingerprint(root)
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT digest FROM memory WHERE id = (SELECT MAX(id) FROM memory)"
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return bool(fp)   # 库空而速记本有内容 → 需要重建
    try:
        saved = json.loads(row[0])
    except Exception:
        return True
    return saved != fp


def rebuild(root: Path | str) -> dict:
    """把速记本里已有条目全部重建进索引（作者手改后可点重建）。

    每条独立事务（先删同 anchor 旧条目，再插），中断最多停在中途、库不脏。
    领航员/书房伙伴的速记本也在 glob 里（速记本-*.md 通配），一并收。
    """
    from .study import semantic as _sem

    root_p = Path(root)
    base = root_p / "外置大脑"
    if not base.is_dir():
        return {"ok": True, "建了": 0}
    import re

    vecs, metas = [], []
    for f in sorted(base.glob("速记本-*.md")):
        role = f.stem.replace("速记本-", "")
        text = f.read_text(encoding="utf-8", errors="replace")
        for piece in re.split(r"(?=^### )", text, flags=re.M):
            if not piece.strip():
                continue
            first = piece.split("\n")[0].strip()
            body = "\n".join(piece.split("\n")[1:]).strip()
            if len(body) < 8:
                continue
            vecs.append(body[:400])
            metas.append((role, first, body[:200]))
    if not vecs:
        return {"ok": True, "建了": 0}
    if not _sem.ready().get("可语义检索"):
        return {"ok": False, "reason": "语义引擎不可用"}
    vs = _sem.embed(vecs)
    if vs is None:
        return {"ok": False, "reason": "向量算不出来"}
    norm = _norm_root(root_p)
    fp = json.dumps(_fingerprint(root_p), ensure_ascii=False)
    with _LOCK:
        conn = _connect()
        try:
            for (role, anchor, digest), v in zip(metas, vs):
                conn.execute(
                    "DELETE FROM memory WHERE anchor = ? AND book = ?",
                    (anchor, norm),
                )
                conn.execute(
                    "INSERT INTO memory (role, book, anchor, digest, vector, created)"
                    " VALUES (?,?,?,?,?,?)",
                    (role, norm, anchor, digest, _blob(v), int(time.time())),
                )
            conn.execute(
                "INSERT INTO memory (role, book, anchor, digest, vector, created)"
                " VALUES (?,?,?,?,?,?)",
                ("系统", norm, "__fingerprint__", fp, _blob(vs[0]), int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "建了": len(metas)}
