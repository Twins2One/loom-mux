"""棒级体检项:给流水线每一棒的**中间产物**各挂一组确定性检查。

为什么要有它:章级总分只能告诉你「这章掉分了」,翻译不成「改哪一棒的 prompt」。
每一项都设计成「上游有、这一棒输出里没了」的**差分**,所以命中就能定位到棒。

红线:
- 全部确定性、零 LLM 调用——同一份文本跑两次结果必须逐字相同。
- 对 loom 的复用只走 loom.evalapi 门面,不 import 私有符号。
- 只活在 evals 里给开发者做回归归因,绝不进产品 UI/用户路径(ADR-0002)。
- 某棒缺席(WYSIWYG 旁路 / 断点续跑跳过)记 skipped,**不记 0 分**——旁路不是失败。
"""

from __future__ import annotations

import re

from loom.evalapi import (
    STEP_SHORT_BUDGETS,
    outline_budget,
    detect_aitell,
    parse_scene_budgets,
    scene_range,
    split_edit_note,
)

from .graders import GraderResult

_WS = re.compile(r"\s+")


def _chars(text: str) -> int:
    """去空白字数。与 graders._body_len 同口径,但中间产物没有 H1 标题,不必剥。"""
    return len(_WS.sub("", text or ""))


def skipped(step: str, item: str) -> GraderResult:
    """这一棒没有产物时的统一形状。

    passed=True 是刻意的:「这棒没跑」不该污染通过判定;gating=False 让它不进门禁;
    detail 带 [skipped] 前缀。

    聚合侧(evals/aggregate.py)按 detail 的 **[skipped] 或 [not-measurable]**(后者见
    下面 not_measurable())前缀识别"genuinely 没测到"、排除出分布——不是靠
    gating=False 本身。gating=False 还覆盖 observe-only 项(如 写手·AI翻转句:
    weight=0.0、gating=False,但每次都真测了),那类项必须留在分布里,否则真实
    信号会被误报成"从未测过"(Important-1)。
    """
    return GraderResult(f"{step}·{item}", 0.0, True, weight=0.0, gating=False,
                        detail=f"[skipped] {step} 这一棒没有产物(旁路或续跑跳过)")


def not_measurable(name: str, reason: str) -> GraderResult:
    """某体检项本次结构性测不出(不是「没跑」,是这个信号在当前输入下不适用)。

    与 skipped() 并列的第二种「没测到」——三处调用方(大纲师·场次数无标注时、
    大纲师·篇幅预算 chapter_target<=0 时、编辑·留痕围栏恒不可测)原先各自手写同一份
    weight=0.0/gating=False/passed=True/[not-measurable] 前缀,收进这个小helper 防
    第三处再手抄一遍漂掉某个字段。

    passed=True 是刻意的:「测不出」不该判定失败;weight=0.0/gating=False 让它不进
    门禁;detail 统一带 [not-measurable] 前缀,供 evals/aggregate.py 据此排除出分布
    (同 skipped() 的排除机制,见其 docstring)。
    """
    return GraderResult(name, 0.0, True, weight=0.0, gating=False,
                        detail=f"[not-measurable] {reason}")


# ─────────────────────────────── 设定师 ───────────────────────────────

def grade_setter(anchor: str | None, hardfact_terms: list[str]) -> list[GraderResult]:
    """设定师产出「本章设定锚点」(≤350 字的语义选择)。

    体检两项:
    - 硬设定专名:case 声明必须带上的境界/金手指/地名等专名,锚点里在不在。
      锚点丢了专名,下游大纲师/写手就只能靠 hardfacts 直送兜底,是设定漂移的上游根因。
    - 锚点篇幅:超过 STEP_SHORT_BUDGETS["设定师"](350)说明它在复述整份世界观,
      会稀释下游 prompt。
    """
    if not anchor:
        return [skipped("设定师", "硬设定专名"), skipped("设定师", "锚点篇幅")]

    terms = hardfact_terms or []
    missing = [t for t in terms if t not in anchor]
    total = len(terms)
    score = 1.0 if total == 0 else max(0.0, 1.0 - len(missing) / total)
    term_result = GraderResult(
        "设定师·硬设定专名", round(score, 3), not missing, weight=0.30,
        detail=f"声明 {total} 个硬设定专名,锚点缺 {len(missing)} 个",
        evidence=[f"锚点里没有:「{m}」" for m in missing])

    budget = STEP_SHORT_BUDGETS.get("设定师", 350)
    n = _chars(anchor)
    over = max(0, n - budget)
    len_result = GraderResult(
        "设定师·锚点篇幅", round(max(0.0, 1.0 - over / max(1, budget)), 3), over == 0,
        weight=0.10, detail=f"{n} 字(预算 {budget} 字)")
    return [term_result, len_result]




