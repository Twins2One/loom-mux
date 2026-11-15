# -*- coding: utf-8 -*-
r"""作者智库角色的一对一对谈（含候选卡）。

两条路径之一：**我亲自点开某个角色，一对一聊**。
另一条是领航员路由（/api/navigator/suggest，只给建议不开会话）。

## 落点怎么来（丁方案，交叉审查定）

三次真机实测证明：本地 35B **能把整数参数写对**（玩具工具 add(a,b) 两次全对），
但只要参数是「要从上下文精确复制的长标识符」（如 `外置大脑/设定集/10-世界观/金手指.md#核心功能`），
它就会漏、会编、会写成 XML。**落点是簿记，不是创作。**

所以：**模型不再生成落点**。落点由会话绑定（`locked_slot` 参数）：
- 有 locked_slot → schema 里 `提设定` 只暴露 `内容` 一个参数，落点由服务端补；
- 没有 locked_slot（作者没指定格子就开聊）→ 走旧的自由模式，模型自己填落点。

不做「系统按关键词猜落点」的兜底——**猜错是哑失败**：卡片照样漂亮，作者注意力在内容上，
不会发现写错了地方。宁可让作者先点选。

## 工具调用两条路

1. **function calling（首选）**：后端有 `complete_with_tools` 时走它。
2. **文本协议（兜底）**：`TOOL:` + ```loom 围栏。

## 熔断

同一个工具、同一个失败原因，连续 2 次即停——真机见过 8 连漏，模型还会产出「我逐槽位补上」的
假道歉，污染后续推理。

## 落盘
对话记录 `.伙伴对话/agent-<角色>.jsonl`（人可读可删）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import collab, deliberate, partner_tools, proactive, role_contracts, scratchpad
from .agents import load_agent
from .parse import parse_tool_blocks, strip_protocol_lines, stream_line_relay
from .request_turn import local_effect

AGENT_CHAT_DIR = ".伙伴对话"
_MAX_TOOLS_PER_MSG = 5    # 一条消息最多几个工具调用（= 一次能出几张候选卡；1-5 可选）
_MAX_TOOL_ROUNDS = 6      # 本轮最多几轮工具调用（智库八人放宽到 deliberate.AGENT_TOOL_ROUNDS）
# 熔断阈值分两档（审查跟进项②）：
# - 锁定落点（丁方案）：落点参数不存在，没有「换个落点再试」的纠错路径，2 次就够；
# - 自由模式：模型可能第一次落点猜错、第二次改对，2 次太紧会误杀，放宽到 3。
_MAX_SAME_FAIL = 2        # 锁定模式
_MAX_SAME_FAIL_FREE = 3   # 自由模式（无 locked_slot）

# 「这一轮到底有没有往前走」的判据(要求"智库更主动")。
# 命中即认为它给了作者能接着走的东西:写了「下一步:…」,或者问出了一个问题。
# 两条都没有 = 只总结了一句 → 当场催一次(只催一次,不叠加轮数)。
_NEXT_RE = re.compile(r"下一步\s*[：:]\s*(.+)")
_QUESTION_RE = re.compile(r"[？?]|要不要|是否|哪个|哪一种|哪一格|你(想|希望|觉得|打算|更倾向)")


def _actionable(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and bool(_NEXT_RE.search(t) or _QUESTION_RE.search(t))


# ── 界面状态标记不是作者的话（2026-11-12 用户报的真实缺陷）────────────────────
# 现象：作者把角色给的候选卡整块复制到输入框、改几个字再发回来，卡片上那句
#   「✓ 已落库 → 外置大脑/设定集/00-立项.md」
# 跟着进了正文 ⇒ 模型读到「已落库」，就以为这条**已经落进设定集了**，
# 于是把作者改过的内容当成"已确认"的东西，不再当新提案复核。
#
# 为什么必须在后端剥、不能只靠前端不显示：
#   ① 前端可以不画它（已同时做），但作者从历史记录、从上次的聊天里复制照样会带上；
#   ② 更根本的原因是**这句话从来不是作者写的**，它是界面画的状态。
#      「作者说的话」和「界面画的状态」混在同一个字符串里送进模型，是这条链路上的结构错误。
# 空白类：半角空格/Tab **加全角空格 U+3000**。真实界面文本里全角空格很常见
# （「✓ 已落库　→　路径」一测就露——只写 [ \t] 会漏，这条是实测踩到的）。
_SP = r"[ \t\u3000]*"
_UI_MARK_PATTERNS = (
    # 完整形态：✓ 已落库 → 路径 ／ 已落库 → 路径 ／ 已落库: 路径
    # **必须把行尾换行一起吃掉**：只删行内容会留下一整行空白，
    # 作者看到的是一堆空行（实测踩到，三条用例当场红）。
    re.compile(r"(?m)^" + _SP + r"[✓✔]?" + _SP + r"已落库" + _SP
               + r"(?:→|->|=>|－>|：|:).*(?:\r?\n|$)"),
    # 行内夹带：（已落库）／(已落库)
    re.compile(r"[（(]" + _SP + r"已落库" + _SP + r"[）)]"),
    # 单独一行的 ✓ 或 ✔（前端"完成"标记常独立成行）
    re.compile(r"(?m)^" + _SP + r"[✓✔]" + _SP + r"(?:\r?\n|$)"),
)


def strip_ui_marks(text: str) -> tuple[str, int]:
    """剥掉界面状态标记，返回 (干净文本, 剥掉几处)。

    只剥**有明确标记形状**的行：`已落库 → …` / `（已落库）` / 单独一个 ✓。
    普通行里出现「已落库」三个字（作者自己在讨论它）不动——那可能是他真正想说的话。
    """
    s = str(text or "")
    n = 0
    for pat in _UI_MARK_PATTERNS:
        s, k = pat.subn("", s)
        n += k
    if n:
        s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s, n


def _last_step(root: Path, role: str) -> str:
    """上一轮这位角色自己写下的「下一步」(跨轮承接用)。空串 = 没有。"""
    try:
        events = history(root, role, tail=8)
    except Exception:      # noqa: BLE001 - 历史读不出来不该把这一轮堵死
        return ""
    for ev in reversed(events):
        if ev.get("t") != "assistant":
            continue
        text = (ev.get("text") or "").strip()
        if not text:
            continue
        m = _NEXT_RE.search(text)
        if m:
            return m.group(1).strip()[:120]
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return (lines[-1].strip()[:120] if lines else "")
    return ""


# 落库之后自动续一轮时注入的话(:
# 「落库的动作能否再次触发角色继续思考?让它看看还有没有未完成的计划轮次什么的」)。
# 为什么由**代码**注入而不是让作者再打一句:这一轮不是作者说的,不能伪造一条 user 气泡
# (那会让对话记录撒谎)。所以走 resume 通道:不记 user 事件,只在提示词里交代经过。
RESUME_NOTE = """## 刚刚发生的事（不是你编的，是系统告诉你的）
作者**已经拍板落库**了你上一轮提的那条候选卡。也就是说那件事定下来了，别再问、别再改。
现在按你自己的节奏往下走:
- 你在「商讨计划」里还有没走完的轮次 → 直接走下一轮;
- 计划已经走完 → 看这一步落地之后带出什么新问题(新的一致性、新的漏洞、下一步该定什么),
  该提新的候选卡就提,该问就问,最后写一行「下一步:…」;
