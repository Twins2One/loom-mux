# -*- coding: utf-8 -*-
"""对话压缩(的「压缩/速记」)。

需求背景:
1、长对话接近模型上下文上限时自动压缩上下文;
2、提醒模型在触发压缩前及时将待记项记入速记本。

现状与缺口(先讲清楚,免得又变成"以为有、其实没有"):
-已有:`context_guard`40%/60%两道阈值 + `partner.py`60%时收尾;
 `partner_context._render_tail`超12000字**掐头**;`_render_event`认得 `summary`事件。
-缺的:掐掉的那部分**丢了**。60%时写进速记本的是一句固定话("上下文接近上限,对话强制收尾"),
下一轮读回来等于没记 ——这就是本模块补的那一格。

两条设计纪律:
1. **确定性压缩,零 LLM、零花费**。压缩是每轮都可能跑的活,不能一压缩就花一次钱;
而且"压缩"本身要再调模型,长对话里就会套娃。
2. **压缩不许编**。只从已有事件里挑(作者说过的、拍板过的、模型给过结论的),一个字都不新写。
挑剩的不删原文 ——原 jsonl一个字节不动,压缩只影响「这一轮发给模型的历史」。
"""
from __future__ import annotations

_KEEP_AUTHOR =12
_KEEP_DECIDE =8
_KEEP_AGENT =10
_ITEM_MAX =120

_KIND = {
    "user": "author",
    "confirm": "decide",
    "candidate": "decide",
    "assistant": "agent",
}


def _clip(s, n=_ITEM_MAX):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n -1] + "\u2026"


def _bucket(events):
    out = {"author": [], "decide": [], "agent": []}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = _KIND.get(str(ev.get("t") or ""))
        if kind is None:
            continue
        if kind == "author":
            text = ev.get("text")
        elif kind == "agent":
            text = ev.get("text")
        else:
            text = ev.get("text") or (str(ev.get("slot", "")) + " " + str(ev.get("content", "")) if ev.get("slot") else ev.get("id", ""))
        text = _clip(text)
        if text:
            out[kind].append(text)
    return out


def summarize(events, role=""):
    """把一段将被掐掉的历史压成摘要。events必须是被丢弃的那一段;空串=没东西可压。"""
    b = _bucket(events)
    if not any(b.values()):
        return ""
    lines = []
    if b["author"]:
        lines.append("\u4f5c\u8005\u8bf4\u8fc7(\u6309\u5148\u540e):")
        lines.extend("- " + t for t in b["author"][-_KEEP_AUTHOR:])
    if b["decide"]:
        lines.append("\u5df2\u6709\u7ed3\u8bba/\u62cd\u677f:")
        lines.extend("- " + t for t in b["decide"][-_KEEP_DECIDE:])
    if b["agent"]:
        lines.append((role or "\u6211") + "\u7ed9\u8fc7\u7684\u7ed3\u8bba:")
        lines.extend("- " + t for t in b["agent"][-_KEEP_AGENT:])
    return "\n".join(lines)


def split_for_compact(tail, *, keep_chars):
    """按字数预算把尾部切成(要压掉的旧段, 要留着的近段)。

    从后往前数，**数满预算就停**。
    ⚠ 两个边界必须对：①总字数没超预算 → 一切不切
    （否则「没超预算却把全部压成一句摘要」，原文全丢）；
    ②切点至少保留最后一条（否则压完没有任何对话可看）。
    """
    total = sum(len(str(ev.get("text") or ev.get("content") or "")) for ev in tail)
    if total <= keep_chars:
        return [], list(tail)
    acc =0
    cut = len(tail)
    for i in range(len(tail) -1, -1, -1):
        ev = tail[i]
        acc += len(str(ev.get("text") or ev.get("content") or ""))
        if acc > keep_chars:
            cut = i +1
            break
    cut = max(cut,1)
    return tail[:cut], tail[cut:]


def compress(tail, *, keep_chars, role=""):
    """入口:返回(压缩后的 tail, 摘要文本)。

    压缩后的 tail = 一条 summary事件 + 近段。
    ⚠ 近段里的 tool_result 也要剔掉：它们是一次性的中间产物，
    真要回查应该重新读文件（工具随时可调）。留在历史里只会把
    下一轮又撞回同一个上限——压缩就白做了。
    """
    old, recent = split_for_compact(tail, keep_chars=keep_chars)
    if not old:
        return tail, ""
    text = summarize(old, role=role)
    if not text:
        return tail, ""
    keep = [ev for ev in recent if str(ev.get("t") or "") != "tool_result"]
    return [{"t": "summary", "text": text}] + keep, text