def _toward_target(name: str, n_before: int, n_after: int, chapter_target: int,
                   *, weight: float, label_before: str, label_after: str) -> GraderResult:
    """按【离章目标更近还是更远】评分,而不是【长度变没变】。

    真事故(2026-08-12):旧口径是 1-|1-ratio|,即「这一棒不该改长度」。但产品在
    _length_hint 里明确给编辑/润色师**压缩授权**——「原稿明显超目标就顺手压回来」。
    两者正面冲突:一次让终稿距目标偏差从 0.236 降到 0.142(客观变好)的改动,
    被旧口径判成「润色师·篇幅保持 回归」(p=0.0247)。判据在惩罚产品刻意要的行为。

    新口径只问一件事:这一棒把稿子推向目标了,还是推离了?
    - passed:没推离(容 0.02 噪声),或推完之后本来就落在容差带内
    - score:按推完之后离目标多远算(越近越高)——仍然扣得动「已经达标还乱压」

    chapter_target<=0 时无从判断,记不可测(不伪造分数)。
    """
    if chapter_target <= 0:
        return not_measurable(name, f"章目标 {chapter_target} 非正数,无从判断离目标更近还是更远")
    dev_before = abs(n_before - chapter_target) / chapter_target
    dev_after = abs(n_after - chapter_target) / chapter_target
    # 与 gen case 的 len_tolerance 封顶同口径:落在 ±25% 内就算达标,不管它动没动
    ok = dev_after <= dev_before + 0.02 or dev_after <= 0.25
    arrow = "更近" if dev_after < dev_before else ("更远" if dev_after > dev_before else "持平")
    return GraderResult(
        name, round(max(0.0, min(1.0, 1.0 - dev_after)), 3), ok, weight=weight,
        detail=(f"{label_before} {n_before} → {label_after} {n_after} 字"
                f"(目标 {chapter_target};离目标 {dev_before:.0%}→{dev_after:.0%},{arrow})"))


# ─────────────────────────────── 大纲师 ───────────────────────────────

def grade_outliner(outline: str | None, chapter_target: int,
                   must_include: list[str]) -> list[GraderResult]:
    """大纲师产出「本章场景骨头(分镜细纲)」。

    体检三项(全部复用产品自己的判据,不另立一套——重写必漂):
    - 必含要素:case 的 must_include 有没有在细纲这层就丢掉(丢在这里 = 写手根本没机会写)。
    - 场次数:落在 scene_range(chapter_target) 声明的区间内(evalapi 接缝,与喂 prompt
      的 _scene_budget 同源)。parse_scene_budgets 数的是「约X字」标注数,不是场景数——
      细纲完全没标注时这个信号测不出场景数(不能报「0 场」,那是假数据),记不可测;
      「没标」这个缺陷交给下面的篇幅预算项单独抓,不在这里重复计。
    - 篇幅预算标注:每场标了「约X字」且各场合计与章目标偏差 ≤30%(与产品
      _check_scene_budget 的 0.3 阈值同口径)。chapter_target<=0 时偏差率没有意义,
      这项检查本身不适用,记不可测(不伪造分数,也不让 passed/score 互相矛盾)。
    """
    if outline is None:
        return [skipped("大纲师", "细纲篇幅"),
                skipped("大纲师", "必含要素"), skipped("大纲师", "场次数"),
                skipped("大纲师", "篇幅预算")]

    must = must_include or []
    missing = [k for k in must if k not in outline]
    total = len(must)
    inc_score = 1.0 if total == 0 else max(0.0, 1.0 - len(missing) / total)
    inc = GraderResult("大纲师·必含要素", round(inc_score, 3), not missing, weight=0.30,
                       detail=f"必含 {total} 项,细纲缺 {len(missing)} 项",
                       evidence=[f"细纲里没有:「{m}」" for m in missing])

    budgets = parse_scene_budgets(outline)
    if not budgets:
        # 没有「约X字」标注 → 数不出场景数(parse_scene_budgets 抓的是标注,不是场景标题)。
        # 报「0 场」会把「没标」这个缺陷在这里和下面的篇幅预算项重复计一遍,还给出假数据。
        cnt = not_measurable(
            "大纲师·场次数",
            "细纲没有「约X字」标注,场次数无法从这个信号测出(该缺陷已由"
            "「大纲师·篇幅预算」的「没标」记录,这里不重复计)")
    else:
        lo, hi = scene_range(chapter_target)
        n_scenes = len(budgets)
        in_range = lo <= n_scenes <= hi
        cnt = GraderResult("大纲师·场次数", 1.0 if in_range else 0.0, in_range, weight=0.20,
                           detail=f"{n_scenes} 场(目标 {chapter_target} 字 → 应 {lo}-{hi} 场)")

    total_budget = sum(budgets)
    if not budgets:
        bud = GraderResult("大纲师·篇幅预算", 0.0, False, weight=0.15,
                           detail="各场都没标「约X字」,写手篇幅无锚")
    elif chapter_target <= 0:
        # 章目标非正数 → 偏差率(drift / chapter_target)没有意义,这项检查不适用。
        # 不能一边 passed=True 一边 score=0.0——不适用就不计分、不进门禁,别伪造数字。
        bud = not_measurable(
            "大纲师·篇幅预算",
            f"章目标 {chapter_target} 非正数,篇幅预算检查不适用(各场合计约 {total_budget} 字)")
    else:
        drift = abs(total_budget - chapter_target)
        ok = drift <= chapter_target * 0.3
        bud = GraderResult("大纲师·篇幅预算",
                           round(max(0.0, 1.0 - drift / chapter_target), 3), ok,
                           weight=0.15,
                           detail=f"各场合计约 {total_budget} 字(章目标 {chapter_target},容差 30%)")
    # 细纲自身篇幅:上限按章目标现算(outline_budget,evalapi 接缝),不是写死的 450。
    # 这一项此前【不存在】——旧的 450 既没 grader 也没 warn 覆盖,改成多少都没有信号
    # 告诉你它对不对,于是 20/20 越线也没人发现(spec §10.4)。
    obudget = outline_budget(chapter_target) if chapter_target > 0 else 0
    if obudget <= 0:
        olen = not_measurable("大纲师·细纲篇幅",
                              "章目标字数 ≤0,细纲上限按它派生,这项无从算起")
    else:
        n = _chars(outline)
        over = max(0, n - obudget)
        olen = GraderResult(
            "大纲师·细纲篇幅", round(max(0.0, 1.0 - over / max(1, obudget)), 3), over == 0,
            weight=0.10, detail=f"{n} 字(上限 {obudget} 字,按 {chapter_target} 字章目标现算)")
    return [olen, inc, cnt, bud]