- 确实没什么要接着做的 → 一句话说清"这件事收口了",不用硬凑内容。"""
# function calling 的函数名必须是 ASCII（OpenAI 规范），中文名进 description。
_TOOL_ASCII = {
    "提设定": "propose_setting",
    "看地基": "view_foundation",
    "读文件": "read_file",
    "学改法": "learn_edit",
    "问同事": "ask_colleague",
    "看数据": "read_stats",
    "改指纹": "tune_fingerprint",
    #后加的五把也要映射——中转商对中文工具名报 invalid（实测 503/11152）。
    "查知识": "lookup_knowledge",
    "取稿": "fetch_transcript",
    "看档案": "view_style_archive",
    "记要点": "remember_note",
    "想起": "recall_memory",
    "蒸馏": "distill_corpus",
    "搜书房": "search_library",
    "落笔记": "save_note",
}
_ASCII_TO_TOOL = {v: k for k, v in _TOOL_ASCII.items()}

# Qwen 系列模型的 function calling 常把 arguments 写成 XML 风格而不是 JSON。
_XML_PARAM_RE = re.compile(r"<parameter=([^>\n]+)>\s*(.*?)\s*(?:</parameter>|$)", re.S)


def _parse_args(raw: str) -> dict:
    """解析 function calling 的 arguments：先认 XML 参数块，再按 JSON，最后空。

    真机实测最糟的一种是「JSON 合法但语义错乱」：
        {"内容": "</parameter>\\n<parameter=落点>\\n外置大脑/xxx.md"}
    直接 json.loads 会得到 {"内容": "外置大脑/xxx.md"} —— 落点丢了、内容串味。
    所以**只要有 `<parameter=` 就先走 XML 提取**。
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    if "<parameter=" in raw:
        out: dict = {}
        for m in _XML_PARAM_RE.finditer(raw):
            key = m.group(1).strip()
            val = _strip_xml(m.group(2))
            if key and val:
                out[key] = val
        if out:
            return out
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return {k: _strip_xml(v) if isinstance(v, str) else v for k, v in obj.items()}
    except ValueError:
        pass
    return {}


def _strip_xml(s: str) -> str:
    """去掉模型误塞进值里的 XML 参数标签残渣。"""
    return re.sub(r"</?parameter[^>]*>", "", s or "").strip()


def _chat_rel(role: str) -> str:
    from .collab import _ALL_ROLES
    if role not in _ALL_ROLES:
        raise ValueError(f"未知角色: {role}")
    return f"{AGENT_CHAT_DIR}/agent-{role}.jsonl"


def _tools_schema(locked_slot: str = "", role: str = "") -> list[dict]:
    """把 partner_tools.REGISTRY 翻成 OpenAI tools schema（0.9.0 起：经
    partner_tools.schema_of/registry_specs 单一实现生成，本函数只做转发）。

    `locked_slot` 非空时，`提设定` 收成**单参** `内容`——模型看不见落点，也就没机会漏它。
    `role` 非空时按 `ToolSpec.roles` 过滤：不该这个角色用的工具，连 schema 都不给它。
    """
    return partner_tools.schema_of(partner_tools.registry_specs(role, locked_slot=locked_slot))


def archive_current(root: Path | str, role: str, stamp: str) -> dict:
    """把该角色的对话归档、另起一段。

    做法与伙伴对话一致:**只改名,不删**——旧段仍留在 `.伙伴对话/` 里可查可删。
    没有对话文件时也算成功(返回 archived=False),前端照常重画成空的一段。
    """
    from . import request_turn
    with request_turn._guard:
        request_turn.retire(root, role)
        return _archive_retired(root, role, stamp)


def _archive_retired(root, role, stamp):
    p = Path(root) / _chat_rel(role)
    if not p.is_file():
        return {"ok": True, "archived": False, "reason": "这个角色还没有对话记录"}
    if p.stat().st_size == 0:
        return {"ok": True, "archived": False, "reason": "这一段是空的,不用归档"}
    dst = p.with_name(f"{p.stem}.{stamp}{p.suffix}")
    n = 1
    while dst.exists():                       # 同一秒连点两次也别覆盖
        dst = p.with_name(f"{p.stem}.{stamp}-{n}{p.suffix}")
        n += 1
    p.rename(dst)
    return {"ok": True, "archived": True, "归档为": dst.name}


def history(root: Path | str, role: str, *, tail: int = 50) -> list[dict]:
    """读该角色的对话记录（最近 tail 条）。"""
    p = Path(root) / _chat_rel(role)
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out[-tail:]


def _append_event(root: Path, role: str, event: dict) -> None:
    p = root / _chat_rel(role)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _knowledge_items(root: Path, agent) -> list[tuple[str, str]]:
    """按角色的 reads 取材料（设定集分区 / 大纲 / 技能）。缺失路径静默跳过。

    再并上 `collab.EXTRA_READS` 的代码级补读。为什么不在 agents/*.md 里改:
    老书用的是书内那份角色副本,改出厂模板碰不到它们 —— 而「产物 → 消费者」这类接口
    必须对所有书立刻生效(外部分析点名的孤岛根因之一就是这个)。
    """
    from .agents import _read_file_items
    rels = list(agent.reads or [])
    for rel in collab.EXTRA_READS.get((getattr(agent, "name", "") or "").strip(), ()):
        if rel not in rels:
            rels.append(rel)
    return _read_file_items(root, rels, lambda ev: None)


