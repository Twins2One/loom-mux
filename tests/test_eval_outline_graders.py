# -*- coding: utf-8 -*-
"""细纲三棒体检项（2026-10-26，设计稿 v2）。

这些测试同时是**判据的可红证明**：每条 gating 都要有一个"故意做坏"的反例能把它打红
——只有正例的测试证明不了判据有分辨力（实测那 5 章全是好样本，min 覆盖率 0.993）。
"""
from __future__ import annotations

import pytest

from evals.aggregate import aggregate_runs
from evals.generate import grade_steps
from evals.stepgraders import (
    _coverage,
    _norm_field_name,
    grade_outline_1,
    grade_outline_2,
    grade_outline_3,
    rules_sha,
)


# ── 夹具：一份"真机形状"的细纲（字段带粗体、场景标题带场次预算括注） ──────────
def _scene(n: int, title: str = "下水道", budget: int = 800) -> str:
    return f"""### 场景 {n}：{title}（约{budget}字）
- **地点**：下水道
- **时间**：夜
- **参与者**：顾明
- **氛围**：湿、暗
- **情节**：
  1. 他往前爬。
  2. 他听见人声。
- **人物反应**：他不敢停。
- **转场方式**：硬切
"""


def _outline(n_scene: int = 2, *, hook: str = "人声从井盖缝里传下来",
             fields: tuple[str, ...] = ("地点", "时间", "参与者", "氛围", "情节", "人物反应", "转场方式"),
             drop_top: str = "") -> str:
    scenes = []
    for i in range(1, n_scene + 1):
        blk = _scene(i)
        for f in ("地点", "时间", "参与者", "氛围", "情节", "人物反应", "转场方式"):
            if f not in fields:
                blk = "\n".join(l for l in blk.splitlines() if f"**{f}**" not in l)
        scenes.append(blk)
    tops = [("基本信息", "- 章节类型：铺陈\n- 预计字数：3000\n- 核心事件：顾明听见人声"),
            ("情绪曲线", "- 起始情绪：闷\n- 结束情绪：紧\n- 关键转折点：场景 1 听见人声"),
            ("场景列表", "\n".join(scenes)),
            ("关键细节", f"- 伏笔：手电\n- 钩子：{hook}\n- 必写细节：手电是温的"),
            ("写作提示", "- 开篇：从身体感受进\n- 节奏：慢\n- 重点：闷")]
    body = ["# 第 1 章细纲：爬"]
    for title, text in tops:
        if title == drop_top:
            continue
        body.append(f"## {title}\n{text}")
    return "\n\n".join(body)


COMMENTS = ("<!-- 分镜: 贴近地面 / 低角度 / 顾明 -->\n"
            "<!-- 视角: 限制性第三人称 -->\n"
            "<!-- 镜头: 远景→中景→特写 -->\n"
            "<!-- 节奏: 慢动作 -->\n")


def _step3(base: str | None = None, *, keep: str = "四", extra: str = "", drop_scene: int = 0) -> str:
    text = base if base is not None else _outline(2)
    parts = [text]
    for k in ("分镜", "视角", "镜头", "节奏"):
        if keep == "四" or k in keep:
            parts.append(f"<!-- {k}: 说明 -->")
    out = "\n".join(parts)
    if drop_scene:
        blocks = out.split("### 场景 ")
        out = "### 场景 ".join(b for i, b in enumerate(blocks) if i != drop_scene)
    return out + extra


# ── 第 1 棒 ────────────────────────────────────────────────────────────────
def test_第1棒好样本全过():
    res = grade_outline_1(_outline(2))
    assert len(res) == 10
    gated = [g for g in res if g.gating]
    assert len(gated) == 1 and gated[0].passed
    assert "章级五节" in gated[0].name


def test_第1棒缺一节就把gating打红():
    res = grade_outline_1(_outline(2, drop_top="情绪曲线"))
    g = next(x for x in res if x.gating)
    assert not g.passed and "情绪曲线" in g.detail


def test_第1棒钩子为空也打红():
    res = grade_outline_1(_outline(2, hook=""))
    g = next(x for x in res if x.gating)
    assert not g.passed and "钩子" in g.detail


def test_字段名带括注不算缺字段_真机41处():
    """实测回归：`**情节（切入点）**` 必须被认成「情节」，否则整条判据全是假阴性。"""
    text = _outline(1).replace("- **情节**：", "- **情节（切入点）**：")
    res = grade_outline_1(text)
    fld = next(x for x in res if "有值率" in x.name)
    assert fld.score == 1.0, fld.detail


def test_氛围漏了只扣分不打红():
    res = grade_outline_1(_outline(2, fields=("地点", "时间", "参与者", "情节", "人物反应", "转场方式")))
    fld = next(x for x in res if "有值率" in x.name)
    assert 0.7 <= fld.score < 1.0
    assert fld.gating is False           # 场景级永远不做 gating
    assert "氛围" in fld.detail


