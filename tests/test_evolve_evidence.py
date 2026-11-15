"""证据采集:轨迹里 agent 提交的产物 vs 盘上作者改后的那份。

spec 2026-08-16 §1.3 —— 这是 Loom 比 Prime Agent 强的那一点:Prime Agent 的 `/refine`
只能让模型回看轨迹**自评**「这个决策本可以更好」(没人在旁边逐字改它的输出);
而 Loom 的作者每一章都在手改,那是 **ground truth,不是评价**。

于是 refine 不必问「本可以更好吗」,只需看「**作者实际改成了什么**」——顺带绕开了打分
(ADR 0002/0006 的红线)。

细纲是四个可进化对象里【唯一】有干净逐产物证据的:它是 WYSIWYG 文件,两边都在盘上。
正文的手改混着 voice(已归写作指纹)和剧情,归因要猜——不猜。
"""
from __future__ import annotations

from loom import evolve, paths, trail


_ARTIFACT = "本章细纲(含分镜)"   # 细纲链最后一棒的产物(六棒:细纲师→设定师→分镜师)
_PERSONA = "章节分镜师"          # 产它的那棒。evolve._COMPARABLE 由 artifacts.ARTIFACTS 派生


def _lay(project, n: int, ai: str, author: str | None) -> None:
    """铺一章的证据:轨迹里记 agent 交的那份;盘上放作者手里的那份。"""
    trail.record_commit(project, n, _ARTIFACT, ai, f"v2:sig{n}")
    p = paths.outline_path(project, n)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(author if author is not None else ai, encoding="utf-8")


def test_作者改过的细纲才算证据(project):
    _lay(project, 1, "一(约400字)。二(约400字)。三(约400字)。", "一(约600字)。二(约600字)。")
    _lay(project, 2, "一(约400字)。二(约400字)。", None)     # 没改 → 不是证据
    got = evolve.collect(project)
    assert [e.chapter for e in got] == [1]
    assert got[0].persona == _PERSONA
    assert "约600字" in got[0].author and "三(约400字)" in got[0].ai


def test_只有一边的章不算证据(project):
    """作者自己手写细纲(agent 没交过)→ 没有 AI 侧,无从比较;
    agent 交了但作者没打开过 → 上一条已覆盖。两种都不该冒充证据。"""
    p = paths.outline_path(project, 3)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("作者自己写的细纲。", encoding="utf-8")
    assert evolve.collect(project) == []


def test_同一章多次提交取最后一次(project):
    """agent 会回头重来、同一件产物提交多次。作者改的是【最后那份】,比较也该拿它。"""
    trail.record_commit(project, 1, _ARTIFACT, "第一版", "v2:a")
    trail.record_commit(project, 1, _ARTIFACT, "第二版", "v2:b")
    p = paths.outline_path(project, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("作者改的", encoding="utf-8")
    got = evolve.collect(project)
    assert len(got) == 1 and got[0].ai == "第二版"


def test_按人格筛(project):
    _lay(project, 1, "AI 的", "作者的")
    assert len(evolve.collect(project, persona=_PERSONA)) == 1
    assert evolve.collect(project, persona="写手") == []


def test_证据不够就不提议(project):
    """一章的差异可能只是这一章特殊。要跨多章反复出现才算「你的改法」——
    同 fingerprint 的思路:单次证据不足以改写规则。"""
    _lay(project, 1, "AI 的", "作者的")
    assert evolve.ripe(project, _PERSONA, min_edits=2) is False
    _lay(project, 2, "AI 的", "作者的")
    assert evolve.ripe(project, _PERSONA, min_edits=2) is True


def test_空白差异不算改过(project):
    """尾部空行/换行差异不是「作者改了它」——落盘时补的 \\n 会让每一章都误判成证据。"""
    _lay(project, 1, "一(约400字)。二(约400字)。", "一(约400字)。二(约400字)。\n\n")
    assert evolve.collect(project) == []


def test_细纲编码损坏不让collect抛穿(project):
    """终审④:`UnicodeDecodeError` 继承自 `ValueError` 不是 `OSError`,裸 `except OSError`
    抓不到它——盘上细纲文件编码损坏时 `collect` 会直接抛穿,整条自进化证据采集链跟着崩。
    这一章跳过即可(同 OSError 那半:读不了就当没有证据),其余章节照常收。"""
    trail.record_commit(project, 1, _ARTIFACT, "AI 的", "v2:sig1")
    p = paths.outline_path(project, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xfe")   # 非法 UTF-8,读它必抛 UnicodeDecodeError
    assert evolve.collect(project) == []   # 不该抛穿,这一章当没有证据跳过


def test_重新生成细纲不污染证据源(project):
    """终审②Important:`collect` 的判据是「轨迹里最后一次提交的细纲 ≠ 盘上那份」即认作者改过。
    `agents.regen_outline`(走 /api/outline/regen)会用【新的 AI 输出】覆盖同一份
    `正文/.细纲/第N章.md`——作者点一次「重新生成细纲」,这份全新 AI 稿若不记进轨迹,就会被
    误判成「作者改成的」喂进 refine 的证据,直接违背判据只能是作者实际改成了什么这条设计红线。
    """
    from conftest import ScriptedBackend
    from loom.agents import regen_outline
    from loom.config import load_config, save_config

    # 先铺一版「旧」的 agent 提交(regen 之前的轨迹底子)
    trail.record_commit(project, 1, _ARTIFACT, "原始细纲(agent 交的)。", "v2:old")
    cfg = load_config(project)
    cfg.gate_rounds = 0
    save_config(project, cfg)
    # 六棒流水线里细纲是【三棒接力】(细纲师→设定师→分镜师),每棒覆盖写同一份细纲,
    # 所以这里要三条脚本;只给两条会在分镜师那棒撞上「返回空」的闸。
    be = ScriptedBackend([
        "第1章 细纲:朝会起,遇刺转。",                     # 章节细纲师
        "第1章 细纲(含设定):朝会起,遇刺转。补设定锚点。",   # 章节设定师
        "第1章 细纲(含分镜):朝会起,遇刺转。补镜头:近景/切镜。",  # 章节分镜师
    ])
    regen_outline(project, 1, be, load_config(project))
    assert len(be.calls) == 3, "细纲三棒各一次调用(结构兜底在流水线里有,regen 这条链没有追加调用)"
    # 关键前置:regen 必须把新细纲记进轨迹(产物名与 ARTIFACTS 对齐),否则下面那句
    # 「collect 为空」会因为「这条产物压根不可比」而空过,什么也没钉住。
    assert trail.read_commits(project, 1)[-1]["产物"] == _ARTIFACT
    assert evolve.collect(project) == []   # 新 AI 稿刚落盘,不该被当成「作者改的」证据
