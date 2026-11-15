# -*- coding: utf-8 -*-
"""书房伙伴的工具（0.8.3，：「看多个教材不该调用工具吗」）。

守四件：
1.三把工具全只读，**不写盘**（这是作者长期约束：提示词里不得涉及高危操作）；
2.「读一册」只放行知识库内的 .md（工具参数是模型填的，不能当可信输入）；
3.工具坏了不打断整轮（run永不抛，回一句话让模型接着说）；
4.文本协议能解析出工具调用，原生协议能走 function calling。
"""
from __future__ import annotations

import pytest

from loom.study import tools as T


def test_three_readonly_tools():
    #2026-10-31：加了「查这本书」——书籍的信息让对应书籍的角色便于搜
    assert set(T.TOOLS) == {"列书册", "搜书房", "读一册", "查这本书"}
    for name, spec in T.TOOLS.items():
        assert callable(spec["run"]), name
        assert spec["desc"], name


def test_schema_is_openai_shape():
    sc = T.schema()
    assert len(sc) ==4
    for item in sc:
        assert item["type"] == "function"
        fn = item["function"]
        assert fn["name"] in {v: k for k, v in T.TOOL_ASCII.items()}, fn["name"]  # 2026-10-31：出口用 ASCII 名
        assert fn["parameters"]["type"] == "object"


def test_unknown_tool_fails_softly():
    got = T.run("不存在的工具", {})
    assert got["ok"] is False
    assert "没有这个工具" in got["text"]


def test_read_book_rejects_outside_path(tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("不该被读到", encoding="utf-8")
    got = T.run("读一册", {"路径": str(outside)})
    assert got["ok"] is True
    assert "不该被读到" not in got["text"]


def test_read_book_rejects_non_markdown(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("txt", encoding="utf-8")
    got = T.run("读一册", {"路径": str(p)})
    assert "txt" not in got["text"]


def test_text_protocol_parses_tool_lines():
    from loom.study import ask as A
    text = "用：搜书房" + chr(10) + "查询：非线性叙事"
    got = A._parse_text_tools(text)
    assert got == [("搜书房", {"查询": "非线性叙事"})]


def test_text_protocol_ignores_plain_reply():
    from loom.study import ask as A
    assert A._parse_text_tools("这是一句普通回答。") == []


def test_contract_lists_all_tools():
    c = T.contract()
    for name in T.TOOLS:
        assert name in c