def test_大段引语算偷写正文():
    text = _outline(1).replace("  1. 他往前爬。", "  1. 他说：「" + "我" * 120 + "」")
    res = grade_outline_1(text)
    assert not next(x for x in res if "偷写正文" in x.name).passed


def test_情节编号跳号():
    text = _outline(1).replace("  1. 他往前爬。\n  2. 他听见人声。", "  1. 他往前爬。\n  3. 他听见人声。")
    res = grade_outline_1(text)
    assert not next(x for x in res if "编号连续" in x.name).passed


def test_转折点引用不存在的场景():
    text = _outline(2).replace("关键转折点：场景 1 听见人声", "关键转折点：场景 9 听见人声")
    res = grade_outline_1(text)
    assert not next(x for x in res if "转折点不越界" in x.name).passed


def test_名册相关的三项没有名册时记测不出():
    res = grade_outline_1(_outline(2), roster=None)
    for name in ("核心事件有着落", "必写细节落地", "参与者与情节一致"):
        g = next(x for x in res if name in x.name)
        assert g.detail.startswith("[not-measurable]"), (name, g.detail)
        assert g.gating is False and g.passed is True


def test_核心点名册角色但正文没有():
    text = _outline(1).replace("核心事件：顾明听见人声", "核心事件：鲁恩听见人声")
    res = grade_outline_1(text, roster=["鲁恩"])
    g = next(x for x in res if "核心事件有着落" in x.name)
    assert not g.passed and "鲁恩" in g.detail


def test_场景标题几乎一样会提示():
    text = _outline(2).replace("### 场景 2：下水道", "### 场景 2：下水道里")
    res = grade_outline_1(text)
    assert not next(x for x in res if "标题不重复" in x.name).passed


def test_第1棒缺席记skipped而不是0分():
    res = grade_outline_1(None)
    assert all(x.detail.startswith("[skipped]") for x in res)
    assert all(x.gating is False for x in res)


# ── 第 2 棒 ────────────────────────────────────────────────────────────────
def test_第2棒丢场景标题把gating打红():
    prev = _outline(3)
    cur = _outline(2)                     # 少一个场景
    res = grade_outline_2(cur, prev)
    g = next(x for x in res if x.gating)
    assert not g.passed and "场景标题" in g.name


def test_第2棒原样照抄不算丢标题但会被记为没干活():
    prev = _outline(2)
    res = grade_outline_2(prev, prev)      # 一字不改
    assert next(x for x in res if x.gating).passed
    assert not next(x for x in res if "有实质增补" in x.name).passed


def test_第2棒换字段名算契约漂移():
    prev = _outline(1)
    cur = prev.replace("- **氛围**：湿、暗", "- **进入方式**：从气味进").replace(
        "- **人物反应**：他不敢停。\n- **转场方式**：硬切", "")
    res = grade_outline_2(cur, prev)
    d = next(x for x in res if "契约漂移" in x.name)
    assert not d.passed and "进入方式" in d.detail


def test_第2棒把某场整段重写算原文被替换():
    prev = _outline(1)
    cur = prev.replace("  1. 他往前爬。\n  2. 他听见人声。", "  1. 完全不同的一件事发生了。\n  2. 又是另一件。") \
             .replace("他不敢停。", "他反而停下了。") \
             .replace("湿、暗", "干净、亮") \
             .replace("下水道", "剧院")
    res = grade_outline_2(cur, prev)
    assert not next(x for x in res if "原文被替换" in x.name).passed


def test_第2棒混进对写手说话():
    """口径：细纲**本来就是**给写手的指令，所以只抓"对写手本人的口吻"（务必/建议你/请确认），
    不抓「注意力」「注意」这类词——实测第一版用 `注意` 撞上「注意力被拽走」，两章假阳性。"""
    prev = _outline(1)
    cur = prev.replace("- **地点**：下水道",
                       "- **地点**：下水道\n- 务必第三章前把这里压住，建议你先别写对白")
    res = grade_outline_2(cur, prev)
    assert not next(x for x in res if "指令性语言" in x.name).passed


def test_注意力这种词不算对写手说话():
    prev = _outline(1)
    cur = prev.replace("他不敢停。", "他的注意力被气味拽走，逼自己不停。")
    res = grade_outline_2(cur, prev)
    assert next(x for x in res if "指令性语言" in x.name).passed


def test_第2棒没有上一棒时记测不出():
    res = grade_outline_2(_outline(1), None)
    g = next(x for x in res if "场景标题只增不减" in x.name)
    assert g.detail.startswith("[not-measurable]") and g.passed   # 记"测不出",不记 0 分


