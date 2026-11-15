"""事件契约测试:钉住 引擎↔CLI↔webui 的事件契约(单一真相在 loom/events.py)。

三道锁:
  ① 每个构造函数的键集合 + type 值(改事件形状必先改这里,等于改契约要过会签);
  ② 消费覆盖:每种事件 CLI._render 吃得下(调用不炸),webui handleEvent 源码里
     出现该 type 字符串——或者在带理由的豁免清单里(防"发了没人听"静默丢);
  ③ 引擎源码不许再出现内联 {"type": ...}(防绕过 events.py 回潮)。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from loom import events
from loom.cli import _render

_LOOM_PKG = Path(events.__file__).parent
_APP_JS = _LOOM_PKG / "webui" / "app.js"

# 一条 gate 硬伤的样例(中文键 类别/问题/证据 是 gates.Issue.as_dict 的既有前端契约)
_ISSUE = {"类别": "设定漂移", "问题": "境界名叫错了", "证据": "元婴三层"}

# 契约本体:type → (构造样例入参, 期望键集合)。新增事件必须在这里登记。
CONTRACT: dict[str, tuple[dict, set[str]]] = {
    # 织章流水线
    "pipeline_start": (dict(chapter=3, roles=["设定师", "写手"]), {"type", "chapter", "roles"}),
    "agent_start": (dict(role="写手"), {"type", "role"}),
    "agent_chunk": (dict(role="写手", delta="一段流式字"), {"type", "role", "delta"}),
    "agent_done": (dict(role="写手", produces="本章初稿"), {"type", "role", "produces"}),
    "agent_skip": (dict(role="写手", reason="已完成且上游未变"), {"type", "role", "reason"}),
    "edit_note": (dict(chapter=3, path=Path("/tmp/留痕.md")), {"type", "chapter", "path"}),
    "overlong": (dict(chapter=3, chars=1100, target=800), {"type", "chapter", "chars", "target"}),
    "debug_report": (dict(chapter=3, issues=[_ISSUE], path=Path("/tmp/留痕.md")),
                      {"type", "chapter", "issues", "path"}),
    "sensitive": (dict(chapter=3, count=2, hits=[{"word": "示例", "count": 2}]),
                  {"type", "chapter", "count", "hits"}),
    "chapter_done": (dict(chapter=3, path=Path("/tmp/第3章.md"), title="标题", chars=2500,
                          preview="终稿前 300 字", text="# 标题\n\n正文"),
                     {"type", "chapter", "path", "title", "chars", "preview", "text"}),
    "outline_done": (dict(chapter=3), {"type", "chapter"}),
    # 质量关卡
    "gate_start": (dict(label="质检", role="编辑", round=1), {"type", "label", "role", "round"}),
    "gate_pass": (dict(label="质检", role="编辑", round=1), {"type", "label", "role", "round"}),
    "gate_issues": (dict(label="质检", role="编辑", round=1, issues=[_ISSUE]),
                    {"type", "label", "role", "round", "issues"}),
    "gate_revise": (dict(label="质检", role="编辑", round=1), {"type", "label", "role", "round"}),
    "gate_exhausted": (dict(label="质检", role="编辑", rounds=2, issues=[_ISSUE]),
                       {"type", "label", "role", "rounds", "issues"}),
    # 外置大脑起草 / 指纹 / learn 附赠动作
    "draft_done": (dict(written=["世界观"], skipped=["人物卡"]), {"type", "written", "skipped"}),
    "seed_done": (dict(path=Path("/tmp/指纹.md"), source="sample"), {"type", "path", "source"}),
    "learn_done": (dict(path=Path("/tmp/指纹.md"), chapter=3, shrink_warning=""),
                   {"type", "path", "chapter", "shrink_warning"}),
    "recap_done": (dict(chapter=3, path=Path("/tmp/卡章纲.md")), {"type", "chapter", "path"}),
    "recap_skip": (dict(chapter=3), {"type", "chapter"}),
    "enrich_done": (dict(chapter=3, 世界观="新增一条设定", 人物卡=""),
                    {"type", "chapter", "世界观", "人物卡"}),
    "enrich_skip": (dict(chapter=3), {"type", "chapter"}),
    # 通用
    "info": (dict(message="正在干活…"), {"type", "message"}),
    "warn": (dict(message="有点问题,不阻断"), {"type", "message"}),
    "error": (dict(message="意外错误"), {"type", "message"}),
    # 2026-11-03 补登记:作者叫停事件（events.cancelled 已上线跑,契约漏了它——
    # registry 比契约多出 cancelled 被本文件第 85 行断言拦下。补上会签。）
    "cancelled": (dict(chapter=3, at_role="编辑", half_path=None), {"type", "chapter", "role", "half"}),
    # 2026-11-08 任务准备架构（§9）:流水线域 type 信封任务事件,载荷与对话/书房 t 信封同构
    "task_prepared": (dict(task_id="t-1", revision=1, scope={"domain": "pipeline"},
                           request_id="r1", goal="写第三章"),
                      {"type", "task_id", "revision", "scope", "request_id", "goal"}),
    "plan_updated": (dict(task_id="t-1", revision=2, scope={"domain": "pipeline"},
                          request_id="r1", summary="计划 v2:共 3 步"),
                     {"type", "task_id", "revision", "scope", "request_id", "summary"}),
    "task_status": (dict(task_id="t-1", revision=3, scope={"domain": "pipeline"},
                         request_id="r1", status="running", stop_reason=""),
                    {"type", "task_id", "revision", "scope", "request_id",
                     "status", "stop_reason"}),
}

# webui 有意不消费的事件(handleEvent 只吃 /api/write 的 ndjson 流;下面这些流程
# 在 server 侧走 JSON 端点——要么不传 progress,要么收集后拼进 JSON 响应):
WEBUI_EXEMPT: dict[str, str] = {
    "seed_done": "seed 端点不走流式:server 调 seed_from_* 不传 progress,webui 靠 JSON 响应刷新",
    "learn_done": "learn 端点把事件收进列表、以 JSON 返回(server 提取 shrink_warning),webui 消费 JSON",
    "recap_done": "recap 在 learn 内部跑,webui 从 learn JSON 响应的「卡章纲」字段读结果",
    "recap_skip": "同 recap_done:跳过与否 webui 不关心",
    "enrich_done": "enrich 在 learn 内部跑,webui 从 learn JSON 响应的 世界观补充/人物卡补充 读结果",
    "enrich_skip": "同 enrich_done:跳过与否 webui 不关心",
    "draft_done": "brain_draft 端点以 JSON 返回 written/skipped,不走事件流",
    "outline_done": "webui 有自己的按钮态(outline_regen 端点 JSON 返回),有意不消费(见 events.py 模块头)",
    "overlong": "0.3.7 新增,前端提示待接",
}


def test_registry_matches_contract():
    """EVENT_TYPES 注册表与契约表一一对应:新事件两边都要登记,删事件两边都要删。"""
    assert set(events.EVENT_TYPES) == set(CONTRACT)


@pytest.mark.parametrize("etype", sorted(CONTRACT))
def test_constructor_keys_and_type(etype):
    """① 钉住每个构造函数:type 值 = 注册名,键集合 = 契约,path 一律落成 str。"""
    kwargs, expected_keys = CONTRACT[etype]
    ev = events.EVENT_TYPES[etype](**kwargs)
    assert ev["type"] == etype
    assert set(ev) == expected_keys
    if "path" in ev:
        assert isinstance(ev["path"], str)  # 事件要能过 json.dumps(ndjson 流),Path 必须先落 str


@pytest.mark.parametrize("etype", sorted(CONTRACT))
def test_cli_render_accepts_every_event(etype):
    """② CLI 侧覆盖:每种事件喂给 _render 都不炸(没分支的静默忽略也算既有行为)。

    选"调用后无异常"而非源码扫描:_render 对未知 type 本就静默放过(fire-and-forget
    不变量),真正要防的是【有分支但字段名对不上】的 KeyError。
    """
    kwargs, _ = CONTRACT[etype]
    _render(events.EVENT_TYPES[etype](**kwargs))


def _handle_event_body() -> str:
    """从 app.js 里按花括号配平抠出 handleEvent 函数体(webui 消费事件的唯一入口)。"""
    src = _APP_JS.read_text(encoding="utf-8")
    start = src.index("function handleEvent(ev)")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("app.js 的 handleEvent 花括号没配平——先修 JS 再谈契约")


def test_webui_handles_every_event_or_exempt():
    """② webui 侧覆盖:每个 type 要么出现在 handleEvent 源码里,要么在豁免清单(带理由)。"""
    body = _handle_event_body()
    for etype in sorted(CONTRACT):
        consumed = f'"{etype}"' in body
        if etype in WEBUI_EXEMPT:
            # 豁免过期检查:哪天 webui 真消费了,就把它从豁免清单里删掉
            assert not consumed, f"{etype} 已在 handleEvent 里被消费,豁免清单过期了"
        else:
            assert consumed, (f"事件 {etype} 在 webui handleEvent 里没有分支,又不在豁免清单——"
                              f"新事件会被 webui 静默丢,要么消费、要么带理由豁免")


def test_no_inline_type_dict_in_engine():
    """③ 防回潮:引擎源码(loom/*.py)不许再有内联 {"type": ...},发事件只能走 events.py。"""
    pat = re.compile(r"""\{\s*["']type["']\s*:""")
    # 非事件豁免（2026-11-03 批D 复核逐个确认过，都不是流事件）:
    #   agent_chat.py  — tool 协议的 JSON Schema（parameters.properties）
    #   backends.py    — 视觉探测的 OpenAI API payload（content 数组）
    #   guides.py      — 引导注册表的 when/done_when 判定描述（结构化数据，非事件）
    #   partner_tools.py — 工具合一后的 OpenAI tools schema 生成（schema_of，0.9.0 起agent_chat/ask/study.tools 共用这里，与 agent_chat 同性质）
    _NOT_EVENTS = {"agent_chat.py", "backends.py", "guides.py", "partner_tools.py"}
    offenders = []
    for py in sorted(_LOOM_PKG.glob("*.py")):
        if py.name == "events.py":  # 唯一豁免:契约本体自己
            continue
        if py.name in _NOT_EVENTS:
            continue
        if pat.search(py.read_text(encoding="utf-8")):
            offenders.append(py.name)
    assert not offenders, f"这些文件绕过 events.py 内联发事件了:{offenders}"
