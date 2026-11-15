"""反压缩检测器:揪出「这一章把大弧跑完了」的细纲。

为什么需要它(作者 2026-10-18 提的问题):长书最常见的坏法不是写崩,是**提前收线** ——
前几章把主线解决掉,后面几十章只能注水。提示词里加了「只推不收」,但**提示词管不住模型**,
所以要有一把尺子事后量。

口径与代价(必须先说清):
  · 这是**词面启发式**,不是语义判断 —— 它只认「收束语」与「缺未解问题」两个信号;
  · 它会**误报**:正经写到卷末就该收束的地方,它照样会喊(所以只报「可疑」,不拦稿);
  · 它也会**漏报**:把收束写成「他把那封信收进抽屉」这种不含收束语的字面,它看不出来。
  ⇒ 定位:**提示,不是闸**。人看一条「可疑」,三秒钟决定要不要改;不看也不会坏。
"""

from __future__ import annotations

import re
from pathlib import Path

# 收束语:这些词出现在细纲里,通常意味着「本章把某个层面的问题解决了」。
# 分四类,便于人一眼看出是哪一类被撞上。
_CLOSERS = {
    "彻底了结": ("彻底解决", "一并解决", "永久解决", "再也不会", "从此再没有", "尘埃落定"),
    "真相揭开": ("真相大白", "水落石出", "一切真相", "谜底揭开", "全部真相"),
    "告一段落": ("告一段落", "到此为止", "画上句号", "了结", "收尾了", "落幕"),
    "大局已定": ("大局已定", "胜负已分", "已成定局", "再无悬念"),
}

# 「留下未解问题」的信号:细纲里有这类话,说明写的时候想过后面。
#
# ⚠ 交叉审查 P0-2 的血:细纲契约(artifacts)与大纲师模板**强制**每份细纲写「章末留两个未解问题」
# 与「章末钩子」,于是 has_open 永远 True,检测器在生产上**从不触发** —— 绿着,但是死的。
# 处置:**不再拿它豁免收束语**。收束语出现就报(带上"若这是卷末则正常"的话),
# 把 has_open 降级成备注。代价是多报几次卷末章;收益是它真的会响。
_OPENERS = ("未解", "悬念", "还没有", "仍未", "留待", "待下章", "下一章", "章末钩", "钩子")

_SENT_SPLIT = re.compile(r"[。！？\n]")


def _sentence_of(text: str, pos: int) -> str:
    """把命中词所在的那一句截出来(给人看,不整段甩)。"""
    start = 0
    for m in _SENT_SPLIT.finditer(text):
        if m.start() >= pos:
            return text[start:m.start()].strip()
        start = m.end()
    return text[start:start + 120].strip()


def scan(text: str) -> dict:
    """扫一段细纲。返回 {可疑: bool, 命中: [...], 有未解信号: bool, 建议: str}。"""
    text = text or ""
    hits = []
    for kind, words in _CLOSERS.items():
        for w in words:
            i = text.find(w)
            if i >= 0:
                hits.append({"类": kind, "词": w, "句": _sentence_of(text, i)[:80]})
                break        # 同一类只报第一处,免得刷屏
    has_open = any(w in text for w in _OPENERS)
    return {
        "可疑": bool(hits),
        "命中": hits,
        "有未解信号": has_open,
        "建议": ("这章出现了收束语。若这不是卷末章,把它改成「只推不收」;"
                 "是卷末就按正常收束看。" if hits else ""),
    }

def report_line(result: dict) -> str:
    """一行结论,供报告直接用。"""
    if not result.get("可疑"):
        return ""
    kinds = "、".join(sorted({h["类"] for h in result.get("命中", [])}))
    tail = "(这章也写了未解问题,按卷末收束看即可)" if result.get("有未解信号") else ""
    return f"提示:这章有收束语({kinds})—— 不是卷末就改成「只推不收」{tail}。"
