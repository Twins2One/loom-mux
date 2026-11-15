"""地基门异常配置：缺配置文件 / 损坏 toml 都必须给结构化、可理解的响应，不得 500。

审查 审查记录：
① foundation_progress 在配置缺失/损坏时缺「缺失项详情」→ 取用方 KeyError；
② 三 HTTP 入口在「无 loom.toml」是 400、「有但损坏的 toml」是 500，要求归一。
"""
from __future__ import annotations

import pytest

from conftest import require_http_transport

from loom import navigator

_KEYS = ("done", "完成项", "缺失项", "缺失项详情")


def _broken_project(tmp_path):
    root = tmp_path / "坏配置书"
    root.mkdir()
    (root / "loom.toml").write_text("this is = not [ valid toml\n", encoding="utf-8")
    return root


def test_foundation_progress_structure_is_stable_without_config(tmp_path):
    missing_root = tmp_path / "没有配置文件"
    missing_root.mkdir()
    progress = navigator.foundation_progress(missing_root)
    for key in _KEYS:
        assert key in progress, f"缺配置时缺少键 {key}"
    assert progress["done"] is False


def test_foundation_progress_structure_is_stable_with_broken_config(tmp_path):
    progress = navigator.foundation_progress(_broken_project(tmp_path))
    for key in _KEYS:
        assert key in progress, f"损坏配置时缺少键 {key}"
    assert progress["done"] is False


def test_foundation_block_never_raises_on_broken_config(tmp_path):
    """异常配置下必须返回结构化结果，不得抛 KeyError。"""
    block = navigator.foundation_block(_broken_project(tmp_path), 1)
    assert isinstance(block, dict)
    assert block["code"] in ("config_unreadable", "foundation_incomplete", "not_a_book")
    assert isinstance(block.get("missing", []), list)
    assert isinstance(block.get("missing_details", []), list)


def _client():
    require_http_transport()
    from starlette.testclient import TestClient
    import loom.server as server
    return TestClient(server.app, base_url="http://127.0.0.1")


def _chapter_one_responses(root):
    client = _client()
    cases = [
        ("/api/write", {"root": str(root), "chapter": 1}),
        ("/api/chapter/run-step", {"root": str(root), "chapter": 1, "step": "责任编辑"}),
        ("/api/chapter/step", {"root": str(root), "chapter": 1, "step": "责任编辑"}),
    ]
    return [(url, client.post(url, json=body)) for url, body in cases]


def test_three_entrances_agree_on_missing_and_broken_config(tmp_path):
    """两种情况响应码一致、都不进 worker、都在开流前返回。"""
    without = tmp_path / "没有配置文件"
    without.mkdir()
    broken = _broken_project(tmp_path)

    missing_codes, broken_codes = set(), set()
    for url, r in _chapter_one_responses(without):
        assert r.status_code != 500, f"{url} 缺配置返回 500"
        assert not r.headers.get("content-type", "").startswith("application/x-ndjson"), \
            f"{url} 在缺配置时开了流（应开流前返回）"
        missing_codes.add(r.status_code)
    for url, r in _chapter_one_responses(broken):
        assert r.status_code != 500, f"{url} 损坏配置返回 500"
        assert not r.headers.get("content-type", "").startswith("application/x-ndjson"), \
            f"{url} 在损坏配置时开了流（应开流前返回）"
        broken_codes.add(r.status_code)

    assert missing_codes == broken_codes, f"两种配置异常响应码不一致:{missing_codes} vs {broken_codes}"
    assert missing_codes == {400} or missing_codes == {409}, missing_codes