# ──────────────────────────────── 写手 ────────────────────────────────

def grade_writer(draft: str | None, target_chars: int,
                 must_include: list[str], anchors: list[str]) -> list[GraderResult]:
    """写手产出「本章初稿」。这是第一份完整正文,建立后两棒的比较基准。

    体检三项:初稿篇幅 vs 章目标、必含要素命中、AI 翻转句命中数(初稿基线,
    供润色师那棒算「降了多少」)。
    """
    if draft is None:
        return [skipped("写手", "初稿篇幅"), skipped("写手", "必含要素"),
                skipped("写手", "AI翻转句")]

    n = _chars(draft)
    # 初稿容差刻意比终稿宽:后面还有编辑/润色两棒会动篇幅。只挡「离谱」。
    lo, hi = target_chars * 0.5, target_chars * 1.5
    len_ok = lo <= n <= hi
    d = 0.0 if len_ok else (lo - n if n < lo else n - hi)
    length = GraderResult("写手·初稿篇幅",
                          round(max(0.0, 1.0 - d / max(1, target_chars)), 3), len_ok,
                          weight=0.20, detail=f"{n} 字(章目标 {target_chars} ±50%)")

    must = must_include or []
    missing = [k for k in must if k not in draft]
    total = len(must)
    inc = GraderResult("写手·必含要素",
                       round(1.0 if total == 0 else max(0.0, 1.0 - len(missing) / total), 3),
                       not missing, weight=0.30,
                       detail=f"必含 {total} 项,初稿缺 {len(missing)} 项",
                       evidence=[f"初稿里没有:「{m}」" for m in missing])

    hits = detect_aitell(draft, anchors or [])
    ai = GraderResult("写手·AI翻转句", round(1.0 / (1.0 + len(hits)), 3), True,
                      weight=0.0, gating=False,
                      detail=f"命中 {len(hits)} 处(初稿基线,供润色师那棒算降幅)",
                      evidence=[h.evidence for h in hits])
    return [length, inc, ai]


# ──────────────────────────────── 编辑 ────────────────────────────────

def grade_editor(edited: str | None, draft: str | None,
                 must_include: list[str],
                 chapter_target: int) -> list[GraderResult]:
    """编辑产出「本章改稿」;controller 在落 ledger 前会把《本章改动留痕》切出另存到
    .审稿留痕/(loom/agents.py:682-685 `_split_edit_note`),只把干净正文交给下游润色师
    并写进 ledger。所以这里收到的 `edited` 就是 evals.generate.collect_steps 从 ledger
    读出来的那份干净正文——从来不带留痕围栏。

    体检三项(全是 改稿 vs 初稿 的差分):
    - 留痕围栏:**不可测**,标记 [not-measurable]。真实跑必定测不出——不是编辑没写
      留痕,是这份信号在到达 ledger 之前就已经被 controller 剥走了(见上)。
      千万别把它"修回"成真检查:那样每次真实跑都会假失败(围栏在 ledger 里恒为空),
      污染「最弱棒」归因,违反本模块「不造数」的红线。想恢复真覆盖需要新开一个
      loom/evalapi.py 门面去读 .审稿留痕/ 目录,不在这个函数的范围内。
    - 必含要素保持:**初稿有、改稿没了**才算编辑改丢的。初稿本来就缺的不赖它——
      归因必须指对棒,否则闭环会把作者引到错的地方。
    - 篇幅变化:编辑被明令「篇幅保持原稿量级、绝不扩写」,超出 ±30% 就是没守。
      算篇幅前先过一遍 split_edit_note 剥留痕围栏——对真实 ledger 内容是 no-op
      (反正从来没有围栏),但万一未来上游行为变化,这层剥离仍是正确的防线。
    """
    if edited is None or draft is None:
        return [skipped("责任编辑", "留痕围栏"), skipped("责任编辑", "必含要素保持"),
                skipped("责任编辑", "篇幅变化")]

    body, _note = split_edit_note(edited)
    fence = not_measurable(
        "责任编辑·留痕围栏",
        "留痕围栏在进 ledger 前已被 controller 剥离(loom/agents.py:682-685 "
        "_split_edit_note 把《本章改动留痕》切出另存到 .审稿留痕/,只把干净正文交给"
        "下游润色师并落盘 ledger),这项体检从 ledger 读到的编辑产出永远不含围栏,"
        "测不出来不代表编辑没写留痕")

    must = must_include or []
    dropped = [k for k in must if k in draft and k not in body]
    keep = GraderResult("责任编辑·必含要素保持",
                        round(1.0 if not must else max(0.0, 1.0 - len(dropped) / len(must)), 3),
                        not dropped, weight=0.35,
                        detail=f"初稿有而改稿没了的必含项:{len(dropped)} 个",
                        evidence=[f"编辑把「{k}」改丢了" for k in dropped])

    size = _toward_target("责任编辑·篇幅变化", _chars(draft), _chars(body), chapter_target,
                          weight=0.10, label_before="初稿", label_after="改稿")
    return [fence, keep, size]


# ─────────────────────────────── 润色师 ───────────────────────────────