def _build_user_prompt(root: Path, role: str, knowledge: list[tuple[str, str]],
                       user_text: str, tool_results: list[str], locked_slot: str = "",
                       *, resume: bool = False, task_plan_active: bool = False) -> str:
    parts: list[str] = []
    if knowledge:
        ctx = "\n\n".join(f"### {rel}\n{text.strip()}" for rel, text in knowledge)
        parts.append("## 这本书的现有材料（你的工作依据）\n" + ctx)

    snips = scratchpad.load_snippets(role, root, max_entries=5)
    if snips:
        lines = []
        for s in snips:
            note = f" · {s['note']}" if s.get("note") else ""
            lines.append(f"- [{s['time']}]{note} {s['body'][:200]}")
        parts.append("## 你的速记本（最近 5 条，只属于你）\n" + "\n".join(lines))

    # 承接上一轮的「下一步」。作者常只回一个「继续/好」,没有这条它就会重新问一遍
    # ——那正是"不够主动"的最直观表现。只接智库那八人(领航员有自己的出题卡协议)。
    if proactive.applies(role):
        step = _last_step(root, role)
        if step:
            parts.append("## 上一轮你自己说的「下一步」（不是作者说的）\n" + step +
                         "\n作者这次要是只说「继续／好／嗯」，就直接做这一件，不要重复提问。")
        # 商讨模式:把「谈了几轮 / 上次的计划」跨轮带过去,
        # 谈满就刹车(不许再问,这轮必须落地)。这两条都只给智库八人。
        prog = deliberate.progress(root, role)
        if prog.get("计划"):
            parts.append("## 你自己的商讨计划（上一轮写的，接着往下走）\n"
                         f"计划:{prog['计划']}\n"
                         f"按你的计划,这是**第 {prog['轮次'] + 1} 轮**"
                         + (f"（最多 {deliberate.MAX_QA_ROUNDS} 轮）" if prog["轮次"] else "") + "。")
        elif prog.get("轮次"):
            parts.append(f"## 商讨进度\n上一张候选卡之后,你们已经谈了 {prog['轮次']} 轮"
                         f"(最多 {deliberate.MAX_QA_ROUNDS} 轮)。")
        _brake = deliberate.force_block(prog.get("轮次", 0))
        if _brake and not task_plan_active:
            # 夜间补缺(设计 §5 末条):接入结构任务状态后,旧刹车不得与新计划分别
            # 作出相互冲突的决定——活动任务已有结构化计划时,force_block 旧刹车让位
            # (有效约束/计划进度已由任务上下文块注入,不再叠加旧计数刹车)。
            parts.append(_brake)

    if tool_results:
        parts.append("## 你刚才调用的结果\n" + "\n\n".join(tool_results))

    # 落库后自动续一轮。这一轮没有"作者说的话",所以把经过交代清楚。
    if resume:
        parts.append(RESUME_NOTE)

    if locked_slot:
        parts.append(f"## 本次会话的写入位置（已锁定）\n`{locked_slot}`\n"
                     "你只负责写内容，位置不用管、也不要提。")

    if (user_text or "").strip():
        parts.append("## 现在作者对你说\n" + user_text)
    elif resume:
        # 续轮里作者没说话:别留一个空的「作者对你说」,那会让模型以为作者回了句空白
        parts.append("## 现在\n作者没有新的话,是你在接着自己上一轮的进度往下走。")

    if locked_slot:
        parts.append(
            "## 你的回应方式\n"
            "用第一人称，直接给判断和建议，别复述材料。\n"
            "**要提交设定就调用 `propose_setting`，它只需要内容**——位置已经锁好了。\n"
            "正文里只写「为什么这么改」，不要把设定正文整段写在回复里（写正文等于没提交）。\n"
        )
    else:
        parts.append(
            "## 你的回应方式\n"
            "用第一人称，直接给判断和建议，别复述材料。\n"
            "**任何要写进设定集的内容，必须调用 `propose_setting` 提交成候选卡。**\n"
            "正文里只写「为什么这么改」，**不要把设定正文写在回复里**——写在正文里等于没提交。\n"
            "不需要工具就正常说话。\n"
        )
    return "\n\n".join(parts)


def _emit_tool_result(root: Path, role: str, emit, name_cn: str, args: dict, *,
                      ts: str, backend, locked_slot: str = "",
                      bound_task_id: str = "", conversation_id: str = "", body: str = "") -> dict:
    """执行一个工具并 emit（两条路共用）。调用方负责处理 proposal 终结。

    `locked_slot` 非空且工具是「提设定」时，**强制用会话锁定值覆盖落点**——
    模型要是仍自作主张给了落点，一律无视（丁方案的结构性保证）。

    F2 修复（分片3）：proposal 事件携带服务端绑定的任务身份
    （task_id/conversation_id）——决策回连（confirm/reject）据此精准落位，
    不再按「role+最近 updated_ms」猜。旧卡（无绑定）回连时明确未关联。
    """
    if name_cn == "提设定" and locked_slot:
        args = dict(args)
        args.pop("落点", None)
        args["落点"] = locked_slot
    from . import request_turn
    ticket = getattr(emit, "_request_ticket", None)
    if ticket is not None and not request_turn.current(ticket):
        return {"t": "result", "cancelled": True}
    emit({"t": "tool", "name": name_cn, "params": args})
    try:
        ev = partner_tools.run_tool(root, name_cn, args, ts=ts, backend=backend, body=body,
                                    request_ticket=ticket)
    except Exception as e:      # noqa: BLE001 - 工具失败不该打断整轮
        ev = {"t": "result", "error": f"{type(e).__name__}: {e}"}
    if ticket is not None and not request_turn.current(ticket):
        return {"t": "result", "cancelled": True}
    if bound_task_id and isinstance(ev, dict) and ev.get("t") == "proposal":
        # F2 回执整改（历史回执 审查记录 P2-1）：服务端权威绑定**强制覆盖**
        # ——不用 setdefault（handler/模型侧任何预置的 task_id 不得冒充归属）。
        ev = dict(ev)
        ev["task_id"] = "" if bound_task_id == "__refused__" else bound_task_id
        ev["conversation_id"] = conversation_id or "默认"
    emit(ev)
    return ev


def _is_failed(ev: dict) -> bool:
    return bool(ev.get("error"))


def _fail_sig(name_cn: str, ev: dict) -> str:
    """失败指纹：同工具 + 同错误 = 同一个病。

    取前 128 字符（原先 48，审查指出两段不同错误的前 48 字符可能恰好相同，
    如「Failed to parse tool arguments: expected...」后接缺引号/缺括号两种毛病）。
    """
    return f"{name_cn}::{str(ev.get('error') or '')[:128]}"


def _resume_gate(root: Path, role: str, conversation_id: str, task_id: str,
                 decision_event_id: str) -> bool:
    """按已接受的决策原子领取一次续轮，领取先于模型执行。

    任务、角色和 accepted 决策必须匹配；未知、重复或已暂停的任务拒绝。
    领取记录随事件日志重建恢复，重复调用不会产生第二次模型请求。
    """
    if not task_id or not decision_event_id:
        return False
    try:
        from . import task_state as _tstate
        from .paths import TASKS_DIR
        base = Path(root) / TASKS_DIR / _tstate.sanitize_conversation_id(conversation_id)
        # 检查与领取同一临界区；领取先于模型调用，重复/并发请求不再执行。
        with _tstate._scope_lock(base):
            st = _tstate.load_snapshot(base, task_id)
            if st is None or st.status not in ("ready", "running", "waiting_author"):
                return False
            if (st._d.get("scope") or {}).get("role") != role:
                return False
            decision_id = str(decision_event_id)
            proposal_id = decision_id.removeprefix("confirm:")
            accepted = next((d for d in st._d.get("decisions") or []
                             if d.get("kind") == "accepted" and
                             (d.get("proposal_id") == proposal_id or
                              d.get("source_event") == decision_id)), None)
            if accepted is None:
                return False
            decision_id = str(accepted["proposal_id"])
            claims = st._d.get("resume_claims", [])
            if decision_id in claims or f"confirm:{decision_id}" in claims:
                return False
            _tstate.commit(base, st, {"kind": "resume_claim", "decision_id": decision_id},
                           expected_revision=st.revision)
            return True
    except Exception:
        return False


