"""磁盘布局的唯一真相:一本书在盘上长什么样,只在这里声明一次。

- 目录常量 + 各产物的路径构造:正文章节 / .原稿快照 / .细纲 / .历史 / .回收站 /
  .审稿留痕 / 外置大脑各文件 / .指纹历史 / ledger。其余模块一律从这里取,不再各自拼字面量。
- CHAPTER_ARTIFACTS 注册表:一章在盘上的全部关联产物;chapters 的删除/重编号按它整体搬,
  新增每章产物时在此登记一行,搬家就不会漏。
- 纯 stdlib、不 import 任何 loom 模块——谁都能安全依赖它,永不成环。
"""

from __future__ import annotations

import re
from pathlib import Path

# ── 目录(相对项目根;点开头 = 引擎自动维护,作者一般不手碰)────────────────
BODY_DIR = "正文"                       # 章节正文(作者手改的定稿)
SNAPSHOT_DIR = f"{BODY_DIR}/.原稿"       # AI 原稿快照 + 续跑账本(只给 learn 的 diff / 断点续跑)
OUTLINE_DIR = f"{BODY_DIR}/.细纲"        # 每章分镜细纲(可看可改,WYSIWYG)
HALF_DIR = f"{BODY_DIR}/.半稿"           # 被作者叫停时,已跑完那一棒的稿子落这儿(绝不进正式正文)
TRAIL_DIR = f"{BODY_DIR}/.轨迹"          # agent 模式的写章轨迹(产物级续跑;删了只是下次从头跑)
HISTORY_DIR = f"{BODY_DIR}/.历史"        # 单章版本历史(覆盖前快照,后悔药)
TRASH_DIR = f"{BODY_DIR}/.回收站"        # 删章的整章产物落这里,可恢复
REVIEW_DIR = ".审稿留痕"                 # 编辑留痕 / 关卡残留 / 各类提醒(人可读可改)
NAV_TRACE_REL = f"{REVIEW_DIR}/领航员留痕.md"   # 领航员出题失败的现场记录(人可读可删,不派生状态)
PARTNER_DIR = ".伙伴对话"                    # 伙伴对话日志(整书一条;人可读可删,删=失忆书无恙)
PARTNER_CUR_REL = f"{PARTNER_DIR}/当前.jsonl"
EVOLVE_DIR = ".进化"                     # 人格增补的落盘前快照(可一键撤销);删了只是撤不了,书不动
BRAIN_DIR = "外置大脑"                   # 每本书的状态文件(人写主体 + AI 追加子块)
FP_HISTORY_DIR = f"{BRAIN_DIR}/.指纹历史"  # learn 前的指纹备份(一键撤销)
DECONSTRUCT_DIR = f"{BRAIN_DIR}/.拆书"    # 拆书隔离草稿区(没有任何 agent 读它)
TASKS_DIR = ".任务状态"                  # 任务准备架构(2026-11-08):书内任务事件/快照子区
                                        # 结构 .任务状态/<会话代际>/<task_id>.jsonl(+.快照.json)


# ── 外置大脑各文件(相对项目根)────────────────────────────────────────────
def brain_rel(name: str) -> str:
    """外置大脑文件的相对路径(name 不带 .md)。"""
    return f"{BRAIN_DIR}/{name}.md"


# ── 设定集(作者 2026-10-20 裁定 3)────────────────────────────────────────
# 静态事实的唯一真相。原 世界观/ + 人物/ + 立项卡 全部收进来,按角色职责编号分区:
#   00-立项    领航员/设定师的地基锚点(平台/分区/题材/读者/卖点)
#   10-世界观  设定师的产出(世界规则/力量/地理/金手指)
#   20-人物    设定师 + 人物相关角色的产出
#   30-战斗    战斗设计师的产出
#   40-知识    知识顾问的产出(提案确认后入账)
#   90-速记本  14 个角色各自的工作记录(互不交叉阅读)
SET_DIR = f"{BRAIN_DIR}/设定集"
SET_CARD_REL = f"{SET_DIR}/00-立项.md"
SET_WORLD_DIR_REL = f"{SET_DIR}/10-世界观"
SET_CHARS_DIR_REL = f"{SET_DIR}/20-人物"
SET_BATTLE_DIR_REL = f"{SET_DIR}/30-战斗"
SET_KNOW_DIR_REL = f"{SET_DIR}/40-知识"
SET_SCRATCH_DIR_REL = f"{SET_DIR}/90-速记本"
# 50-自定义:地基那几张卡是**有限的**,角色想记一件卡里没有的事时
# 以前无处可落(落点必须已存在)。这一格专给「按作者的想法长出来的新项」:
# 角色自造的新项一律落在这里(新建 .md),与 10/20/30/40 并列,侧栏同样看得到。
SET_CUSTOM_DIR_REL = f"{SET_DIR}/50-自定义"

