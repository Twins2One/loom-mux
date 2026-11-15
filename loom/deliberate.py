# -*- coding: utf-8 -*-
"""商讨模式:让「作者智库」八人**先谈,再改**()。

需求背景:「当前『作者智库』的运行模式太封闭了,能否改成和领航员一样,
经多轮商讨(轮次由模型自行规划)后再提出修改方案。」

## 为什么以前"封闭"

不是模型不会谈,是三处把它往"一次答完"上推:

1. `proactive.INSTRUCTION` 写着「每轮必须落到一个动作」「不要重复上一轮的提问」——
   于是它急着出卡,或急着下结论。
2. 一轮里模型一开口,这一轮就结束了(没有工具调用 = 收工),它没有"我先谈两轮"的余地。
3. 提示词里没有"我打算谈几轮"这个概念,模型也就不会规划。

## 这一层怎么改

**不改"能不能谈",改"知不知道自己正在谈"。** 三件事:

- `INSTRUCTION`:要求它在开头写一行「商讨计划:…(预计 N 轮)」,并且**N 由它自己定**;
  每轮只推进一件事;计划走完、或作者说"按你说的办",才出候选卡。
- `progress()`:从它自己的对话流水里,数一数"上次出卡之后已经谈了几轮"、
  上一次的「商讨计划」「下一步」是什么。**跨轮记得自己在第几轮** —— 这是项目前做不到的。
- `force_block()`:问到 `MAX_QA_ROUNDS` 轮还没落地,就**替它刹车**:这一轮必须出方案,
  不许再问。没有这条,模型会真的无限问下去(每问一次都是钱)。

## 边界(刻意不做的事)

- **不替它规划**。轮次是模型自己写的计划;这里只做两件事:把计划跨轮带过去,以及兜底刹车。
- **不自动连发**。轮与轮之间永远等作者回话 —— 没有"模型自己跟自己聊三轮"这回事,
  那既不是商讨,也是自动花钱。
- **不改写作引擎六棒**。它们跑的是按章落地的流水线,没有"商讨"这个环节。
"""
from __future__ import annotations

import re
from pathlib import Path

from .proactive import PROACTIVE_ROLES

# 同一件事最多谈几轮(谈满就刹车,必须落地)。一次对话 = 一次调用,不设上限就是给钱开口子。
MAX_QA_ROUNDS = 3

# 智库八人可以多给一些工具往返:商讨期要读文件、看地基、问同事,6 轮容易中途被截断。
AGENT_TOOL_ROUNDS = 10

_PLAN_RE = re.compile(r"商讨计划\s*[：:]\s*(.+)")
_NEXT_RE = re.compile(r"下一步\s*[：:]\s*(.+)")


def applies(role: str) -> bool:
    return (role or "").strip() in PROACTIVE_ROLES


def instruction(role: str) -> str:
    """给智库八人注入的「先商讨,后改」段落。空串 = 这个角色不吃这套。"""
    if not applies(role):
        return ""
    return f"""## 先商讨,再动手（轮次你自己定）
你要改这本书的东西之前,**先跟作者谈清楚**。不要听完一句话就直接出候选卡 ——
那种"一锤子买卖"看着利索,实际上是在替作者拍板。

1. **开头先写一行计划**:`商讨计划:第 1~N 轮,分别解决什么(预计 N 轮)`。
   N 由你定(一般 1~{MAX_QA_ROUNDS} 轮),后面可以改,但要说一句为什么改。
2. **每轮只推进一件事**:先把你担心的分歧一次列清(最多 3 条),但**这一轮只问其中最关键的一条**。
3. 中途缺证据就去查:读文件、看地基、问同事 —— 拿到再说话,别凭印象。
4. 计划走完、或作者说「按你说的办／就这样／你定」时,那一轮**必须**用工具提交候选卡;
   正文只写「为什么这么改」,不要把设定正文写在回复里。
5. 每轮最后一行写 `下一步:…`(只写一件事),让作者知道下一句该说什么。
6. **最多谈 {MAX_QA_ROUNDS} 轮**。谈满还没落地,这一轮就必须给方案或候选卡;
   真被卡住,就明说「我需要你先定 A,才能出方案」——那是结论,不是又一轮提问。
7. 提问也是动作:只要你说清了「下一步」,这一轮就不算空转。
"""


# ── 跨轮记账:从角色自己的对话流水里读,不新造存储 ──────────────────────────────
def _events(root: Path | str, role: str) -> list[dict]:
    from . import agent_chat

    try:
        return agent_chat.history(root, role, tail=40)
    except Exception:      # noqa: BLE001 - 读不出来就当第一次谈,不该把这一轮堵死
        return []


def rounds_since_proposal(events: list[dict]) -> int:
    """上次出候选卡之后,已经"谈"了几轮(一轮 = 一条 assistant 回复)。

    认出卡(proposal)或作者拍板(confirm)就清零 —— 那是"这件事已经落地"的界碑。
    """
    n = 0
    for ev in reversed(events or []):
        t = ev.get("t")
        if t in ("proposal", "confirm"):
            break
        if t == "assistant" and (ev.get("text") or "").strip():
            n += 1
        if n > 50:      # 防御:坏数据别把这里变成无底洞
            break
    return n


def plan_of(text: str) -> str:
    """从某条回复里抽「商讨计划」那一行。空串 = 这条没写计划。"""
    m = _PLAN_RE.search(text or "")
    return m.group(1).strip()[:120] if m else ""


def last_assistant(events: list[dict]) -> str:
    for ev in reversed(events or []):
        if ev.get("t") == "assistant" and (ev.get("text") or "").strip():
            return ev["text"]
    return ""


def progress(root: Path | str, role: str) -> dict:
    """这一轮开工前该知道的事:谈到第几轮、上次的计划、上次的下一步。"""
    evs = _events(root, role)
    text = last_assistant(evs)
    m = _NEXT_RE.search(text or "")
    return {"轮次": rounds_since_proposal(evs),
            "计划": plan_of(text),
            "下一步": m.group(1).strip()[:120] if m else ""}


def force_block(rounds: int) -> str:
    """谈满了就刹车。空串 = 还没谈满,不用管。"""
    if rounds < MAX_QA_ROUNDS:
        return ""
    return f"""(刹车)这件事你们已经谈了 {rounds} 轮。这一轮**不要再提问**,必须落到方案上:
① 能用工具提交候选卡,就提交(curated 内容写进「内容」,正文只写为什么这么改);
② 确实还差东西,就一句话写出「我需要你先定 X,才能给方案」,并把 X 说成两个以内的具体选项。
作者要的是下一步能动手的东西,不是又一轮问答。"""
