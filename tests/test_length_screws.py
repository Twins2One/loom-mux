"""字数契约（螺丝①②）+ 细纲结构兜底 —— 2026-10-20 按六棒流水线重写。

**为什么整篇重写**:原文件测的是五棒时代的契约(`_length_hint("编辑", …)`、
`_length_hint("设定师", 350, …)`),而"设定师/大纲师"现在是**作者智库角色**、不再进流水线,
"编辑"也已改名"责任编辑"。那些断言测的是一套已经不存在的设计,留着只会永远红。

现在钉住的是**今天真机付过代价**的四条:
1. 细纲第 1 棒有绝对字数上限,第 2/3 棒**只增不减**(旧的"三棒共用一个 ≤1250"是无解冲突);
2. 正文三棒有"压缩授权阈值"(超 1.2 倍才给压,达标时一个字都不提压);
3. 「各场预算」只认场景标题行里的「约X字」,**不把「预计字数：约 800 字」当成一场**(误报过三棒);
4. 上游结尾小节被整节丢掉时,代码**逐字补回**;中间小节只报不搬。
"""
from __future__ import annotations

from pathlib import Path

from loom.agents import _length_hint, _parse_scene_budgets, _restore_missing_sections
from loom.artifacts import LEN_AUTH


# ── 1. 细纲三棒:第 1 棒管上限,第 2/3 棒只增不减 ────────────────────────

def test_细纲师_拿绝对上限与场次预算():
    h = _length_hint("章节细纲师", 1250, 800)
    assert "1250" in h                      # 上限还在
    assert "拆 2-3 场" in h and "约X字" in h  # 并要求给每场标预算


def test_设定师_按上游实测字数只增不减():
    """旧实现让第 2、3 棒也吃「细纲本身 ≤1250 字」,而上游会交 2157 字 ⇒ 无解,模型只能压。
    真机后果:分镜师把 2157 压成 1009,顺手带走 4 个整节和 8 条设定注释。"""
    h = _length_hint("章节设定师", 1250, 800, actual_chars=2157)
    assert "2157" in h
    assert "只增不减" in h or "不重写、不精简" in h
    assert "≤1250" not in h                 # 绝对上限不许再出现在这两棒


def test_分镜师_同样只增不减():
    h = _length_hint("章节分镜师", 1250, 800, actual_chars=2331)
    assert "2331" in h
    assert "≤1250" not in h


def test_没给实测字数时退回旧的上限口径():
    """第 2 棒没有上游(极少见的边角)时不能给出"只增不减"这种空话。"""
    h = _length_hint("章节设定师", 1250, 800, actual_chars=0)
    assert "1250" in h


# ── 2. 正文两棒:压缩授权有阈值(超 1.2 倍才给压) ───────────────────────

def test_达标时一个字都不提压():
    h = _length_hint("责任编辑", 800, 800, actual_chars=int(800 * LEN_AUTH))  # 恰在阈值上
    assert "原稿实测" not in h
    assert "篇幅目标约 800 字" in h
    assert "保持原有篇幅量级" in h


def test_超标才把实测数摆上桌():
    h = _length_hint("润色师", 800, 800, actual_chars=1000)
    assert "原稿实测 1000 字" in h and "超 25%" in h and "绝不扩写" in h


def test_写手有硬性篇幅要求():
    h = _length_hint("写手", 800, 800)
    assert "800" in h and "硬性篇幅要求" in h


# ── 3. 各场预算解析:不许把「预计字数」当成一场 ─────────────────────────

def test_各场预算不把预计字数算进去():
    """真机误报:三场 350+300+150 本来正好等于目标 800,却被算成 1600
    (因为 `## 基本信息` 里的「- 预计字数：约 800 字」也被抓走了),
    于是连着三棒警告「和目标差得多」。模型是对的,判据是错的。"""
    outline = (
        "# 第 1 章细纲：废矿\n\n"
        "## 基本信息\n- 预计字数：约 800 字\n\n"
        "## 场景列表\n\n"
        "### 场景 1：废矿藏身（约 350 字）\n- 情节\n\n"
        "### 场景 2：反杀（约 300 字）\n- 情节\n\n"
        "### 场景 3：师姐入矿（约 150 字）\n- 情节\n"
    )
    assert _parse_scene_budgets(outline) == [350, 300, 150]
    assert sum(_parse_scene_budgets(outline)) == 800


def test_模型没写在标题里时退回全文口径():
    outline = "# 细纲\n\n## 基本信息\n- 预计字数：约 800 字\n"
    assert _parse_scene_budgets(outline) == [800]


# ── 4. 细纲链结构兜底:尾部小节逐字补回 ────────────────────────────────

PREV = "# 第 1 章细纲：x\n\n## 基本信息\n- a\n\n## 关键细节\n- 钩子:y\n\n## 写作提示\n- 开篇要冷\n"


