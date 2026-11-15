# -*- coding: utf-8 -*-
"""作者智库 8 人的**互相询问**()。

需求背景:「能否使得作者智库 8 人间各有联动,例如大纲师在安排情节时会自动询问
情绪节奏设计师和战斗设计师的意见。」

两条都做,而且都放在**代码层**(不写进书内 agents 副本 —— 老书的角色提示词是各自一份,
改出厂模板碰不到它们;注入才能一次改完全部书):

1. `EXTRA_READS`:补"该读没读"的接口(那份外部分析点出的孤岛根因之一:
   情绪曲线自称消费者是细纲师,可细纲师的 reads 里根本没有它)。
2. `COLLEAGUES` + 「问同事」工具:大纲师要排情节时,可以真的向情绪节奏设计师 /
   战斗设计师发一次**聚焦的单轮询问**,拿到一句意见再继续写。
   一次询问 = 一次模型调用,所以**每轮上限 2 次**,且对话里会显示"询问了谁"。
"""
from __future__ import annotations

from . import paths

# ── 1. 代码级补读(产物 → 消费者)────────────────────────────────────────────
# 只补"那份分析明确点出、且不影响写作成本"的几条;新增一行即可扩展。
EXTRA_READS: dict[str, tuple[str, ...]] = {
    # 细纲师要按情绪曲线排节奏;也要先看到作者手写的增补可能落在这两处。
    "章节细纲师": ("外置大脑/分析/情绪曲线.md",),
    # 编辑总监查一致性,唯一真源是实体词典与状态账本。
    "编辑总监": ("外置大脑/实体词典.md", "外置大脑/状态账本.md"),
    # 语料分析师给写作建议,方法论在 skills 里,别让它凭感觉说。
    # 2026-10-25 修:原来写成 "loom/skills/去AI味.md" —— 书根下没有 loom/ 这一层
    # (skills/ 就是书根下的技能目录,_READ_PREFIXES 也按 "skills/" 放行),
    # 于是这条补读一直走「跳过缺失文件」,等于从来没读到过。路径口径与别的角色一致。
    # 同时补上它自己维护的那份「去AI味口径」:要调口径,先得知道自己现在的口径长什么样
    # (老书的 agents/语料分析师.md 是书内副本,改出厂模板碰不到它 —— 所以放这儿)。
    "语料分析师": ("skills/去AI味.md", f"{paths.ANALYSIS_DIR_REL}/去AI味口径.md"),
}

# ── 2. 谁该问谁(写进提示词,让模型知道"顺手同事"是谁)────────────────────────
COLLEAGUES: dict[str, tuple[tuple[str, str], ...]] = {
    "大纲师": (("情绪节奏设计师", "这一卷/这一段的憋与爆怎么排"), ("战斗设计师", "关键冲突打起来好不好看")),
    "设定师": (("战斗设计师", "力量体系落到战斗上有没有漏洞"), ("知识顾问", "这条设定的出处与依据")),
    "战斗设计师": (("设定师", "这一招合不合本书的体系与代价"), ("情绪节奏设计师", "这场打斗放在哪一段最有效")),
    "情绪节奏设计师": (("大纲师", "这段节奏会不会把主线压塌"),),
    # 2026-11-12：人物侧写师的两块咬合面正好是「世界规则」与「情绪节拍」。
    "人物侧写师": (("设定师", "这条人物设计会不会顶掉世界观规则/已有设定"),
                   ("情绪节奏设计师", "这个人物该在哪一段承担爽点或憋屈")),
    "知识顾问": (("设定师", "这条知识要不要写进设定集、写在哪一格"),),
    "编辑总监": (("批评家", "这几章最刺眼的问题在哪"), ("语料分析师", "文风是不是漂了")),
    "批评家": (("编辑总监", "这些问题够不够格拦下这一卷"),),
    "语料分析师": (("批评家", "这几处像不像 AI 写的"),),
}

# 「问同事」每轮上限(一次询问 = 一次模型调用;作者只付一轮钱,但别被无限放大)
MAX_CONSULTS_PER_TURN = 2


