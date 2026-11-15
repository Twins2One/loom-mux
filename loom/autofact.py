"""自动沉淀（2026-11-03 批 B 二阶段）：写完一章后，把终稿里的关键事实抽进记忆库。

**设计**：
- **规则抽取零成本**：`atomize` 拆条（句号切→逗号攒段→补主语）+ 实体抽取
  （书名号/引号/已知专名）——终稿正文里有实体的原子句就是候选事实。
- **上限与过滤**：每章 ≤5 条；每条 30~80 字；含至少一个已知实体；
  排除对话体（引号开头的句子多是台词，不是事实）。
- **隔离**：只写当前书的记忆（book=项目根）；跨书只读边界不变。
- **异步不阻塞**：在 daemon 线程里跑——织章已经花了几分钟，沉淀晚几秒没关系，
  但绝不能拖慢 chapter_done 给前端的信号。
- **静默**：任何异常吞掉。自动沉淀是加分项，失败不影响正文与账本。

**挂点**：`_finish_chapter` 的终稿落盘之后（收尾流程尾部）。
"""

from __future__ import annotations

import re
import threading


def _candidate_facts(final_text: str, known_entities: list[str]) -> list[str]:
    """从终稿抽候选事实句：有实体的原子句，排除台词。"""
    from .memory_index import atomize, extract_entities

    body = str(final_text or "")
    # 去掉 markdown 符号（正文里的 # * > 等）
    body = re.sub(r"[#>*_`]", "", body)
    atoms = atomize(body, known_entities)

    known = set(known_entities)
    # 动态实体：正文里的书名号/引号专名也入候选
    known.update(extract_entities(body))

    out = []
    for a in atoms:
        if len(a) < 15 or len(a) > 90:
            continue
        # 含至少一个已知实体
        if not any(e in a for e in known):
            continue
        # 排除台词（引号开头/占比高的句子）
        if a.strip().startswith(("“", '"', "「")):
            continue
        quotes = sum(1 for ch in a if ch in "“”\"「」")
        if quotes >= 4:
            continue
        out.append(a)
        if len(out) >= 8:   # 候选上限（后面再裁 5 条）
            break
    return out


def _pick_facts(candidates: list[str], limit: int = 5) -> list[str]:
    """从候选里挑 limit 条：优先长句（信息量大），打散实体（避免全是同一个人的事）。"""
    ranked = sorted(candidates, key=len, reverse=True)
    picked: list[str] = []
    used_ents: set[str] = set()
    for c in ranked:
        if len(picked) >= limit:
            break
        # 简单打散：前 limit//2 条不管，后面的要求引入新实体（近似）
        if len(picked) >= limit // 2:
            # 不做精确实体判重（成本高），只做文本相似粗筛
            if any(_overlap(c, p) > 0.6 for p in picked):
                continue
        picked.append(c)
        used_ents.update(extract_known(c))
    return picked


def extract_known(text: str) -> set[str]:
    """轻量实体识别（书名号/引号内 + 2-4 字汉字专名候选，滤常见词）。"""
    from .memory_index import extract_entities

    return set(extract_entities(text))


def _overlap(a: str, b: str) -> float:
    """两串的字符级重合度（0~1）。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def sediment(project_root: Path, chapter_n: int, final_text: str,
             known_entities: list[str] | None = None, limit: int = 5) -> dict:
    """把一章的终稿事实沉淀进记忆库（线程目标函数——在 daemon 线程里跑）。

    返回统计 dict（也写日志）。**绝不抛异常**——沉淀失败不影响正文与账本。
    """
    from . import memory as _m
    from .memory_index import extract_entities as _ee

    stats = {"章": chapter_n, "候选": 0, "记了": 0, "失败": ""}
    try:
        known = list(known_entities or [])
        known.extend(_ee(final_text))
        cands = _candidate_facts(final_text, known)
        stats["候选"] = len(cands)
        if not cands:
            return stats
        facts = _pick_facts(cands, limit=limit)
        for i, fact in enumerate(facts):
            src = f"自动沉淀/第{chapter_n}章/{i+1}"
            r = _m.remember(project_root, "自动沉淀", str(project_root), fact, src)
            if r.get("ok"):
                stats["记了"] += 1
                # 事实里的实体写进 entity_index（三通道的实体精确匹配要用）
                try:
                    mid = int(r["id"])
                    for e in _ee(fact, known):
                        with _m._LOCK:
                            conn = _m._connect()
                            try:
                                conn.execute(
                                    "INSERT OR IGNORE INTO entity_index(entity, memory_id) VALUES (?,?)",
                                    (e, mid),
                                )
                                conn.commit()
                            finally:
                                conn.close()
                except Exception:
                    pass
        # 记忆条目也带实体 → entity_index 自动跟上（remember 内部会同步）
    except Exception as e:  # noqa: BLE001
        stats["失败"] = str(e)[:150]
    return stats


def sediment_async(project_root: Path, chapter_n: int, final_text: str,
                   known_entities: list[str] | None = None, limit: int = 5,
                   on_done=None) -> threading.Thread:
    """异步版：起 daemon 线程跑 sediment，立刻返回线程对象（调用方不等待）。

    用户裁定 2026-11-12（静默失败缺陷登记 第2条）：`sediment` 把失败原因写进
    `stats["失败"]` 之后原来没有任何人收集——daemon 线程里的失败既无日志也无痕迹。
    现在把 `stats` 交给调用方的 `on_done(stats)`，由调用方落成可追溯留痕。
    回调自身出错也不许反过来炸掉沉淀线程（沉淀仍是加分项）。
    """
    def _run() -> None:
        stats = sediment(project_root, chapter_n, final_text, known_entities, limit)
        if on_done is None:
            return
        try:
            on_done(stats)
        except Exception:   # noqa: BLE001 - 留痕失败不能把沉淀线程带崩
            pass

    t = threading.Thread(
        target=_run,
        name=f"loom-sediment-ch{chapter_n}",
        daemon=True,
    )
    t.start()
    return t
