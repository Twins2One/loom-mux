# -*- coding: utf-8 -*-
"""去AI味口径(:让作者靠对话优化「去AI味」)。

边界:代码侧的检测规则一条都不许模型改;可调的只有三件属于作者自己的事——
签名句豁免、额外禁用表达、跨句号「。是」算不算。这里逐条钉住。
"""
from __future__ import annotations

from loom import aitell, paths

POLICY = """# 去AI味口径

> 这是检测器的口径,不是正文。

## 豁免句

> （逐字抄你自己的原话）
> 那不是笑。是不在意。

## 额外禁用表达

- （一行一条）
- 仿佛
- 宛如

## 跨句号翻转

不算
"""


def _write(project, text: str) -> None:
    p = project / paths.SLOP_POLICY_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_没有口径文件时用出厂默认(project):
    pol = aitell.load_policy(project)
    assert pol.banned == ()
    assert pol.cross_sentence is False
    assert isinstance(pol.anchors, tuple)


def test_口径文件读出三件事(project):
    _write(project, POLICY)
    pol = aitell.load_policy(project)
    assert "仿佛" in pol.banned and "宛如" in pol.banned
    assert not any("一行一条" in b for b in pol.banned)      # 占位行不是口径
    assert "那不是笑。是不在意。" in pol.anchors
    assert not any("逐字抄" in a for a in pol.anchors)
    assert pol.cross_sentence is False


def test_跨句号可以改成算(project):
    _write(project, POLICY.replace("不算", "算"))
    assert aitell.load_policy(project).cross_sentence is True


def test_口径里的豁免句真的豁免(project):
    _write(project, POLICY)
    pol = aitell.load_policy(project)
    text = "那不是笑。是不在意。"
    assert aitell.detect(text, list(pol.anchors)) == []
    # 换个不豁免的说法,同一句法就会被抓到(证明检测本身没被关掉)
    assert aitell.detect("那不是笑，而是不在意。", list(pol.anchors))


def test_额外禁用表达命中就报(project):
    _write(project, POLICY)
    pol = aitell.load_policy(project)
    issues = aitell.detect("她仿佛站在雾里。", list(pol.anchors),
                           cross_sentence=pol.cross_sentence, banned=list(pol.banned))
    assert issues and issues[0].kind == aitell._KIND_BANNED
    assert "仿佛" in issues[0].desc
    assert "仿佛" in issues[0].evidence


def test_禁用表达不会刷屏(project):
    _write(project, POLICY)
    pol = aitell.load_policy(project)
    text = "仿佛" * 30
    issues = aitell.detect(text, [], banned=list(pol.banned))
    assert 0 < len(issues) <= aitell._MAX_HITS


def test_签名句里的禁用词也豁免(project):
    _write(project, POLICY)
    pol = aitell.load_policy(project)
    sig = "仿佛那一刻我又回到了十六岁。"
    issues = aitell.detect(sig, [sig], banned=list(pol.banned))
    assert issues == []


def test_不传口径时行为与老版本一致(project):
    # 老调用点只传 anchors,一个都不该多出「禁用表达」这类新命中
    assert aitell.detect("他仿佛走了很久。", []) == []


def test_口径文件坏掉不影响检测(project):
    _write(project, "\x00\x01 这不是 markdown")
    pol = aitell.load_policy(project)             # 不抛
    assert isinstance(pol, aitell.Policy)