# ── 第 3 棒 ────────────────────────────────────────────────────────────────
def test_第3棒四类注释齐备则过():
    res = grade_outline_3(_step3(), _outline(2))
    assert all(x.passed for x in res if x.gating)


def test_第3棒缺一类注释就打红():
    res = grade_outline_3(_step3(keep="分镜视角镜头"), _outline(2))
    g = next(x for x in res if "四类分镜注释" in x.name)
    assert not g.passed and "节奏" in g.detail


def test_第3棒丢掉第1棒的场景就打红_传递性():
    base = _outline(3)
    prev = base
    res = grade_outline_3(_step3(base, drop_scene=2), prev, base)
    g = next(x for x in res if "含第1棒全部" in x.name)
    assert not g.passed


def test_第3棒没保留设定注释():
    prev = _outline(1) + "\n<!-- 设定: 顾明的子体碎了 -->\n"
    cur = _outline(1) + "\n<!-- 分镜: x -->"
    res = grade_outline_3(cur, prev)
    assert not next(x for x in res if "保留了设定注释" in x.name).passed


def test_注释未闭合会被抓到():
    res = grade_outline_3(_step3() + "\n<!-- 分镜: 没写完", _outline(2))
    assert not next(x for x in res if "注释未闭合" in x.name).passed


def test_逐场注释覆盖只报分布不判定():
    res = grade_outline_3(_step3(), _outline(2))
    g = next(x for x in res if "逐场注释覆盖" in x.name)
    assert g.gating is False and "场带注释" in g.detail


# ── 归因（reds / severity / weakest / ties）───────────────────────────────
def _steps_ok() -> dict:
    return {
        "章节细纲师": _outline(2),
        "章节设定师": _outline(2) + "\n<!-- 设定: 补充 -->\n",
        "章节分镜师": _step3(_outline(2) + "\n<!-- 设定: 补充 -->\n"),
        "写手": "正文" * 300,
        "责任编辑": "正文" * 300,
        "润色师": "正文" * 300,
    }


def test_grade_steps覆盖六棒且带版本与判据指纹():
    rep = grade_steps(_steps_ok(), {"chapter_chars": 800})
    assert set(rep["steps"]) == {"章节细纲师", "章节设定师", "章节分镜师",
                                 "写手", "责任编辑", "润色师"}
    assert rep["report_version"] == "v3"
    assert len(rep["rules_sha"]) == 16
    assert isinstance(rep["reds"], list)


def test_最弱棒用棒内严重度而不是跨棒权重():
    steps = _steps_ok()
    steps["章节分镜师"] = _outline(2)                     # 四类注释全缺 → 一条 gating 红
    rep = grade_steps(steps, {"chapter_chars": 800})
    assert rep["reds"], rep
    assert rep["reds"][0]["role"] == "章节分镜师"
    assert 0 < rep["reds"][0]["severity"] <= 1.0
    assert rep["weakest"] == "章节分镜师"


def test_两棒都红且严重度接近时报并列而不是硬挑一个():
    steps = _steps_ok()
    steps["章节分镜师"] = _outline(1)                    # 注释缺 + 丢一场 → 2/2 红 = 1.0
    steps["章节细纲师"] = _outline(2, hook="")           # 钩子空 → 1/1 红 = 1.0
    rep = grade_steps(steps, {"chapter_chars": 800})
    assert rep["weakest"] is None
    assert set(rep["weakest_ties"]) == {"章节细纲师", "章节分镜师"}


def test_全过时没有红棒也没有最弱棒():
    rep = grade_steps(_steps_ok(), {"chapter_chars": 800})
    assert rep["reds"] == [] and rep["weakest"] is None and rep["weakest_ties"] == []


def test_汇总能读出red_rate与tie_rate():
    a = grade_steps(_steps_ok(), {"chapter_chars": 800})
    b = {**_steps_ok(), "章节分镜师": _outline(2)}
    b = grade_steps(b, {"chapter_chars": 800})
    agg = aggregate_runs([a, b])
    assert agg["red_rate"].get("章节分镜师") == 0.5
    assert "weakest" in agg


# ── 判据指纹 ───────────────────────────────────────────────────────────────
def test_判据指纹稳定且是16位():
    assert rules_sha() == rules_sha()
    assert len(rules_sha()) == 16 and all(c in "0123456789abcdef" for c in rules_sha())


def test_字段名归一化与覆盖率():
    assert _norm_field_name("**情节（切入点）**") == "情节"
    assert _norm_field_name("设定（备查，本章") == "设定"
    assert _coverage("今天下雨了", "今天下雨了，路上很滑") == 1.0
    assert _coverage("今天下雨了", "完全不同的一篇文章") < 0.3