# ── 分析产物(2026-10-20 收尾：给两个"分析型"角色定落点)──────────────────────
# 为什么**不**放进设定集：设定集装的是"静态事实"（世界是什么样、人是谁）。
# 语料分析师的词频报告、情绪节奏设计师的情绪曲线都是**派生分析**——会随写得多而变、
# 会有很多份、且不进任何"事实"判定。混进设定集会让"事实源"变脏。
ANALYSIS_DIR_REL = f"{BRAIN_DIR}/分析"
CORPUS_REPORT_REL = f"{ANALYSIS_DIR_REL}/语料分析.md"      # 语料分析师：单人维护一份，覆盖写
EMOTION_CURVE_REL = f"{ANALYSIS_DIR_REL}/情绪曲线.md"      # 情绪节奏设计师：按章列情绪值，覆盖写
# 去AI味口径(「让作者靠对话优化去AI味逻辑」)：
# 这是**检测器的口径数据**,不是代码 —— 代码侧那套句法规则(aitell)一个字都不许模型改,
# 但「哪些句子是我的签名句(豁免)」「哪些表达我绝不这么写(加报)」「跨句号的。是算不算」
# 这三件事本来就属于作者自己的偏好,写在这儿,由 aitell.load_policy 读。
# 与写作指纹同一条红线:只出候选卡,作者拍板才落盘。
SLOP_POLICY_REL = f"{ANALYSIS_DIR_REL}/去AI味口径.md"

FINGERPRINT_REL = brain_rel("写作指纹")
CARD_REL = brain_rel("卡章纲")
OVERVIEW_REL = brain_rel("全书大纲")   # 最上面那一层:一句话故事/核心冲突/三条主线/结局方向(一个文件,不按章)
VOLUMES_REL = brain_rel("卷纲")   # 卷→章之间的那一层:每卷起止章 + 该卷主线/卷末钩子(切卷靠它)
BANNED_REL = brain_rel("违禁词")
STATEBOOK_REL = brain_rel("状态账本")   # 跨章状态流水账(物品/状态/规则/时钟),除虫自动追加、人可改可删

# 兼容位:大量模块按这几个名字引用。单文件形态指向设定集内的同名文件(新书不用),
# 目录形态指向设定集分区 —— 见下方 brain_form 的双形态判定。
PROJECT_CARD_REL = SET_CARD_REL
WORLD_REL = f"{SET_DIR}/10-世界观.md"
CHARS_REL = f"{SET_DIR}/20-人物卡.md"
WORLD_DIR_REL = SET_WORLD_DIR_REL
CHARS_DIR_REL = SET_CHARS_DIR_REL
GROWTH_NAME = "成长档案.md"   # learn 的 [AI补充·第N章] 都进它:AI 有自己的文件,永不碰人写的文件

# [AI补充] 可能住的全部文件(enrich 写入/删章清理/重编号/prompt 折叠都认这一份清单):
# 设定集目录形态=各分区的 成长档案.md(AI 自留地,物理隔离)
SUPP_RELS = (WORLD_REL, CHARS_REL,
             f"{WORLD_DIR_REL}/{GROWTH_NAME}", f"{CHARS_DIR_REL}/{GROWTH_NAME}")


def brain_form(root: Path | str, file_rel: str, dir_rel: str) -> str:
    """外置大脑某部位的形态:"file" | "dir" | "none"(单文件优先)。"""
    root = Path(root)
    if (root / file_rel).is_file():
        return "file"
    if (root / dir_rel).is_dir():
        return "dir"
    return "none"


def brain_dir_files(root: Path | str, dir_rel: str) -> list[Path]:
    """目录形态的成员文件(名字排序稳定;成长档案固定排最后——先读人写的,再读 AI 补的)。"""
    d = Path(root) / dir_rel
    if not d.is_dir():
        return []
    files = sorted(p for p in d.glob("*.md") if p.name != GROWTH_NAME)
    growth = d / GROWTH_NAME
    return files + ([growth] if growth.is_file() else [])