def run_turn(root: Path | str, role: str, user_text: str, backend, *, protocol: str = "",
             emit, ts: str = "", should_cancel=None, locked_slot: str = "",
             extra: dict | None = None, resume: bool = False,
             conversation_id: str = "", task_id: str = "",
             decision_event_id: str = "") -> None:
    """跑一轮对话。emit(event)：user / assistant_delta / assistant / tool / result / proposal / error。

    `locked_slot`（丁方案）：作者指定了要改哪一格时传进来，落点就此绑定，模型只写内容。
    `extra`：额外要盖在每条事件上的标记，通常
    `{"via": "议事", "panel": <轮次>}` —— 这样这一轮会记进角色自己的对话流水，
    但仍看得出「它是在议事例会上说的」，一对一界面能标灰提醒（作者选的口径：
    要写进去，但别让作者误以为那是两人私聊）。

    `conversation_id`（2026-11-08 任务准备架构）：另起对话的代际;空 = 当前代际「默认」。
    `task_id`/`decision_event_id`：承接既有任务/接受后续轮（§8）。任务准备失败不拦对话——
    发诊断后回退既有路径（§6）。
    """
    root = Path(root)
    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    user_text = (user_text or "").strip()
    # 界面标记不是作者的话：候选卡上的「✓ 已落库 → 路径」被整块复制回来时，
    # 模型会以为这条已经落进设定集，从而不再把作者改过的内容当新提案复核。
    # 在**入站口**剥（这一处之后 prompt、约束抽取、落历史全部用它），并发一条灰字说明，
    # 免得作者以为"我写的字被吃了"。
    user_text, _ui_marks = strip_ui_marks(user_text)
    locked_slot = (locked_slot or "").strip()
    conversation_id = (conversation_id or "").strip() or "默认"
    # 夜间补缺 P1-2:conversation_id 客户端可控,禁路径分隔符/..段,非法值回退「默认」。
    from .task_state import sanitize_conversation_id as _san_cid
    conversation_id = _san_cid(conversation_id)

    from . import request_turn as _requests
    _ticket = _requests.begin(root, role, conversation_id, authorize=(
        (lambda: _resume_gate(root, role, conversation_id, task_id, decision_event_id))
        if resume else None))
    if _ticket is None:
        emit({"t": "status", "text": "本次续轮未获授权或已经执行，已跳过。"})
        return
    _original_cancel = should_cancel

    def _cancelled() -> bool:
        return not _requests.current(_ticket) or bool(_original_cancel and _original_cancel())

    should_cancel = _cancelled

    def _emit(ev: dict) -> None:
        if not _requests.current(_ticket):
            return
        ev.setdefault("ts", ts)
        if extra:
            for k, v in extra.items():
                ev.setdefault(k, v)
        if locked_slot and ev.get("t") == "proposal":
            # 侧边栏要显示「写入位置（已锁定）」
            ev.setdefault("locked_slot", locked_slot)
        with _requests.effect(_ticket) as allowed:
            if not allowed:
                return
            _append_event(root, role, ev)
            emit(ev)

    _emit._request_ticket = _ticket

    # ── 任务准备层(§6:定位任务→约束→进度;失败发诊断回退,零额外规划调用)────────
    # F4（块6c）续轮执行门：decision_event_id 关联任务的续轮已消费 →
    # 本轮降普通输入（不触发 resume 通道——RESUME_NOTE 不注入、goal 判空）。
    prepared = None
    try:
        from . import task_state as _tstate, turn_prepare as _tp
        from .paths import TASKS_DIR
        base = root / TASKS_DIR / conversation_id
        # 接受后续轮:decision_event_id 带着回连原任务(§8,confirm 在伙伴流水但任务在对话域)
        _goal = user_text if (user_text and not resume and _is_task_goal(user_text)) else ""
        prepared = _tp.prepare_turn(
            base_dir=base, domain="chat", role=role, conversation_id=conversation_id,
            task_id=task_id, goal=_goal, user_text=user_text,
            origin_event_id=decision_event_id, request_id=f"{ts}-{role}",
            request_ticket=_ticket)
        if prepared is not None and prepared.get("task") is not None:
            for ev in prepared.get("events") or ():
                _emit(ev)
    except Exception as _pe:     # noqa: BLE001 - 准备层失效:诊断 + 回退(§6)
        try:
            _emit({"t": "status", "text": f"任务准备层这次没接上（{_pe}），按普通对话继续。"})
        except Exception:        # noqa: BLE001
            pass
        prepared = None

    # 本轮绑定的任务身份(§8 迟到输出边界):prepared 定位的任务 task_id 贯穿
    # 约束折叠/计划折叠/收尾——收尾绝不重新认领"当前扫描到的任务"。
    # F1 回执根修（历史回执 审查记录 P1）：「准备层明确拒绝绑定」（task_binding="refused"）
    # 用哨兵值抑制三处空绑定回退扫描——原来 refused 被压成空串,收尾按 role 重扫
    # 认领了别人的活动任务（B.last_outcome 被 A 请求的输出污染,探针实跑复现）。
    # 哨兵走 load_snapshot 精确加载路径 → 返回 None → 三函数全部 no-op。
    _binding = (prepared or {}).get("task_binding") if prepared else None
    _task_id_bound = (prepared.get("task") or {}).get("task_id", "") if prepared else ""
    if _binding == "refused":
        _bound_task_id = "__refused__"
    else:
        _bound_task_id = _task_id_bound
    if user_text:
        # 夜间补缺 P1-1:作者分句立约 → constraint 事件折叠进活动任务(§10 场景1)。
        _fold_constraints_from_user_text(root, role, conversation_id, user_text,
                                         _emit, f"{ts}-{role}", bound_task_id=_bound_task_id)
        _emit({"t": "user", "text": user_text})
        if _ui_marks:
            # 剥了什么要说一句：否则作者改过的卡"看起来发了"、模型却收到别的东西。
            _emit({"t": "info",
                   "text": f"（已去掉 {_ui_marks} 处界面标记，如「✓ 已落库 → …」。"
                           f"那句话是卡片上画的、不是你写的；去掉它我才不会以为这一格已经落盘。）"})
    elif resume:
        # 续轮不是作者说的话:记一条自己的标记(灰字),别伪造 user 气泡(那会让记录撒谎)
        _emit({"t": "resume", "text": "已落库,接着上一轮往下走"})

    try:
        agent = load_agent(root, role)
        knowledge = _knowledge_items(root, agent)
        system = (agent.system_prompt or "").strip()
        system += ("\n\n候选内容和角色历史自述都属于讨论；proposal 事件证明候选已提出，"
                   "confirm 事件才是确认依据。作者把候选内容改过再发来，是新版本，"
                   "旧卡的确认不能证明这个版本已经保存。")
        # 「智库八人要更主动」:这段由**代码**注入,不写进书内 agents 副本
        # ——老书的角色提示词是各自一份,改出厂模板碰不到它们;注入才是一次改完全部书生效。
        if proactive.applies(role):
            system = system + "\n\n" + proactive.INSTRUCTION
        # 谁该问谁(顺带把「本轮的询问人 + 预算」交代给工具层)
        for _extra in (role_contracts.contract_text(role), role_contracts.advice_text(role)):
            if _extra:
                system = system + "\n\n" + _extra
        # 先商讨再动手(谈判轮次由模型自己规划,跨轮记得自己在第几轮)
        _delib = deliberate.instruction(role)
        if _delib:
            system = system + "\n\n" + _delib
        _colleague_txt = collab.instruction(role)
        if _colleague_txt:
            system = system + "\n\n" + _colleague_txt
        collab.begin_turn(role)
        # 协议由作者选(text / json / native / 空=自动)。
        # native 想要的是"结构化 tool_calls",前提是后端真有这个能力;没有就**降级并说清**,
        # 而不是静默用文本协议(那样作者会以为自己在用 native)。
        _proto = (protocol or "").strip().lower()
        _supports_fc = hasattr(backend, "complete_with_tools")
        if _proto == "native" and not _supports_fc:
            use_fc = False
            _emit({"t": "error", "text":
                   "⚠️ 当前后端不支持原生函数调用(没有 tools 能力),已自动降级为文本协议。"
                   "功能照常,但工具参数靠文本解析,遇到复杂参数偶尔会不稳;"
                   "想真用上 native,请换成支持 tools 的 OpenAI 兼容端点。"})
        elif _proto in ("text", "json"):
            use_fc = False          # 作者明确要文本类协议,就别自作聪明走 FC
        else:
            use_fc = _supports_fc   # 空 = 自动(原行为)

        # 文本协议才需要工具契约段；function calling 下由 schema 描述，不占 prompt
        if not use_fc:
            system = system + "\n\n" + partner_tools.render_contract(role)
        # 行动模式取向段（默认档是空串，等于不加）——两条路径都要挂
        _mh = _mode_hint()
        if _mh:
            system = system + "\n\n" + _mh
        # 任务准备架构(§7 书内专家对话行):目标/有效约束/计划进度进提示词;
        # 角色 在正常答复里给计划更新(文本段 TASK_PLAN:),零额外规划调用。
        if prepared is not None and prepared.get("task") is not None:
            prepared = _requests.refresh(prepared, base)
            _tctx = _task_context_block(prepared)
            if _tctx:
                system = system + "\n\n" + _tctx

        # 夜间补缺(§5):活动任务带结构化计划时,旧 deliberate force_block 刹车让位(统一读数)
        _task_plan_active = bool(prepared is not None and prepared.get("task") is not None
                                 and (prepared.get("task") or {}).get("plan", {}).get("steps"))
        if use_fc:
            _run_fc(root, role, backend, system, knowledge, user_text, _emit, ts,
                    should_cancel, locked_slot, resume,
                    prepared=prepared, conversation_id=conversation_id,
                    task_plan_active=_task_plan_active)
        else:
            _run_text(root, role, backend, system, knowledge, user_text, _emit, ts,
                      should_cancel, locked_slot, resume,
                      prepared=prepared, conversation_id=conversation_id,
                      task_plan_active=_task_plan_active)
    except Exception as e:      # noqa: BLE001 - 兜底，别让流挂死
        _emit({"t": "error", "text": f"{type(e).__name__}: {e}"})


