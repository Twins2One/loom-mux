# -*- coding: utf-8 -*-
"""工具协议可以**只钉协议、不动模型**(2026-10-21 热修复的回归钉)。

真机现象:解压版里把某一格的「工具协议」从默认改成 native,点保存 →
界面弹「现在是跟随默认」,下拉框又跳回 text。
根因两处:
  ① 前端 `saveRoute` 判「跟随默认」时只比 provider/model/base_url,把协议算漏了
     ⇒ 只改协议时 body.tool_protocol 被清空;
  ② 后端 `resolve()` 只回一个「没钉就填 text」的值 ⇒ 跟随默认的格子在界面上恒显示 text,
     作者看到「又回 text」,以为没存进去。
这里钉后端那一半(前端是 app.js,由 selfcheck 的语法检查 + 人工复核兜):只钉协议也要真的存住、
并且**回报的口径要区分「钉住的」与「生效的」**。
"""
from __future__ import annotations

from loom import routes
from loom.config import load_config


def test_只钉工具协议也能写进loom_toml(project):
    r = routes.set_route(load_config(project), "写作", tool_protocol="native")
    assert r["ok"] is True
    assert r["routes"]["写作"]["tool_protocol"] == "native"
    # 模型三框仍然空着(= 跟随默认),不被协议带着走
    assert not r["routes"]["写作"].get("model")
    assert not r["routes"]["写作"].get("provider")


def test_钉住协议后_resolve_把钉住的与生效的分开报(project):
    cfg = load_config(project)
    routes.save_routes(project, cfg, routes.set_route(cfg, "写作", tool_protocol="native")["routes"])
    row = routes.resolve(load_config(project), "写作")
    assert row["写明"]["tool_protocol"] == "native"   # 作者钉住的那个
    assert row["工具协议"] == "native"                # 实际生效的那个
    assert row["工具协议已钉"] is True
    assert row["跟随默认"] is True                    # 模型仍然跟随默认 —— 两件事


def test_没钉协议时写明给空串而不是text(project):
    """界面靠这个值决定下拉框显示「默认(自动)」还是具体一项;填 text 就会假显示成钉了 text。"""
    row = routes.resolve(load_config(project), "写作")
    assert row["写明"]["tool_protocol"] == ""
    assert row["工具协议已钉"] is False
    assert row["工具协议"] == "text"      # 生效值仍是文本协议(后端不认 tools 时的行为)


def test_四框全空才恢复跟随默认(project):
    cfg = load_config(project)
    cfg = routes.save_routes(project, cfg, routes.set_route(cfg, "写作", tool_protocol="json")["routes"])
    assert "写作" in load_config(project).routes
    empty = routes.set_route(cfg, "写作")
    assert empty["跟随默认"] is True
    assert "写作" not in empty["routes"]


def test_对话槽钉了native后preferred识别它(project):
    cfg = load_config(project)
    routes.save_routes(project, cfg, routes.set_route(cfg, "对话", tool_protocol="native")["routes"])
    assert routes.preferred_tool_protocol(load_config(project)) == "native"


def test_协议只认三档(project):
    bad = routes.set_route(load_config(project), "写作", tool_protocol="yaml")
    assert bad["ok"] is False and "工具协议" in bad["reason"]