def test_尾节被整节丢掉时逐字补回():
    """真机 ×3:设定师每次都丢 `## 写作提示`(它觉得跟"设定核对"无关)。
    提示词已经逐条列了 9 个标题,模型照丢 ⇒ 只能代码补。"""
    cur = "# 第 1 章细纲：x\n\n## 基本信息\n- a\n\n## 关键细节\n- 钩子:y\n"
    fixed, restored, unfixed = _restore_missing_sections(PREV, cur)
    assert restored == ["## 写作提示"]
    assert unfixed == []
    assert "开篇要冷" in fixed          # 内容逐字搬回
    assert fixed.rstrip().endswith("开篇要冷")


def test_中间小节只报不搬():
    """位置推断不可靠,搬错比丢更糟 —— 中间小节只回报给调用方去 warn。"""
    cur = "# 第 1 章细纲：x\n\n## 关键细节\n- 钩子:y\n\n## 写作提示\n- 开篇要冷\n"
    fixed, restored, unfixed = _restore_missing_sections(PREV, cur)
    assert restored == []
    assert unfixed == ["## 基本信息"]
    assert fixed == cur                  # 正文一个字不动


def test_结构没缺就原样返回():
    fixed, restored, unfixed = _restore_missing_sections(PREV, PREV)
    assert (restored, unfixed) == ([], [])
    assert fixed == PREV


# ── 4b. 场景标题补了「（约N字）」不算丢节(判据归一) ────────────────────

def test_补场次预算不算丢节():
    """2026-10-21 真机误报:分镜师给每个场景标题补了「（约400字）」——那正是我们要的标注,
    逐字比对却判成"上一棒的场景全丢了",白追问一次模型。"""
    prev = ("# 细纲\n\n## 场景列表\n\n### 场景 1：废矿深处，追兵逼近\n- a\n\n"
            "### 场景 2：濒死→反杀\n- b\n\n## 写作提示\n- 开篇要冷\n")
    cur = ("# 细纲\n\n## 场景列表\n\n### 场景 1：废矿深处，追兵逼近（约400字）\n- a\n\n"
           "### 场景 2：濒死→反杀（约 400 字）\n- b\n\n## 写作提示\n- 开篇要冷\n")
    fixed, restored, unfixed = _restore_missing_sections(prev, cur)
    assert (restored, unfixed) == ([], [])      # 一个都不该报
    assert fixed == cur                          # 一个字都不该动


def test_真的丢了整场仍然要报():
    prev = "# 细纲\n\n### 场景 1：甲\n- a\n\n### 场景 2：乙\n- b\n\n## 写作提示\n- c\n"
    cur = "# 细纲\n\n### 场景 1：甲（约300字）\n- a\n\n## 写作提示\n- c\n"
    fixed, restored, unfixed = _restore_missing_sections(prev, cur)
    assert unfixed == ["### 场景 2：乙"]          # 这个是真的丢了
    assert restored == [] and fixed == cur


def test_追问只在缺得更少且没变短时才采纳():
    """真机形态(2026-10-21 整章实跑):设定师把「### 场景 3」和「## 关键细节」整节删了,
    1910 字压成 1074 字,终稿只剩两场、章末钩子那场没了。追问一次是兜底,
    但**不能一追问就无条件采用** —— 模型可能一边补回那节、一边把别处又砍了。"""
    from loom.agents import _should_take_section_retry

    # 缺 2 个 → 缺 0 个,且变长了:采纳
    assert _should_take_section_retry(["a", "b"], [], 1074, 1900) is True
    # 缺 2 → 缺 1:改善了,采纳
    assert _should_take_section_retry(["a", "b"], ["b"], 1074, 1500) is True
    # 缺得一样多:不采纳(白换)
    assert _should_take_section_retry(["a", "b"], ["c", "d"], 1074, 1500) is False
    # 补回了小节但整体变短:不采纳(这是又压缩了一次)
    assert _should_take_section_retry(["a", "b"], [], 1900, 1200) is False
    # 空回复:不采纳
    assert _should_take_section_retry(["a"], [], 1074, 0) is False


# ── 5. 模板里不许自带与任务行打架的字数 ────────────────────────────────

def test_细纲三棒模板不带硬编码场次字数():
    """字数上限单一真源 = StepSpec/outline_budget;模板自带数字会和任务行打架,
    两个矛盾数字正是模型无视二者的诱因。"""
    root = Path(__file__).resolve().parent.parent / "loom" / "templates" / "agents"
    for name in ("章节细纲师.md", "章节设定师.md", "章节分镜师.md"):
        md = (root / name).read_text(encoding="utf-8")
        assert "300 字" not in md and "350 字" not in md, f"{name} 里又出现了硬编码字数"