def grade_polisher(polished: str | None, edited: str | None,
                   anchors: list[str],
                   chapter_target: int) -> list[GraderResult]:
    """润色师产出「本章终稿」,职责是擦掉通用机器味、保住写作指纹。

    体检两项(全是 终稿 vs 改稿 的差分):
    - AI味下降:改稿里有 aitell 命中时,终稿必须**真的降下来**(after < before)——
      持平不算达标,「没降」就是这一棒白跑了(它的职责就是擦这个)。改稿本来就零命中
      时无处可降,终稿保持零即算达标。
    - 篇幅保持:被明令「绝不扩写」,同时也不该越擦越短。低于改稿 80% 就是擦过头。

    注:改稿带《本章改动留痕》围栏,算 AI 味与篇幅前必须先剥掉——留痕不是正文。
    """
    if polished is None or edited is None:
        return [skipped("润色师", "AI味下降"), skipped("润色师", "篇幅保持")]

    edited_body, _ = split_edit_note(edited)
    before = len(detect_aitell(edited_body, anchors or []))
    after = len(detect_aitell(polished, anchors or []))
    dropped = before - after
    # 改稿本来就零命中 → 终稿保持零即算达标(没有可降的);否则必须真降(严格 <),
    # 持平(after == before)算「没降」,不能放行——见 docstring。
    ai_ok = after < before if before > 0 else after == 0
    ai = GraderResult("润色师·AI味下降",
                      round(1.0 if before == 0 else max(0.0, dropped / before), 3), ai_ok,
                      weight=0.35,
                      detail=f"改稿 {before} 处 → 终稿 {after} 处(降 {dropped} 处)")

    size = _toward_target("润色师·篇幅保持", _chars(edited_body), _chars(polished), chapter_target,
                          weight=0.10, label_before="改稿", label_after="终稿")
    return [ai, size]

# ═══════════════════════════════════════════════════════════════════════════
# 细纲三棒（2026-10-26，设计稿 v2 §2；实测见 §8）
#
# 为什么这三棒要单做：六棒里前三个是「接力改同一份细纲」，而体检表此前只覆盖后三棒
# ⇒ 细纲写歪时报告会把最弱棒指到「写手」，指错人。
#
# 设计口径（全部来自 5 章真机实测，不是拍脑袋）：
# - **场景级判据一律不做 gating**：七字段有值率实测 80%~100%（氛围 80%、转场 85%），
#   任何「每场七字段齐备」的 gating 都会稳定红，而多数决会把系统性误报钉成结论。
# - **gating 只留 3 条**，且实测通过率都是 5/5：章级五节+钩子、四类注释章级齐备、
#   标题只增不减。
# - **字段名必须先归一化**：实测 41 处字段名带自由括注（`**情节（切入点）**`），
#   不归一就会把「有」误报成「缺」。
# - 原始覆盖率实测 0.993~1.000，所以「原文被替换」的阈值取 0.85（安全余量极大）。
# ═══════════════════════════════════════════════════════════════════════════

_SCENE_RE = re.compile(r"^###\s*场景\s*(\d+)", re.M)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_FIELD_RE = re.compile(
    # 冒号后面只能用 [ \t]*：用 \s* 会让空值字段把**下一行**吃成值
    # （实测坑：`- 钩子：` 后面紧跟 `- 必写细节：…`，\s* 跨过换行把下一行当成了钩子）。
    r"^-[ \t]*(?:\*\*(?P<b>[^*\n]+?)\*\*|(?P<p>[^\s:*][^*\n:]{0,10}))[ \t]*[：:][ \t]*(?P<v>[^\n]*)", re.M)
_TAIL_PAREN = re.compile(r"\s*[（(][^）)]*[）)]\s*$")
_TAIL_OPEN = re.compile(r"\s*[（(][^）)]*$")          # 只有左括号的截断形态
_SCENE_ORD_TAIL = re.compile(r"\s*[（(]\s*约?\s*\d+\s*字\s*[）)]\s*$")
_SCENE_ORD_RE = re.compile(r"^\s*(\d+)\s*[.、．]", re.M)
_QUOTE_RE = re.compile(r"[「“\"][^」”\"]{1,200}?[」”\"]")
_COMMENT_RE = re.compile(r"<!--\s*(分镜|视角|镜头|节奏)\s*[：:]")
_SET_COMMENT_RE = re.compile(r"<!--\s*设定")
_TALK_RE = re.compile(r"务必|建议你|建议先|请确认|请注意|要不要我|不建议|记得要")
_PLACEHOLDER_RE = re.compile(r"\{\{.+\}\}|请在此|待补充|TODO|TBD")

_CH_FIELDS = ("地点", "时间", "参与者", "氛围", "情节", "人物反应", "转场方式")
_CH_TOP = ("基本信息", "情绪曲线", "场景列表", "关键细节", "写作提示")
_CH_TOP_FIELDS = ("钩子", "伏笔", "必写细节", "核心事件", "关键转折点", "预计字数")
_COMMENT_KINDS = ("分镜", "视角", "镜头", "节奏")


def _norm_field_name(raw: str) -> str:
    """字段名归一：去 `*`、去首尾空白、去尾部括注（含"只有左括号没右括号"的截断形态）。

    实测必需：41 处字段名带自由括注（`**情节（切入点）**`、`**设定（备查，本章…`）；
    不归一就会把「有」误报成「缺」——第一版测量脚本就是这么错的。
    """
    s = (raw or "").strip().strip("*").strip()
    s = _TAIL_PAREN.sub("", s)
    return _TAIL_OPEN.sub("", s).strip()


def _scenes(text: str) -> list[tuple[int, str]]:
    hits = list(_SCENE_RE.finditer(text or ""))
    out: list[tuple[int, str]] = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out.append((int(m.group(1)), text[m.start():end]))
    return out