# 明确任务判定(§5:简单问答不建任务,以本轮有效答复结束;只有明确工作目标才建任务)。
# 判据从严:命中「命令式动作」词表才算任务;疑问句(…吗?/什么/怎么)是问答不建。
_TASK_GOAL_RE = re.compile(
    r"重写|重做|改成|把.{1,20}改成|给(我|出)|写(一|这|下|第|个)?(章|段|份|版|个)|做(一|这)?(套|个|版)"
    r"|整理|分析|设计|规划|列(一|个)?(份|个|表)|补充|完善|修(改|订|复)|更新|替换|扩写|压缩|蒸馏"
    r"|取稿|落笔记|查(证|一下)|搜(一|之)?(下|书房)|提(案|候选)|出(一|几)?(套|个|版|案)|继续"
    r"|任务")
_QA_RE = re.compile(r"[吗呢嘛]\s*[?？!！。]?\s*$|[?？]\s*$|^(什么|怎么|为什么|是啥|啥)"
                    r"|是什么|是什么意思|干嘛的?|干什么的?")

def _is_task_goal(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) < 4:
        return False
    if _QA_RE.search(t):
        return False
    return bool(_TASK_GOAL_RE.search(t))


# ── 作者显式约束提取(夜间补缺 P1-1,§10 场景1)────────────────────────────────
# 作者分句立约(「记住:力量必须带代价」)也要跨轮承接:constraint 事件折叠进任务状态,
# 下一轮 turn_prepare 的有效约束提取(turn_prepare.py:100-107)自然带上。
# 来源区分:作者原话=explicit(真);模型转述不在本提取面(不做,不猜)。
_CONSTRAINT_PREFIX_RE = re.compile(
    r"^(记住[:：]|记一下[:：]|以后|别|不要|不用|必须|一定要|不许|禁止|规则[:：]|约束[:：])"
)
_QA_TAIL_RE = re.compile(r"[?？]\s*$")


def _extract_constraints(user_text: str) -> list[str]:
    """从作者输入提取显式约束句(前缀判别,从严:疑问句/空串不提)。

    支持:
    - 单句立约:整句以约束前缀开头;
    - 一句话里分号/句号分段的多个约束子句:逐段判前缀。
    返回约束内容列表(每条 ≤400 字,与 task_state constraint 截断口径一致)。
    """
    t = (user_text or "").strip()
    if not t or _QA_TAIL_RE.search(t):
        return []
    out: list[str] = []
    # 按中英文分号/句号切子句(保留前缀判别的语义单位);问号已经整体排除。
    for seg in re.split(r"[;；。]", t):
        s = seg.strip()
        if not s or len(s) < 2:
            continue
        if _CONSTRAINT_PREFIX_RE.match(s):
            content = s[:400]
            if content not in out:
                out.append(content)
    return out


@local_effect
def _fold_constraints_from_user_text(root: Path, role: str, conversation_id: str,
                                     user_text: str, _emit, request_id: str,
                                     bound_task_id: str = "") -> None:
    """作者输入里带显式约束且有活动任务 → 折叠 constraint 事件(失败只诊断,不拦对话)。"""
    try:
        cons = _extract_constraints(user_text)
        if not cons:
            return
        from . import task_state as _tstate
        from .paths import TASKS_DIR
        base = root / TASKS_DIR / (_tstate.sanitize_conversation_id(conversation_id))
        st = None
        if bound_task_id:
            # §8 迟到输出边界(复审 审查记录):本轮请求开始时绑定的 task_id 贯穿收尾——
            # 按 id 精确加载,绝不重新认领"当前按 role 扫描到的任务"(那可能是另起对话后的 B)。
            cand = _tstate.load_snapshot(base, bound_task_id)
            if cand and cand.status in ("ready", "waiting_author") and \
                    ((cand._d.get("scope") or {}).get("role", "") in ("", role)):
                st = cand
            # 绑定任务已不在可承接状态(paused/completed 等) → st=None,本轮不写任何任务
        else:
            for candidate_id in _tstate.task_ids(base):
                cand = _tstate.load_snapshot(base, candidate_id)
                # 夜间补缺最终班 P1:约束折叠也按 role 匹配(§2f)
                if cand and cand.status in ("ready", "waiting_author") and \
                        ((cand._d.get("scope") or {}).get("role", "") in ("", role)):  # paused/blocked 不承接(另起对话归档后不复活)
                    st = cand
                    break
        if st is None:
            return      # 无活动任务:约束随当轮 user 文本进模型,不入任务状态
        for content in cons:
            # 6a（F5）：约束折叠改 commit_retry——并发下锁内追盘重放，不丢更新
            _tstate.commit_retry(base, st,
                           {"kind": "constraint", "content": content, "explicit": True,
                            "scope": "任务", "source_event": f"user:{request_id}"},
                           expected_revision=st.revision)
        _emit({"t": "status",
               "text": f"已记下 {len(cons)} 条约束,后面各轮都会照办。"})
    except Exception as e:      # noqa: BLE001 - 约束折叠失败不拦答复
        try:
            _emit({"t": "status", "text": f"约束没入账({e}),本轮答复照常。"})
        except Exception:       # noqa: BLE001
            pass


