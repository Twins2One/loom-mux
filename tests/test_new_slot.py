# -*- coding: utf-8 -*-
"""「按作者的想法长出新项」——地基卡里没有的东西也能落。

以前:`提设定` 的落点必须是 `看地基` 里已有的槽,角色想记一件卡外的事就无处可落
(报「落点不存在」)。现在两条新路,都仍然只出候选卡、作者拍板才落盘:
  A. 已有设定文件里**新加一节**:`<已有 .md>#<新小节名>`
  B. **新建一份**:`外置大脑/设定集/50-自定义/<名字>.md#<小节名|@body>`
"""
from __future__ import annotations

import pytest

from loom import partner_store as ps
from loom import paths, usecases
from loom import partner_tools as pt

CUSTOM = paths.SET_CUSTOM_DIR_REL


def _propose(project, 落点, 内容, ts="t1"):
    """跑一次提设定并把候选卡落进 store(对话循环平时干的就是这一步)。"""
    payload = pt.run_tool(project, "提设定", {"落点": 落点, "内容": 内容}, ts=ts)
    assert payload["t"] == "proposal", payload
    ps.append_event(project, {"t": "proposal", "ts": ts, "id": payload["id"], **{
        k: v for k, v in payload.items() if k in ("slot", "content", "before", "new")}})
    return payload


def test_新文件_拍板后真的建出来并进侧栏(project):
    payload = _propose(project, f"{CUSTOM}/灵脉志.md#分布", "灵脉沿地脉走向,越往北越稀。")
    assert payload.get("new", {}).get("kind") == "file"
    out = usecases.partner_confirm(project, payload["id"], ts="t2")
    assert "error" not in out, out
    p = project / CUSTOM / "灵脉志.md"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# 灵脉志")
    assert "## 分布" in text and "越往北越稀" in text
    # 侧栏能看到这一格
    entries = usecases._brain_entries(project)
    custom = next(e for e in entries if e.get("name") == "自定义")
    assert any(k["name"] == "灵脉志" for k in custom["children"])


def test_新文件_不带小节名也能量(project):
    payload = _propose(project, f"{CUSTOM}/杂记.md", "灵兽不认主,只认喂食的人。")
    assert payload["new"]["kind"] == "file"
    out = usecases.partner_confirm(project, payload["id"], ts="t2")
    assert "error" not in out, out
    text = (project / CUSTOM / "杂记.md").read_text(encoding="utf-8")
    assert text.startswith("# 杂记") and "只认喂食的人" in text


def test_已有文件里加一节(project):
    rel = f"{paths.SET_WORLD_DIR_REL}/力量体系.md"
    before = (project / rel).read_text(encoding="utf-8")
    payload = _propose(project, f"{rel}#灵脉", "灵脉枯竭是有人在抽——这是本书的暗线。")
    assert payload["new"]["kind"] == "h2"
    out = usecases.partner_confirm(project, payload["id"], ts="t2")
    assert "error" not in out, out
    after = (project / rel).read_text(encoding="utf-8")
    assert after.startswith(before.rstrip()[:40])          # 原有内容一字不动
    assert "## 灵脉" in after and "有人在抽" in after


def test_已有槽位仍走老路_不回归(project):
    """老路径:落点就是看地基里的真实槽 → 候选卡不该带 new。"""
    slot_id = None
    for spec in __import__("loom.journey", fromlist=["STAGES"]).STAGES:
        from loom import slots
        for s in slots.stage_slots(project, spec):
            slot_id = s.id
            break
        if slot_id:
            break
    payload = _propose(project, slot_id, "随便填个值")
    assert not payload.get("new")
    assert usecases.partner_confirm(project, payload["id"], ts="t2").get("error") is None


@pytest.mark.parametrize("bad", [
    "正文/第1章.md#备注",                       # 正文不许
    "agents/写手.md#增补",                      # 角色提示词不许
    f"{paths.SET_DIR}/../外置大脑/写作指纹.md#x",  # 穿越
    f"{CUSTOM}/笔记.txt#x",                     # 非 .md
    f"{paths.SET_CHARS_DIR_REL}/新角色.md#软肋",  # 人物目录不许凭空建
    f"{paths.SET_CARD_REL}#新节",               # 行式骨架不许塞 H2
])
def test_不合法的新落点被拒(project, bad):
    from loom import slots as slots_mod

    with pytest.raises(ValueError):
        slots_mod.plan_new_slot(project, bad)
    # 产品口径:run_tool 绝不抛,一律回可回喂的错误事件(模型自纠)
    ev = pt.run_tool(project, "提设定", {"落点": bad, "内容": "x"}, ts="t")
    assert ev["t"] == "result" and ev.get("error"), ev


def test_落盘不覆盖已有正文(project):
    """极窄窗口:提案挂起期间同名文件被别人建了 → 只能追加,不许覆盖。"""
    p = project / CUSTOM / "抢先.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# 抢先\n\n作者自己写的,别动。\n", encoding="utf-8")
    payload = _propose(project, f"{CUSTOM}/抢先.md#补充", "AI 追加的一条。")
    out = usecases.partner_confirm(project, payload["id"], ts="t2")
    assert "error" not in out, out
    text = p.read_text(encoding="utf-8")
    assert "作者自己写的,别动" in text and "AI 追加的一条" in text