# ── 每章产物 ──────────────────────────────────────────────────────────────
def chapter_no(n) -> int:
    """章号唯一的合法性判据:必须是 ≥0 的整数(**0 = 楔子,是合法的**)。

    以前各端点各收各的 int,于是 `chapter=-3` 一路走到底 → 造出 `正文/第-3章.md`,
    还进了章列表(next_chapter 变 -2)。章号从这一处过(交叉审查 P2-3)。
    """
    try:
        v = int(n)
    except (TypeError, ValueError):
        raise ValueError("章号得是整数。") from None
    if v < 0:
        raise ValueError("章号不能是负数(第 0 章是楔子)。")
    return v


def chapter_rel(n: int) -> str:
    """正文章节的相对路径(给 snapshot_chapter 这类吃 rel 字符串的口)。"""
    return f"{BODY_DIR}/第{chapter_no(n)}章.md"


def chapter_path(root: Path | str, n: int) -> Path:
    return Path(root) / BODY_DIR / f"第{chapter_no(n)}章.md"


def half_path(root: Path | str, n: int) -> Path:
    """被叫停时的半稿落点。刻意用 .txt:它不是定稿,别让阅读器/导出把半稿当正文章节。"""
    return Path(root) / HALF_DIR / f"第{n}章.txt"


def snapshot_path(root: Path | str, n: int) -> Path:
    return Path(root) / SNAPSHOT_DIR / f"第{n}章.md"


def ledger_path(root: Path | str, n: int) -> Path:
    return Path(root) / SNAPSHOT_DIR / f"第{n}章.ledger.json"


def trail_path(root: Path | str, n: int) -> Path:
    return Path(root) / TRAIL_DIR / f"第{n}章.jsonl"


def outline_path(root: Path | str, n: int) -> Path:
    return Path(root) / OUTLINE_DIR / f"第{n}章.md"


def history_dir(root: Path | str, key: str) -> Path:
    """某章的版本历史目录;key 是「第N章」这样的章名(见 CHAP_RE 的捕获组)。"""
    return Path(root) / HISTORY_DIR / key


def review_note_path(root: Path | str, n: int) -> Path:
    return Path(root) / REVIEW_DIR / f"第{n}章.md"


def fp_history_path(root: Path | str, n: int) -> Path:
    return Path(root) / FP_HISTORY_DIR / f"第{n}章-learn前.md"


# 正文章节 rel 的判定正则(fsutil 历史快照用):命中「正文/第N章.md」取章名做 key。
# 【红线】不匹配一律由调用方静默跳过——外置大脑等非章节文件的保存走同一写盘口,
# 改成 raise 会炸掉它们的保存路径(评审已否决)。
CHAP_RE = re.compile(rf"^{BODY_DIR}/(第.+?章)\.md$")


def chapter_numbers(root: Path | str) -> list[int]:
    """盘上已有的章号(升序)。文件名即真相:正文/第N章.md。

    **认不出的名字跳过,绝不抛**:`正文/第番外章.md` 这种一度能让整本书打不开
    (project_state / 导出 / 写章全挂在 int() 上,审查 B 的 P1)。跳过谁由 `odd_chapter_files` 报出来。
    """
    body = Path(root) / BODY_DIR
    if not body.is_dir():
        return []
    out: list[int] = []
    for p in body.glob("第*章.md"):
        try:
            out.append(int(p.stem[1:-1]))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def odd_chapter_files(root: Path | str) -> list[str]:
    """正文目录里「看着像章节、但名字认不出章号」的文件。**只报不改**。"""
    body = Path(root) / BODY_DIR
    if not body.is_dir():
        return []
    bad = []
    for p in body.glob("第*章.md"):
        try:
            int(p.stem[1:-1])
        except (TypeError, ValueError):
            bad.append(p.name)
    return sorted(bad)


# 一章在盘上的全部关联产物:(相对目录, 文件名模板, 是否目录)。
CHAPTER_ARTIFACTS: list[tuple[str, str, bool]] = [
    (BODY_DIR, "第{n}章.md", False),
    (SNAPSHOT_DIR, "第{n}章.md", False),
    (SNAPSHOT_DIR, "第{n}章.ledger.json", False),
    (OUTLINE_DIR, "第{n}章.md", False),
    # 漏了这行 = 删掉第 3 章后,新的第 3 章会重放【旧第 3 章】的产物(串章)
    (TRAIL_DIR, "第{n}章.jsonl", False),
    (HISTORY_DIR, "第{n}章", True),
    (REVIEW_DIR, "第{n}章.md", False),
    (FP_HISTORY_DIR, "第{n}章-learn前.md", False),
]