def instruction(role: str) -> str:
    """给某个角色注入的「你可以问谁」段落。空串 = 这个角色没有预设同事。"""
    pairs = COLLEAGUES.get((role or "").strip())
    if not pairs:
        return ""
    lines = ["## 你的顺手同事（可以真的去问）", "",
             "遇到不是你这门专业的问题时,**别自己硬猜**:调一次工具 `问同事`,"
             "把问题写成一句话问过去,拿到对方的一句意见再继续。一次询问 = 一次模型调用,"
             f"所以每轮最多问 {MAX_CONSULTS_PER_TURN} 次,只在真的卡住时用。", ""]
    for name, when in pairs:
        lines.append(f"- **{name}** —— {when}")
    lines.append("")
    lines.append("问完要**自己判断**要不要采纳(你是这门专业的人);不采纳也要说一句为什么。")
    return "\n".join(lines)


def consult(asker: str, target: str, question: str, *, root, backend, protocol: str = "") -> str:
    """向另一个角色发一次**聚焦的单轮询问**,返回它的一句话意见(失败返回空串)。"""
    _consume()
    q = (question or "").strip()
    if not q:
        raise ValueError("问同事缺少「问题」参数。")
    if target not in dict(COLLEAGUES.get(asker, ())) and target not in _ALL_ROLES:
        raise ValueError(f"「{target}」不是可询问的同事。")
    from .agents import load_agent
    from . import agent_chat

    agent = load_agent(root, target)
    parts = [f"你是「{target}」。另一位同事「{asker}」在推进这本书时向你求助,请只回答他这一件事。",
             "", "## 问题", q, "",
             "## 怎么答", "给判断和理由,≤200 字;要引用这本书已有的设定,别自己发明新体系。",
             "只输出答复本身,不要寒暄。"]
    system = (agent.system_prompt or "").strip() + "\n\n" + "\n".join(parts)
    knowledge = agent_chat._knowledge_items(root, agent)
    ctx = "\n\n".join(f"### {rel}\n{text.strip()[:1500]}" for rel, text in knowledge[:6])
    user = (("## 这本书的材料\n" + ctx + "\n\n") if ctx else "") + "## 问题\n" + q
    try:
        out = backend.complete(system, user, max_chars=800) or ""
    except Exception:      # noqa: BLE001 - 同事答不上来不该把当前这轮拖崩
        return ""
    return out.strip()


# 可被询问的角色全集(含流水线六棒,方便将来扩展;当前智库常驻角色都在里面)
# 2026-11-12 审查 审查记录 P1：meta 层修了、chat 层第二道校验漏了；名册须与 scratchpad.AUTHOR_ROLES 同口径
_ALL_ROLES = ("领航员", "设定师", "大纲师", "知识顾问", "语料分析师", "战斗设计师",
              "情绪节奏设计师", "人物侧写师", "编辑总监", "批评家",
              "章节细纲师", "章节设定师", "章节分镜师", "写手", "责任编辑", "润色师",
              "市场分析师")

# ── 3. 本轮预算(一次询问 = 一次模型调用,别让模型把作者的钱当纸烧)──────────────
# 2026-10-25(议事例会):原来是**进程级**字典 —— 四个角色并发跑时,后开跑的那个
# `begin_turn` 会把前一个的计数清零,「每轮最多问 2 次」形同虚设,账也记到别人头上。
# 改成线程局部:一轮对话 = 一条工作线程,天然隔离;并发数就是作者设的上限。
import threading as _threading

_local = _threading.local()


def begin_turn(role: str) -> None:
    """每轮对话开头清一次计数,并记下「这轮是谁在问」。"""
    _local.asker = (role or "").strip()
    _local.used = 0


def current_asker() -> str:
    return getattr(_local, "asker", "") or ""


def _consume() -> None:
    used = getattr(_local, "used", 0)
    if used >= MAX_CONSULTS_PER_TURN:
        raise ValueError(f"这一轮已经问过 {MAX_CONSULTS_PER_TURN} 次同事了；先把手上的事做完。")
    _local.used = used + 1
