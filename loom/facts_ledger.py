"""结构化事实账本（T3 · 用户 2026-11-11 裁定）。

与两套既有东西**分开**（混用会污染语义）：
- 不塞设定集 md：那里是散文式静态事实，机器不可比对；
- 不塞 memory.sqlite：记忆库管**召回**（模糊、按相似度），本账本管**比对**（精确、按实体+属性）。

落点：`外置大脑/设定集/结构化事实.jsonl`，每本书一个文件（书间隔离）。

条目形状：{实体, 属性, 取值, 状态(确认|草案), 来源, 更新时间}；比对键 = 归一化后的 (实体, 属性)。

**只承诺"可保证"的部分**：同实体同属性不同取值 → 报冲突。需要推理才成立的矛盾不报。
**不阻塞写作**：冲突只提示；抽取/落盘任何异常都不上抛（与"记忆是加分项"同一纪律），
但**一律打印一行原因到 stderr**——静默吞掉会让"抽取恒 0 条"和"正文没有事实"分不开。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

# 比对只认这两个状态；任何其它值一律降级为「草案」（宁可少报，不可谎报确认）
STATUSES = ("确认", "草案")

_MAX_ENTRIES_PER_CALL = 40      # 单次抽取上限：账本是比对用，不是全文摘录
_MAX_TEXT = 6000               # 送模型抽取的正文上限（与书房注入同量级）
_FIELD_MAX = 200               # 单字段长度上限（防一条脏数据把 jsonl 撑坏）


def _p(root: Path | str) -> Path:
    from . import paths

    return Path(root) / paths.SET_DIR / "结构化事实.jsonl"


def _norm(s: str) -> str:
    """比对键归一化：去首尾空白、全角空格、大小写、书名号/引号。

    只做**确定可做**的归一，不做同义词归并——"顾明"与"顾 明"算同一实体，
    "顾明"与"小明"不算（那需要推理，属于本账本明确不承诺的部分）。
    """
    t = str(s or "").strip().replace("\u3000", " ")
    t = re.sub(r"\s+", "", t)
    return t.strip("《》〈〉「」『』\"'“”‘’").casefold()


def _clean(value, limit: int = _FIELD_MAX) -> str:
    return str(value if value is not None else "").strip()[:limit]


def normalize_entry(raw: dict, *, status: str = "草案", source: str = "",
                    now: str = "") -> dict | None:
    """把一条原始条目收成账本形状；缺实体或属性 → None（不记半条）。"""
    if not isinstance(raw, dict):
        return None
    ent = _clean(raw.get("实体") if "实体" in raw else raw.get("entity"))
    attr = _clean(raw.get("属性") if "属性" in raw else raw.get("attribute"))
    val = _clean(raw.get("取值") if "取值" in raw else raw.get("value"))
    if not ent or not attr or not val:
        return None
    st = _clean(raw.get("状态")) or status
    if st not in STATUSES:
        st = "草案"
    return {"实体": ent, "属性": attr, "取值": val, "状态": st,
            "来源": _clean(raw.get("来源")) or _clean(source),
            "更新时间": _clean(raw.get("更新时间")) or (now or _now())}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_with_stats(root: Path | str) -> dict:
    """读全部条目，并**如实报出坏行数**（用户 2026-11-11 裁定 P2）。

    坏行逐行跳过、绝不因一行坏数据把整本账读空；同时把坏行数交回给调用方，
    免得"有 3 行读不成"和"账本是空的"看起来一样。

    返回 {"条目": [...], "坏行": n, "读取失败": "原因或空串"}。
    """
    f = _p(root)
    out: list[dict] = []
    bad = 0
    if not f.exists():
        return {"条目": out, "坏行": 0, "读取失败": ""}      # 还没建账本：正常的"空"
    if not f.is_file():
        # 账本位置被同名的目录/坏链接占住：这是**读不了**，不是"没写"。
        # 两者必须在报告里长得不一样，否则作者查不出事实为什么没进来。
        msg = f"账本位置不是可读文件（{type(f).__name__}占位）"
        print(f"[事实账本] 读取失败 {f}：{msg}", file=sys.stderr, flush=True)
        return {"条目": out, "坏行": 0, "读取失败": msg}
    try:
        raw = f.read_text(encoding="utf-8")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[事实账本] 读取失败 {f}：{msg}", file=sys.stderr, flush=True)
        return {"条目": out, "坏行": 0, "读取失败": msg}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            bad += 1        # 语法坏行（半行/手改坏）：跳过并计数
            continue
        if not isinstance(item, dict):
            bad += 1        # 非对象行（如 []、字符串）：跳过并计数
            continue
        got = normalize_entry(item, now=str(item.get("更新时间") or ""))
        if got:
            out.append(got)
        else:
            bad += 1        # 是对象但缺实体/属性/取值：算"读不成事实"的一行
    if bad:
        print(f"[事实账本] {f} 有 {bad} 行读不成事实、已跳过（其余 {len(out)} 行正常）",
              file=sys.stderr, flush=True)
    return {"条目": out, "坏行": bad, "读取失败": ""}


def load(root: Path | str) -> list[dict]:
    """读全部条目；坏行跳过，保留同一账本内其它有效行。"""
    return load_with_stats(root)["条目"]


def compare(existing: list[dict], news: list[dict]) -> list[dict]:
    """同 (实体,属性) 不同取值 → 冲突。**不**做需要推理的矛盾。

    状态组合分开标记：草案vs草案 / 草案vs确认 / 确认vs确认——三者严重程度不同，
    混成一句"有冲突"会让"草案碰草案"和"两条确认打架"看起来一样严重。
    """
    index: dict[tuple[str, str], list[dict]] = {}
    for e in existing:
        index.setdefault((_norm(e["实体"]), _norm(e["属性"])), []).append(e)
    out: list[dict] = []
    for n in news:
        for old in index.get((_norm(n["实体"]), _norm(n["属性"])), []):
            if _norm(old["取值"]) == _norm(n["取值"]):
                continue    # 同值不是冲突（重复登记）
            pair = f"{old['状态']}vs{n['状态']}"
            out.append({"实体": n["实体"], "属性": n["属性"], "类型": pair,
                        "既有": {"取值": old["取值"], "状态": old["状态"], "来源": old["来源"]},
                        "新的": {"取值": n["取值"], "状态": n["状态"], "来源": n["来源"]}})
    return out


def record(root: Path | str, entries: list[dict]) -> int:
    """追加条目；完全重复（键+取值+状态）不重复写。返回实际写入条数。

    失败不抛：账本是加分项，写不进去不能挡住正文。
    """
    try:
        entries = [e for e in (entries or []) if e]
        if not entries:
            return 0
        have = {( _norm(e["实体"]), _norm(e["属性"]), _norm(e["取值"]), e["状态"]) for e in load(root)}
        todo = []
        for e in entries:
            k = (_norm(e["实体"]), _norm(e["属性"]), _norm(e["取值"]), e["状态"])
            if k in have:
                continue
            have.add(k)
            todo.append(e)
        if not todo:
            return 0
        f = _p(root)
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            for e in todo:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        return len(todo)
    except Exception as e:
        print(f"[事实账本] 写入失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 0


_PROMPT = (
    "从下面这段文字里抽取**可核对的结构化事实**，只输出 JSON 数组，不要解释。\n"
    "每条：{{\"实体\":\"人物/地点/物品/规则的专名\",\"属性\":\"短属性名\",\"取值\":\"具体取值\"}}。\n"
    "只抽原文写明的事实；推测、比喻、修辞、对白里的情绪不算。原文没写的不许补。\n"
    "抽取上限 {n} 条。没有可抽的就输出 []。\n\n【正文】\n{text}"
)


def extract_entries(text: str, backend=None, *, limit: int = _MAX_ENTRIES_PER_CALL) -> list[dict]:
    """AI 自行抽取（用户裁定：录入靠 AI 抽取，不靠作者手填）。

    仍不抛（绝不影响调用方落盘），但**失败一律留痕到 stderr**——
    「抽取恒 0 条」和「正文本来就没有事实」看起来一样，不留痕就没人会发现链路断了。
    """
    body = str(text or "").strip()
    if not body:
        return []
    if backend is None:
        print("[事实账本] 未提供抽取后端，跳过 AI 抽取（0 条）", file=sys.stderr, flush=True)
        return []
    try:
        prompt = _PROMPT.format(n=limit, text=body[:_MAX_TEXT])
    except Exception as e:
        # 模板里含 JSON 花括号，未转义时 str.format 会抛 KeyError 并让抽取恒 0（P1-a）
        print(f"[事实账本] 抽取模板渲染失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return []
    try:
        raw = backend.complete("你是事实抽取器，只输出 JSON 数组。", prompt)
    except Exception as e:
        print(f"[事实账本] 抽取调用失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return []
    return parse_entries(raw, limit=limit)


def parse_entries(raw, *, limit: int = _MAX_ENTRIES_PER_CALL) -> list[dict]:
    """从模型输出里抠 JSON 数组（容忍 ```json 围栏与前后废话）。"""
    s = str(raw or "")
    if not s.strip():
        return []
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data[:limit]:
        got = normalize_entry(item)
        if got:
            out.append(got)
    return out


def check(root: Path | str, *, text: str = "", backend=None, source: str = "",
          status: str = "草案", entries: list[dict] | None = None) -> dict:
    """一个比对时点的统一动作：抽取（或直接用现成条目）→ 比对 → 登记。

    返回 {"冲突": [...], "登记": n, "抽取": n}；**任何异常都吞掉**，返回空结果。
    """
    try:
        if entries is None:
            entries = extract_entries(text, backend)
        entries = [normalize_entry(e, status=status, source=source) for e in entries or []]
        entries = [e for e in entries if e]
        conflicts = compare(load(root), entries) if entries else []
        added = record(root, entries) if entries else 0
        return {"冲突": conflicts, "登记": added, "抽取": len(entries)}
    except Exception as e:
        print(f"[事实账本] 比对/登记失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return {"冲突": [], "登记": 0, "抽取": 0}


def conflict_lines(conflicts: list[dict], limit: int = 5) -> list[str]:
    """把冲突翻成人话（给事件/UI 用）。只提示，不带"必须改"的口吻。"""
    out = []
    for c in (conflicts or [])[:limit]:
        out.append(f"{c['实体']}·{c['属性']}：既有「{c['既有']['取值']}」（{c['既有']['状态']}）"
                   f"与新的「{c['新的']['取值']}」（{c['新的']['状态']}）不一致")
    return out
