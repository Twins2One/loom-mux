"""OpenAI 兼容后端的显式超时:SDK 默认 600s 太长,挂了要等 10 分钟;超时映射成友好错误。"""
from __future__ import annotations

import pytest

from loom.backends import _deepseek_error, _openai_compat_error


def test_client_has_explicit_timeout(monkeypatch):
    pytest.importorskip("openai")
    from loom.backends import OpenAICompatBackend
    from loom.config import Config

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    be = OpenAICompatBackend(Config(provider="deepseek", model="deepseek-v4-pro"), "deepseek")
    t = be._client.timeout
    assert t.connect == 10.0
    assert t.read == 120.0                      # 非流式整体 120s,不再是 SDK 默认 600s
    assert be._stream_timeout.read == 300.0     # 流式按 chunk 间隔计 read,放宽
    assert be._stream_timeout.connect == 10.0


def test_timeout_maps_to_friendly_error():
    err = _deepseek_error(Exception("Request timed out."))
    assert err.code == "deepseek_timeout"
    assert "超时" in str(err)

    err2 = _openai_compat_error(Exception("Request timed out."))
    assert err2.code == "model_timeout"
    assert "超时" in str(err2)


def test_other_errors_still_map_as_before():
    assert _deepseek_error(Exception("rate limit exceeded")).code == "deepseek_rate_limited"
    assert _deepseek_error(Exception("boom")).code == "deepseek_call_failed"
    assert _openai_compat_error(Exception("boom")).code == "model_call_failed"


class _Rejected(Exception):
    """形状贴近 SDK 的鉴权异常:有 status_code,消息里带供应商原话。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status


def test_key_rejected_is_not_reported_as_key_missing():
    """401/403 = key 填了但被拒,不能报成「还没填」——两者下一步动作完全不同。

    现场(2026-10-28 供应商 token 失效):报「还没填 key」会让作者把同一把 key 再填一遍,
    一字不差地再失败一次,永远走不到「去后台查 token」。
    """
    err = _openai_compat_error(_Rejected(401, 'Invalid token'))
    assert err.code == "openai_compat_key_rejected"
    text = str(err)
    assert "还没填" not in text
    assert "被供应商拒" in text
    assert "Invalid token" in text            # 供应商原话与 HTTP 细节必须留痕
    assert "后台" in text                     # 下一步指向供应商后台,而不是顶栏再填一遍

    assert _openai_compat_error(_Rejected(403, 'forbidden')).code == "openai_compat_key_rejected"
    # 没有 status、只有原话时也要认出来
    assert _openai_compat_error(Exception("invalid token")).code == "openai_compat_key_rejected"


def test_key_truly_missing_keeps_its_own_code(monkeypatch):
    """构造期就拦下(没发请求)才叫「没填」,这个码不能变。"""
    pytest.importorskip("openai")
    from loom.backends import LoomBackendError, OpenAICompatBackend
    from loom.config import Config

    monkeypatch.delenv("LOOM_OPENAI_COMPAT_KEY", raising=False)
    with pytest.raises(LoomBackendError) as ei:
        OpenAICompatBackend(Config(provider="openai_compat", model="glm-4",
                                   base_url="https://open.bigmodel.cn/api/paas/v4"), "openai_compat")
    assert ei.value.code == "openai_compat_key_missing"
    assert "还没填" in str(ei.value)