def _scene_titles(text: str) -> list[str]:
    """每个场景的**归一标题行**（`### 场景 1：废矿深处（约800字）` → `### 场景 1：废矿深处`）。

    2026-10-26 修：原来只返回 `### 场景 N`，把标题正文丢了 —— 那样"某个场景被整段换掉
    但编号还在"就检测不到，近似重复也永远判不出来。
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        if not _SCENE_RE.match(line):
            continue
        s = _SCENE_ORD_TAIL.sub("", _TAIL_PAREN.sub("", line.strip()))
        out.append(_WS.sub("", s))
    return out


def _fields(block: str) -> dict[str, str]:
    """块内字段 → 值。**续行并进上一个字段**：真机里「情节」的值常写成下面一列编号条目，
    值是空的、内容在后续行 —— 只取冒号后那一段会把它们全判成"空"（第一版实测就错在这）。
    """
    got: dict[str, str] = {}
    cur = ""
    for line in (block or "").splitlines():
        m = _FIELD_RE.match(line)
        if m:
            cur = _norm_field_name(m.group("b") or m.group("p") or "")
            v = (m.group("v") or "").strip()
            if cur and cur not in got:
                got[cur] = v
            continue
        s = line.strip()
        if not s or s.startswith("#"):
            if s.startswith("#"):
                cur = ""            # 换了小节，不再并入上一个字段
            continue
        if cur and cur in got:
            got[cur] = (got[cur] + "\n" + s).strip()
    return got


def _bigrams(text: str) -> set[str]:
    s = _WS.sub("", text or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _coverage(old: str, new: str) -> float:
    """原文 bigram 有多少仍出现在新文本里。1.0 = 一字未改地保留。"""
    a = _bigrams(old)
    if not a:
        return 1.0
    return len(a & _bigrams(new)) / len(a)


def _scene_title_text(line: str) -> str:
    """场景标题里**去掉编号**的那部分（比对"像不像"时要剥掉必然不同的「场景 N」）。"""
    s = _TAIL_PAREN.sub("", (line or "").strip())
    s = _SCENE_ORD_TAIL.sub("", s)
    return s.split("：", 1)[1].strip() if "：" in s else s


def _near_dup(titles: list[str], thresh: float = 0.70) -> list[str]:
    """两两标题相似度 > thresh 的对（按字集 Jaccard 粗判，便宜且够用）。

    阈值 0.70：「下水道」对「下水道里」= 0.75，该报（同一个场景拆成两场）；
    而「医院走廊」对「医院病房」= 0.33，不该报。这条只是观察项，宁可略松。
    """
    bad: list[str] = []
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            a, b = set(titles[i]), set(titles[j])
            if a and b and len(a & b) / len(a | b) > thresh:
                bad.append(f"{titles[i]} ≈ {titles[j]}")
    return bad


def _roster_hits(text: str, roster: list[str]) -> list[str]:
    if not roster:
        return []
    return [n for n in roster if n and n in (text or "")]


def grade_outline_1(outline: str | None, roster: list[str] | None = None) -> list[GraderResult]:
    """第 1 棒（章节细纲师）：定结构。1 条 gating + 9 条观察项。"""
    items = ("章级五节与钩子", "场景字段有值率", "情节有实质", "未偷写正文", "情节编号连续",
             "转折点不越界", "核心事件有着落", "必写细节落地", "场景标题不重复", "参与者与情节一致")
    if outline is None:
        return [skipped("章节细纲师", n) for n in items]
    text = outline
    scenes = _scenes(text)
    n_scene = len(scenes)
    heads = {h.strip() for h in _H2_RE.findall(text)}
    ch_fields = _fields(text)
    miss_top = [k for k in _CH_TOP if k not in heads]
    hook = ch_fields.get("钩子", "")
    top_ok = not miss_top and bool(hook.strip())
    top = GraderResult(
        "章节细纲师·章级五节与钩子", 1.0 if top_ok else 0.0, top_ok, weight=0.30, gating=True,
        detail=("五节齐、钩子非空" if top_ok
                else "缺节:" + "/".join(miss_top) if miss_top else "钩子为空"),
        evidence=([f"缺小节:{k}" for k in miss_top] + ([] if hook.strip() else ["钩子一行为空"])))

    # 场景字段有值率（不做 gating：实测 80%~100%，做 gating 会稳定红）
    per_field = {k: 0 for k in _CH_FIELDS}
    filled = 0
    for _n, blk in scenes:
        fm = _fields(blk)
        for k in _CH_FIELDS:
            v = fm.get(k, "")
            if v.strip() and v.strip() not in ("无", "暂无", "待补", "TBD", "—", "略"):
                per_field[k] += 1
                filled += 1
    total = max(1, n_scene * len(_CH_FIELDS))
    rate = filled / total
    worst = sorted(_CH_FIELDS, key=lambda k: per_field[k])[:3]
    fld = GraderResult(
        "章节细纲师·场景字段有值率", round(rate, 3), rate >= 0.85, weight=0.20, gating=False,
        detail=f"{filled}/{total} 格有值（{n_scene} 场）；最低三个字段："
               + "、".join(f"{k} {per_field[k]}/{n_scene}" for k in worst),
        evidence=[f"{k} 只有 {per_field[k]}/{n_scene} 场填了" for k in worst if per_field[k] < n_scene])

    # 情节有实质：每场情节条目 ≥ 2
    thin: list[str] = []
    for n, blk in scenes:
        part = re.split(r"^-\s*\**情节[^*\n]*\**\s*[：:]", blk, maxsplit=1, flags=re.M)
        body = part[1] if len(part) > 1 else ""
        cnt = len(_SCENE_ORD_RE.findall(body)) or (1 if body.strip() else 0)
        if cnt < 2:
            thin.append(f"场景 {n} 只有 {cnt} 条情节")
    solid = GraderResult(
        "章节细纲师·情节有实质", round(1.0 - len(thin) / max(1, n_scene), 3), not thin,
        weight=0.15, gating=False,
        detail=f"{n_scene} 场里 {len(thin)} 场情节条目 <2", evidence=thin[:6])

    # 未偷写正文：单场直接引语累计 ≥100 字，或任一条 ≥40 字
    noisy: list[str] = []
    for n, blk in scenes:
        qs = _QUOTE_RE.findall(blk)
        total_q = sum(_chars(q) for q in qs)
        longest = max((_chars(q) for q in qs), default=0)
        if total_q >= 100 or longest >= 40:
            noisy.append(f"场景 {n}：引语 {len(qs)} 条/共 {total_q} 字，最长 {longest} 字")
    no_draft = GraderResult(
        "章节细纲师·未偷写正文", 0.0 if noisy else 1.0, not noisy, weight=0.10, gating=False,
        detail=(f"{len(noisy)} 个场景疑似写成正文" if noisy else "没有大段直接引语"),
        evidence=noisy[:5])

    # 情节编号连续
    jumps: list[str] = []
    for n, blk in scenes:
        nums = [int(x) for x in _SCENE_ORD_RE.findall(blk)]
        if nums and nums != list(range(1, len(nums) + 1)):
            jumps.append(f"场景 {n} 编号 {nums[:8]}")
    seq = GraderResult(
        "章节细纲师·情节编号连续", 0.0 if jumps else 1.0, not jumps, weight=0.05, gating=False,
        detail=(f"{len(jumps)} 处编号跳号" if jumps else "编号连续"), evidence=jumps[:4])

    # 转折点不越界（只在明确写了「场景 N」时判）
    turn = ch_fields.get("关键转折点", "")
    cited = [int(x) for x in re.findall(r"场景\s*(\d+)", turn)]
    over = [x for x in cited if x > n_scene]
    turnr = GraderResult(
        "章节细纲师·转折点不越界", 0.0 if over else 1.0, not over, weight=0.05, gating=False,
        detail=(f"转折点引用了不存在的场景 {over}（本章只有 {n_scene} 场）" if over
                else (f"引用了 {cited}" if cited else "转折点没引用场景编号，不判")),
        evidence=[f"场景 {x} 不存在" for x in over])

    # 核心事件有着落（名册角色名至少 1 个出现在正文）
    core = ch_fields.get("核心事件", "")
    named = _roster_hits(core, roster or [])
    if not (roster or []):
        core_r = not_measurable("章节细纲师·核心事件有着落", "没有专名册，无从比对")
    elif not named:
        core_r = GraderResult("章节细纲师·核心事件有着落", 1.0, True, weight=0.05, gating=False,
                              detail="核心事件里没点名册角色，不判")
    else:
        # 只在**场景正文**里找：核心事件那一行本身也含这个名字，拿全文找必然假阳性
        scene_text = "".join(b for _n, b in scenes)
        hit = [n for n in named if n in scene_text]
        ok = bool(hit)
        core_r = GraderResult("章节细纲师·核心事件有着落", 1.0 if ok else 0.0, ok,
                              weight=0.05, gating=False,
                              detail=(f"核心事件的角色 {named} 出现在正文:{hit}" if ok
                                      else f"核心事件点了 {named}，正文里一个都没出现"),
                              evidence=[] if ok else [f"核心事件:「{core[:60]}」"])

    # 必写细节落地（只判能匹配到名册专名的条目）
    must = ch_fields.get("必写细节", "")
    lost: list[str] = []
    if (roster or []) and must:
        for piece in re.split(r"[、；;。\n]|^\s*[-*]\s*", must, flags=re.M):
            piece = piece.strip()
            if not piece:
                continue
            named_piece = _roster_hits(piece, roster or [])
            if named_piece and not any(n in text.replace(must, "") for n in named_piece):
                lost.append(f"{named_piece}（要求:{piece[:30]}）")
    must_r = (not_measurable("章节细纲师·必写细节落地", "没有专名册或没写必写细节")
              if not ((roster or []) and must) else
              GraderResult("章节细纲师·必写细节落地", 0.0 if lost else 1.0, not lost,
                           weight=0.05, gating=False,
                           detail=(f"{len(lost)} 条具名细节没在场景里出现" if lost else "具名细节都落地了"),
                           evidence=lost[:5]))

    # 场景标题不重复（比对时剥掉必然不同的「场景 N」）
    dup = _near_dup([_scene_title_text(t) for t in _scene_titles(text)])
    dup_r = GraderResult("章节细纲师·场景标题不重复", 0.0 if dup else 1.0, not dup,
                         weight=0.05, gating=False,
                         detail=(f"{len(dup)} 对近似标题" if dup else "标题两两不像"),
                         evidence=dup[:4])

    # 参与者与情节一致（只报「整章参与者里从未出现的名册名字」）
    parts_set: set[str] = set()
    for _n, blk in scenes:
        for nm in _roster_hits(_fields(blk).get("参与者", ""), roster or []):
            parts_set.add(nm)
    stray: list[str] = []
    if roster:
        scene_text = "".join(b for _n, b in scenes)
        for nm in roster:
            if nm and nm in scene_text and nm not in parts_set:
                stray.append(nm)
    parti = (not_measurable("章节细纲师·参与者与情节一致", "没有专名册") if not roster else
             GraderResult("章节细纲师·参与者与情节一致", 0.0 if stray else 1.0, not stray,
                          weight=0.05, gating=False,
                          detail=(f"{len(stray)} 个名册名字出现在场景里、却从未进过任何「参与者」"
                                  if stray else "场景里出现的名册名字都在参与者里"),
                          evidence=[f"{n} 从未进参与者" for n in stray[:5]]))
    return [top, fld, solid, no_draft, seq, turnr, core_r, must_r, dup_r, parti]


_KNOWN_FIELDS = set(_CH_FIELDS) | set(_CH_TOP_FIELDS) | {
    # 章级三节的字段（实测必需：第一版只放了上面两族，于是 `- 章节类型：`/`- 起始情绪：`/
    # `- 开篇：` 全被当成"契约外字段"，5 章全红 —— 那是假阳性，不是漂移）。
    "章节类型", "起始情绪", "结束情绪", "开篇", "节奏", "重点",
}


def _new_lines(cur: str, prev: str) -> list[str]:
    """这一棒**新增**的行(逐行差集)。用于"新增文本里有没有混进指令/占位"。"""
    old = {l.strip() for l in (prev or "").splitlines() if l.strip()}
    return [l.strip() for l in (cur or "").splitlines() if l.strip() and l.strip() not in old]


def grade_outline_2(outline: str | None, prev: str | None) -> list[GraderResult]:
    """第 2 棒（章节设定师）：增补设定。1 条 gating + 5 条观察项。"""
    items = ("场景标题只增不减", "结构契约漂移", "原文被替换", "第2棒有实质增补",
             "未混入指令性语言", "无模板占位残留")
    if outline is None:
        return [skipped("章节设定师", n) for n in items]
    cur = outline
    prev = prev or ""

    # ① 标题只增不减（gating；实测 5/5 通过）
    if not prev:
        keep = not_measurable("章节设定师·场景标题只增不减", "上一棒产物缺席，无从比对")
    else:
        lost = [t for t in _scene_titles(prev) if t not in _scene_titles(cur)]
        keep = GraderResult("章节设定师·场景标题只增不减", 0.0 if lost else 1.0, not lost,
                            weight=0.30, gating=True,
                            detail=(f"丢了 {len(lost)} 个场景标题" if lost else
                                    f"{len(_scene_titles(prev))} 个场景标题全在"),
                            evidence=lost[:6])

    # ② 结构契约漂移：出现已知集合外的字段名（实测：`进入方式` 顶替了六个字段）
    seen: dict[str, int] = {}
    paren = 0
    for m in _FIELD_RE.finditer(cur):
        raw = (m.group("b") or m.group("p") or "").strip()
        name = _norm_field_name(raw)
        if name:
            seen[name] = seen.get(name, 0) + 1
        if _TAIL_PAREN.search(raw.strip("*").strip()):
            paren += 1
    unknown = [k for k in seen if k not in _KNOWN_FIELDS]
    drift = GraderResult(
        "章节设定师·结构契约漂移", 0.0 if unknown else 1.0, not unknown, weight=0.15, gating=False,
        detail=(f"出现 {len(unknown)} 个契约外字段：{'、'.join(unknown[:5])}；"
                f"另有 {paren} 处字段名带自由括注"),
        evidence=[f"契约外字段:{k} ×{seen[k]}" for k in unknown[:6]])

    # ③ 原文被替换（同名场景覆盖率 < 0.85，且原文 ≥80 字）
    replaced: list[str] = []
    if prev:
        prev_blocks = dict(_scenes(prev))
        for n, blk in _scenes(cur):
            old = prev_blocks.get(n)
            if old and _chars(old) >= 80:
                cov = _coverage(old, blk)
                if cov < 0.85:
                    replaced.append(f"场景 {n} 原文覆盖率 {cov:.2f}")
    repl = GraderResult(
        "章节设定师·原文被替换", 0.0 if replaced else 1.0, not replaced, weight=0.20, gating=False,
        detail=(f"{len(replaced)} 个场景原文被大量改写" if replaced else "同名场景原文都保住了"),
        evidence=replaced[:5])

    # ④ 第 2 棒有实质增补（保险丝：实测增量是原文的 1.5~2.9 倍，几乎不触发）
    delta = _chars(cur) - _chars(prev) if prev else _chars(cur)
    cov_all = _coverage(prev, cur) if prev else 0.0
    idle = bool(prev) and delta < 50 and cov_all > 0.98
    work = GraderResult(
        "章节设定师·第2棒有实质增补", 0.0 if idle else 1.0, not idle, weight=0.05, gating=False,
        detail=(f"增量仅 {delta} 字、覆盖率 {cov_all:.3f} —— 这一棒几乎没干活" if idle
                else f"增量 {delta} 字（覆盖率 {cov_all:.3f}）"))

    # ⑤ 未混入指令性语言（对作者/对写手说话，不是设定正文）
    talks = _TALK_RE.findall("\n".join(_new_lines(cur, prev)))
    talk = GraderResult(
        "章节设定师·未混入指令性语言", 0.0 if len(talks) >= 2 else 1.0, len(talks) < 2,
        weight=0.10, gating=False,
        detail=f"新增文本里命中 {len(talks)} 处指令词（注意/务必/请/记得/建议）",
        evidence=[] if len(talks) < 2 else [f"命中 {len(talks)} 处"])

    # ⑥ 无模板占位残留
    holes = _PLACEHOLDER_RE.findall(cur)
    hole = GraderResult(
        "章节设定师·无模板占位残留", 0.0 if holes else 1.0, not holes, weight=0.05, gating=False,
        detail=(f"命中 {len(holes)} 处模板占位" if holes else "没有模板残留"),
        evidence=list(dict.fromkeys(holes))[:5])
    return [keep, drift, repl, work, talk, hole]


def grade_outline_3(outline: str | None, prev: str | None, first: str | None = None) -> list[GraderResult]:
    """第 3 棒（章节分镜师）：标镜头。2 条 gating + 4 条观察项。"""
    items = ("场景标题含第1棒全部", "四类分镜注释齐备", "注释未闭合", "逐场注释覆盖",
             "保留了设定注释", "原文被替换")
    if outline is None:
        return [skipped("章节分镜师", n) for n in items]
    cur = outline
    prev = prev or ""
    first = first or prev

    # ① 传递性：第 3 棒必须含第 1 棒全部场景标题（谁丢的一目了然，不另做"跨棒一致性"项）
    if not first:
        keep = not_measurable("章节分镜师·场景标题含第1棒全部", "第 1 棒产物缺席，无从比对")
    else:
        lost = [t for t in _scene_titles(first) if t not in _scene_titles(cur)]
        keep = GraderResult("章节分镜师·场景标题含第1棒全部", 0.0 if lost else 1.0, not lost,
                            weight=0.30, gating=True,
                            detail=(f"比第 1 棒少了 {len(lost)} 个场景标题" if lost
                                    else f"第 1 棒的 {len(_scene_titles(first))} 个场景标题全在"),
                            evidence=lost[:6])

    # ② 四类分镜注释章级齐备（gating；实测 5/5 通过）
    kinds = {m.group(1) for m in _COMMENT_RE.finditer(cur)}
    missing = [k for k in _COMMENT_KINDS if k not in kinds]
    comm = GraderResult(
        "章节分镜师·四类分镜注释齐备", round(len(kinds) / len(_COMMENT_KINDS), 3), not missing,
        weight=0.30, gating=True,
        detail=("四类都在" if not missing else "缺:" + "、".join(missing)),
        evidence=[f"没有 <!-- {k}: … -->" for k in missing])

    # ③ 注释未闭合（末尾有 `<!--` 却没有 `-->`）
    tail = cur[-400:]
    unclosed = tail.count("<!--") > tail.count("-->")
    close = GraderResult(
        "章节分镜师·注释未闭合", 0.0 if unclosed else 1.0, not unclosed, weight=0.10, gating=False,
        detail=("产物末尾有未闭合的 <!-- 注释" if unclosed else "注释都闭合了"))

    # ④ 逐场注释覆盖（只出分布，不做判定：纯对话场确实可以没有镜头注释）
    blocks = _scenes(cur)
    covered = sum(1 for _n, b in blocks if "<!--" in b)
    cover = GraderResult(
        "章节分镜师·逐场注释覆盖", round(covered / max(1, len(blocks)), 3), True,
        weight=0.10, gating=False,
        detail=f"{covered}/{len(blocks)} 场带注释")

    # ⑤ 保留第 2 棒的设定注释（第 2 棒会插 <!-- 设定: … -->，实测 20/20 保留）
    p_set = len(_SET_COMMENT_RE.findall(prev))
    c_set = len(_SET_COMMENT_RE.findall(cur))
    if p_set == 0:
        kept = not_measurable("章节分镜师·保留了设定注释", "上一棒没插设定注释")
    else:
        ok = c_set >= p_set
        kept = GraderResult(
            "章节分镜师·保留了设定注释", round(min(1.0, c_set / p_set), 3), ok,
            weight=0.10, gating=False,
            detail=f"上一棒 {p_set} 条设定注释，这一棒 {c_set} 条",
            evidence=[] if ok else [f"少了 {p_set - c_set} 条设定注释"])

    # ⑥ 原文被替换
    replaced: list[str] = []
    if prev:
        prev_blocks = dict(_scenes(prev))
        for n, blk in _scenes(cur):
            old = prev_blocks.get(n)
            if old and _chars(old) >= 80:
                cov = _coverage(old, blk)
                if cov < 0.85:
                    replaced.append(f"场景 {n} 原文覆盖率 {cov:.2f}")
    repl = GraderResult(
        "章节分镜师·原文被替换", 0.0 if replaced else 1.0, not replaced, weight=0.15, gating=False,
        detail=(f"{len(replaced)} 个场景原文被大量改写" if replaced else "同名场景原文都保住了"),
        evidence=replaced[:5])
    return [keep, comm, close, cover, kept, repl]


def rules_sha() -> str:
    """判据指纹：把本模块所有 `grade_*` 的源码 + 规则常量一起算 sha256（取前 16 位）。

    为什么要它：`report_version` 只能告诉你"口径换代了"，说不出"这次只改了一个词表"。
    两批 step_report 拿来比之前，先比 `rules_sha`——**不同则分数不可直接比**。
    算法含函数源码（正则、阈值、词表都在里面）与 `_` 开头的常量 repr，所以改规则必然变指纹；
    改注释（函数体外）不会变——这是刻意的：注释不改规则。
    """
    import hashlib
    import inspect

    parts: list[str] = []
    for name, obj in sorted(globals().items()):
        if name.startswith("grade_") and callable(obj):
            try:
                parts.append(inspect.getsource(obj))
            except OSError:      # 打包/交互环境下拿不到源码时跳过，不影响指纹稳定性
                continue
    consts = sorted(
        (k, repr(v)) for k, v in globals().items()
        if k.startswith("_") and isinstance(v, (str, int, float, tuple, frozenset, set, re.Pattern))
    )
    parts.append(repr(consts))
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


