"""写章取消:(a) 只停后续步骤,**不打断在途的那一次请求**。

为什么要说清语义:一次「写手」调用是一次阻塞的 HTTP 请求,中途掐断要把 cancel token
一路贯穿 backend → 客户端(改动面大,且各家 SDK 的中断行为不一致)。而作者的真实需求是
「别让它越写越远」——**每一棒之间**检查一次就够了。

代价(必须让作者知道):
- 点「停」之后,**当前这一棒仍会写完**(它已经在路上),下一棒不再开始;
- 已经生成的正文不丢:段落**已经跑完**的稿子会落到 `正文/.半稿/第N章.txt`,
  正式章节文件(正文/第N章.md)一个字节都不动,所以「写了一半」不会污染定稿与 learn。

用 root 做键:同一本书同时只允许一个写任务(写入锁保证),不同书互不影响。
"""

from __future__ import annotations

import threading
from pathlib import Path

_pending: set[str] = set()
_guard = threading.Lock()


def _key(root: Path | str) -> str:
    return str(Path(root).resolve())


def request(root: Path | str) -> None:
    """请求停:下一次「棒与棒之间」的检查点会生效。重复请求无副作用。"""
    with _guard:
        _pending.add(_key(root))


def pending(root: Path | str) -> bool:
    with _guard:
        return _key(root) in _pending


def clear(root: Path | str) -> None:
    """消费掉取消标记(每次开写之前必须调,否则上一次的停会误伤这一次)。"""
    with _guard:
        _pending.discard(_key(root))
