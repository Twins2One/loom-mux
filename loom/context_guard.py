# -*- coding: utf-8 -*-
r"""上下文守卫（现行行为）。

当前调用方仍按旧三档消费结果：
- 预备阈值（35%）：提醒先写速记；
- 低阈值（40%）：提醒尽快收尾；
- 高阈值（60%）：停止本轮并生成摘要。

55% / 70% / 90% 的新三档要等请求级预算、压缩状态机和失败契约一起接入，
不能只改常量。守卫只做判定，不读写上下文。
"""
from __future__ import annotations


PREP_THRESHOLD = 0.35
LOW_THRESHOLD = 0.40
HIGH_THRESHOLD = 0.60

PREP_WARNING = (
    "【上下文守卫·快满了】上下文已用约 35%。"
    "赶快把这轮要记的东西写进速记本：结论/未决问题/下一步。"
    "再过一会儿正文会被压成摘要，摘要里只保留得下要点。"
)
LOW_WARNING = (
    "【上下文守卫·低阈值】上下文已用约 40%，请尽快收尾本轮讨论。"
    "有结论请落盘，有待定问题请记进速记本。"
)
HIGH_WARNING = "【上下文守卫·高阈值】上下文已用约 60%，立即停止并生成摘要。"

SUMMARY_TEMPLATE = """
## 上下文交接摘要 · {role} · {time}

### 本轮已得出结论
{conclusions}

### 本轮未能解决
{open_questions}

### 下一步建议
{next_steps}

### 关键引用
{references}
"""


class GuardResult:
    """守卫判定结果。"""

    __slots__ = ("level", "message")

    def __init__(self, level: str | None, message: str = "") -> None:
        self.level: str | None = level
        self.message: str = message

    def __bool__(self) -> bool:
        return self.level is not None


def check(context_used: int, context_limit: int) -> GuardResult:
    """给定已用 token 数与总容量，返回现行三档判定。"""
    if context_limit <= 0:
        return GuardResult(None)
    ratio = max(0, context_used) / context_limit
    if ratio >= HIGH_THRESHOLD:
        return GuardResult("high", HIGH_WARNING)
    if ratio >= LOW_THRESHOLD:
        return GuardResult("low", LOW_WARNING)
    if ratio >= PREP_THRESHOLD:
        return GuardResult("prep", PREP_WARNING)
    return GuardResult(None)


def build_summary(*,
                  role: str,
                  conclusions: str = "-",
                  open_questions: str = "-",
                  next_steps: str = "-",
                  references: str = "-") -> str:
    """生成高阈值触发后的结构化摘要。"""
    import time
    return SUMMARY_TEMPLATE.format(
        role=role,
        time=time.strftime("%Y-%m-%d %H:%M"),
        conclusions=conclusions.strip() or "-",
        open_questions=open_questions.strip() or "-",
        next_steps=next_steps.strip() or "-",
        references=references.strip() or "-",
    )
