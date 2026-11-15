"""测试夹具:一个临时 loom 项目 + 可编程的假后端。"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.scaffold import init as scaffold_init


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """把用户级配置(~/.loom 默认后端/默认 key)指向临时目录,防开发机的真 ~/.loom 泄漏进测试。"""
    monkeypatch.setenv("LOOM_HOME", str(tmp_path_factory.mktemp("loom_home")))


@pytest.fixture(autouse=True)
def _isolate_library_root(tmp_path_factory, monkeypatch):
    """把「知识库根」指向临时目录。

    2026-10-27 起文风档案住在 `<知识库>/语料库/`（作者裁定汇总到知识库，不再跟书走）⇒
    测试如果不改这一格，就会往开发机真实的 `E:\\Book\\知识库` 里写档案。
    知识库是作者的书稿数据，测试绝不能碰。
    """
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(tmp_path_factory.mktemp("library")))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """离线铺一个真实项目骨架(含 agents / 外置大脑 / 中性默认指纹)。

    2026-11-10 第一章地基硬门：scaffold 默认 idea 为空，新硬门会把章 1 生成用例
    全拦下。这些用例测的是流水线/续跑/轨迹，不是地基门本身 ⇒ 夹具补一句话设定；
    缺项与纯空白行为由 tests/test_foundation_gate.py 专门构造。
    """
    root = scaffold_init("测试书", parent=tmp_path)
    from loom import navigator
    navigator.save_foundation(root, {"idea": "夹具默认一句话设定"})
    return root


class FakeBackend:
    """按 (system, user) -> str 的回调产出。on_chunk 给了就回放一次,够流式路径冒烟。"""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_chars=None, on_chunk=None) -> str:
        self.calls.append((system, user))
        out = self.responder(system, user) if callable(self.responder) else self.responder
        if on_chunk and out:
            on_chunk(out)
        return out


def const(value: str):
    return lambda system, user: value


def _scripted_chunks(text: str, size: int = 6) -> list[str]:
    """把 text 切成 ~size 字的小块;额外在每个「用」紧跟「:/：」之间强制切一刀(哪怕落在
    整块内部)——保证流式测试总能驱动到「用」与「:」分两个 chunk 到达的边界场景
    (对话循环的行缓冲纪律必须在此场景下仍不漏协议行)。"""
    cuts = {i + 1 for i, ch in enumerate(text)
            if ch == "用" and i + 1 < len(text) and text[i + 1] in ":："}
    chunks: list[str] = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        for cp in sorted(cuts):
            if start < cp < end:
                end = cp
                break
        chunks.append(text[start:end])
        start = end
    return chunks


class ScriptedBackend:
    """按顺序弹出预置回复(list pop)驱动多轮对话循环。`stream=True` 时把每条回复切成
    小块喂 on_chunk(见 `_scripted_chunks`,故意在「用」「:」之间切一刀),离线钉住
    §5.2 流式行缓冲纪律;`stream=False`(默认)时 on_chunk 给了就整段回放一次,与
    `FakeBackend` 同款「够流式路径冒烟」。回复列表耗尽后返回空串(不 IndexError)——
    对话循环「回喂再 complete」在脚本用完时能优雅终结,不炸测试。
    """

    def __init__(self, replies: list[str], *, stream: bool = False) -> None:
        self.replies = list(replies)
        self.stream = stream
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_chars=None, on_chunk=None) -> str:
        self.calls.append((system, user))
        out = self.replies.pop(0) if self.replies else ""
        if on_chunk and out:
            if self.stream:
                for chunk in _scripted_chunks(out):
                    on_chunk(chunk)
            else:
                on_chunk(out)
        return out


def require_http_transport():
    """starlette TestClient 需要 httpx;openai 3.0 起环境里装的是 httpx2。

    两个都认——只写 importorskip("httpx") 的话,openai 3 环境下这批 endpoint 测试会
    【静默 skip】,覆盖率悄悄掉一块而 CI 依旧是绿的(比红更危险)。
    """
    import importlib
    for name in ("httpx", "httpx2"):
        try:
            importlib.import_module(name)
            return
        except ModuleNotFoundError:
            continue
    pytest.skip("需要 httpx 或 httpx2(starlette TestClient 的传输层依赖)")
