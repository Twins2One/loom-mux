# -*- coding: utf-8 -*-
"""全局并发闸）。

**为什么要它**：出版本 0.7.2 时，「并发上限」只作用在议事厅内部（`roundtable` 的信号量），
而智库一对一（`/api/agent/chat`）、领航员（`/api/partner/say`）、以及各种动作端点
**没有任何闸**。作者"多开几个角色去查资料"时，实际会并行发出 N 份模型调用：
钱按 N 倍烧、供应商那边容易 429、界面上还看不出谁在跑。

**为什么挂在 backends 层**：所有会花钱的动作最后都走 `backend.complete()` /
`complete_with_tools()`。在这一层加一道闸，等于给"一切模型调用"装同一个水龙头；
逐个端点去贴，漏一个就是一个黑洞。签名用 `functools.wraps` 保住，
所以 `accepts_kwarg()` 那些按签名探测的老代码不受影响。

**口径**：
- 上限来自每本书 `loom.toml [backend] 并发上限`，范围 **2–8**，默认 2；
- 拿不到名额就**排队**，不拒绝——多开查资料是真实需求，硬拒会很难用；
- 排队超过 `timeout` 报明确错误（`ConcurrencyBusy`），不无限挂着；
- 计数只给人看（`stats()` → `/api/routes/concurrency`），不参与任何判定。
"""
from __future__ import annotations

import functools
import threading
import time

DEFAULT_CAP = 2
DEFAULT_TIMEOUT = 300.0     # 排队上限(秒):单次模型调用正常都在 5 分钟内回来

_cond = threading.Condition()
_cap = DEFAULT_CAP
_active = 0
_waiting = 0
_peak = 0


class ConcurrencyBusy(RuntimeError):
    """排队超时:并发名额被占满且等太久。不是"后端坏了",别让上层当成网络错误。"""


def configure(cap: int | None) -> None:
    """设定全局上限（每本书各自一份，谁在跑就按谁的算——单机单书是常态）。"""
    global _cap
    try:
        n = int(cap) if cap is not None else DEFAULT_CAP
    except (TypeError, ValueError):
        n = DEFAULT_CAP
    n = max(1, n)
    with _cond:
        if n != _cap:
            _cap = n
            _cond.notify_all()      # 上限调大了,正在排队的立刻能抢到


def cap() -> int:
    return _cap


class _Slot:
    """`with slot():` —— 拿到名额才继续，退出时归还并通知排队者。"""

    __slots__ = ("timeout",)

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self):
        global _active, _waiting, _peak
        with _cond:
            _waiting += 1
            try:
                deadline = time.monotonic() + self.timeout
                while _active >= _cap:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise ConcurrencyBusy(
                            f"并发已满（上限 {_cap}，已占 {_active}），排队超过 "
                            f"{int(self.timeout)} 秒仍未轮到。先等手上的活收尾，或把"
                            f"「并发上限」调大再试。")
                    _cond.wait(min(left, 5.0))
                _active += 1
                _peak = max(_peak, _active)
            finally:
                _waiting -= 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        global _active
        with _cond:
            _active -= 1
            _cond.notify()
        return False


def slot(timeout: float | None = None) -> _Slot:
    return _Slot(DEFAULT_TIMEOUT if timeout is None else timeout)


def stats() -> dict:
    """给界面看的一份读数：上限 / 正在跑 / 在排队 / 峰值。"""
    with _cond:
        return {"上限": _cap, "运行中": _active, "排队": _waiting, "峰值": _peak}


def reset_for_tests() -> None:
    """测试用：把计数与上限拨回初值（不碰任何正在跑的线程——测试里都是串行的）。"""
    global _cap, _active, _waiting, _peak
    with _cond:
        _cap, _active, _waiting, _peak = DEFAULT_CAP, 0, 0, 0
        _cond.notify_all()


def gated(fn):
    """装饰一次模型调用。`functools.wraps` 保住签名——`accepts_kwarg()` 靠签名探测，不能破。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with slot():
            return fn(*args, **kwargs)

    return wrapper
