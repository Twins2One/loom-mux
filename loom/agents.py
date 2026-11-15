"""Agent 链:按顺序跑 5 个工序,累积一个"本章工作区",每步读到目前为止的全部产物。

Agent 的系统提示词写在 agents/<角色>.md 里(顶部 YAML 声明 reads),不硬编码进这里。
引擎不依赖任何前端:进度通过 progress(event: dict) 回调发出,CLI / 桌面端各自渲染。
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import events, gates, paths, runlog
from .backends import Backend, LoomBackendError
from .chaptertext import compose, strip_title
from .config import Config
from .errors import render
from .fsutil import atomic_write_text, snapshot_chapter
from .guard import STEP, chapter_profile, validate_output
# 留痕围栏/标题解析共置 parse.py(S7);EDIT_NOTE_SENTINEL 保留导出(旧哨兵,读侧永久兼容)
from .parse import EDIT_NOTE_MARKS, EDIT_NOTE_SENTINEL  # noqa: F401
from .parse import clean_title as _clean_title
from .parse import split_edit_note as _split_edit_note
from .parse import strip_edit_note as _strip_edit_note

Progress = Callable[[dict], None]


def review_start_ok(project_root: Path, chapter: int, step: str) -> bool:
    """单棒审稿的**起跑**判据（不限章号）：step 是编辑类 + 该章正文已落盘非空。

    审查 审查记录：起跑语义是「拿现稿去审」，与「第一章地基门豁免」
    是两件事——把章号条件混进起跑判断，会让章 2 的编辑单棒仍被 agentic 分流
    当成整章写作。
    """
    if (step or "").strip() not in ("责任编辑", "润色师"):
        return False
    p = paths.chapter_path(project_root, chapter)
    try:
        return p.is_file() and bool(p.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def review_pass_ok(project_root: Path, chapter: int, step: str) -> bool:
    """第一章**地基门豁免**判据：只有章 1 的审稿起跑才豁免。

    用户裁定点破的前提：放行的话，作者还没落盘审稿信息，编辑拿什么审？⇒ 无现稿仍拦。
    整章生成（from_step 为空）与空 step（全流程）永远不放行。**唯一实现在这里**，
    入口与流水线都调它，避免两处判据漂移。
    """
    try:
        if int(chapter or 0) != 1:
            return False
    except (TypeError, ValueError):
        return False
    return review_start_ok(project_root, chapter, step)

# 运行时注入段的标签（只进 prompt、不进上游签名）。见 run_pipeline._sig。
_RUNTIME_WS_LABELS = frozenset({
    "相关记忆（此前章节沉淀·自动检索）",
    "记忆检索覆盖提示",
})


def _noop(event: dict) -> None:
    pass


# 产物侧行为(篇幅预算、gate 挂接)归产物规格表 artifacts.py(spec 2026-08-16 §4);
# 这里保薄别名保引用面——evalapi.py 的 evals 接缝与既有测试都按 `agents.*` 引用它们,
# 两边永远是同一个对象、不可能漂。
from . import artifacts as _artifacts  # noqa: E402
from .artifacts import outline_budget  # noqa: E402,F401
from .artifacts import scene_budget as _scene_budget  # noqa: E402,F401
from .artifacts import scene_range as _scene_range  # noqa: E402,F401

# 质检/去AI味 关卡:挂在某一棒产出之后。挑硬伤→回炉,不打分、不硬阻断(见 ADR-0006)。
# role -> (人看的名字, 复审提示词, 回炉提示词, 复审要读的设定/方法论, 是否带上一章看钩子)
# 【派生量】真相是 artifacts.GATES + ArtifactSpec.gate;这里只把它翻成老流水线用的角色键形状。
_GATES: dict[str, tuple[str, str, str, list[str], bool]] = {
    a.persona: (g.label, g.critic_system, g.revise_system, list(g.reads), g.wants_prev)
    for a in _artifacts.ARTIFACTS if a.gate and a.persona
    for g in (_artifacts.GATES[a.gate],)
}



# ── 工序表(S4:拓扑与工序行为的唯一真相;此前散作 5 处 role== 特判焊死在主循环里) ──
# 【红线】安全攸关行为(哨兵协议/gate 挂接/WYSIWYG 旁路)只住在这张代码侧表里,
# 绝不下放到用户可编辑的 agents/*.md frontmatter——用户删一行 yaml 不该能让留痕混进正文。
@dataclass(frozen=True)
class StepSpec:
    role: str
    short_budget: int | None = None   # None → config.chapter_chars(写手/编辑/润色师)
    budget_fn: Callable[[int], int] | None = None  # 给了就按章目标现算(大纲师),优先于 short_budget
    wants_prev: bool = False          # prompt 注入上一章正文(接钩子)
    wants_hardfacts: bool = False     # prompt 注入硬设定逐字块
    wants_state: bool = False         # prompt 注入状态账本摘录(物品/规则数值现状)
    #   注:状态账本曾与 hardfacts 共用 wants_hardfacts 开关。拆开是因为编辑要账本
    #   (自检有「物品/状态连续性」一项)却【不该】吃硬设定逐字块——那块是给写手防专名漂移的,
    #   编辑吃了只是灌 token。见 spec §10.1。
    outline_wysiwyg: bool = False     # 细纲链首棒:盘上已有细纲即旁路整条链、首次生成即落盘
    outline_step: bool = False        # 这一步的产物是「细纲」:产出后覆盖写盘(细纲链第2/3棒用)
    edit_sentinel: bool = False       # 输出带哨兵留痕:流式过滤 + 切分落 .审稿留痕(责任编辑)
    deslop: bool = False              # 该棒 gate 挂确定性预筛(aitell+fatigue,润色师)
    foreshadow_after: bool = False    # 该棒后扫伏笔悬空(责任编辑)


STEPS: tuple[StepSpec, ...] = (
    StepSpec("章节细纲师", budget_fn=outline_budget, wants_prev=True, wants_hardfacts=True,
             wants_state=True, outline_wysiwyg=True, outline_step=True),
    StepSpec("章节设定师", budget_fn=outline_budget, wants_hardfacts=True, outline_step=True),
    StepSpec("章节分镜师", budget_fn=outline_budget, outline_step=True),
    StepSpec("写手", wants_prev=True, wants_hardfacts=True, wants_state=True),
    # 编辑拿上一章正文 + 状态账本:它的 12 项自检里「承诺兑现/章首衔接/时间连续性/
    # 物品·状态连续性」四项此前结构上拿不到材料(spec §10.1),只能靠 ≤350 字锚点的二手转述。
    # 刻意【不】给 wants_hardfacts:那是写手防专名漂移用的,编辑不需要。
    StepSpec("责任编辑", wants_prev=True, wants_state=True, edit_sentinel=True, foreshadow_after=True),
    StepSpec("润色师", deslop=True),
)

# 派生量(既有消费点:ledger.resume_point 按 PIPELINE 遍历;测试引用 _SHORT)
PIPELINE = [s.role for s in STEPS]
_SHORT = {s.role: s.short_budget for s in STEPS if s.short_budget}
_SPEC_BY_ROLE = {s.role: s for s in STEPS}
# 这一站之后已经有散文了(喂给它们的输入就是正文):停在这些站 = 「直接看正文」,照常收尾落盘;
# 停在它们之前 = 只有骨架,绝不写空正文、不发 chapter_done。
_PROSE_ROLES = ("写手", "责任编辑", "润色师")

# 正文棒的**输出预算地板**(真机实测,不是拍脑袋)。
#
# `_budget_tokens` 按 `max_chars * 2.2` 现算,chapter_chars=800 ⇒ 1760 token。
# 但本地 Qwen 交一章 800 字的稿子,输出侧要吃掉 ~2100 token(它习惯先写一段自述再落正文):
#   责编棒 max_chars=800  ⇒ tok=1760(正好撞上限) 产出 462 字,断在句中,且**没有留痕块**;
#   同一棒 max_chars=3000 ⇒ tok=2104          产出 1179 字,完整,留痕块齐全。
# 更要紧的是:这个网关**撞上限时仍然回报 finish_reason=stop**,所以"看到 length 再补预算"
# 这条条件重试根本触发不了。⇒ 只能给地板价,不能靠条件判断。
#
# 注意 max_chars 在这里只影响 max_tokens(防截断),**不影响篇幅指令**——
# 篇幅由 `_length_hint` 的 target_chars 单独说。所以抬它不会让稿子变长。
_PROSE_OUTPUT_FLOOR = 3000        # → max_tokens ≈ 6600

# agents/<角色>.md 缺 produces 字段时的兜底文案(角色元数据,非工序行为,不进 StepSpec)
_PRODUCES = {
    "章节细纲师": "本章细纲",
    "章节设定师": "本章细纲(含设定)",
    "章节分镜师": "本章细纲(含分镜)",
    "写手": "本章初稿",
    "责任编辑": "本章改稿",
    "润色师": "本章终稿",
}

# 编辑产出"改稿 + 《本章改动留痕》",用围栏/哨兵分隔(解析在 parse.split_edit_note,三态兼容)。
# 留痕给作者看,绝不进正文/快照/写作指纹。


def _save_edit_note(project_root: Path, chapter_n: int, note: str, progress: Progress) -> None:
    """留痕落盘外的 .审稿留痕/(人可读可改,绝不被任何 learn/写作指纹流程读取)。"""
    path = paths.review_note_path(project_root, chapter_n)
    atomic_write_text(path, note + "\n")
    progress(events.edit_note(chapter_n, path))


def _save_gate_remaining(project_root: Path, chapter_n: int, label: str,
                         remaining: list, progress: Progress, *, header: str | None = None) -> None:
    """gate 跑满轮数仍未解决的硬伤 → 追加进审稿留痕(不阻断,只留痕给作者看)。

    header 给了就用它当小标题(伏笔悬空提醒复用此函数,但措辞不同、与"跑满复审轮数"无关)。
    """
    path = paths.review_note_path(project_root, chapter_n)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [header or f"\n## {label}未解决项(跑满复审轮数仍残留,未阻断,供你定夺)"]
    for i in remaining:
        ev = f" | 证据:「{i.evidence}」" if getattr(i, "evidence", "") else ""
        lines.append(f"- {i.kind}:{i.desc}{ev}")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    progress(events.edit_note(chapter_n, path))


def _flag_overlong(project_root: Path, chapter_n: int, body: str, target: int, progress: Progress) -> None:
    """终稿超长留痕:正文字数(去标题去空白,与 evals grade_length 同口径)超目标 1.25 倍 → 提醒可能注水。

    字数是软指令(_length_hint),模型偶尔越线交给作者定夺——纯提示、绝不阻断出稿(ADR-0006)。
    """
    n = len(re.sub(r"\s+", "", strip_title(body)))
    if target <= 0 or n <= int(target * _artifacts.LEN_OVER):   # 螺丝③:与 artifacts 的篇幅口径同源
        return
    try:
        progress(events.overlong(chapter_n, n, target))   # 独立事件,不只默默写留痕
        path = paths.review_note_path(project_root, chapter_n)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## 篇幅提醒(非阻断,供你定夺)\n"
                    f"- 本章 {n} 字,超出目标 {target} 字较多,可能注水——建议手删或重写\n")
        progress(events.edit_note(chapter_n, path))
    except Exception as e:
        _swallow("篇幅超长留痕写入", e, progress)


_SCENE_BUDGET_RE = re.compile(r"约\s*(\d+)\s*字")
# 场景标题行(`### 场景 1：xxx（约 350 字）`)。`## 基本信息` 里的「- 预计字数：约 800 字」
# 也是「约X字」,但它**不是**某一场的预算 —— 见 _parse_scene_budgets 的注释。
_SCENE_HEAD_RE = re.compile(r"#{2,4}\s*场景\s*[0-9一二三四五六七八九十]+\s*[:：]")

# ── 被吞掉的异常也要留名 ──────────────────────────────────────────────
# 「附赠类操作绝不拖累出稿」这条纪律不变(见下方各调用点的注释),但**吞干净就等于**
# 运行账本(R12)无从归因:篇幅留痕没写进去、轨迹没记上、检测器没建起来,事后全查不出来。
# 所以:行为照旧吞,但每个吞点都往这里记一笔(带上「哪一处」与异常类型)。上限 50 条,
# 越过就丢最老的——它只为归因,不是审计日志。
_SWALLOWED: list[dict] = []
_SWALLOW_MAX = 50


def _swallow(where: str, exc: BaseException, progress: Progress | None = None) -> None:
    _SWALLOWED.append({"where": where, "type": type(exc).__name__, "msg": str(exc)[:200]})
    del _SWALLOWED[:-_SWALLOW_MAX]
    if progress is not None:
        try:   # 连「报这一笔」本身都不许把主流程带崩
            progress(events.warn(f"{where}失败,已跳过({type(exc).__name__}:{exc})"))
        except Exception:   # noqa: 必须静默——再往这儿记一笔就回到同一件事上了
            pass


def swallowed() -> list[dict]:
    """最近被吞掉的异常(供运行账本 / 自检读取)。空列表 = 一次都没吞过。"""
    return list(_SWALLOWED)


_HEAD_LINE_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")


def _heading_blocks(text: str) -> list[tuple[str, list[str]]]:
    """把细纲切成 [(标题行, 该节所有行)] —— 标题行做键,用来做结构比对/补回。"""
    blocks: list[tuple[str, list[str]]] = []
    cur_head, cur = None, []
    for line in (text or "").splitlines():
        m = _HEAD_LINE_RE.match(line)
        if m:
            if cur_head is not None:
                blocks.append((cur_head, cur))
            cur_head, cur = line.rstrip(), [line]
        elif cur_head is not None:
            cur.append(line)
        # 第一个标题之前的内容(极少数情况)直接丢弃:细纲不该有裸开头
    if cur_head is not None:
        blocks.append((cur_head, cur))
    return blocks


_HEAD_BUDGET_TAIL_RE = re.compile(r"[（(]\s*约?\s*\d+\s*字\s*[）)]\s*$")


def _head_key(head: str) -> str:
    """标题比对用的归一化键:**只认"级别 + 标题正文"**,把末尾的「（约400字）」剥掉。

    为什么必须归一(真机踩到):分镜师给每个场景标题补了场次预算
    「### 场景 1：废矿深处，追兵逼近（约400字）」——那正是我们要的标注,
    可逐字比对会把它判成"上一棒的场景 1 丢了",于是白追问一次模型、白花一次调用。
    判据错比缺失本身更贵:它会让兜底自己在正常输出上开火。
    """
    h = (head or "").strip()
    m = re.match(r"^(#+)\s*(.*)$", h)
    if not m:
        return h
    return m.group(1) + " " + _HEAD_BUDGET_TAIL_RE.sub("", m.group(2)).strip()


def _restore_missing_sections(prev: str, cur: str) -> tuple[str, list[str], list[str]]:
    """细纲链的结构兜底:上一棒有、这一棒整节没了的**尾部小节**,逐字补回去。

    返回 (补好后的正文, 补回的小节, 没敢自动搬移的中间小节)。

    为什么要代码兜底(真机):`## 写作提示` 连着三次被「章节设定师」整节丢掉——
    它觉得那一节跟"设定核对"无关,就不写了。提示词里已经逐条列了 9 个标题,模型照丢。
    尾部小节的位置天然确定(它就在文末),补回不会打乱顺序,所以适合用代码做;
    中间小节缺失不做自动搬移(位置推断不可靠),只回报给调用方去 warn。

    比对用 `_head_key`(剥掉「（约N字）」),否则模型给场景标题补场次预算就会被误判成丢节。
    """
    if not (prev or "").strip() or not (cur or "").strip():
        return cur, [], []
    prev_blocks = _heading_blocks(prev)
    cur_text = cur
    cur_heads = {_head_key(h) for h, _ in _heading_blocks(cur)}
    restored, unfixed = [], []
    for i, (head, lines) in enumerate(prev_blocks):
        if _head_key(head) in cur_heads:
            continue
        is_tail = (i == len(prev_blocks) - 1)          # 上一棒的最后一节
        if not is_tail:
            unfixed.append(head)
            continue
        body = "\n".join(lines).rstrip()
        if body:
            cur_text = cur_text.rstrip() + "\n\n" + body + "\n"
            restored.append(head)
    return cur_text, restored, unfixed


def _parse_scene_budgets(outline: str) -> list[int]:
    """抓**各场**的「约X字」标注(螺丝④⑤共用)。没标返回空列表。

    旧实现全文扫「约X字」,于是 `## 基本信息` 里的
    「- 预计字数：约 800 字」被当成某一场的预算 —— 三场 350+300+150 本来正好等于
    目标 800,却被算成 800+350+300+150=**1600**,系统连着三棒误报
    「细纲各场字数总和(约1600字)与本章目标(800字)差得多」。模型是对的,判据是错的。
    现在只认场景标题行里的约定;模型若没写在场标题里,才退回全文口径(保持旧行为兜底)。
    """
    per_scene: list[int] = []
    for line in outline.splitlines():
        s = line.strip()
        if _SCENE_HEAD_RE.search(s):
            per_scene += [int(m) for m in _SCENE_BUDGET_RE.findall(s)]
    if per_scene:
        return per_scene
    return [int(m) for m in _SCENE_BUDGET_RE.findall(outline)]


def _should_take_section_retry(old_missing: list[str], new_missing: list[str],
                               old_len: int, new_len: int) -> bool:
    """追问一次之后,要不要用新那版覆盖旧的?

    条件写死两条,都是为了**不让兜底变成新的压缩**:
    1. 缺得更少(`len(new_missing) < len(old_missing)`)—— 没改善就白换;
    2. 没变短(`new_len >= old_len`)—— 真机踩过:模型"补回删掉的那节"的同时把别处又砍了。
    抽成纯函数是为了能直接测(追问路径要等真机刚好丢中间节才会走到,不能只靠运气验)。
    """
    return bool(new_len) and len(new_missing) < len(old_missing) and new_len >= old_len


def _check_scene_budget(project_root: Path, chapter_n: int, outline: str, chapter_target: int,
                        reused: bool, progress: Progress) -> None:
    """细纲字数确定性校验(非阻断,只 warn/info)。任何异常吞掉,绝不拖累出稿。
    reused=True(WYSIWYG 沿用已有细纲)→⑤陈旧提醒;False(大纲师刚产出)→④缺标注/偏差。"""
    try:
        budgets = _parse_scene_budgets(outline)
        total = sum(budgets)
        if reused:
            if total > 0 and chapter_target > 0 and abs(total - chapter_target) > chapter_target * 0.3:
                progress(events.info(
                    f"这份细纲按旧字数(约{total}字)拆的、当前目标 {chapter_target} 字,"
                    "建议在「本章细纲」点『重新生成细纲』按新目标重拆。"))
        else:
            if not budgets:
                progress(events.warn("细纲各场没标「约X字」篇幅预算,写手篇幅可能失控——可重新生成细纲。"))
            elif chapter_target > 0 and abs(total - chapter_target) > chapter_target * 0.3:
                progress(events.warn(
                    f"细纲各场字数总和(约{total}字)与本章目标({chapter_target}字)差得多,写手篇幅可能失控。"))
    except Exception as e:
        _swallow("细纲场次字数核对", e, progress)
    # 反压缩:与场次字数一样,是「出稿前看一眼」的提示,不拦稿
    _check_anticompress(outline, progress)


def _check_anticompress(outline: str, progress: Progress) -> None:
    """反压缩:细纲里出现收束语却又没留未解问题 → 提示「可能在提前收线」。

    **非阻断、只提示**(词面启发式,会误报也会漏报;口径写在 anticompress.py 顶部)。
    任何异常吞掉,绝不拖累出稿。
    """
    try:
        from .anticompress import report_line, scan

        line = report_line(scan(outline))
        if line:
            progress(events.warn(line))
    except Exception as e:
        _swallow("反压缩检测", e, progress)


def _flag_stale_foreshadow(project_root: Path, chapter_n: int, config: Config, progress: Progress) -> None:
    """编辑棒后顺手扫一遍卡章纲:埋了很久仍无推进/回收的伏笔 → 进审稿留痕提醒。

    纯本地、不发 LLM、不打分、不回炉、不阻断(它读卡章纲、质检回炉改的是正文,放回炉只会空转)。
    任何异常都吞掉——附赠类提醒绝不拖累出稿(同 _scan_sensitive)。卡章纲没 recap 伏笔行时自然为空。
    """
    if getattr(config, "foreshadow_distance", 0) <= 0:
        return
    try:
        from .hooks import stale
        issues = stale(project_root, chapter_n, config.foreshadow_distance)
    except Exception:
        return
    if not issues:
        return
    _save_gate_remaining(
        project_root, chapter_n, "伏笔悬空", issues, progress,
        header="\n## 伏笔悬空提醒(非阻断,供你定夺;若本章正回收可忽略)",
    )


_EDIT_TAIL = max(len(m) for m in EDIT_NOTE_MARKS)   # 尾窗取最长开标记,半截标记不漏判


def _edit_stream_filter(progress: Progress) -> Callable[[str], None]:
    """编辑棒的流式过滤:只把开标记【之前】的干净改稿流给前端,标记及其后的留痕不外流。

    兼容两种开标记(新围栏 <LOOM:EDIT-NOTE> / 旧哨兵);标记可能被切成多个 delta,
    故按累计串判断、并留一个尾窗防止半截标记漏判。
    """
    st = {"buf": "", "emitted": 0, "cut": False}

    def cb(delta: str) -> None:
        if st["cut"]:
            return
        st["buf"] += delta
        hits = [i for i in (st["buf"].find(m) for m in EDIT_NOTE_MARKS) if i != -1]
        idx = min(hits) if hits else -1
        if idx == -1:
            clean = st["buf"][: max(0, len(st["buf"]) - _EDIT_TAIL)]
        else:
            clean = st["buf"][:idx]
        new = clean[st["emitted"]:]
        if new:
            progress(events.agent_chunk("责任编辑", new))
            st["emitted"] += len(new)
        if idx != -1:
            st["cut"] = True

    return cb


def _missing_edit_note(text: str) -> bool:
    """交付契约机检:这一棒该不该交出留痕?没留痕标记 = 没交改稿。

    为什么用标记而不是"看起来像不像正文":留痕标记是模板已经要求(责任编辑.md)、
    `split_edit_note()` 已经能识别的**确定性契约**;靠长度或启发式判断"这算不算改稿"
    都会误判。真机代价见 docs/流水线诊断-v2-审查.md §5.5——责任编辑交了 2424 字
    自述分析,因为没有标记被 `split_edit_note` 当成全文正文,一路传到终稿 395 字。
    """
    return not any(m in (text or "") for m in EDIT_NOTE_MARKS)


@dataclass
class Agent:
    name: str
    reads: list[str] = field(default_factory=list)
    reads_first_chapter: list[str] = field(default_factory=list)
    produces: str = ""
    system_prompt: str = ""


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """极简 YAML frontmatter 解析(只够解析本项目自己的 agents/*.md)。"""
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    meta: dict = {}
    current_key: str | None = None
    for raw in fm.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and current_key:
            meta.setdefault(current_key, []).append(line.lstrip()[2:].strip())
        elif ":" in line:
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            current_key = key
            meta[key] = val if val else []
    return meta, body.strip()


# 「入口自由」:六步那套是**世界驱动**的顺序,可真人开书有三种入口
# (先有世界 / 先有人物 / 先有一个画面)。角色文件里写着「顺序固定,别自己加步」,
# 把后两种卡死了。这段由**代码**注入 —— 老书用的是书内 agents/领航员.md 副本,
# 改出厂模板碰不到它们,注入才能一次对所有书生效(与主动性/契约同一套路子)。
_NAV_FLEX = """## 起手方式（比六步优先，先读这一段）
六个阶段是**清单**,不是必经顺序。开口第一件事是问他:**"你脑子里现在已经有什么了?"**
- **先有世界/设定/金手指** → 从「世界观」起手,再补人物与立项。
- **先有一个人**(「我想写个这样的主角」) → 从「人物」起手,世界观跟着他的处境长出来。
- **先有一个画面/一句台词/一个梗** → 先把这一场记下来(可以进
  `外置大脑/设定集/50-自定义/` 的一小节,也可以直接写一章试水),再从它反推需要什么设定。
**别为了走完六步,逼他先编一个自己都不信的世界。**
门禁口径:真正卡门的只有三格 —— **立项 / 世界观 / 人物**。
卷纲、卡章纲都可以跳;他说「先写一章看看」,就让他写,写章前只提醒一句缺什么,不拦。
"""


def load_agent(project_root: Path, role: str) -> Agent:
    path = project_root / "agents" / f"{role}.md"
    if not path.exists():
        from .errors import render
        raise FileNotFoundError(render("agent_prompt_missing", detail=str(path)))
    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return Agent(
        name=meta.get("name", role) or role,
        reads=meta.get("reads", []) or [],
        reads_first_chapter=meta.get("reads_first_chapter", []) or [],
        produces=meta.get("produces", _PRODUCES.get(role, "")),
        system_prompt=body + ("\n\n" + _NAV_FLEX if role == "领航员" else ""),
    )


def _iter_dir_md(d: Path, exclude: tuple[str, ...] = ()):
    """目录型 reads → 该目录下所有 .md（**递归**）。

    为什么要递归（实测发现）：`外置大脑/设定集` 是个父目录，里面是
    `00-立项.md` + `10-世界观/`、`20-人物/`、`30-战斗/`、`40-知识/`、`90-速记本/` 五个子目录。
    原先用 `glob("*.md")` 只扫到 00-立项.md，导致**大纲师/批评家/知识顾问读设定集时漏掉全部
    世界观与人物**——表现就是"角色说没材料"。这是联动断掉的根因之一。

    排除三类（见 docs/角色联动设计-待审查.md §八 F1）：
    - **`90-速记本/`**：14 个角色各自的私有工作记录，进别人的 prompt 就是互相污染。
      ⚠ 旧写法 `skip={"速记本"}` 是**精确匹配**，`"90-速记本" in {"速记本"}` 恒为 False，
      等于从来没排除过（只要速记本里有内容就会漏进 8 个角色的上下文）。
    - **任何以 `.` 开头的段**：`正文/` 下有 `.原稿/`（AI 原稿快照）、`.细纲/`、`.历史/`，
      它们是引擎内部产物、不是稿子；同 `partner_tools._safe_read_path` 的「点开头段拒绝」口径。
    - **`README.md`**：说明文件，不是材料。

    `exclude`：按**文件名**排除的名单，用来落「立项卡只进设定师」那条红线
    （见 `_READS_EXCLUDE`）。这里只比文件名而不比项目内相对路径，是因为 `d` 已经是
    reads 指到的那个目录本身，拿不到「项目根 → 文件」的相对路径；调用方传 `paths.SET_CARD_REL`
    这类带目录的常量也照样能用（取它的 basename 比）。
    """
    banned = {Path(x).name for x in exclude}
    out = []
    for f in d.rglob("*.md"):
        rel_parts = f.relative_to(d).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if any(part in {"90-速记本", "速记本"} for part in rel_parts):
            continue
        if f.name == "README.md":
            continue
        if f.name in banned:
            continue
        out.append(f)
    return out


def _read_file_items(project_root: Path, rels: list[str], progress: Progress,
                     exclude: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """reads 清单 → [(展示名, 文本)]。knowledge 拼装与续跑签名共用这一份,保证同源。
    占位提示行不进 prompt;剥完无实质内容的文件(出厂模板)整份跳过——
    占位模板是写给作者的填写说明,不是书的设定(docs/design/proposals/立项即铺底.md §1)。"""
    from .parse import is_substantive, strip_placeholder_hints
    items: list[tuple[str, str]] = []

    def _add(name: str, raw: str) -> None:
        text = strip_placeholder_hints(raw)
        if is_substantive(text):
            items.append((name, text))

    for rel in rels:
        p = project_root / rel
        if p.is_dir():
            # 目录型 reads(如 skills/题材):读其中所有 .md(跳过 README,排序保证签名稳定)
            for f in sorted(_iter_dir_md(p, exclude)):
                if f.stem != "README":
                    _add(f"{rel}/{f.name}", f.read_text(encoding="utf-8"))
        elif p.exists():
            if p.name in {Path(x).name for x in exclude}:
                continue          # 直接点名的单文件 reads 也要服从同一条排除(见 _READS_EXCLUDE)
            _add(rel, p.read_text(encoding="utf-8"))
        else:
            progress(events.warn(f"跳过缺失文件 {rel}"))
    return items


def _read_files(project_root: Path, rels: list[str], progress: Progress) -> str:
    return "\n\n".join(f"【{rel}】\n{text.strip()}"
                       for rel, text in _read_file_items(project_root, rels, progress))


def _prev_chapter(project_root: Path, chapter_n: int) -> str:
    """读上一章【手改后的】正文做行文衔接(不是 .原稿 快照);去掉标题行,只喂正文体。"""
    if chapter_n <= 1:
        return ""
    p = paths.chapter_path(project_root, chapter_n - 1)
    return strip_title(p.read_text(encoding="utf-8")).strip() if p.exists() else ""


# 二级标题行(`## 标题`,容忍前导空格与「##标题」无空格;### 不算):切世界观小节用。
_H2_RE = re.compile(r"^\s{0,3}##\s*(?!#)(.+?)\s*$")
_H3_RE = re.compile(r"^\s{0,3}###\s*(?!#)(.+?)\s*$")
# 三级及更深标题行(### ~ ######):剔反转子块时要知道层级,才能"剔到同级/更浅标题为止"。
_H3PLUS_RE = re.compile(r"^\s{0,3}(#{3,6})\s*(.+?)\s*$")
# 任意层级标题行(# ~ ######):`_drop_spoiler_h2_sections` 按层级复位要认得比 H2 更浅的 H1。
_HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s*(.+?)\s*$")

# 世界观里【必须逐字照搬】的硬设定小节——按标题(H2,H2 没命中再看其内 H3)含这些词命中,
# 整段原文透传给大纲师/写手。只圈"规则 + 专名"(境界阶梯/金手指代价/地名势力;历史/都市题材的
# 时代格局/主角优势 同级;系统流的系统机制/因果法则 同级):这些一旦被复述就漂
# (F~SSS 凭空多出"一阶0级"、"一中"写成"二中")。故意不圈「一句话定位」(情绪基调、本就该意译)。
_HARDFACT_KW = ("力量", "体系", "境界", "等级", "修为", "实力", "修炼", "金手指",
                "地理", "势力", "阵营", "地图", "区域", "时代", "优势", "系统", "因果")

# 反转/真相/底牌类小节:即便标题撞上硬设定关键词(如「## 势力背后的真相」),也绝不逐字喂写手
# ——逐字喂等于提前抖包袱,踩本功能唯一的硬红线。deny 压过 allow,且连嵌进 ### 的反转一并剥掉。
_SPOILER_KW = ("冰山", "真相", "反转", "伏笔", "底牌", "终局", "结局", "隐藏", "暗线", "秘密")

# 人物卡里「类型 · 名字」的名字分隔符:专名册只认带它的标题,免得把「## 主角」占位标题、
# learn 追加的「## AI 补充…」段名当人名喂写手。
_NAME_SEP = ("·", "・", "•")


def _md_sections(md: str, head_re: re.Pattern) -> list[tuple[str, str]]:
    """按 head_re 命中的标题行切成 [(标题文本, 含标题行的整段)];前言忽略,更深层标题归属其上段。"""
    sections: list[tuple[str, str]] = []
    head: str | None = None
    buf: list[str] = []
    for ln in md.splitlines():
        m = head_re.match(ln)
        if m:
            if head is not None:
                sections.append((head, "\n".join(buf).rstrip()))
            head, buf = m.group(1), [ln.rstrip()]
        elif head is not None:
            buf.append(ln.rstrip())
    if head is not None:
        sections.append((head, "\n".join(buf).rstrip()))
    return sections


def _md_h2_sections(md: str) -> list[tuple[str, str]]:
    """把 markdown 按二级标题切;H1 与前言忽略,### 归属其上层 H2。"""
    return _md_sections(md, _H2_RE)


def _drop_spoiler_subsections(section: str) -> str:
    """从一段里剔掉标题命中 _SPOILER_KW 的子块(### 及更深都算,连同其子孙),
    直到出现同级或更浅的标题为止——只认 ### 会被写进 #### 的反转绕过。"""
    out: list[str] = []
    skip = 0  # >0 = 正在剔除,值为命中反转的标题层级
    for ln in section.splitlines():
        m = _H3PLUS_RE.match(ln)
        if m:
            level = len(m.group(1))
            if skip and level <= skip:
                skip = 0
            if not skip and any(s in m.group(2) for s in _SPOILER_KW):
                skip = level
        if not skip:
            out.append(ln)
    return "\n".join(out).rstrip()


def _drop_spoiler_h2_sections(text: str) -> str:
    """从整份 markdown 里剔掉标题命中 _SPOILER_KW 的 H2 节(连同其下所有子标题),
    保留 H2 之前的前言与其余 H2 节——单文件形态的冰山真相多半是 `## 冰山真相`
    (见 loom/sample/agents/设定师.md 的 reads 形状、draft.py 一键起稿模板、
    journey.py 的 slot_order),`_drop_spoiler_subsections` 只认 ### 及更深,
    补上这一层 H2 层 deny(与 `_pick_hardfacts` 的 `if spoiler(head): continue` 同一先例)。

    用层级比较复位(同 `_drop_spoiler_subsections` 口径):skip 记的是命中反转的标题层级,
    遇到同级或更浅(含 H1)的标题就复位——只认下一个「##」复位会漏掉「##」之后紧跟的「#」,
    把它连同其后的整块内容一并连坐吞掉。
    """
    out: list[str] = []
    skip = 0  # >0 = 正在剔除,值为命中反转的标题层级(此处只会是 2)
    for ln in text.splitlines():
        m = _HEAD_RE.match(ln)
        if m:
            level = len(m.group(1))
            if skip and level <= skip:
                skip = 0
            if not skip and level == 2 and any(s in m.group(2) for s in _SPOILER_KW):
                skip = level
        if not skip:
            out.append(ln)
    return "\n".join(out).rstrip()


def _deny_spoiler_items(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """按反转段 deny 过滤 knowledge 条目(`_knowledge_prompt(deny_spoiler=True)` 专用)。

    为什么三层 deny 都只作用在世界观条目上:冰山真相这类反转段本就只住在世界观里
    (目录形态 外置大脑/世界观/冰山真相.md、单文件形态 外置大脑/世界观.md 的
    `## 冰山真相` 小节)。而 `_SPOILER_KW` 那十个词(冰山/真相/反转/伏笔/底牌/终局/
    结局/隐藏/暗线/秘密)在世界观以外是**正常词汇**,不是反转信号:卡章纲会写
    「## 第12章 · 真相揭露」,人物卡会写「## 配角 · 秘密情人」,写手的 skills 方法论
    会写「### 怎么埋伏笔」。deny 的范围一旦放宽过世界观这一层,就变成见词就杀,
    静默吃掉作者自己写的东西——这正是这里三层判据都要收在 `in_world_dir`/
    `rel == paths.WORLD_REL` 之内、不能只看关键词的原因。

    与 `_hardfacts_for` 目录形态那条先例同一判据、**复用**同一份 `_SPOILER_KW`/
    `_drop_spoiler_subsections`,不另写一套关键词(两处 deny 判据只能有一份,否则会漂):
    文件名(stem)命中 _SPOILER_KW 的条目整份剔除——**只对世界观目录内的条目生效**
    (`paths.WORLD_DIR_REL`,如 世界观/冰山真相.md 这种目录形态),与 `_hardfacts_for`
    同一判据同一范围;人物卡/技能卡等其他 read 条目文件名撞上关键词(如
    外置大脑/人物/配角·秘密情人.md)不该被株连整份消失。

    H2 层与 H3+ 层 deny(`_drop_spoiler_h2_sections`/`_drop_spoiler_subsections`,单文件
    形态的冰山真相是 `## 冰山真相`)同理**只对世界观条目生效**(`in_world_dir` 或单文件
    形态 `rel == paths.WORLD_REL`)——此前两层都无条件套在每一个 read 条目上,卡章纲/
    人物卡/skills 方法论这些不在世界观里的条目,只要标题撞上反转关键词(如「## 第12章 ·
    真相揭露」「## 配角 · 秘密情人」「### 怎么埋伏笔」)整节就被静默剔除,同一批修复里
    stem-deny 收窄的判词正是「不该套到世界观以外的条目上」——H2 层先照此收窄过一轮,
    H3+ 层在这里又犯了同一个错(写手读的 skills/网文大神.md 追加「### 怎么埋伏笔」,标题
    撞上「伏笔」,作者自己写的写作手艺被整节吞掉),两层判据必须一致,故收进同一个 if。
    """
    out: list[tuple[str, str]] = []
    for rel, text in items:
        in_world_dir = rel.startswith(f"{paths.WORLD_DIR_REL}/")
        if in_world_dir and any(s in Path(rel).stem for s in _SPOILER_KW):
            continue   # 目录形态、且在世界观目录内:整份文件名就是反转(如 冰山真相.md),整份剔除
        if in_world_dir or rel == paths.WORLD_REL:
            text = _drop_spoiler_h2_sections(text)      # H2 层 deny 只对世界观条目生效
            text = _drop_spoiler_subsections(text)       # H3+ 层同一判据,不再对其余条目无差别生效
        out.append((rel, text))
    return out


def _name_roster(card_path: Path) -> str:
    """从人物卡抓「## 类型 · 名字」式专名册——只认带名字分隔符的标题,顺手滤掉 learn 追加的
    「## AI 补充…」段与「## 主角」这类没填名的占位标题:宁可空,也绝不把段名当人名喂写手。"""
    if not card_path.is_file():
        return ""
    names = [head for head, _ in _md_h2_sections(card_path.read_text(encoding="utf-8"))
             if any(s in head for s in _NAME_SEP) and "补充" not in head]
    return "\n".join(f"- {n}" for n in names)


def _name_roster_for(project_root: Path) -> str:
    """专名册双形态:老书读 人物卡.md 的 H2;目录形态读 人物/ 的【文件名】(一人一文件,
    文件名即「类型·名字」)。占位「未命名」与成长档案不进名册——绝不把占位喂写手。"""
    form = paths.brain_form(project_root, paths.CHARS_REL, paths.CHARS_DIR_REL)
    if form == "file":
        return _name_roster(project_root / paths.CHARS_REL)
    if form == "dir":
        names = [f.stem for f in paths.brain_dir_files(project_root, paths.CHARS_DIR_REL)
                 if f.name != paths.GROWTH_NAME
                 and any(s in f.stem for s in _NAME_SEP) and "未命名" not in f.stem]
        return "\n".join(f"- {n}" for n in names)
    return ""


_CONSTRAINT_KW = ("底线", "身段", "命格", "原则", "禁忌")


def _char_constraints_for(project_root: Path) -> str:
    """人物卡里标签含 底线/身段/命格/原则/禁忌 的行,逐字直送写手——人设跌份的根治点。
    纯字符串切片,双形态同口径;成长档案与 [AI补充] 照旧排除;无命中返回空串。

    两类占位一律不进(与 _name_roster_for 同口径「绝不把占位喂写手」):
    ①【占位人物】文件名/小节名含「未命名」的 scaffold 卡——否则写手 prompt 里会凭空
      多出一个叫「未命名」的人;②【没填完的行】标签命中但冒号后为空(`- 底线:`)——
      纯噪声,且换个文件名绕不过 ① 的过滤,故要单独拦。
    """
    def pick(name: str, text: str) -> list[str]:
        out = []
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("-"):
                continue
            body = s.lstrip("- ").strip()
            parts = re.split(r"[:：]", body, 1)
            value = parts[1].strip() if len(parts) > 1 else ""
            if any(kw in parts[0] for kw in _CONSTRAINT_KW) and value:
                out.append(f"- {name}|{body}")
        return out

    lines: list[str] = []
    form = paths.brain_form(project_root, paths.CHARS_REL, paths.CHARS_DIR_REL)
    if form == "file":
        for head, body in _md_h2_sections((project_root / paths.CHARS_REL).read_text(encoding="utf-8")):
            if "AI 补充" in head or "AI补充" in head or "未命名" in head:
                continue
            lines += pick(head, body)
    elif form == "dir":
        for f in paths.brain_dir_files(project_root, paths.CHARS_DIR_REL):
            if f.name == paths.GROWTH_NAME or "未命名" in f.stem:
                continue
            lines += pick(f.stem, f.read_text(encoding="utf-8"))
    return "\n".join(lines)


def _pick_hardfacts(md: str) -> list[str]:
    """世界观里命中 _HARDFACT_KW 的小节。H2 优先;H2 没命中再看其内的 H3(用户把硬设定写在
    三级标题下);整份没用 ## 时按 H3 顶层切。deny(_SPOILER_KW)一律压过 allow。"""
    def hard(h: str) -> bool: return any(kw in h for kw in _HARDFACT_KW)
    def spoiler(h: str) -> bool: return any(s in h for s in _SPOILER_KW)
    picked: list[str] = []
    h2s = _md_h2_sections(md)
    for head, body in h2s:
        if spoiler(head):
            continue
        if hard(head):
            picked.append(_drop_spoiler_subsections(body))
        else:
            picked += [_drop_spoiler_subsections(b) for h, b in _md_sections(body, _H3_RE)
                       if hard(h) and not spoiler(h)]
    if not h2s:  # 整份世界观没写 ##、只用了 ###
        picked = [_drop_spoiler_subsections(b) for h, b in _md_sections(md, _H3_RE)
                  if hard(h) and not spoiler(h)]
    return [p for p in picked if p.strip()]


def _hardfacts_for(project_root: Path, progress: Progress = _noop) -> str:
    """确定性切出【硬设定】原文,绕开设定师的有损复述,逐字透传给大纲师/写手:
    世界观里命中 _HARDFACT_KW 的小节(境界阶梯/金手指代价/地名势力)+ 人物卡专名册。

    红线:纯字符串切片,不调 LLM、不打分、不建实体库;空/缺失一律返回空串(附加增强,绝不阻断出稿)。
    反转段(_SPOILER_KW)deny 压过 allow,绝不逐字喂写手。
    世界观有内容却一节都没命中 → 发 warn 提示检查标题写法(只提示,照旧不阻断)。
    注:learn 自动追加到「## AI 补充」段的新层级/新势力【不】进逐字块(那段标题不含硬设定关键词,
    且自动追加块不该被当真相喂写手);要让它们逐字护身,把它们手并进 ## 力量体系 / ## 地理 等正式段。
    """
    blocks: list[str] = []
    form = paths.brain_form(project_root, paths.WORLD_REL, paths.WORLD_DIR_REL)
    if form == "file":   # 老书单文件:H2 标题关键词切片(既有路径,零迁移)
        picked = _pick_hardfacts((project_root / paths.WORLD_REL).read_text(encoding="utf-8"))
        if picked:
            blocks.append("\n\n".join(picked))
        else:
            progress(events.warn(
                "世界观里没识别到硬设定小节(如「## 力量体系」「## 地理 / 势力」),"
                "等级/专名这次没有逐字保护——请检查标题写法"))
    elif form == "dir":  # 目录形态:按【文件名】选(比标题匹配稳),deny 压过 allow,整文件逐字
        picked = []
        for f in paths.brain_dir_files(project_root, paths.WORLD_DIR_REL):
            stem = f.stem
            if any(s in stem for s in _SPOILER_KW) or f.name == paths.GROWTH_NAME:
                continue   # 冰山真相/成长档案 绝不逐字喂写手
            if any(kw in stem for kw in _HARDFACT_KW):
                picked.append(_drop_spoiler_subsections(f.read_text(encoding="utf-8").strip()))
        if picked:
            blocks.append("\n\n".join(p for p in picked if p.strip()))
        else:
            progress(events.warn(
                "世界观目录里没识别到硬设定文件(如「力量体系.md」「地理与势力.md」),"
                "等级/专名这次没有逐字保护——请检查文件名"))
    roster = _name_roster_for(project_root)
    if roster:
        blocks.append("【人物专名册(照此写,别改名/别另造名)】\n" + roster)
    constraints = _char_constraints_for(project_root)
    if constraints:
        blocks.append("【人物硬约束(逐字遵守:身份/底线/身段不许跌份)】\n" + constraints)
    return "\n\n".join(b for b in blocks if b.strip())


# 标题生成(附赠动作:用户选了「自动起标题」。失败/空一律静默回退无标题,绝不阻断出稿)
_TITLE_SYSTEM = (
    "你是网文编辑,给这一章起一个【章节标题】。要求:6-16 字,贴合内容、有点钩子、不剧透章末反转;"
    "不要带「第N章/第一章」之类章号,不要书名号/引号/星号包裹,只输出标题本身这一行,别的什么都不要输出。"
)


def _generate_title(backend: Backend, prose: str) -> str:
    """给本章起个标题。任何失败(网络/空响应/格式不对)都吞掉、回退空串——绝不让附赠动作拖累出稿。

    预算为什么是 600 而不是 24(真机):24 字 → max_tokens 只有 52,
    本地 Qwen 连"想一下"的余量都没有,直接吐半句 `用户要求给一章网文起章节标题，需要`,
    而 `clean_title` 又按"首航 ≤24 字"把它当成了标题。抬高到 600(≈1320 token)才有落款余地。
    """
    try:
        raw = backend.complete(_TITLE_SYSTEM, f"这一章的正文如下,给它起个标题:\n\n{prose[:1200]}",
                               max_chars=600)
    except Exception:
        return ""
    return _clean_title(raw)


def _outline_path(project_root: Path, chapter_n: int) -> Path:
    """本章细纲(大纲师的分镜)落盘处:可看可改。一旦存在,大纲师就读它(WYSIWYG)——
    你改了它,重写本章就按你的来;想要新方案,清空它或点「重新生成细纲」。"""
    return paths.outline_path(project_root, chapter_n)


def _knowledge_for(project_root: Path, chapter_n: int, role: str) -> tuple[Agent, str]:
    """原始 knowledge(不折叠)——续跑签名 v1 垫片与既有调用点用它,保持字节稳定。"""
    a, items = _knowledge_items(project_root, chapter_n, role)
    return a, "\n\n".join(f"【{rel}】\n{text.strip()}" for rel, text in items)


# 裁定 6③:实体词典按「50 章一块」推给这两个角色。
# 不给责任编辑/润色:它们改的是已写出来的字,不需要另带一张人名词表(白占 token)。
_ENTITY_ROLES = ("章节细纲师", "写手")


def _entity_block(project_root: Path, chapter_n: int, role: str) -> tuple[str, str] | None:
    """这一块的实体词典节(标题, 正文)。没词可推、或角色不吃这张表 -> None。"""
    if role not in _ENTITY_ROLES:
        return None
    from . import entities
    ents, (lo, hi) = entities.subset_for_block(project_root, chapter_n)
    if not ents:
        return None
    head = f"实体词典(第{lo}-{hi}章会用到的那几条)"
    lines = ["| id | 类型 | 正名 | 别名 |", "|---|---|---|---|"]
    for e in ents:
        lines.append("| " + " | ".join([e.id, e.kind, e.name, "、".join(e.aliases)]) + " |")
    lines.append("")
    lines.append("名字一律照这里的正名写;别名该怎么叫见「人物」设定。")
    return head, "\n".join(lines)


def _knowledge_prompt(project_root: Path, chapter_n: int, role: str, *,
                      deny_spoiler: bool = False) -> tuple[Agent, str]:
    """进 prompt 的 knowledge:远章 AI 追加块按预算折叠(budget.py,确定性)。
    签名仍吃原始文件(_knowledge_items)——折叠是 (文件,章号) 的纯函数,不影响续跑语义。

    `deny_spoiler`=True(终审②critical,`write_tools._handle_persona`「取人格」工具专用):
    knowledge 先过一遍反转段 deny(见 `_deny_spoiler_items`)。旧流水线 `run_pipeline` 的
    调用点不传这个参数——那边一棒一次调用,写手 reads 本就不含世界观,结构上已经隔离
    (ADR 0010 红线的主语是写手)。agent 化后 `_handle_persona` 的返回值进 writeloop 的
    trail、每一轮都重新拼进 prompt——「取人格:设定师」取到的冰山真相原文会绕过写手自己的
    reads 边界,躺进它落字那次调用的上下文里,这里补上等价保护。
    """
    from . import budget
    a, items = _knowledge_items(project_root, chapter_n, role)
    if deny_spoiler:
        items = _deny_spoiler_items(items)
    blocks = []
    for rel, text in items:
        if rel == paths.CARD_REL:
            # 两道刀:①已完卷的逐章行折成「一卷一行」(budget.fold_past_volumes,靠卷纲切卷)
            # ②远章的 [AI回顾] 块折叠。两刀都是 (文件,章号) 的纯函数,签名仍吃原文。
            text = budget.card_for_prompt(project_root, text, chapter_n)
        elif rel in paths.SUPP_RELS:   # 老书单文件 + 目录形态的 成长档案.md,一份清单
            text = budget.fold_supplements(text, chapter_n)
        blocks.append(f"【{rel}】\n{text.strip()}")
    # R22 文风档案:按「注入矩阵」只给该读的那几层(大纲师只读篇级,设定师不读)。
    # 没蒸过档案 / 指针没指 → 这里是空串,零影响。
    try:
        from .stylepack import prompt_block as _style_block

        _sb = _style_block(project_root, role)
        if _sb:
            blocks.append(_sb)
    except Exception as _e:      # noqa: 档案坏了绝不该拖累出稿(同违禁词扫描的待遇)
        # 不静默:把「哪一处、什么异常」留在 stderr,出稿照走
        print(f"[文风档案] 注入跳过({type(_e).__name__}: {_e})", file=__import__('sys').stderr)
        _swallow("文风档案注入", _e, None)      # 也要进吞异常账本,否则「面板显示在用、实际零注入」查不到(审查 P1-2)
    # 裁定 6③:实体词典按 50 章一块推(块内专名一致;块换才换)。坏了不许拖累出稿。
    try:
        _eb = _entity_block(project_root, chapter_n, role)
        if _eb:
            blocks.append(f"【{_eb[0]}】\n{_eb[1]}")
    except Exception as _e:      # noqa: 同文风档案的待遇
        print(f"[实体词典] 注入跳过({type(_e).__name__}: {_e})", file=__import__('sys').stderr)
        _swallow("实体词典注入", _e, None)
    # 新架构:速记本注入(防上下文污染:只读最新片段,不塞全文)
    try:
        from . import scratchpad
        _snips = scratchpad.load_snippets(role, project_root, max_entries=3)
        if _snips:
            _sp_lines = ["\n\n## 【速记本·本角色最近结论】"]
            for _s in _snips:
                _sp_lines.append(f"\n[{_s['time']}] {_s['note'] or '工作记录'}\n{_s['body']}")
            blocks.append("".join(_sp_lines))
    except Exception:   # 速记本坏了不影响出稿
        pass
    return a, "\n\n".join(blocks)


def _gate_knowledge(project_root: Path, role: str, gate_reads, prev: str, progress) -> str:
    """审查关(质检/去AI味)的知识块:读文件 + 章末接续 + **文风档案**。

    抽成函数是为了能**行为断言**:原来这两行写在流水线深处,自检只能比对源码文本,
    等于没测(R22 的审查侧一直没接上,就是这么漏过去的 —— 交叉审查 P1-1)。
    """
    # 注意:这里有两套闸登记表 —— agents._GATES(编辑/润色师) 与 artifacts.GATES(质检/去AI味)。
    # 谁登记都可能被叫来,查不到就当「不需要上一章章末」,不许 KeyError 把流水线打断。
    from .artifacts import GATES as _GATES_SPEC

    _spec = _GATES_SPEC.get(role)
    need_prev = bool(_spec[4]) if isinstance(_spec, tuple) else bool(getattr(_spec, "wants_prev", False))
    if role in _GATES:
        need_prev = bool(_GATES[role][4])
    gk = _read_files(project_root, gate_reads, _noop)
    if need_prev and prev:
        gk += "\n\n【上一章章末(看本章有没有接住它的钩子)】\n" + prev[-800:]
    try:
        from .stylepack import prompt_block as _style_block_g

        _gsb = _style_block_g(project_root, role)
        if _gsb:
            gk += "\n\n" + _gsb
    except Exception as _ge:      # noqa: BLE001 - 档案坏了不拖累复审
        _swallow("文风档案(审查侧)", _ge, progress)
    return gk


# R27:知识条目的反向索引,这几个角色必须读得到(④)。
# 放在代码里而不是只写进 agents/*.md 的 reads:老书用的是它自己那份 agents 副本,
# 只改出厂模板的话,已经在写的书一辈子看不到知识条目。
# 6 棒改造后:吃知识索引的是细纲链前两棒(填情节要参考、校设定要对账)。
_KNOW_INDEX_ROLES = ("章节细纲师", "章节设定师")


# 「立项卡只进设定师」这条红线的**结构性**落地(C:排除名单)。
#
# 为什么需要它:`00-立项.md` 是一张**决策卡**(这本书写给谁看、放在哪、对标谁),
# 不是这个世界的世界事实。六棒里只有「章节设定师」负责立规矩,该看它;
# 其余吃 `外置大脑/设定集` 的棒(细纲师 / 分镜师 / 责任编辑)看到它,会把「对标谁」
# 当成写作素材用 —— 那是真污染。
#
# 为什么以前等于没管:红线只写在注释里,而 reads 记的是**目录**(`外置大脑/设定集`),
# 装配 prompt 时 `_iter_dir_md` 递归扫出来就把 00-立项.md 一起带上了。
# 测试做的是字符串成员判断(`"…/00-立项.md" not in reads`)—— 目录里当然没有这个字符串,
# 于是断言恒真、结构上永远查不出来(见 docs/待外部意见-三项-20261021.md §2)。
# 所以现在落成数据:这张表就是那条红线的唯一实现点。
_READS_EXCLUDE: dict[str, tuple[str, ...]] = {
    "章节细纲师": (paths.SET_CARD_REL,),
    "章节分镜师": (paths.SET_CARD_REL,),
    "责任编辑": (paths.SET_CARD_REL,),
    # 章节设定师**不列**:它正是那个该看立项卡的棒。
    # 遗留人格(大纲师/批评家/知识顾问等)暂不动 —— 它们不在六棒链上,口径见
    # docs/口径-立项卡与冰山真相-20261021.md。
}


def _knowledge_items(project_root: Path, chapter_n: int, role: str) -> tuple[Agent, list[tuple[str, str]]]:
    a = load_agent(project_root, role)
    rels = list(a.reads) + (a.reads_first_chapter if chapter_n == 1 else [])
    from .knowledge import INDEX_REL

    if role in _KNOW_INDEX_ROLES and INDEX_REL not in rels and (project_root / INDEX_REL).is_file():
        rels.append(INDEX_REL)
    return a, _read_file_items(project_root, rels, _noop, _READS_EXCLUDE.get(role, ()))


def _length_hint(role: str, step_budget: int, chapter_target: int, actual_chars: int = 0) -> str:
    """任务行里的字数指令(按角色措辞)。篇幅只靠说,绝不靠调小 max_tokens 截断——
    那会拦腰斩章 + 复发思考型空响应(见 backends._budget_tokens,2.0.1 踩过)。
    三管齐下:大纲师按章目标定场次并给每场标字数(结构闸)、写手照场预算写满即收、
    编辑/润色师有压缩授权(超了压回来)——单靠给写手一句软话,实测被多场细纲的结构压力碾过。"""
    if not step_budget:
        return ""
    if role == "章节细纲师":
        return (f"细纲本身 ≤{step_budget} 字。本章正文目标约 {chapter_target} 字——"
                f"按目标定场次:{_scene_budget(chapter_target)},并给每场标注「约X字」的篇幅预算、"
                f"总和≈{chapter_target}。场次宁少勿多,别用多场细纲把写手的篇幅撑爆。")
    if role in ("章节设定师", "章节分镜师"):
        # 交叉审查:第 2、3 棒原来也吃「细纲本身 ≤1250 字」这条绝对上限,
        # 而第 1 棒实际会产出 2157 字 —— 于是第 3 棒被同时要求"保留原文"与"≤1250",无解。
        # 真机后果:分镜师把 2157 字压成 1009 字,顺手带走 4 个整节和 8 条设定注释。
        # 改成"以上一棒实测字数为基准、只增不减":绝对上限只对第 1 棒(定结构那一棒)成立。
        if actual_chars:
            add = "设定依据" if role == "章节设定师" else "分镜注释"
            return (f"上一棒交的细纲实测 {actual_chars} 字。**在它上面增补,不重写、不精简**——"
                    f"字数只会比它多,多出来的全是你新加的{add};"
                    "场次划分、各场「约X字」篇幅预算、已有内容一律不动,不许靠压缩别处腾地方。")
        return (f"细纲本身 ≤{step_budget} 字。本章正文目标约 {chapter_target} 字——"
                f"按目标定场次:{_scene_budget(chapter_target)},并给每场标注「约X字」的篇幅预算、"
                f"总和≈{chapter_target}。场次宁少勿多,别用多场细纲把写手的篇幅撑爆。")
    if role == "写手":
        return (f"正文约 {chapter_target} 字({_artifacts.LEN_PCT}),这是发布的硬性篇幅要求:细纲各场标了字数就照它写,"
                "写满即收、宁短勿长;不为凑字加铺垫,也不因写顺了就超篇。结尾留钩。")
    if role in ("责任编辑", "润色师"):
        # 螺丝①:超标才把实测字数摆上桌给压缩授权装牙(LLM 自己数不准);达标不提,免对合格稿瞎压。
        # 达标分支的措辞与 artifacts._revise_contract 逐字对齐:真机付过代价——达标稿被下游
        # 两道无条件「压回来」连压两次,终稿 -25%。
        if actual_chars and actual_chars > chapter_target * _artifacts.LEN_AUTH:
            over = round((actual_chars / chapter_target - 1) * 100)
            return (f"原稿实测 {actual_chars} 字,目标 {chapter_target} 字(超 {over}%)——"
                    "删冗余描写、重复信息与注水铺垫,压回目标量级,不动情节骨架;绝不扩写。")
        return (f"篇幅目标约 {chapter_target} 字({_artifacts.LEN_PCT}),原稿篇幅已达标——**保持原有篇幅量级**,"
                "只改该改的地方;既不要为了精简而删情节血肉,也不要扩写。")
    return f"≤{step_budget} 字。"


def _build_user_prompt(chapter_n: int, role: str, agent: Agent, knowledge: str,
                       prev: str, workspace: list[tuple[str, str]], hardfacts: str = "",
                       target_chars: int = 0, *, chapter_target: int = 0, wants_prev: bool = False,
                       wants_hardfacts: bool = False, wants_state: bool = False,
                       book_title: str = "", state_snapshot: str = "", drama: bool = False) -> str:
    # 书名必进 prompt:没有它,模型对「这本书是什么」一无所知(第一章漂移的根因一)
    head = (f"# 你要写的是《{book_title}》第 {chapter_n} 章。" if book_title.strip()
            else f"# 你要写的是第 {chapter_n} 章。")
    parts = [head]
    if knowledge:
        parts.append("## 你要遵循的设定/方法论\n" + knowledge)
    if hardfacts and wants_hardfacts:
        parts.append("## 硬设定(逐字照搬,等级/境界名、专名、金手指代价一字不改、不许新增体系)\n"
                     + hardfacts)
    if state_snapshot and wants_state:
        parts.append("## 当前状态(状态账本摘录——物品消耗/规则数值以此为准,"
                     "不许复活已消耗物品、不许改规则数值)\n" + state_snapshot)
    if prev and wants_prev:
        parts.append("## 上一章正文(接住它的结尾钩子,别重复、别断裂)\n" + prev[-1500:])
    if workspace:
        from . import budget
        ws = budget.drop_superseded(workspace)   # 被改稿取代的旧全文稿不下传;锚点/细纲逐字保留
        ctx = "\n\n".join(f"### {label}\n{text}" for label, text in ws)
        parts.append("## 本章工作区(上游工序已产出,基于它继续)\n" + ctx)
    actual = 0
    if role in ("责任编辑", "润色师", "章节设定师", "章节分镜师") and workspace:
        actual = len(re.sub(r"\s+", "", strip_title(workspace[-1][1])))   # 上游稿实测字数,与 evals/_flag_overlong 同口径
    if drama and role == "写手":
        parts.append(
            "## 你的任务\n产出【分镜剧本】（剧作模式）。\n格式要求（中文剧本惯例）：\n"
            "1. 场景标题行：`△ 场景N - 内/外景 - 地点 - 日/夜`\n"
            "2. 人物名顶格，后跟冒号接对白：`顾明：别跟着我。`\n"
            "3. 动作指示用括号：`(他停下来，没回头)`\n"
            "4. 场景描述用叙述体，不带引号\n"
            "5. 不要小说化的心理描写/润色修辞——剧本是给拍摄/制作看的蓝图\n"
            "只输出剧本正文，不要解释你在做什么。")
    else:
        parts.append(f"## 你的任务\n产出【{agent.produces}】。"
                 f"{_length_hint(role, target_chars, chapter_target or target_chars, actual)}"
                 "只输出这一项,不要解释你在做什么。")
    return "\n\n".join(parts)


# ── 流水线任务事件(夜间补缺缺口1,§9):run_pipeline 跑章时 emit 任务事件 ─────────
# 六棒流水线投影为一个任务:开跑建/接任务发 task_prepared,每棒 done 折 plan_update 发
# plan_updated,收尾发 task_status。任何失败只吞进 _swallow——任务事件是增量展示,
# 绝不拖累出稿(与留痕/账本同一纪律)。
def _task_progress(progress, task_id):
    def bound(event):
        return progress(event)
    bound._pipeline_task_id = task_id
    return bound


def _pipeline_identity(base, chapter_n, progress):
    from . import task_state as ts
    bound = getattr(progress, "_pipeline_task_id", "")
    if bound:
        return bound
    prefix = f"t-pipe-{chapter_n:04d}"
    states = [ts.load_snapshot(base, tid) for tid in ts.task_ids(base)
              if tid == prefix or tid.startswith(prefix + "-")]
    states = [st for st in states if st is not None]
    if states:
        return max(states, key=lambda st: (st._d["created_ms"], st.task_id)).task_id
    return prefix


def _pipeline_task(project_root: Path, chapter_n: int, config: Config,
                   progress: Progress, *, finish: bool = False,
                   stop_reason: str = "", final_status: str = "completed") -> str | None:
    from . import task_state as ts
    from .paths import TASKS_DIR
    base = project_root / TASKS_DIR / "流水线"
    with ts._scope_lock(base):
        return _pipeline_task_locked(project_root, chapter_n, config, progress,
                                     finish=finish, stop_reason=stop_reason,
                                     final_status=final_status)


def _pipeline_task_locked(project_root: Path, chapter_n: int, config: Config,
                          progress: Progress, *, finish: bool = False,
                          stop_reason: str = "", final_status: str = "completed") -> str | None:
    def _scope(tid: str, rev: int) -> dict:
        return {"domain": "pipeline", "book": str(project_root), "chapter": chapter_n,
                "task_id": tid}

    try:
        from . import task_state as _ts
        from .paths import TASKS_DIR
        base = project_root / TASKS_DIR / "流水线"
        base.mkdir(parents=True, exist_ok=True)
        tid = _pipeline_identity(base, chapter_n, progress)
        st = _ts.load_snapshot(base, tid)
        if not finish and st is not None and not st.is_active():
            from uuid import uuid4
            tid = f"t-pipe-{chapter_n:04d}-{uuid4().hex}"
            st = None
        if st is None:
            goal = f"完成《{config.title}》第 {chapter_n} 章的六棒流水线工序"
            st = _ts.TaskState.create(
                {"domain": "pipeline", "book": str(project_root), "chapter": chapter_n,
                 "conversation_id": "流水线"}, goal)
            # 首次保留兼容身份；终态后的新执行分配独立身份，活动重试沿用。
            st._d["task_id"] = tid
            _ts.append_task_event(base, tid, {"kind": "start", "scope": _scope(tid, 0),
                                              "goal": goal, "initial_state": st.to_dict(),
                                              "ts": st._d["created_ms"]})
            _ts.save_snapshot(base, st)
            progress(events.task_prepared(tid, st.revision, _scope(tid, st.revision),
                                          goal=goal))
        if finish:
            if st.is_active():
                # 收尾班(补2,§5):任务 completed 后快照不允许再有 pending 步骤——
                # 未请求/未发生的工序标 skipped + 理由「未请求的工序不算漏交」,
                # 消除「4 步 completed 其余 pending 却任务 completed」的自相矛盾。
                # 6b 修复（F7）：skipped 标记改走 plan_update 事件（涨 revision、
                # 进日志——原 set_step+save 裸写，恢复重建丢 skipped 理由）。
                #
                # C07 修复（2026-11-12 用户裁定）：收尾有两种性质，不能共用一套措辞。
                # completed = 流水线真跑完（未跑的是「未请求」）；
                # cancelled = 作者叫停（未跑的是「叫停导致」，不是没请求）。
                # 直接复用 finish=True 会硬写成 completed，所以由 final_status 决定。
                _cancelled = (final_status or "completed") == "cancelled"
                _skip_reason = ("作者叫停，未跑的工序" if _cancelled
                                else "未请求的工序不算漏交")
                _plan_reason = ("收尾:作者叫停,未跑工序标 skipped" if _cancelled
                                else "收尾:未请求工序标 skipped")
                _outcome = (f"第 {chapter_n} 章流水线被作者取消（{stop_reason or '作者取消'}），"
                            f"未跑工序已标 skipped"
                            if _cancelled else f"第 {chapter_n} 章流水线跑完")
                _steps_now = []
                for s in st.plan_steps():
                    if s.get("status") in ("pending", "running", "waiting_author", "blocked"):
                        _steps_now.append({**s, "status": "skipped",
                                           "skipped_reason": _skip_reason})
                    else:
                        _steps_now.append(dict(s))
                if _steps_now:
                    _ts.commit_retry(base, st, {"kind": "plan_update",
                                                "reason": _plan_reason,
                                                "steps": _steps_now})
                    st = _ts.load_snapshot(base, tid) or st
                _ts.commit_retry(base, st, {"kind": "outcome", "summary": _outcome})
                _ts.commit_retry(base, st, {"kind": "status", "status": final_status,
                                            "stop_reason": stop_reason})
                # 6b 修正：commit_retry 追盘重放后**重读盘上状态**再发事件
                # ——原用内存 st（旧 status/revision），前端收到 completed 前的旧值。
                st = _ts.load_snapshot(base, tid) or st
            progress(events.task_status(tid, st.revision, _scope(tid, st.revision),
                                        status=st.status, stop_reason=stop_reason))
        return tid
    except Exception as e:
        _swallow("流水线任务事件", e, progress)


def _pipeline_running(project_root: Path, chapter_n: int, config: Config,
                      progress: Progress) -> None:
    """C07 修复（2026-11-12 用户裁定）：第一棒真正开跑时把任务从 ready 推进到 running。

    原先只在建立时写 ready、收尾时写 completed，中间没有任何状态提交，于是
    「正在跑」与「还没开始」在产品上不可区分（取消之后也停在 ready）。
    这里只补一次；终态/未知任务不碰（不能让这条把已结束的任务"复活"），失败只吞。
    """
    try:
        from . import task_state as _ts
        from .paths import TASKS_DIR
        base = project_root / TASKS_DIR / "流水线"
        with _ts._scope_lock(base):
            tid = _pipeline_identity(base, chapter_n, progress)
            st = _ts.load_snapshot(base, tid)
            if st is None or not st.is_active() or st.status == "running":
                return
            _ts.commit_retry(base, st, {"kind": "status", "status": "running"})
            st = _ts.load_snapshot(base, tid) or st
        progress(events.task_status(
            tid, st.revision,
            {"domain": "pipeline", "book": str(project_root), "chapter": chapter_n,
             "task_id": tid},
            status=st.status))
    except Exception as e:
        _swallow("流水线任务运行态", e, progress)


def _pipeline_step_done(project_root: Path, chapter_n: int, role: str, produces: str,
                        progress: Progress) -> None:
    """每棒 done:计划投影一步 completed + plan_updated(§9)。失败只吞。"""
    try:
        from . import task_state as _ts
        from .paths import TASKS_DIR
        base = project_root / TASKS_DIR / "流水线"
        tid = _pipeline_identity(base, chapter_n, progress)
        st = _ts.load_snapshot(base, tid)
        if st is None or not st.is_active():
            return
        steps = st.plan_steps()
        sid = next((s["id"] for s in steps if s.get("title") == role), None)
        if sid is None:
            # 计划还没建:首次跑到这里用 STEPS 投影建一份再标完成
            titles = [s.role for s in STEPS]
            steps_in = [{"id": f"s{i+1}", "title": t,
                         "status": "done" if t in (role,) else "pending"}
                        for i, t in enumerate(titles)]
            _ts.commit(base, st, {"kind": "plan_update", "reason": "按固定工序投影",
                                  "steps": steps_in}, expected_revision=st.revision)
            st = _ts.load_snapshot(base, tid) or st
            sid = next((s["id"] for s in st.plan_steps() if s.get("title") == role), None)
        if sid:
            # 6b 修复（F7）：步骤完成改走 plan_update 事件（涨 revision、进日志、
            # result_ref 随事件走——原 set_step+save_snapshot 裸写，恢复重建丢进度）。
            _steps_next = []
            for s in st.plan_steps():
                if s["id"] == sid:
                    _steps_next.append({**s, "status": "completed",
                                        "result_ref": f"{produces}已入工作区"})
                else:
                    _steps_next.append(dict(s))
            _ts.commit_retry(base, st, {"kind": "plan_update",
                                        "reason": f"「{role}」完成",
                                        "steps": _steps_next})
            st = _ts.load_snapshot(base, tid) or st
        scope = {"domain": "pipeline", "book": str(project_root), "chapter": chapter_n,
                 "task_id": tid}
        progress(events.plan_updated(tid, st.revision, scope,
                                     summary=f"「{role}」完成"))
    except Exception as e:
        _swallow("流水线计划更新", e, progress)


def _agentic_step_done(project_root: Path, chapter_n: int, name: str,
                       progress: Progress) -> None:
    """夜间补缺最终班(§2a):agentic 循环内每提交一件产物,计划投影一步 completed +
    plan_updated(与 _pipeline_step_done 同构;产物名即步骤名,STEPS 无该产物时先按
    ARTIFACTS 投影建计划再标完成)。零额外模型调用,失败只吞——不拖累出稿。"""
    try:
        from . import artifacts as _art, task_state as _ts
        from .paths import TASKS_DIR
        base = project_root / TASKS_DIR / "流水线"
        tid = _pipeline_identity(base, chapter_n, progress)
        st = _ts.load_snapshot(base, tid)
        if st is None or not st.is_active():
            return
        steps = st.plan_steps()
        sid = next((s["id"] for s in steps if s.get("title") == name), None)
        if sid is None:
            # 计划还没建:按产物清单投影建一份(标题=产物名,当前这件标 completed)
            titles = [a.name for a in _art.ARTIFACTS]
            steps_in = [{"id": f"s{i+1}", "title": t,
                         "status": "completed" if t == name else "pending"}
                        for i, t in enumerate(titles)]
            _ts.commit(base, st, {"kind": "plan_update", "reason": "agentic 产物清单投影",
                                  "steps": steps_in}, expected_revision=st.revision)
            st = _ts.load_snapshot(base, tid) or st
            sid = next((s["id"] for s in st.plan_steps() if s.get("title") == name), None)
        if sid:
            # 6b 修复（F7）：同 _pipeline_step_done——plan_update 事件折叠
            # （涨 revision、进日志、result_ref 随事件走）。
            _steps_next = []
            for s in st.plan_steps():
                if s["id"] == sid:
                    _steps_next.append({**s, "status": "completed",
                                        "result_ref": f"{name}已入工作区"})
                else:
                    _steps_next.append(dict(s))
            _ts.commit_retry(base, st, {"kind": "plan_update",
                                        "reason": f"「{name}」已提交",
                                        "steps": _steps_next})
            st = _ts.load_snapshot(base, tid) or st
        scope = {"domain": "pipeline", "book": str(project_root), "chapter": chapter_n,
                 "task_id": tid}
        progress(events.plan_updated(tid, st.revision, scope, summary=f"「{name}」已提交"))
    except Exception as e:
        _swallow("agentic 计划更新", e, progress)


def _record_outline_trail(project_root: Path, chapter_n: int, produces: str, text: str,
                          system_prompt: str, workspace: list, prev: str, cfg_bits: dict) -> None:
    """把大纲师刚产出的细纲记进轨迹(供 evolve 拿它当「AI 交的那份」)。

    签名口径与 `write_tools.artifact_sig` 一致(都走 `resume.sig_v2`),两条路记出来的
    轨迹形状相同——`evolve.collect` 不必关心这一章是哪条路写的。

    任何异常都吞掉:轨迹是学习用的便利,绝不该让一章写不出来(同 `trail.record_commit` 自身的纪律)。
    """
    try:
        from . import resume as resume_mod, trail
        _a, items = _knowledge_items(project_root, chapter_n, "章节细纲师")
        sig = resume_mod.sig_v2(system_prompt, items, list(workspace), prev, cfg_bits)
        trail.record_commit(project_root, chapter_n, produces, text, sig)
    except Exception as e:
        _swallow("细纲轨迹记录", e)


def run_pipeline(
    project_root: Path,
    chapter_n: int,
    backend: Backend,
    config: Config,
    progress: Progress = _noop,
    *,
    slow: float = 0.0,
    resume: bool = True,
    critic_backend: Backend | None = None,
    backend_for: Callable[[str], Backend] | None = None,
    stop_after: str | None = None,
    from_step: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[Path, str]:
    """跑一章。返回 (终稿路径, 终稿文本)。进度通过 progress 回调发出。

    resume=True 时跳过 ledger 里"已完成且上游未变"的工序(断点续跑,省 DeepSeek 计费);
    上游(reads 文件 / 已累积产物 / 上一章正文)任一变化,从该工序起重算。

    critic_backend 给了就让质检/去AI味的**复审员**走它(通常是便宜模型);写作/回炉仍用 backend。

    backend_for 给了就按**工序**取后端(裁定 1A:写手与编辑/润色从此可以分开配)。
    没给就全程用 backend —— 老行为一字不差。

    stop_after 给了(或 loom.toml 的「停在哪一站」有值)就跑到那一站即停,不再往下走:
    大纲师=只到细纲(剩下的作者自己来)、写手=只到初稿(直接看正文)、编辑=跳过润色。
    空=跑满五步出终稿(默认,行为与老版一字不差)。agent 循环不吃这个开关——它以「产物」为单位,
    没有固定的「棒」可停。

    should_stop 是「作者叫停」(语义见 cancel.py):**每两棒之间**问一次,true 就停在当前棒之后。
    当前这一棒的请求不会被打断(那要 cancel token 贯穿 backend,改动面大),代价是:点了停,
    手上这一棒仍会写完。已跑完那一棒的稿子落 `正文/.半稿/第N章.txt`,**正式正文一个字节不动**,
    也不发 chapter_done(前端就不会显示「这章写完了」)。
    """
    from . import ledger, resume as resume_mod

    # 第一章地基硬门（2026-11-10）：**分流之前**——agentic 循环忽略 from_step/stop_after，
    # 不能按「传入的是审查棒」豁免；普通与 agentic 都在这里被同一只读判据拦住。
    # T1（2026-11-11）：编辑类单棒 + 正文已落盘非空 = 审稿，放行（无现稿仍拦）。
    if chapter_n == 1:
        from .navigator import foundation_block
        _fb = foundation_block(project_root, chapter_n)
        if _fb and not review_pass_ok(project_root, chapter_n, from_step or ""):
            raise LoomBackendError(_fb["error"], code=_fb["code"])

    # T1 P1 修复（2026-11-11，审查 审查记录）：agentic 循环忽略
    # from_step/stop_after，会把「编辑类单棒」当成整章写作（独立复现：首个后端调用
    # 是整章写作 agent，非责任编辑）。编辑单棒 + 现稿必须走下面普通流水线的显式
    # 起跑路径；其余 agentic 行为一字不动。
    if getattr(config, "agentic", False) and not review_start_ok(
            project_root, chapter_n, from_step or ""):   # 【实验·灰度】走 agent 循环;默认关
        return _run_agentic(project_root, chapter_n, backend, config, progress, critic_backend,
                            backend_for=backend_for)

    # 停在哪一站:显式参数优先,其次读项目配置;两者都空 = 跑满五步(老行为)。
    stop_after = (stop_after or getattr(config, "stop_after", "") or "").strip()
    # 写错站名原来会**静默跑满五步**(等于设置被无视,还多花钱)—— 同函数的 from_step 是 raise 的,
    # 这里对齐:认不出就报错并列出合法值(交叉审查 P1-7)。
    if stop_after:
        _legal = [s.role for s in STEPS]
        if stop_after not in _legal:
            raise LoomBackendError(
                f"「停在哪一站」写的是「{stop_after}」,没有这一站。合法的写法:{' / '.join(_legal)}。"
                f"(留空 = 跑满五步出终稿)", code="bad_stop_after")

    # 剧作模式哨兵（④档）——跑到写手出剧本体后跳过责任编辑/润色师。
    # stop_after 机制表达不了"跑完某棒但跳过后两棒"，所以剧作用哨兵+棒级过滤实现。
    _skip_roles: set[str] = set()
    if stop_after == "__drama__":
        stop_after = ""   # 跑到写手（不下探责编/润色）
        _skip_roles = {"责任编辑", "润色师"}

    progress(events.pipeline_start(chapter_n, PIPELINE))
    task_id = _pipeline_task(project_root, chapter_n, config, progress)
    progress = _task_progress(progress, task_id)
    prev = _prev_chapter(project_root, chapter_n)
    hardfacts = _hardfacts_for(project_root, progress)  # 硬设定逐字块,进大纲师/写手 prompt
    # 状态快照:不进续跑签名。账本不在任何角色 reads 里,手改/补账不会触发重跑
    # (与 hardfacts 不同——那个的源文件在设定师 reads 里会自愈)。补账后想让半截章吃到
    # 新账,用 --force/重写。
    from . import statebook
    state_snap = statebook.snapshot_for(project_root, chapter_n - 1)

    # 注:hardfacts 故意不进续跑签名。它只取自 世界观.md / 人物卡.md,而这两份都在设定师
    # (永远是 PIPELINE[0])的 reads 里——改硬设定必先让设定师签名失配、从下标 0 全量重跑,
    # 大纲师/写手自然吃到新硬设定。再折进签名只会白白冲掉在跑章节的 ledger、触发无谓重计费。
    # 唯一缺口:纯代码改了 _HARDFACT_KW/切片逻辑而世界观文件没动时,半截章续跑会沿用旧切法的
    # 写手稿——属升级期一次性、自愈(--force/--no-resume 或动一下世界观即重算),不值得为它毁 ledger。
    # title 进签名:改书名 → 设定师起全量重跑,绝不拿旧书名的半截稿续跑
    _cfg_bits = {"chapter_chars": config.chapter_chars, "gate_rounds": config.gate_rounds,
                 "title": config.title}

    def _sig(role: str, ws: list[tuple[str, str]]) -> str:
        a, items = _knowledge_items(project_root, chapter_n, role)
        # 口径（2026-11-10 用户裁定）：签名只代表**作者内容**。运行时注入（记忆检索命中、
        # 降级提示）只进 prompt，不进签名 —— 否则检索结果一变，作者没改任何东西也会被
        # 判「上游变了」而白重跑本棒及下游（真花钱）。注入项按标签剔除；重算侧
        # （resume_point 用账本产出拼 workspace）本来就没有这些项，两边同口径。
        author_ws = [(k, v) for k, v in ws if k not in _RUNTIME_WS_LABELS]
        return resume_mod.sig_v2(a.system_prompt, items, author_ws, prev, _cfg_bits)

    def _upstream_v1(role: str, ws: list[tuple[str, str]]) -> str:
        _, knowledge = _knowledge_for(project_root, chapter_n, role)
        return resume_mod.sig_v1(knowledge, ws, prev)

    def _stale_local(role: str, ledger_output: str) -> bool:
        """作者手改过本地可编辑产物(大纲师的 WYSIWYG 细纲)→ 这一棒不能跳。

        两边都 strip 再比:账本存的是模型原文(record_step 未 strip),文件存的是
        output.strip()+"\\n"——不 strip 会因尾部空白每次都误判「改过」,白重跑写手及下游。
        """
        spec = _SPEC_BY_ROLE.get(role)
        if spec is None or not spec.outline_wysiwyg:
            return False
        p = _outline_path(project_root, chapter_n)
        if not p.is_file():
            return False
        disk = p.read_text(encoding="utf-8").strip()
        return bool(disk) and disk != (ledger_output or "").strip()

    # R24「从第 N 步起重算」:先把这一步及之后的完成记录作废,续跑就会自然从那里重来;
    # 前面的工序照旧跳过(不重花钱),上游工作区由 resume_point 按账本前缀重建。
    if from_step:
        _fs = (from_step or "").strip()
        if _fs not in PIPELINE:
            raise ValueError(f"不认识的一步:{_fs}(只认 {' / '.join(PIPELINE)})")
        _dropped = ledger.invalidate_from(project_root, chapter_n, _fs)
        progress(events.info(f"按你的要求从「{_fs}」重新开始"
                             + (f"(作废 {len(_dropped)} 步的完成记录)" if _dropped else "")))

    # T1（2026-11-11）：编辑类单棒显式从该棒起跑、以盘上现稿为上游工作区。
    # 原实现 from_step 只作废记录，实际起跑位置由续跑账本决定 —— 点「责任编辑」
    # 仍可能从细纲师起跑，/api/chapter/step 更是 resume=False 从 0 起跑，等于二次生成。
    _review_start = (from_step or "").strip()
    if _review_start in ("责任编辑", "润色师"):
        _body_path = paths.chapter_path(project_root, chapter_n)
        try:
            _body_text = (_body_path.read_text(encoding="utf-8").strip()
                          if _body_path.is_file() else "")
        except OSError:
            _body_text = ""
        if _body_text:
            _review_agent, _ = _knowledge_prompt(project_root, chapter_n, _review_start)
            start_idx = PIPELINE.index(_review_start)
            workspace = [(_review_agent.produces, _body_text)]
            progress(events.info(
                f"从「{_review_start}」起跑：以盘上现稿为上游，不再重跑前面的棒。"))
        else:
            _review_start = ""
    else:
        _review_start = ""

    if _review_start:
        pass   # 显式起跑：前面棒不跑，workspace 已用现稿就位
    elif resume:
        start_idx, workspace = resume_mod.resume_point(project_root, chapter_n, _sig, _upstream_v1,
                                                       stale_local=_stale_local)
        for role in PIPELINE[:start_idx]:
            progress(events.agent_skip(role, "已完成且上游未变"))
        if start_idx < len(PIPELINE) and _SPEC_BY_ROLE[PIPELINE[start_idx]].outline_wysiwyg:
            # 花钱可解释:让作者知道跳过失效是因为细纲被改了,不是签名失配
            progress(events.info(f"第 {chapter_n} 章细纲已改动,从大纲师起重算"))
    else:
        start_idx, workspace = 0, []

    last_role, last_text = "", ""
    _running_marked = False   # C07：ready → running 只在第一棒真正开跑时提交一次
    for spec in STEPS[start_idx:]:
        role = spec.role
        # 剧作模式:责任编辑/润色师下线——跳棒不落半稿（剧作的终态就是
        # 写手的剧本体，跳过是设计而非中断）。
        if role in _skip_roles:
            progress(events.info(f"剧作模式：跳过「{role}」。"))
            continue
        # 取消检查点:每棒之前问一次——上一棒已经完整落盘,此刻停下不会留下半截产物
        if should_stop is not None and should_stop():
            half = _half_draft(project_root, chapter_n, last_role, last_text)
            progress(events.cancelled(chapter_n, last_role or "起笔前", half))
            if half is not None:
                progress(events.info(f"已按你的要求停下:第 {chapter_n} 章的半稿在「正文/.半稿」,正式正文没动。"))
            else:
                progress(events.info("已按你的要求停下:后面的工序没跑。"))
            # C07 修复（2026-11-12 用户裁定①）：取消也必须把任务收束到终态。
            # 原实现这里直接 return，任务是建任务时写的 ready，此后再无状态提交 ⇒
            # 「取消过」与「根本没开工」在任务快照上完全一样，后续按任务状态判断的地方全被误导。
            # 以 final_status="cancelled" 收尾（不能复用默认的 completed），未跑工序标 skipped。
            _pipeline_task(project_root, chapter_n, config, progress, finish=True,
                           stop_reason="作者取消", final_status="cancelled")
            _p = _outline_path(project_root, chapter_n)
            p = _p if (_p and Path(_p).is_file()) else (project_root / paths.CARD_REL)
            return Path(p), last_text
        agent, knowledge = _knowledge_prompt(project_root, chapter_n, role)
        step_backend = backend_for(role) if backend_for is not None else backend
        if not _running_marked:
            _running_marked = True
            _pipeline_running(project_root, chapter_n, config, progress)
        progress(events.agent_start(role))
        up_sha = _sig(role, workspace)  # 记录入此工序时的上游签名 v2(供下次续跑比对)

        max_chars = (spec.budget_fn(config.chapter_chars) if spec.budget_fn
                     else (spec.short_budget or config.chapter_chars))
        # 正文棒给输出预算地板(见 _PROSE_OUTPUT_FLOOR 的注释)。
        # 只影响 max_tokens,不影响篇幅指令——`_length_hint` 拿到的是上面的 max_chars。
        _call_chars = max(max_chars, _PROSE_OUTPUT_FLOOR) if role in _PROSE_ROLES else max_chars
        outline_path = _outline_path(project_root, chapter_n) if spec.outline_step else None
        # from_step == 这一步:盘上已有细纲**不再当真相**。不然「从细纲师重跑」会被旧细纲旁路,
        # 一句话都不问模型就跳过(交叉审查 P1)。
        _regen_this = bool(from_step) and role == from_step and spec.outline_wysiwyg
        if (outline_path and spec.outline_wysiwyg and not _regen_this and outline_path.is_file()
                and outline_path.read_text(encoding="utf-8").strip()):
            # 已有细纲(多半你手改过)→ 直接用它,细纲三棒全不跑;改它/清空它即重新生成(WYSIWYG)。
            output = outline_path.read_text(encoding="utf-8").strip()
            progress(events.agent_chunk(role, output))
            progress(events.info(f"第 {chapter_n} 章沿用你的细纲(在「本章细纲」里改它 / 重新生成)"))
            _check_scene_budget(project_root, chapter_n, output, config.chapter_chars, True, progress)
        else:
            # 批B 三通道联动检索：写手棒组装 prompt 前，用工作区文本
            # 检索记忆库相关事实——百万字上下文的实时检索注入（比全量塞便宜三个量级）。
            # 只对写手做：上游棒（细纲/设定/分镜）不需要前情召回。
            if role == "写手":
                try:
                    from . import memory as _mem
                    _ws_text = "\n".join(txt for _, txt in workspace)
                    _linked = _mem.recall(project_root, _ws_text, scope="book", k=3)
                    _hits = _linked.get("条", []) if _linked.get("ok") else []
                    if _hits:
                        _linked_lines = [
                            f"[{h.get('book','').split(chr(92))[-1] if h.get('book') else '全局'}]  {h.get('digest','')[:80]}" for h in _hits
                        ]
                        workspace.append(("相关记忆（此前章节沉淀·自动检索）",
                            "\n".join(_linked_lines)))
                    # 用户裁定 2026-11-12（静默失败缺陷登记 第1条）：检索不可用**不能**
                    # 长得像「本来就没有相关记忆」。memory.recall 已经造好 notice/reason_code，
                    # 这里必须把它送到作者看得见的进度流；库正常零命中时 notice 为空 → 不报故障。
                    # 走 _swallow：它自己已保证「报这一笔」不许把主流程带崩。
                    _notice = str(_linked.get("notice") or "").strip()
                    if not _linked.get("ok") and _notice:
                        _swallow(f"第 {chapter_n} 章写手前记忆检索（"
                                 f"{_linked.get('reason_code') or 'retrieval_unavailable'}）",
                                 RuntimeError(_notice), progress)
                except Exception as e:
                    # 原为 except: pass。检索坏了仍然不许挡写手，但必须留痕——
                    # 否则「没召回」到底是「没有」还是「坏了」永远分不出来。
                    _swallow(f"第 {chapter_n} 章写手前记忆检索", e, progress)
            user_prompt = _build_user_prompt(chapter_n, role, agent, knowledge, prev, workspace, hardfacts,
                                             target_chars=max_chars, chapter_target=config.chapter_chars,
                                             wants_prev=spec.wants_prev, wants_hardfacts=spec.wants_hardfacts,
                                             wants_state=spec.wants_state,
                                             book_title=config.title, state_snapshot=state_snap)
            # 哨兵棒(编辑)的输出含哨兵+留痕,流式时只放哨兵前的干净改稿;其余棒原样透传。
            chunk_cb = (_edit_stream_filter(progress) if spec.edit_sentinel
                        else (lambda d, r=role: progress(events.agent_chunk(r, d))))
            _t0 = time.monotonic()
            _cin = len(agent.system_prompt or "") + len(user_prompt or "")
            try:
                output = step_backend.complete(agent.system_prompt, user_prompt,
                                               max_chars=_call_chars, on_chunk=chunk_cb)
            except Exception as _e:      # noqa: BLE001 - 记一笔失败原因后原样抛出(不改变控制流)
                _log_call(project_root, chapter_n, role, ok=False,
                          ms=int((time.monotonic() - _t0) * 1000), chars_in=_cin,
                          backend=step_backend, error=f"{type(_e).__name__}: {_e}", progress=progress)
                raise
            _log_call(project_root, chapter_n, role, ok=True,
                      ms=int((time.monotonic() - _t0) * 1000), chars_in=_cin,
                      chars_out=len(output or ""), backend=step_backend, progress=progress)
            # 细纲链结构兜底:上一棒有的**尾部小节**被这一棒整节丢了 → 逐字补回。
            # 为什么在这里而不是提示词里:提示词已经逐条列了 9 个标题,模型照样丢 `## 写作提示`
            # (它觉得那节跟自己的活无关)。尾部位置天然确定,补回不会打乱顺序,交给代码做。
            if outline_path and role in ("章节设定师", "章节分镜师") and workspace:
                _prev_text = workspace[-1][1]
                _fixed_text, _restored, _unfixed = _restore_missing_sections(_prev_text, output)
                if _restored:
                    progress(events.warn(
                        f"「{role}」把上一棒结尾的小节整节丢了,已逐字补回:{'、'.join(_restored)}"))
                    output = _fixed_text
                if _unfixed:
                    # 中间小节(尤其**整场**)丢了:位置推断不可靠,所以不搬,先追问一次让它自己补。
                    # 真机证据(整章实跑):设定师把 `### 场景 3` 和 `## 关键细节` 整节删了,
                    # 1910 字压成 1074 字,终稿只剩两场、**章末钩子那场没了**。提示词里已写"只增不减",
                    # 模型照删 ⇒ 只能机器兜。追问一次(不额外花钱级别:同一个本地后端),仍缺就告警放行,不中止。
                    progress(events.warn(
                        f"「{role}」丢了 {len(_unfixed)} 个小节,带着清单让它重来一次:"
                        f"{'、'.join(_unfixed)}"))
                    _retry_user = (user_prompt
                                   + "\n\n## 上一次交付不合格:漏了上一棒已有小节\n"
                                     "你上一次交的细纲里**少了**下面这些小节(标题原样):\n"
                                   + "".join(f"- {h}\n" for h in _unfixed)
                                   + "\n要求:\n"
                                     "1. 这一棒是**在上一棒的细纲上增补**,不是重写、也不是精简。\n"
                                     "2. 上面这些小节的标题与内容必须**逐字保留**;场景小节要把该场走向写完。\n"
                                     "3. 这次把**完整一份细纲**重新输出:上一棒有什么小节,你就要有什么小节,\n"
                                     "   只允许在原有内容上增补(设定注释/镜头注释),一个字都不许删。\n"
                                     "4. 不要解释,直接给细纲全文。")
                    _t2 = time.monotonic()
                    _cin2 = len(agent.system_prompt or "") + len(_retry_user)
                    try:
                        _retry = step_backend.complete(agent.system_prompt, _retry_user,
                                                       max_chars=_call_chars,
                                                       on_chunk=_edit_stream_filter(progress))
                    except Exception as _e:      # noqa: BLE001
                        # 真机:这一步原来是 `raise`,追问一失败就把**整条流水线带崩**
                        # (作者看到的是"第 2 章停在「章节设定师」之后",账本里连错误行都没有)。
                        # 追问只是"锦上添花",失败就该退回原来的那一版继续跑,绝不中止。
                        _log_call(project_root, chapter_n, role, ok=False,
                                  ms=int((time.monotonic() - _t2) * 1000), chars_in=_cin2,
                                  backend=step_backend, error=f"{type(_e).__name__}: {_e}",
                                  progress=progress)
                        progress(events.warn(
                            f"「{role}」补节那次追问失败了({type(_e).__name__}: {_e})。"
                            f"已退回它这一版原稿继续跑,这一章照出;缺的节:{'、'.join(_unfixed)}"))
                        _retry = ""
                    _log_call(project_root, chapter_n, role, ok=True,
                              ms=int((time.monotonic() - _t2) * 1000), chars_in=_cin2,
                              chars_out=len(_retry or ""), backend=step_backend, progress=progress)
                    _r_text, _r_restored, _r_unfixed = _restore_missing_sections(_prev_text, _retry or "")
                    # 只接受"缺得更少 且 没变短"的那一版;否则保留原样(宁可少,不可又压缩一次)
                    if _should_take_section_retry(_unfixed, _r_unfixed, len(output), len(_retry or "")):
                        output = _r_text
                        _unfixed = _r_unfixed
                        if _r_restored:
                            progress(events.warn(
                                f"补回结尾小节:{'、'.join(_r_restored)}"))
                    if _unfixed:
                        progress(events.warn(
                            f"「{role}」追问一次后仍缺:{'、'.join(_unfixed)}"
                            "——这一章按现有内容继续,作者写完看一眼这几处。"))
            if outline_path:  # 细纲三棒:每棒产出覆盖写盘(第2/3棒基于上一棒改写)
                atomic_write_text(outline_path, output.strip() + "\n")
                _check_scene_budget(project_root, chapter_n, output, config.chapter_chars, False, progress)
                # 轨迹只记细纲链【最后一棒】:那才是 AI 最终交出去的那份。作者之后改了盘上那份,
                # 两者的差异就是自进化的证据(evolve.collect)。
                # 【只记这一支】——上面 WYSIWYG 那支沿用的是作者自己的细纲,记了会把判据
                # 弄反:拿他自己的稿当「AI 交的」,他后续的改动会被误算成「相对 AI 稿的改动」。
                if role == "章节分镜师":
                    _record_outline_trail(project_root, chapter_n, agent.produces, output.strip(),
                                          agent.system_prompt, workspace, prev, _cfg_bits)

            # 交付契约机检:该交留痕的棒没交留痕 = 没交改稿。
            # 旧的处置是"没有标记"就当成全文正文收下(split_edit_note 返回 (text,"")),
            # 于是一篇 2424 字的自述分析被当成改稿、一路传到终稿 395 字
            # (docs/流水线诊断-v2-审查.md §5.5)。现在的处置:先给它一次自纠机会;
            # 仍不合格就**沿用上一棒原稿**(绝不把"非空但无效"的产出顶替上游);
            # 为什么不中止:本地模型可能反复给同一种坏输出,那样这一章永远出不来。
            if spec.edit_sentinel and _missing_edit_note(output):
                progress(events.warn(f"「{role}」这次没交出改稿(缺留痕标记),让它重来一次。"))
                _retry_user = (user_prompt
                               + "\n\n## 上一次交付不合格,重来\n"
                                 "你上一次的输出里没有「改好的整章正文」,也没有 <LOOM:EDIT-NOTE> "
                                 "留痕块,只有检查过程。\n"
                                 "这一次只按下面这个形状交,别的什么都不要写:\n"
                                 "① 先完整写出改好的整章正文;\n"
                                 "② 另起一行,用 <LOOM:EDIT-NOTE> 与 </LOOM:EDIT-NOTE> 围住 3-6 条留痕。\n"
                                 "不许写「让我先分析」「用户要求我」这类过程文字。")
                _t1 = time.monotonic()
                _cin1 = len(agent.system_prompt or "") + len(_retry_user)
                try:
                    _retry = step_backend.complete(agent.system_prompt, _retry_user,
                                                   max_chars=_call_chars,
                                                   on_chunk=_edit_stream_filter(progress))
                except Exception as _e:      # noqa: BLE001 - 记原因后原样抛,不改控制流
                    _log_call(project_root, chapter_n, role, ok=False,
                              ms=int((time.monotonic() - _t1) * 1000), chars_in=_cin1,
                              backend=step_backend, error=f"{type(_e).__name__}: {_e}",
                              progress=progress)
                    raise
                _log_call(project_root, chapter_n, role, ok=True,
                          ms=int((time.monotonic() - _t1) * 1000), chars_in=_cin1,
                          chars_out=len(_retry or ""), backend=step_backend, progress=progress)
                if _missing_edit_note(_retry):
                    # 两次都没有留痕块 = 这一棒没交改稿(真机形态:整篇自述分析)。
                    # 处置:**沿用上一棒原稿**——既不让分析顶替正文,也不让整章卡住。
                    # 已经写完的那份稿子才是保底资产;事由写进 .审稿留痕,作者看得见。
                    # 为什么不直接报错中止:本地模型可能反复给同一种坏输出,那样这一章永远出不来。
                    _upstream = workspace[-1][1] if workspace else ""
                    progress(events.warn(
                        f"「{role}」连续两次都没交出改稿,这一棒跳过:改用上一棒原稿继续。"))
                    _save_edit_note(
                        project_root, chapter_n,
                        f"## 「{role}」这一棒没有交付\n\n"
                        f"它连续两次的输出里都没有「改动留痕」块,形态是自述分析而不是改稿。\n\n"
                        f"处置:本章**沿用上一棒原稿**,没有让分析文本进入正文。\n\n"
                        f"建议:在「{role}」那一栏点「重新生成这一棒」。", progress)
                    output = _upstream or output
                else:
                    output = _retry
        if spec.edit_sentinel:
            output, note = _split_edit_note(output)  # 留痕切出,只把干净正文交给下游润色师
            if note:
                _save_edit_note(project_root, chapter_n, note, progress)

        # 质检/去AI味 关卡:独立复审→回炉(挑硬伤、不打分、不硬阻断)。残留写进留痕,继续往下。
        if role in _GATES and config.gate_rounds > 0:
            label, critic, revise, gate_reads, need_prev = _GATES[role]
            gk = _gate_knowledge(project_root, role, gate_reads, prev, progress)
            gres = gates.run_gate(
                backend, label=label, owner_role=role, critic_system=critic, revise_system=revise,
                draft=output, knowledge=gk, produces=agent.produces,
                rounds=config.gate_rounds, max_chars=max_chars, progress=progress,
                critic_backend=critic_backend,
                detector=_deslop_detector(project_root, chapter_n) if spec.deslop else None,
            )
            output = gres.text
            if gres.remaining:
                _save_gate_remaining(project_root, chapter_n, label, gres.remaining, progress)

        # 伏笔悬空提醒:编辑棒后扫卡章纲,埋了很久仍没还的伏笔进留痕(纯提示,独立于上面的 gate、绝不回炉)
        if spec.foreshadow_after:
            _flag_stale_foreshadow(project_root, chapter_n, config, progress)

        # 每棒非空闸:任一棒返回空/拒答都不该静默落盘、再被下游与 learn 二次污染——直接报错刹住,
        # 已完成的工序已进 ledger,修好(换模型)后续跑只重算这一棒,不浪费前面的字数计费。
        reasons = validate_output(output, STEP)
        if reasons:
            raise LoomBackendError(render("model_output_invalid", detail=f"{role}:" + "；".join(reasons)),
                                   code="model_output_invalid")
        ledger.record_step(project_root, chapter_n, role, output, up_sha)  # 即时落盘=断点可续
        workspace.append((agent.produces, output))
        last_role, last_text = role, output   # 取消时要能说清「停在谁之后」
        progress(events.agent_done(role, agent.produces))
        _pipeline_step_done(project_root, chapter_n, role, agent.produces, progress)  # 夜间补缺:plan_updated
        if stop_after and role == stop_after:
            # 作者按自己的设置要「只到这里」。该落的盘都已经落了(细纲在上面的 WYSIWYG/首生成支
            # 就写过盘),但**绝不假装整章完成**:停在骨架就只回报骨架,不写空正文、不发 chapter_done
            # ——否则前端会显示「这章写完了」,而它其实只是脊柱。
            _pipeline_task(project_root, chapter_n, config, progress, finish=True,
                           stop_reason=f"按设置停在第「{role}」站")
            last_text = workspace[-1][1] if workspace else ""
            if role in _PROSE_ROLES:
                progress(events.info(f"按设置停在第「{role}」这一站:编辑/润色的活儿留给你自己来。"))
                return _finish_chapter(project_root, chapter_n, last_text, backend, config,
                                       progress, critic_backend=critic_backend, hardfacts=hardfacts)
            from . import paths as _paths
            p = (outline_path if (outline_path and Path(outline_path).is_file())
                 else (project_root / _paths.CARD_REL))
            progress(events.info(f"按设置停在第「{role}」这一站:后面的工序没跑,剩下的你自己来。"))
            return Path(p), last_text
        if slow:
            time.sleep(slow)

    _pipeline_task(project_root, chapter_n, config, progress, finish=True)  # 夜间补缺:任务收尾
    return _finish_chapter(project_root, chapter_n, workspace[-1][1], backend, config,
                           progress, critic_backend=critic_backend, hardfacts=hardfacts)


def _log_call(project_root: Path, chapter_n: int, role: str, *, ok: bool, ms: int,
              chars_in: int = 0, chars_out: int = 0, backend=None, error: str = "",
              progress: Callable[[dict], None] | None = None) -> None:
    """把这一棒的调用记进运行账本(R12)。**记账失败绝不影响出稿**——归因后照旧往下走。

    后端早已取到 usage 与 finish_reason(见 backends.OpenAICompatBackend),
    这里顺手递给账本。不带的话,"这一棒是不是被腰斩"事后无法审计。
    """
    try:
        _usage = getattr(backend, "last_usage", None)
        runlog.record(project_root, chapter_n, role=role, ok=ok, ms=ms,
                      chars_in=chars_in, chars_out=chars_out,
                      provider=str(getattr(backend, "provider", "") or ""),
                      model=str(getattr(backend, "model", "") or ""),
                      purpose=str(getattr(backend, "loom_purpose", "") or ""),
                      error=error, attempt=0,
                      tokens=_usage if isinstance(_usage, dict) else None,
                      finish=str(getattr(backend, "last_finish", "") or ""))
    except Exception as e:      # noqa: BLE001 - 账本坏不能拖累正文
        _swallow("运行账本落盘", e, progress)


def _half_draft(project_root: Path, chapter_n: int, role: str, text: str) -> Path | None:
    """作者叫停时,把**已跑完那一棒**的正文稿子另存一份。返回落点;没有可存的东西返回 None。

    为什么只存正文类(写手/编辑/润色师):设定师/大纲师的产物本来就实时落盘在设定文件与细纲里,
    再抄一份只会让作者分不清哪份是准的。为什么存 .txt:`.半稿` 不在任何 reads 里,
    用 .txt 更保险——导出/阅读器不会把半稿当正文章节。
    """
    if role not in _PROSE_ROLES or not (text or "").strip():
        return None
    from .fsutil import atomic_write_text
    p = paths.half_path(project_root, chapter_n)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, text.rstrip() + "\n")
    return p


def _note_sediment(project_root: Path, chapter_n: int, stats: dict,
                   progress: Progress) -> None:
    """把自动沉淀的结果写成**可追溯留痕**（用户裁定 2026-11-12，静默失败缺陷登记 第2条）。

    为什么必须落盘而不只发事件：沉淀跑在 daemon 线程里，它完成时响应流可能已经关了，
    进度事件没人接。落进 `.审稿留痕/第N章.md` 才是能事后复查的证据；事件只是顺带的可见性。
    为什么失败要写在留痕里而不是抛：沉淀始终是加分项，绝不能反过来影响正文与账本。
    """
    st = stats if isinstance(stats, dict) else {}
    failed = str(st.get("失败") or "").strip()
    got = int(st.get("记了") or 0)
    cand = int(st.get("候选") or 0)
    try:
        if failed:
            # 失败必须落盘：daemon 线程完成时响应流可能已关闭，事件没人接，
            # 而 `.审稿留痕/第N章.md` 是事后能复查的证据（含章节号 + 失败摘要）。
            path = paths.review_note_path(project_root, chapter_n)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n## 记忆沉淀失败(非阻断)\n"
                        f"- 第 {chapter_n} 章自动沉淀失败:{failed}\n"
                        f"- 影响:后续章节的自动召回会变少(前情可能断裂);正文与本账本不受影响。\n"
                        f"- 处置:记忆库修好后可手动重建索引,或对本章重跑沉淀。\n")
            progress(events.warn(f"第 {chapter_n} 章事实沉淀失败:{failed}"
                                 f"(正文与账本不受影响,详见 审稿留痕)"))
            progress(events.edit_note(chapter_n, path))
        elif cand or got:
            # 成功只在进度流里报读数，不额外生成留痕文件——
            # 每章多写一个文件超出「只修静默失败」的范围，产品可见面不该被顺手扩大。
            # 候选 0 条且一条没记 = 本章本来就没有可沉淀的事实，不报（否则等于每次都说"记了 0 条"）。
            progress(events.info(f"第 {chapter_n} 章事实沉淀完成:候选 {cand} 条,记入 {got} 条"))
    except Exception as e:      # noqa: BLE001 - 留痕本身失败也不能炸沉淀线程
        # 连落盘都失败时,至少留一行 stderr,不留绝对的空白
        print(f"[自动沉淀] 第 {chapter_n} 章留痕写入失败:{type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def _finish_chapter(project_root: Path, chapter_n: int, final_text: str, backend: Backend,
                    config: Config, progress: Progress, *, critic_backend: Backend | None = None,
                    backend_for: Callable[[str], Backend] | None = None,
                    hardfacts: str = "") -> tuple[Path, str]:
    """收尾:非空硬闸 → 起标题 → 落正文+快照 → 入账本 → 违禁词/超长/除虫 → chapter_done。

    **两条路(流水线 / agent 循环)共用这一份**——收尾动作是 learn 的 diff 源、drifted 判定、
    前端完成信号的地基,绝不能因为中间怎么跑变了就长出第二份会漂的实现。
    """
    from . import ledger
    final_body = _strip_edit_note(final_text)  # 兜底:终稿/快照绝不含留痕哨兵
    # 终稿非空硬闸:别把空/残废正文写进 正文+.原稿(空快照会让下次 learn 学到空、二次污染指纹)
    reasons = validate_output(final_body, chapter_profile(config.chapter_chars))
    if reasons:
        raise LoomBackendError(render("model_output_invalid", detail="；".join(reasons)),
                               code="model_output_invalid")
    # 自动起标题(附赠,失败回退无标题);标题进正文首行 H1,且 .原稿快照/ledger 都同口径带上它
    title = _generate_title(backend, final_body)
    final = compose(title, final_body)
    path = _save_chapter(project_root, chapter_n, final)
    ledger.record_snapshot(project_root, chapter_n, final)  # 与 正文/.原稿 同口径(都含 H1),不会误判 drifted
    _scan_sensitive(project_root, chapter_n, final_body, progress)  # 违禁词扫正文体即可(标题不必扫)
    _flag_overlong(project_root, chapter_n, final_body, config.chapter_chars, progress)  # 超长只留痕,不拦稿
    _scan_continuity(project_root, chapter_n, final_body, critic_backend or backend,
                     config, progress, hardfacts)
    progress(events.chapter_done(chapter=chapter_n, path=path, title=title,
                                  chars=len(final_body), preview=final_body[:300], text=final))
    # 批B 二阶段：自动沉淀——终稿事实抽进记忆库（异步不阻塞，失败静默）。
    # 只在 FullAuto/剧作模式跑（辅助/教学的手动正文不自动沉淀——作者可能还在改）。
    # known_entities 从硬设定/设定集来——写手已用过这些专名，沉淀的原子事实自然关联。
    if getattr(config, "writing_mode", "FullAuto") in ("FullAuto", "剧作"):
        try:
            from . import autofact as _af
            # 用户裁定 2026-11-12（静默失败缺陷登记 第2条）：原实现丢弃线程返回值、
            # 异常 except: pass，沉淀失败时作者/后续章节毫无察觉。现在把 stats 收回来落痕。
            _af.sediment_async(project_root, chapter_n, final_body, hardfacts,
                               on_done=lambda _st: _note_sediment(
                                   project_root, chapter_n, _st, progress))
        except Exception as e:
            # 「没能启动沉淀」也要留痕（不再 pass）
            _swallow(f"第 {chapter_n} 章事实沉淀启动", e, progress)
    return path, final


def _run_agentic(project_root: Path, chapter_n: int, backend: Backend, config: Config,
                 progress: Progress, critic_backend: Backend | None = None,
                 should_cancel=None, backend_for: "Callable[[str], Backend] | None" = None
                 ) -> tuple[Path, str]:
    """agent 模式:一个写作 agent、五个人格、循环到终稿提交(spec 2026-08-16 §3.2)。

    与流水线的差异只在【中间怎么跑】:没有固定拓扑,agent 自己决定顺序、可以回头重来。
    护栏一条不少(挂在产物提交上,见 §4),收尾走与流水线同一份 `_finish_chapter`。

    注:**尚无产物级续跑**(轨迹落盘归 P2);这条路目前每次从头跑。
    """
    from . import write_tools, writeloop
    # 五个人格照旧发进度表:UI 的 pill 进度条与五个头像不用改(§3.2 承诺,产物名/角色名都没变)
    progress(events.pipeline_start(chapter_n, list(PIPELINE)))
    task_id = _pipeline_task(project_root, chapter_n, config, progress)
    progress = _task_progress(progress, task_id)
    # 修(真机测试抓到的**真缺陷**,不是陈旧断言):
    # 外面 `run_pipeline` 在 agent 模式下会传 `backend_for=...`,而这里签名不收 ⇒
    # 只要 loom.toml 里「agent模式」= true,一开写就 TypeError 崩掉。
    # 这里把它接住并原样交给 Session:agent 循环同样要能按"用途格"取模型。
    sess = write_tools.Session(root=project_root, chapter_n=chapter_n, config=config,
                               backend=backend, critic_backend=critic_backend, progress=progress,
                               backend_for=backend_for)
    final_text = writeloop.run_chapter(sess, should_cancel=should_cancel)
    _pipeline_task(project_root, chapter_n, config, progress, finish=True)  # 夜间补缺:agentic 同发任务收尾
    return _finish_chapter(project_root, chapter_n, final_text, backend, config, progress,
                           critic_backend=critic_backend,
                           hardfacts=_hardfacts_for(project_root))


def _aitell_factory(project_root: Path, chapter_n: int, policy):
    """句内 AI 翻转句检测(「不是A而是B」类)+ 作者自列的「绝不这么写」。"""
    def _run(text: str) -> list:
        from .aitell import detect
        return detect(text, list(policy.anchors), cross_sentence=policy.cross_sentence,
                      banned=list(policy.banned))
    return _run


def _fatigue_factory(project_root: Path, chapter_n: int, policy):
    """跨章重复/腔调检测(章首章末雷同、整句复用)。"""
    def _run(text: str) -> list:
        from .fatigue import scan
        return scan(project_root, chapter_n, text, list(policy.anchors))
    return _run


# 确定性预筛注册表:每项是 (root, chapter_n, 口径) -> (text -> list[Issue]) 的工厂。
# 第三参从「anchors 列表」换成 `aitell.Policy` —— 口径文件里除了签名句,
# 还有「绝不这么写」与「跨句号算不算」两件,打包成一个对象传,别让每个检测器各读一遍盘。
# 新接检测器(如三库整合调研里相中的)只在这里加一行——单项异常各自吞掉,绝不拖累出稿。
DETECTORS = (_aitell_factory, _fatigue_factory)


def _deslop_detector(project_root: Path, chapter_n: int):
    """给「去AI味」关卡的确定性预筛:跑 DETECTORS 注册表全部检测器、汇总硬伤清单。

    命中前都先比对写作指纹 anchor + 去AI味口径(`aitell.load_policy`)豁免作者签名句;
    每轮在当前稿上重扫(回炉擦净自然归零)。任何异常都吞掉、返回空——附赠类检测绝不拖累出稿。
    """
    try:
        from .aitell import Policy, load_policy
        policy = load_policy(project_root)
    except Exception as e:
        _swallow("去AI味口径读取", e)
        from .aitell import Policy
        policy = Policy()
    runners = []
    for factory in DETECTORS:
        try:
            runners.append(factory(project_root, chapter_n, policy))
        except Exception as e:
            _swallow("去AI味检测器构建", e)

    def _run(text: str):
        out: list = []
        for r in runners:
            try:
                out += r(text)
            except Exception as e:
                _swallow("去AI味检测器执行", e)
        return out

    return _run


def _scan_continuity(project_root: Path, chapter_n: int, body: str, backend: Backend,
                     config: Config, progress: Progress, hardfacts: str = "") -> None:
    """终稿后连续性除虫(附赠:报告进留痕+事件,状态入账进账本)。吞一切异常,绝不阻断出稿。
    走 critic_backend(便宜模型)——除虫是「评估、管 what」,与质检复审同路由哲学。"""
    if not config.continuity_scan:
        return
    try:
        from .continuity import scan_chapter
        rep = scan_chapter(project_root, chapter_n, body, backend,
                           hardfacts=hardfacts, progress=progress)
        progress(events.debug_report(chapter_n, rep["issues"], rep["note_path"]))
    except Exception as e:  # noqa: BLE001 — 附赠动作绝不阻断出稿,但不静默
        progress(events.warn(f"除虫这次没跑成({type(e).__name__}:{e});不影响本章出稿。"))


def _scan_sensitive(project_root: Path, chapter_n: int, text: str, progress: Progress) -> None:
    """本章终稿过一遍违禁词粗筛;命中只发提示事件,绝不阻断(平台审核终归靠人)。"""
    try:
        from .sensitive import scan
        hits = scan(text, project_root)
    except Exception:
        return
    if hits:
        progress(events.sensitive(chapter=chapter_n,
                                  count=sum(h["count"] for h in hits), hits=hits[:20]))


def _save_chapter(project_root: Path, chapter_n: int, final: str) -> Path:
    out = paths.chapter_path(project_root, chapter_n)
    snap = paths.snapshot_path(project_root, chapter_n)
    snapshot_chapter(project_root, paths.chapter_rel(chapter_n))  # 覆盖前留旧稿历史(force 重写时保住手改)
    atomic_write_text(out, final + "\n")
    atomic_write_text(snap, final + "\n")  # AI 原稿快照,只给 learn 做 diff
    return out


def regen_outline(project_root: Path, chapter_n: int, backend: Backend,
                  config: Config, progress: Progress = _noop, *,
                  backend_for: Callable[[str], Backend] | None = None) -> str:
    """重新生成第 N 章细纲:跑 章节细纲师→章节设定师→章节分镜师,覆盖写 正文/.细纲/第N章.md,返回细纲文本。

    只刷细纲、不碰正文——用于"我想要个新的分镜方案"。之后重写本章会按这份新细纲来。
    设定师此刻会读到最新的世界观/人物卡/卡章纲,所以改了上游再「重新生成细纲」就能吃到。

    终审②Important:新细纲落盘后顺手记进轨迹,当「AI 侧最新一份」。`evolve.collect` 的判据是
    「轨迹里最后一次提交的细纲 ≠ 盘上那份」即认作者改过——不记这一笔,重新生成出的这份 AI 稿
    会被误判成「作者改成的」喂进 refine 的证据,直接违背判据只能是作者实际改成了什么这条红线。
    """
    prev = _prev_chapter(project_root, chapter_n)
    hardfacts = _hardfacts_for(project_root, progress)
    # 状态快照:不进续跑签名(口径与 run_pipeline 同,见该函数内注释)——账本不在任何
    # 角色 reads 里,手改/补账不会触发重跑;补账后想让半截章吃到新账,用 --force/重写。
    from . import statebook
    state_snap = statebook.snapshot_for(project_root, chapter_n - 1)
    workspace: list[tuple[str, str]] = []
    outline = ""
    outline_sig = ""
    for spec in STEPS[:3]:   # 细纲三棒:与主线同一张工序表,不再手抄角色元组
        role = spec.role
        agent, knowledge = _knowledge_prompt(project_root, chapter_n, role)
        _bk = backend_for(role) if backend_for is not None else backend
        progress(events.agent_start(role))
        max_chars = (spec.budget_fn(config.chapter_chars) if spec.budget_fn
                     else (spec.short_budget or config.chapter_chars))  # 与主线 run_pipeline 同口径
        if role == "章节分镜师":
            # 签名必须在 workspace.append 之前算——签的是「产它时的上游」,同
            # run_pipeline._sig / write_tools.artifact_sig 那一套算法(resume.sig_v2)。
            # 终审⑥订正:这条 sig 不是摆设——writeloop._replay 会读下面记进轨迹的这笔
            # 提交的 c["sig"],拿它跟 write_tools.artifact_sig 比对,决定要不要把它当
            # 「上游未变」重放回工作区。这里算它,正是为了让 _replay 的判据有一个合规值
            # 可比,不是给它编一个没人看的占位。这里算的签名(workspace 里带着设定师产物)
            # 与 _replay 日后重新核验时的签名(那份设定师产物没进过 trail、workspace 是空的)
            # 必然对不上——于是 _replay 会判定失配、保守放弃重放,让调用方从头重算,方向安全,
            # 但绝不是「没人读它」(见 tests/test_writeloop_resume.py 的回归测试)。
            from . import resume as resume_mod
            cfg_bits = {"chapter_chars": config.chapter_chars,
                        "gate_rounds": config.gate_rounds, "title": config.title}
            _, sig_items = _knowledge_items(project_root, chapter_n, role)
            outline_sig = resume_mod.sig_v2(agent.system_prompt, sig_items, workspace, prev, cfg_bits)
        user_prompt = _build_user_prompt(chapter_n, role, agent, knowledge, prev, workspace, hardfacts,
                                         target_chars=max_chars, chapter_target=config.chapter_chars,
                                         wants_prev=spec.wants_prev, wants_hardfacts=spec.wants_hardfacts,
                                             wants_state=spec.wants_state,
                                         book_title=config.title, state_snapshot=state_snap)
        # 按工序取后端(裁定 1A):细纲三棒都归「大纲」槽。**这条线原来漏了形参,
        # 引用的 backend_for 根本不存在 ⇒ 点「重新生成细纲」直接 NameError(交叉审查 P0-2)。**
        out = _bk.complete(
            agent.system_prompt, user_prompt, max_chars=max_chars,
            on_chunk=lambda d, r=role: progress(events.agent_chunk(r, d)),
        )
        # 每棒非空闸(与主线同口径):细纲师空产物不再静默喂下一棒、空转白花一次调用
        reasons = validate_output(out, STEP)
        if reasons:
            raise LoomBackendError(render("model_output_invalid", detail=f"{role}:" + "；".join(reasons)),
                                   code="model_output_invalid")
        workspace.append((agent.produces, out))
        progress(events.agent_done(role, agent.produces))
        outline = out
    if not outline.strip():  # 模型这次没出细纲 → 不拿空覆盖你原来的细纲
        raise LoomBackendError(render("model_output_invalid", detail="细纲:模型这次返回空"),
                               code="model_output_invalid")
    atomic_write_text(_outline_path(project_root, chapter_n), outline.strip() + "\n")
    from . import trail
    trail.record_commit(project_root, chapter_n, agent.produces, outline.strip(), outline_sig)
    _check_scene_budget(project_root, chapter_n, outline.strip(), config.chapter_chars, reused=False, progress=progress)
    progress(events.outline_done(chapter_n))
    return outline.strip()