def _task_context_block(prepared: dict) -> str:
    """把 PreparedTurn 的任务事实折成提示词段(§4.2:目标/有效约束/计划进度/待决事项)。"""
    t = prepared.get("task") or {}
    lines = ["## 当前任务（系统准备的事实，不是你编的）",
             f"- 任务 {t.get('task_id')} · 状态 {t.get('status')} · 修订 v{t.get('revision')}"]
    if t.get("goal"):
        lines.append(f"- 目标:{t['goal']}")
    cons = prepared.get("constraints") or []
    if cons:
        lines.append("- 有效约束:" + ";".join(
            (c["content"] + ("（待核）" if c.get("待核") else "")) for c in cons))
    pp = prepared.get("plan_progress")
    if pp and pp.get("steps"):
        done = sum(1 for s in pp["steps"] if s["status"] == "completed")
        lines.append(f"- 计划 v{pp['plan_revision']}:共 {len(pp['steps'])} 步,已完成 {done}。"
                     "如计划要变,在回复末尾用一段 `TASK_PLAN:` + 编号步骤（可标 [done]/[skip:原因]）说明。")
    pq = prepared.get("pending_questions") or []
    if pq:
        lines.append("- 待作者决定:" + ";".join(pq))
    return "\n".join(lines)


@local_effect
def _apply_plan_update_from_text(root: Path, role: str, conversation_id: str,
                                 text: str, _emit, request_id: str,
                                 bound_task_id: str = "") -> None:
    """答复里带 TASK_PLAN: 就折叠进任务状态并发 plan_updated(§9)。失败只留诊断。"""
    try:
        from . import task_state as _tstate, turn_prepare as _tp
        from .paths import TASKS_DIR
        upd = _tp.parse_plan_update(text or "")
        if upd is None:
            return
        base = root / TASKS_DIR / (_tstate.sanitize_conversation_id(conversation_id))
        # 找该作用域的活动任务(有绑定 task_id 时按 id 精确加载,§8 迟到输出边界)
        st = None
        if bound_task_id:
            cand = _tstate.load_snapshot(base, bound_task_id)
            if cand and cand.status in ("ready", "waiting_author") and \
                    ((cand._d.get("scope") or {}).get("role", "") in ("", role)):
                st = cand
        else:
            for candidate_id in _tstate.task_ids(base):
                cand = _tstate.load_snapshot(base, candidate_id)
                # 夜间补缺最终班 P1:计划折叠也按 role 匹配(§2f)
                if cand and cand.status in ("ready", "waiting_author") and \
                        ((cand._d.get("scope") or {}).get("role", "") in ("", role)):  # paused/blocked 不承接(另起对话归档后不复活)
                    st = cand
                    break
        if st is None:
            return      # 无活动任务:计划段只是文本,不入状态(简单问答,§5)
        steps = [{"id": f"s{i+1}", "title": s["title"], "status": s["status"],
                  "skipped_reason": s.get("skipped_reason", "")}
                 for i, s in enumerate(upd["steps"])]
        _tstate.commit_retry(base, st, {"kind": "plan_update", "reason": upd["reason"],
                                  "steps": steps}, expected_revision=st.revision)
        st = _tstate.load_snapshot(base, st.task_id) or st
        _emit({"t": "plan_updated", "task_id": st.task_id, "revision": st.revision,
               "scope": st._d.get("scope") or {}, "request_id": request_id,
               "text": _tp.plan_update_event_text(st, upd["reason"])})
    except Exception as e:      # noqa: BLE001 - 计划折叠失败不拦答复
        try:
            _emit({"t": "status", "text": f"计划更新没入账（{e}），答复照常显示。"})
        except Exception:       # noqa: BLE001
            pass


# 行动模式三档。prefs（用户级）驱动两处：
# ① 工具往返轮数（主动思考给宽、安静收窄）；② system 附加一段行为取向。
# 读 prefs.json 时不落缓存——改完设置下一轮就生效。
_MODE_ROUNDS = {"主动思考": 8, "默认": None, "安静": 2}

def _action_mode() -> str:
    try:
        from . import userconf
        return userconf.load_prefs().get("mode", "默认")
    except Exception:
        return "默认"

def _mode_hint() -> str:
    """行动模式的附加提示（裁定三档）。默认档不加提示。"""
    mode = _action_mode()
    if mode == "主动思考":
        return (
            "## 行动模式：主动思考\n"
            "工具多用、多方向想：作者一句话值得展开讨论就展开（可反问、可给两个方向的取舍）。\n"
            "讨论里有值得长期记住的信息（作者的决定、你的随想）就调 remember_note 记下来。\n"
            "宁可多一轮把方案打磨完整，也不要急着交半成品。"
        )
    if mode == "安静":
        return (
            "## 行动模式：安静\n"
            "以作者给的方向为中心，快速给出 2-3 个可选方案让作者挑，不做长篇展开。\n"
            "工具只在必要时用（一次最多两三回）。少反问，作者要的是选项不是讨论。"
        )
    return ""   # 默认档不加提示

def _tool_rounds(role: str) -> int:
    """这一轮最多几次工具往返。智库八人要在商讨期读文件/问同事,给宽一点。"""
    base = deliberate.AGENT_TOOL_ROUNDS if deliberate.applies(role) else _MAX_TOOL_ROUNDS
    cap = _MODE_ROUNDS.get(_action_mode())
    return min(base, cap) if cap else base


