"""跨进程写锁:同一本书,在**多个进程**里也只能有一个写任务。

原来只有进程内 `threading.Lock`(usecases._locks),单入口单人的时候看不出问题。
但只要打包版窗口、CLI、开发实例同时开着(作者现在就是这种局面),两边的写任务会一起
往 `.原稿` / `.历史` / 章状态账本 / 事实账本里写 —— 快照互相覆盖,事后很难查是哪边盖的。

做法:**两层锁**,任何一层拿不到就算「忙」:
  ① 进程内 `threading.Lock` —— 挡同一进程里的并发请求(原有能力);
  ② OS 级文件锁(`msvcrt.locking` / `fcntl.flock`)—— 挡**别的进程**。

锁文件放在 `LOOM_HOME/locks/` 下,按「书根的绝对路径」哈希命名,**绝不往书里塞文件**
(书是纯 markdown 的产物契约,不该多出一个锁文件)。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .config import user_config_dir


_BASE_CACHE: list[Path] = []


def _lock_base() -> Path:
    """锁目录。优先 `<LOOM_HOME>/locks`;那里不可写(权限/只读盘/沙箱)就退到系统临时目录。

    互斥要求所有写者实际使用同一锁目录；不同LOOM_HOME或仅一端退到临时目录时，
    不保证跨进程互斥。锁文件不进入作品目录。
    """
    if _BASE_CACHE:
        return _BASE_CACHE[0]
    import tempfile

    def _candidates():
        # 2026-11-03（f2cefcdc 报备）：user_config_dir()/gettempdir() 在受限 token
        # 环境（WMI 起的服务等）自身可能抛 FileNotFoundError——构造候选也要各自兜底。
        try:
            yield user_config_dir() / "locks"
        except Exception:
            pass
        try:
            yield Path(tempfile.gettempdir()) / "loom-locks"
        except Exception:
            pass
    for base in _candidates():
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".probe"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            continue
        _BASE_CACHE.append(base)
        return base
    # 两处均不可用时保留确定路径；FileLock获取失败，不能报告已取得保护。
    _BASE_CACHE.append(Path(tempfile.gettempdir()) / "loom-locks")
    return _BASE_CACHE[0]


def lock_path(root: Path | str, kind: str = "write") -> Path:
    """这本书的锁文件路径。kind 区分「写锁」与「伙伴轮锁」。"""
    key = str(Path(root).resolve()).lower().encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()[:32]
    return _lock_base() / f"{digest}.{kind}.lock"


class FileLock:
    """一个跨进程的「拿得到/拿不到」锁。非阻塞;`release()` 之后可以再 `acquire()`。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh = None
        self._degraded = False   # True = 锁文件不可用，本次获取失败

    # -- 内部 ---------------------------------------------------------
    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        # Windows 的 msvcrt 锁的是「从当前位置起 N 字节」,空文件上没有可锁的区域,
        # 所以先保证文件至少有 1 字节。
        if fh.tell() == 0 and self.path.stat().st_size == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        return fh

    # -- 对外 ---------------------------------------------------------
    def acquire(self) -> bool:
        try:
            fh = self._open()
        except OSError:
            # 未取得OS锁不能报告成功：调用方会据此允许同根写入。
            # 沿用非阻塞失败契约，由上层退还线程锁并拒绝本次写入。
            self._degraded = True
            return False
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    @property
    def held(self) -> bool:
        return self._fh is not None


class HeldLock:
    """两层锁的句柄。调用方只认 `release()`,跟原来的 `threading.Lock` 用法一致。"""

    def __init__(self, thread_lock, file_lock: FileLock) -> None:
        self._thread = thread_lock
        self._file = file_lock
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._file.release()
        finally:
            self._thread.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.release()
