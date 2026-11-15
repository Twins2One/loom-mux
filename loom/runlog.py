"""运行账本(R12):逐棒的 token / 耗时 / 失败原因 / 重试链。

**为什么存在**:一章写完,作者只关心三件事——花了多少、哪一棒失败、重试了几次。
这三件事现在在 Loom 里全查不到(账本 `.原稿/*.ledger.json` 只记签名与产物,不记开销)。

**落盘形态**:`正文/.运行账本/第N章.jsonl`,一行一次调用(追加写,坏行跳过)。
隐藏目录 + 可丢:删了只是看不到历史开销,书一个字不动。

**诚实条款**:`tokens` 只在后端**真的回报了 usage** 时记实数;没回报就是 `None`。
**绝不按字数估一个假 token 数**——估出来的数字一旦被当真,后面的成本决策全歪。

> 2026-10-20 交叉审查纠正:旧版把 `tokens` 写死成 `None`,注释说"后端不回报 usage"。
> 这是**过期论断**:`OpenAICompatBackend.last_usage` 早已从响应里取到
> `prompt_tokens/completion_tokens/total_tokens`(非流式与流式末帧都取),只是没往账本递。
> 数据拿到了却扔掉,导致"这一棒是不是被 max_tokens 腰斩"这类问题**无法事后审计**。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import threading

from . import paths
from .fsutil import atomic_write_text

LOG_DIR = f"{paths.BODY_DIR}/.运行账本"

# 2026-10-25(议事例会):原来 record 是「读全文 → 追加一行 → 整篇写回」。
# 多个角色并发跑时两条线程会各自读到旧全文,后写的那份把前一份的新行**整行吃掉**。
# 这里加一把进程内锁,把这段读-改-写串起来;跨进程(两个 Loom)另有写锁管,不掺和。
_WRITE_LOCK = threading.Lock()
MAX_LINES = 400          # 单章上限:超过就丢最旧的(这是开销流水,不是审计日志)

_FIELDS = ("role", "purpose", "provider", "model", "ms", "chars_in", "chars_out",
           "ok", "error", "attempt", "at")


def _path(root: Path | str, n: int) -> Path:
    return Path(root) / LOG_DIR / f"第{n}章.jsonl"


def record(root: Path | str, chapter: int, *, role: str, ok: bool, ms: int,
           chars_in: int = 0, chars_out: int = 0, provider: str = "", model: str = "",
           purpose: str = "", error: str = "", attempt: int = 0,
           tokens: dict | None = None, finish: str = "") -> dict:
    """记一次调用。**只追加,不改已有行**;超过 MAX_LINES 丢最旧的。

    `tokens`:后端回报的 usage(取不到就 None,不估)。
    `finish`:该次调用的 finish_reason(``length`` = 被截断),审计"腰斩"用。
    """
    row = {"role": role, "purpose": purpose or role, "provider": provider, "model": model,
           "ms": int(ms), "chars_in": int(chars_in), "chars_out": int(chars_out),
           "ok": bool(ok), "error": (error or "")[:300], "attempt": int(attempt),
           "tokens": tokens if isinstance(tokens, dict) else None,   # 见模块头:取不到不编
           "finish": (finish or "")[:20],
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = _path(root, chapter)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:                       # 并发写同一章的流水:读-改-写必须串起来
        lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
        lines.append(json.dumps(row, ensure_ascii=False))
        if len(lines) > MAX_LINES:
            lines = lines[-MAX_LINES:]
        atomic_write_text(p, "\n".join(lines) + "\n")
    return row


def entries(root: Path | str, chapter: int) -> list[dict]:
    """读回这一章的调用流水。坏行跳过(手改坏了不该让界面崩)。"""
    p = _path(root, chapter)
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _int(v) -> int:
    """宽容取整:手改坏的流水行(ms 写成「不是数字」)不该让整张卡炸掉。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def card(root: Path | str, chapter: int) -> dict:
    """本章运行卡:逐棒聚合 + 合计 + 失败原因。"""
    rows = entries(root, chapter)
    by_role: dict[str, dict] = {}
    for r in rows:
        role = r.get("role") or "?"
        g = by_role.setdefault(role, {"棒": role, "调用": 0, "成功": 0, "失败": 0,
                                      "重试": 0, "耗时ms": 0, "输出字数": 0, "失败原因": []})
        g["调用"] += 1
        g["耗时ms"] += _int(r.get("ms"))
        g["输出字数"] += _int(r.get("chars_out"))
        if r.get("ok"):
            g["成功"] += 1
        else:
            g["失败"] += 1
            if r.get("error"):
                g["失败原因"].append(r["error"])
    for g in by_role.values():
        g["重试"] = max(0, g["调用"] - 1)     # 同一棒第 1 次不算重试
        g["失败原因"] = list(dict.fromkeys(g["失败原因"]))[:3]
    # token 只在真有回报的调用上求和;有一次没回报就标"不完整",别把缺数当零
    tok_rows = [r.get("tokens") for r in rows]
    tok_have = [t for t in tok_rows if isinstance(t, dict)]
    tok_total = sum(_int(t.get("total_tokens")) for t in tok_have) if tok_have else None
    tok_partial = len(tok_have) < len(tok_rows)
    return {
        "章": chapter,
        "调用次数": len(rows),
        "总耗时ms": sum(_int(r.get("ms")) for r in rows),
        "总输出字数": sum(_int(r.get("chars_out")) for r in rows),
        "失败次数": sum(0 if r.get("ok") else 1 for r in rows),
        "截断次数": sum(1 for r in rows if (r.get("finish") or "") == "length"),
        "逐棒": list(by_role.values()),
        "tokens": tok_total,
        "tokens说明": (
            "后端回报了 usage,这里按实际值合计。" if tok_have and not tok_partial else
            "部分调用后端没回报 usage,合计不完整(缺的按缺算,不当 0)。" if tok_have else
            "后端没有回报 usage(有些本地端点不给),所以这里没有 token 数;不按字数估。"),
        "账本": str(LOG_DIR) + f"/第{chapter}章.jsonl",
    }