@local_effect
def _finish_turn(root: Path, role: str, conversation_id: str, say: str,
                 _emit, ts: str, had_proposal: bool, bound_task_id: str = "") -> None:
    """一轮收尾:任务生命周期推进 + TASK_PLAN 折叠(§5)。

    - 有活动任务:本轮已答但任务未完 → ready(等后续输入);出了候选卡 → waiting_author;
      答复里带 TASK_PLAN → 折叠进计划。
    - 无活动任务:不动状态(简单问答本来就没有任务)。
    """
    try:
        from . import task_state as _tstate, turn_prepare as _tp
        from .paths import TASKS_DIR
        base = root / TASKS_DIR / (_tstate.sanitize_conversation_id(conversation_id))
        st = None
        if bound_task_id:
            # §8 迟到输出边界(复审 审查记录):收尾只写本轮绑定的 task——
            # 绑定任务已 paused/completed 等 → st=None,本轮 outcome/status 一律不落盘
            cand = _tstate.load_snapshot(base, bound_task_id)
            if cand and cand.status in ("ready", "waiting_author") and \
                    ((cand._d.get("scope") or {}).get("role", "") in ("", role)):
                st = cand
        else:
            for candidate_id in _tstate.task_ids(base):
                cand = _tstate.load_snapshot(base, candidate_id)
                # 夜间补缺最终班 P1:收尾也按 role 匹配——只推进当前角色自己的任务(§2f)
                if cand and cand.status in ("ready", "waiting_author") and \
                        ((cand._d.get("scope") or {}).get("role", "") in ("", role)):  # paused/blocked 不承接(另起对话归档后不复活)
                    st = cand
                    break
        # 注:TASK_PLAN 折叠由两条路径在答复输出时即答即折(§8:先持久化结果);
        # 这里只读最新盘上状态推进生命周期,不重复折叠。
        if st is None:
            return
        with _tstate._scope_lock(base):
            st = _tstate.load_snapshot(base, st.task_id)
            if st is None or st.status not in ("ready", "waiting_author"):
                return
            # R-04（2026-11-10）：先剥掉答复尾部的 TASK_PLAN 协议段再交摘要，
            # 否则 300 字预算被协议段挤占（结论常紧邻协议段之前，不能整段丢弃）。
            _tstate.commit_retry(base, st, {"kind": "outcome",
                                            "summary": _tp.strip_plan_segment(say or "")})
            new_status = "waiting_author" if had_proposal else "ready"
            _tstate.commit_retry(base, st, {"kind": "status", "status": new_status})
            st = _tstate.load_snapshot(base, st.task_id) or st
        _emit({"t": "task_status", "task_id": st.task_id, "revision": st.revision,
               "scope": st._d.get("scope") or {}, "request_id": ts,
               "status": new_status})
    except Exception as e:      # noqa: BLE001 - 状态收尾失败不拦答复展示
        try:
            _emit({"t": "status", "text": f"任务状态收尾没落盘（{e}）。"})
        except Exception:       # noqa: BLE001
            pass


def _run_fc(root: Path, role: str, backend, system: str, knowledge, user_text: str,
            _emit, ts: str, should_cancel, locked_slot: str = "",
            resume: bool = False, prepared: dict | None = None,
            conversation_id: str = "", task_plan_active: bool = False) -> None:
    """function calling 路径：模型直接返回结构化 tool_calls，参数不经文本解析。"""
    _bound_task_id = ((prepared.get("task") or {}).get("task_id", "")
                      if prepared and prepared.get("task") else "__refused__")  # 固定入口无任务绑定
    tools = _tools_schema(locked_slot, role)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": _build_user_prompt(root, role, knowledge, user_text, [],
                                                       locked_slot, resume=resume,
                                                       task_plan_active=task_plan_active)},
    ]
    fail_sig, fail_count = "", 0
    nudged = False        # 这轮已经催过一次
    # 2026-11-08 审查修正(#3)：session 内帧图缓冲——工具结果里的采样帧图剥离后存这里，
    # 在下一次 complete_with_tools 的 user 消息中以真 image_url 部分附带（不进持久历史）。
    pending_imgs: list[dict] = []
    for rnd in range(_tool_rounds(role)):
        if should_cancel is not None and should_cancel():
            return
        if pending_imgs:
            # 有新帧图：附到最后一条 user 消息后（不污染 tool 角色消息——审查 #5：
            # role=tool 带 image_url 不是通用契约，端点可能拒）。
            _imgs_now = pending_imgs
            pending_imgs = []
            messages.append({"role": "user", "content": [
                {"type": "text", "text": "（上面工具结果附带的采样帧图，请结合画面分析。）"},
                *_imgs_now,
            ]})
        res = backend.complete_with_tools(messages, tools, max_chars=2048)
        if should_cancel is not None and should_cancel():
            return
        content = (res.get("content") or "").strip()
        calls = res.get("tool_calls") or []
        if content and not calls and re.search(r"(?:已(?:经)?|我(?:调用|提好|生成)).{0,12}(?:保存|落库|候选卡|提设定)|already saved", content, re.I):
            content = "【本段完成说法未经本轮工具回执核验】\n" + content
            _emit({"t": "status", "code": "action_unverified", "text": "模型完成自述不等于工具成功，请以候选卡及确认回执为准。"})
        if content:
            _emit({"t": "assistant", "text": content})
            # TASK_PLAN 即答即折(§8:先持久化结果)——fc 路径同文本路径
            if "TASK_PLAN:" in content:
                _apply_plan_update_from_text(root, role, conversation_id, content,
                                             _emit, f"{ts}-p{rnd}",
                                             bound_task_id=_bound_task_id)
        if not calls:
            # 同文本路径:智库八人只总结不收工 → 催一次(只一次)
            if not nudged and not _actionable(content) and proactive.applies(role):
                nudged = True
                messages.append({"role": "user", "content": proactive.NUDGE})
                continue
            _finish_turn(root, role, conversation_id, content, _emit, ts, False,
                         bound_task_id=_bound_task_id)
            return

        # 按 OpenAI 规范把 assistant 的 tool_calls 原样写回历史，再逐条回喂结果
        messages.append({
            "role": "assistant",
            "content": content or None,
            "tool_calls": [
                {"id": c.get("id") or f"call_{rnd}_{i}", "type": "function",
                 "function": {"name": c.get("name") or "", "arguments": c.get("arguments") or "{}"}}
                for i, c in enumerate(calls)
            ],
        })

        emitted_proposal = False
        for i, c in enumerate(calls[:_MAX_TOOLS_PER_MSG]):
            name_cn = _ASCII_TO_TOOL.get(c.get("name") or "", c.get("name") or "")
            args = _parse_args(c.get("arguments") or "")
            ev = _emit_tool_result(root, role, _emit, name_cn, args,
                                   ts=f"{ts}-{rnd + 1}-{i}", backend=backend,
                                   locked_slot=locked_slot,
                                   bound_task_id=_bound_task_id,
                                   conversation_id=conversation_id)
            if ev.get("cancelled"):
                return
            if ev.get("t") == "proposal":
                emitted_proposal = True

            # 熔断：同工具同因连续失败（锁定模式 2 次 / 自由模式 3 次）
            if _is_failed(ev):
                sig = _fail_sig(name_cn, ev)
                fail_count = fail_count + 1 if sig == fail_sig else 1
                fail_sig = sig
                limit = _MAX_SAME_FAIL if locked_slot else _MAX_SAME_FAIL_FREE
                if fail_count >= limit:
                    _emit({"t": "error",
                           "text": f"「{name_cn}」连续 {limit} 次同一个错，先停下。"
                                   "换个说法，或先在设定集里点选要改的那一格再聊。"})
                    return
            else:
                fail_sig, fail_count = "", 0

            messages.append({
                "role": "tool",
                "tool_call_id": c.get("id") or f"call_{rnd}_{i}",
                # 2026-11-08 审查修正(#3)：原 json.dumps 整个 ev——帧图 base64 变成
                # 几十万字符纯文本伪输入（既不构成图像输入又撑爆 token）。现剥离
                # images 后只序列化文本载荷；帧图存 session 内 pending 缓冲，
                # 下一次 complete 的 user 消息以真 image_url 部分附带。
                "content": json.dumps({k: v for k, v in ev.items() if k != "images"},
                                      ensure_ascii=False),
            })
            for _im in (ev.get("images") or []):
                if isinstance(_im, dict) and _im.get("b64"):
                    pending_imgs.append({
                        "type": "image_url",
                        "image_url": {"url": "data:" + str(_im.get("mime") or "image/jpeg") + ";base64," + _im["b64"],
                                      "detail": "low"},
                        "采样时间": [round(float(t), 1) for t in (_im.get("times") or [])
                                     if isinstance(t, (int, float))][:6],
                    })
        if emitted_proposal:
            _finish_turn(root, role, conversation_id, content, _emit, ts, True,
                         bound_task_id=_bound_task_id)
            return          # 候选卡已发 = 交作者拍板，本轮终结（防重复卡）


