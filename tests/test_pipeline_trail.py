"""流水线(默认路径)也要写细纲轨迹——否则自进化对绝大多数用户是惰性的。

实测发现:agent 模式默认关,而轨迹此前只由 `write_tools._handle_commit`(agent 专属)
和 `regen_outline` 写。默认路径跑完一章,轨迹为空 → `evolve.collect` 无证据 →
「学改法」永远不 ripe。P3 整块的价值绑死在一个默认关掉的开关上。

判据的方向必须钉死:轨迹记的是【AI 交出去的那份】,盘上那份是【作者手里的】,
两者不同才算「作者改过」。

【2026-10-20 六棒改造后的口径】细纲是三棒接力(`章节细纲师 → 章节设定师 → 章节分镜师`,
逐棒覆盖写同一份细纲),轨迹只记**末棒(章节分镜师)**那份——那才是 AI 最终交出去的。
WYSIWYG 旁路现在只旁路首棒(设定师/分镜师照样在这份细纲上增补,`test_eval_generate`
与 golden 快照都把这条钉死了),所以「盘上那份」跑到最后也是 AI 的末棒产物:
刚跑完时两边相等 → 不是证据;作者之后再改 → 才是证据。方向没变。
"""
from __future__ import annotations

import dataclasses

from loom import agents, evolve, paths, trail
from loom.config import load_config
from loom.parse import EDIT_NOTE_CLOSE, EDIT_NOTE_OPEN

# 细纲三棒是「接力覆盖写同一份细纲」:后一棒只在前一棒上**加**小节。
# 脚本文本必须逐层加小节(L2=L1+小节、L3=L2+小节)——否则 agents._restore_missing_sections
# 会追问一次模型,调用数 +1,把这里钉死的「六棒 + 标题」搞崩。
_L1 = "一(约400字):验伤。二(约400字):遇敌。"
_L2 = _L1 + "\n设定:主角觉醒逆息体质。"
_L3 = _L2 + "\n镜头:矿灯昏黄,洞口逆光剪影。"


def _稿(mark: str) -> str:
    """够长的正文体:要过 chapter_profile 的实字门槛(默认 800 字目标 → 96 实字)。"""
    return f"他没说话。火把的光爬上矿壁,血顺着指缝往下滴。({mark})" * 6


def _改稿(mark: str) -> str:
    """责任编辑这一棒必须带改动留痕哨兵,否则它会「让它重来一次」多烧一次调用。"""
    return (_稿(mark) + "\n" + EDIT_NOTE_OPEN + "\n《本章改动留痕》\n- 钩子更硬。\n"
            + EDIT_NOTE_CLOSE)


def _cfg(project, **over):
    return dataclasses.replace(load_config(project), gate_rounds=0,
                               continuity_scan=False, **over)


def test_流水线跑完一章细纲进轨迹(project):
    from conftest import ScriptedBackend
    # 六棒:章节细纲师 / 章节设定师 / 章节分镜师 / 写手 / 责任编辑 / 润色师,外加起标题
    be = ScriptedBackend([
        _L1,
        _L2,
        _L3,
        _稿("初稿"),
        _改稿("改稿"),
        _稿("终稿"),
        "废矿里的火光",
    ])
    agents.run_pipeline(project, 1, be, _cfg(project))
    commits = trail.read_commits(project, 1)
    # 只记细纲链末棒(章节分镜师)的产物:那才是 AI 最终交出去的那份
    assert [c["产物"] for c in commits] == ["本章细纲(含分镜)"]
    assert "验伤" in commits[0]["text"]


def test_作者改了细纲就成为自进化的证据(project):
    """这条是整个目的:默认路径下,作者改细纲这件事要能被 evolve 看见。"""
    from conftest import ScriptedBackend
    l1 = "一(约400字):验伤。二(约400字):遇敌。三(约400字):爆发。"
    l2 = l1 + "\n设定:主角觉醒逆息体质。"
    l3 = l2 + "\n镜头:矿灯昏黄,洞口逆光剪影。"
    be = ScriptedBackend([l1, l2, l3, _稿("初稿"), _改稿("改稿"), _稿("终稿"), "废矿里的火光"])
    agents.run_pipeline(project, 1, be, _cfg(project))
    assert evolve.collect(project) == []          # 作者还没动,不该有证据
    p = paths.outline_path(project, 1)
    p.write_text("一(约600字):验伤。二(约600字):遇敌。", encoding="utf-8")   # 作者删成两场
    got = evolve.collect(project)
    # 细纲的「人格」现在是末棒章节分镜师(旧版只有大纲师一棒)
    assert len(got) == 1 and got[0].persona == "章节分镜师"
    assert "三(约400字)" in got[0].ai and "约600字" in got[0].author


def test_沿用作者已有细纲时绝不把作者原文当成AI交的(project):
    """WYSIWYG 旁路:首棒章节细纲师不调模型,直接沿用盘上那份。

    六棒改造后旁路只覆盖首棒——设定师/分镜师照样在这份细纲上逐层增补,所以轨迹里
    记的必须是**末棒增补过的那份**,而不是作者原文本身。这条钉的正是那个方向:
    盘上细纲 = AI 交出去的那份时,不该立刻冒出「作者改过」的假证据。
    """
    from conftest import ScriptedBackend
    p = paths.outline_path(project, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("一(约600字):作者自己写的细纲。", encoding="utf-8")
    set_by_author = "一(约600字):作者自己写的细纲。"
    # 首棒被旁路 → 脚本从「章节设定师」开始(六棒少一棒,加标题共 6 次调用)
    be = ScriptedBackend([
        set_by_author + "\n设定:主角觉醒逆息体质。",     # 章节设定师
        set_by_author + "\n设定:主角觉醒逆息体质。\n镜头:矿灯昏黄。",   # 章节分镜师
        _稿("初稿"),
        _改稿("改稿"),
        _稿("终稿"),
        "废矿里的火光",
    ])
    agents.run_pipeline(project, 1, be, _cfg(project))
    commits = trail.read_commits(project, 1)
    assert [c["产物"] for c in commits] == ["本章细纲(含分镜)"]
    assert commits[0]["text"] != set_by_author        # 记的是末棒增补后的那份,不是作者原文
    assert evolve.collect(project) == []              # 盘上与轨迹一致 → 没有假证据


def test_轨迹写失败不拖累出稿(project, monkeypatch):
    """轨迹是省钱/学习的便利,绝不该让一章写不出来(同 trail.record_commit 自身的纪律)。"""
    from conftest import ScriptedBackend

    def boom(*a, **k):
        raise OSError("盘满了")

    monkeypatch.setattr(trail, "record_commit", boom)
    be = ScriptedBackend([
        _L1,
        _L2,
        _L3,
        _稿("初稿"),
        _改稿("改稿"),
        _稿("终稿"),
        "废矿里的火光",
    ])
    path, _final = agents.run_pipeline(project, 1, be, _cfg(project))
    assert path.is_file()