def _run_text(root: Path, role: str, backend, system: str, knowledge, user_text: str,
              _emit, ts: str, should_cancel, locked_slot: str = "",
              resume: bool = False, prepared: dict | None = None,
              conversation_id: str = "", task_plan_active: bool = False) -> None:
    """文本协议路径（兜底）：`TOOL:` / `用:` + ```loom 围栏。后端没有 tools 能力时用。"""
    _bound_task_id = ((prepared.get("task") or {}).get("task_id", "")
                      if prepared and prepared.get("task") else "__refused__")  # 固定入口无任务绑定
    tool_results: list[str] = []
    # 2026-11-08 审查修正(#3)：text 路径帧图缓冲（同 _run_fc 的处理）。
    pending_imgs: list[dict] = []
    fail_sig, fail_count = "", 0
    nudged = False        # 这轮已经催过一次(只催一次,别把费用叠起来)
    format_failures = 0
    for rnd in range(_tool_rounds(role)):
        if should_cancel is not None and should_cancel():
            return

        user_prompt = _build_user_prompt(root, role, knowledge, user_text, tool_results,
                                         locked_slot, resume=resume,
                                         task_plan_active=task_plan_active)
        buf: list[str] = []

        def preview(delta: str) -> None:
            if should_cancel is None or not should_cancel():
                _emit({"t": "assistant_delta", "ts": ts, "text": delta})
        relay, flush_relay = stream_line_relay(preview, flexible=True)

        def on_chunk(delta: str) -> None:
            if should_cancel is not None and should_cancel():
                return
            buf.append(delta)
            relay(delta)

        complete_kwargs = {}
        try:
            from .backends import accepts_kwarg
            if accepts_kwarg(backend, "agent_mode"):
                complete_kwargs["agent_mode"] = True
        except Exception:
            pass

        # 2026-11-08 审查修正(#3)：有新帧图时 user 消息升级 content list（文字+真图）。
        if pending_imgs:
            _imgs_now = pending_imgs
            pending_imgs = []
            _user_msg = [{"type": "text", "text": user_prompt + "\n\n（上面工具结果附带的采样帧图，请结合画面分析。）"},
                         *_imgs_now]
            try:
                raw = backend.complete(system, _user_msg, max_chars=2048,
                                       on_chunk=on_chunk, **complete_kwargs) or "".join(buf)
            except TypeError:
                # 后端不认多模态 user：退纯文字（图片部分丢弃，文字结果全保留——审查 #5）
                raw = backend.complete(system, user_prompt, max_chars=2048,
                                       on_chunk=on_chunk, **complete_kwargs) or "".join(buf)
        else:
            raw = backend.complete(system, user_prompt, max_chars=2048,
                                   on_chunk=on_chunk, **complete_kwargs) or "".join(buf)

        if should_cancel is not None and should_cancel():
            return
        flush_relay()
        say, tools = parse_tool_blocks(
            raw,
            valid_names=set(partner_tools.REGISTRY),
            known_params={n: s.params for n, s in partner_tools.REGISTRY.items()},
            flexible=True,
        )
        say, _botched = strip_protocol_lines(say, flexible=True)
        if _botched and not tools:
            format_failures += 1
            _emit({"t": "status", "code": "tool_format_retry", "text": "工具格式未识别，正在纠正；尚未产生候选卡。"})
            tool_results.append("上次工具格式未识别，没有执行。请按已提供的工具格式重新回答。")
            if format_failures >= 2 or rnd == _tool_rounds(role) - 1:
                _emit({"t": "error", "code": "tool_unparsed", "text": "工具格式连续未识别，本轮停止，未生成候选卡。"})
                return
            continue
        format_failures = 0
        if say.strip():
            _emit({"t": "assistant", "text": say})
            # TASK_PLAN 即答即折(§8:先持久化结果)——不等收尾,催促轮会吃掉最后一轮正文
            if "TASK_PLAN:" in say:
                _apply_plan_update_from_text(root, role, conversation_id, say,
                                             _emit, f"{ts}-p{rnd}",
                                             bound_task_id=_bound_task_id)
        if not tools:
            # 只总结一句就收工 = 作者什么也拿不到。给智库八人一次「别收工,
            # 落到动作上」的催促;已经问过问题/写过下一步的不催(它已经给了能接着走的东西)。
            if not nudged and not _actionable(say) and proactive.applies(role):
                nudged = True
                tool_results.append(proactive.NUDGE)
                continue
            _finish_turn(root, role, conversation_id, say, _emit, ts, False,
                         bound_task_id=_bound_task_id)
            return

        emitted_proposal = False
        for i, tool in enumerate(tools[:_MAX_TOOLS_PER_MSG]):
            ev = _emit_tool_result(root, role, _emit, tool["name"], tool["params"],
                                   body=tool.get("body") or "", ts=f"{ts}-{rnd + 1}-{i}", backend=backend,
                                   locked_slot=locked_slot,
                                   bound_task_id=_bound_task_id,
                                   conversation_id=conversation_id)
            if ev.get("cancelled"):
                return
            if ev.get("t") == "proposal":
                emitted_proposal = True

            if _is_failed(ev):
                sig = _fail_sig(tool["name"], ev)
                fail_count = fail_count + 1 if sig == fail_sig else 1
                fail_sig = sig
                limit = _MAX_SAME_FAIL if locked_slot else _MAX_SAME_FAIL_FREE
                if fail_count >= limit:
                    _emit({"t": "error",
                           "text": f"「{tool['name']}」连续 {limit} 次同一个错，先停下。"})
                    return
            else:
                fail_sig, fail_count = "", 0

            txt = ev.get("text") or ""
            if ev.get("error"):
                txt = json.dumps({"ok": False, "error": ev["error"]}, ensure_ascii=False)
                _emit({"t": "status", "code": "tool_failed", "text": f"{tool['name']}未成功：{ev['error']}"})
            if txt:
                tool_results.append(f"【{tool['name']}】\n{txt}")
            # 2026-11-08 审查修正(#3)：text 路径帧图同样收集——下次 complete 的 user
            # 消息以 content list 附带真图（user_prompt 文字部分恒 str）。
            for _im in (ev.get("images") or []):
                if isinstance(_im, dict) and _im.get("b64"):
                    pending_imgs.append({
                        "type": "image_url",
                        "image_url": {"url": "data:" + str(_im.get("mime") or "image/jpeg") + ";base64," + _im["b64"],
                                      "detail": "low"},
                        "采样时间": [round(float(t), 1) for t in (_im.get("times") or [])
                                     if isinstance(t, (int, float))][:6],
                    })
        if emitted_proposal:
            _finish_turn(root, role, conversation_id, say, _emit, ts, True,
                         bound_task_id=_bound_task_id)
            return
