# -*- coding: utf-8 -*-
r"""领航员(Navigator)。

两种模式:
- **打地基模式**:项目新建后、尚未完成地基时,领航员是唯一可访问的 Agent,
  引导作者填完 7 项基础信息后,切换为常驻球模式。
- **常驻球模式**:右下角图标,职责是路由请求和必要时重建地基。

作者 2026-11-11 裁定:领航员有专属记录本(书间隔离,可记「世界信息缺口观察」);
路由规则仍按初始清单来(后照办)。

路由规则文件:`外置大脑/领航员-路由规则.md`
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from . import scratchpad

# 路由槽常量(供前端/后端使用)。15 角色 = 领航员 + 作者智库 9 + 写作引擎 6
ROUTE_TO_ROLE = Literal[
    "领航员",
    "设定师", "大纲师", "知识顾问", "语料分析师",
    "战斗设计师", "情绪节奏设计师", "人物侧写师", "编辑总监", "批评家",
    "章节细纲师", "章节设定师", "章节分镜师",
    "写手", "责任编辑", "润色师",
]

# 路由规则表(关键词 → 目标角色)
# 优先级:按声明顺序匹配,首次命中即返回。
# 作者审定后可在 `外置大脑/领航员-路由规则.md` 里覆盖这些规则。
DEFAULT_ROUTES: list[tuple[list[str] | str, ROUTE_TO_ROLE]] = [
    # 无法判断 → 领航员自己接
    (["下一步该干嘛", "接下来做什么", "怎么开始", "先做什么", "我下一步",
      "我现在应该", "现在该做", "先做哪个", "找谁"], "领航员"),

    # ── 作者智库（准备 / 审视，不跑单章流水线）──
    (["设定", "世界观", "世界规则", "力量体系", "金手指", "人物设定", "角色设定"], "设定师"),
    # 2026-11-12 人物侧写师入册（用户新需求）：路由边界按「骨架 vs 皮肤」划——
    # 词语落在「世界怎么运转 / 人物有哪些」的归设定师（上一条），
    # 落在「这个人为什么这样、怎么反应、和谁怎么互动」的归人物侧写师。
    # 注意 route() 是**最长关键词命中**：这些词都不与上面的「人物设定」「角色设定」重叠，
    # 所以「人物设定」仍归设定师、「人物卡/角色设计」归侧写师，不会互相抢。
    (["人物卡", "人物侧写", "角色设计", "人物动机", "人物关系", "人物弧光",
      "细化人物", "细化角色", "配角设计", "人物互动"], "人物侧写师"),
    (["大纲", "章纲", "卷纲", "全书结构", "分卷", "章节安排"], "大纲师"),
    (["查一下", "帮我查", "上网", "搜一下", "是什么", "什么意思", "怎么运作", "资料"], "知识顾问"),
    # 2026-10-25:「调指纹 / 调去AI味口径」这两件事只有语料分析师能做(改指纹是它的专属工具),
    # 所以这些词要路由给它。比「去AI味」长,按最长命中优先,不会被润色师那条抢走。
    (["语料", "文风分析", "词频", "句式", "节奏数据", "风格分析",
      "写作指纹", "文风指纹", "去AI味口径", "AI味口径", "改指纹"], "语料分析师"),
    (["战斗", "打斗", "战力", "招式", "武器", "威力"], "战斗设计师"),
    (["情绪", "节奏", "爽点", "张力", "欲罢不能", "憋屈"], "情绪节奏设计师"),
    (["审一下全书", "全局", "整体质量", "跨章", "伏笔回收", "全书一致"], "编辑总监"),
    (["硬伤", "OOC", "设定漂移", "因果断裂", "挑错", "毛病", "批评", "评价"], "批评家"),

    # ── 写作引擎（单章流水线 6 棒）──
    (["细纲", "填情节", "这一章怎么展开", "场次"], "章节细纲师"),
    (["章节设定", "填充设定", "这章的设定对不对", "设定矛盾"], "章节设定师"),
    (["分镜", "镜头", "视角切换", "画面感"], "章节分镜师"),
    (["写第", "开始写", "写下一章", "落字", "出初稿"], "写手"),
    (["改稿", "挑瑕疵", "这一章有没有问题", "润色前的检查", "责任编辑"], "责任编辑"),
    (["润色", "去AI味", "读起来像AI", "不流畅", "套话"], "润色师"),
]


def route(text: str) -> ROUTE_TO_ROLE:
    """根据作者输入的文本,返回应路由到的角色名。

    规则:逐条匹配 DEFAULT_ROUTES,首次命中即返回;全不命中 → 领航员自答。
    规则可被 `外置大脑/领航员-路由规则.md` 覆盖。
    """
    text_lower = text.lower()
    best_role, best_len = "领航员", 0
    for keywords, role in DEFAULT_ROUTES:
        if isinstance(keywords, str):
            keywords = [keywords]
        for kw in keywords:
            if kw.lower() in text_lower and len(kw) > best_len:
                best_len, best_role = len(kw), role  # type: ignore[assignment]
    return best_role


def _load_project_rules(project_root: Path | str) -> list[tuple[list[str], ROUTE_TO_ROLE]]:
    """从项目目录加载作者手写的规则文件,不存在则返回 None。"""
    root = Path(project_root)
    rule_file = root / "外置大脑" / "领航员-路由规则.md"
    if not rule_file.is_file():
        return None
    entries: list[tuple[list[str], ROUTE_TO_ROLE]] = []
    for line in rule_file.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if "→" not in raw:
            continue
        keywords_part, role = raw.split("→", 1)
        role = role.strip()
        if role not in ALL_NAVIGATOR_ROLES:
            continue
        keywords = [kw.strip() for kw in keywords_part.split(",") if kw.strip()]
        if keywords:
            entries.append((keywords, role))  # type: ignore[arg-type]
    return entries if entries else None


def route_from_project(project_root: Path | str, text: str) -> ROUTE_TO_ROLE:
    """路由:优先读项目规则文件(作者手写),不存在时用内置规则。"""
    project_rules = _load_project_rules(project_root)
    rules = project_rules if project_rules else DEFAULT_ROUTES
    text_lower = text.lower()
    best_role, best_len = "领航员", 0
    for keywords, role in rules:
        if isinstance(keywords, str):
            keywords = [keywords]
        for kw in keywords:
            if kw.lower() in text_lower and len(kw) > best_len:
                best_len, best_role = len(kw), role  # type: ignore[assignment]
    return best_role


def _multi(text: str, rules) -> list[str]:
    """按「命中的最长关键词」排序，返回所有命中的角色（去重）。一个都没命中 → 领航员。"""
    text_lower = (text or "").lower()
    hits: list[tuple[int, str]] = []
    for keywords, role in rules:
        if isinstance(keywords, str):
            keywords = [keywords]
        best = 0
        for kw in keywords:
            if kw.lower() in text_lower and len(kw) > best:
                best = len(kw)
        if best > 0:
            hits.append((best, role))
    hits.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, r in hits:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out or ["领航员"]


def route_multi(text: str) -> list[str]:
    """给「该找谁」的建议：可能不止一个角色（作者 2026-10-20 裁定 2）。"""
    return _multi(text, DEFAULT_ROUTES)


def route_multi_from_project(project_root: Path | str, text: str) -> list[str]:
    """同 route_multi，但优先用项目里手写的 领航员-路由规则.md。"""
    project_rules = _load_project_rules(project_root)
    return _multi(text, project_rules if project_rules else DEFAULT_ROUTES)


ALL_NAVIGATOR_ROLES = frozenset([
    "领航员", "设定师", "大纲师", "知识顾问", "语料分析师",
    "战斗设计师", "情绪节奏设计师", "编辑总监", "批评家",
    "章节细纲师", "章节设定师", "章节分镜师",
    "写手", "责任编辑", "润色师",
])


# ── 地基字段定义 ────────────────────────────────────────────────────────────
# 打地基时需要填的每一项,含字段名、问法、写到 Config 时的 key
FOUNDATION_FIELDS: list[dict] = [
    {
        "id": "title",
        "问": "书名是什么？",
        "提示": "请告诉这本书的名字",
        "key": "title",
        "必填": True,
    },
    {
        "id": "idea",
        "问": "一句话设定是什么？",
        "提示": "用一句话概括这本书讲什么",
        "key": "idea",
        "必填": True,
    },
    {
        "id": "chapter_chars",
        "问": "每章目标字数是多少？",
        "提示": "如 800、1200、2000",
        "key": "chapter_chars",
        "必填": True,
    },
    {
        "id": "narrative_voice",
        "问": "叙事人称是什么？",
        "提示": "第一人称 / 第二人称 / 第三人称（全知 / 有限）",
        "key": "narrative_voice",
        "必填": False,
    },
    {
        "id": "core_theme",
        "问": "核心主题是什么？",
        "提示": "这本书想探讨或表达什么",
        "key": "core_theme",
        "必填": False,
    },
    {
        "id": "volume_structure",
        "问": "卷/部划分是怎样的？",
        "提示": "如「3 卷·共 30 章」或「序章+5 部+尾声」",
        "key": "volume_structure",
        "必填": False,
    },
    {
        "id": "core_characters",
        "问": "有哪些核心人物？",
        "提示": "简单列出,人物设计师会逐一细化",
        "key": "core_characters",
        "必填": False,
    },
]


def foundation_progress(root: Path | str) -> dict:
    """返回地基完成进度:哪些字段已填,哪些还空着。

    异常配置（缺 loom.toml / toml 损坏）也必须返回**同一结构**——四个键恒在，
    「缺失项详情」可为对应字段的完整列表。原来缺配置分支少这一个键，取用方
    （foundation_block）直接 KeyError → 端点 500（审查 审查记录 P2）。
    """
    from . import config as C

    root = Path(root)
    if not (root / "loom.toml").is_file():
        return _all_fields_missing()

    try:
        cfg = C.load_config(root)
    except Exception:
        return _all_fields_missing()

    done, missing = [], []
    for f in FOUNDATION_FIELDS:
        k = f["key"]
        v = getattr(cfg, k, None)
        # 2026-11-10 第一章地基硬门：纯空白字符串不算已填（原来只判 truthiness，
        # "   " 会被当成填过 → 硬门被绕过）。数字/其他类型仍沿原判据。
        empty = isinstance(v, str) and not v.strip()
        if f["必填"] and (empty or not v):
            missing.append(f["id"])
        else:
            done.append(f["id"])
    return {
        "done": len(missing) == 0,
        "完成项": done,
        "缺失项": missing,
        "缺失项详情": [{"id": f["id"], "问": f["问"]} for f in FOUNDATION_FIELDS if f["id"] in missing],
    }


def _all_fields_missing() -> dict:
    """配置不可用时的统一结构（键与正常路径一致，切不可少键）。"""
    return {
        "done": False,
        "完成项": [],
        "缺失项": [f["id"] for f in FOUNDATION_FIELDS],
        "缺失项详情": [{"id": f["id"], "问": f["问"]} for f in FOUNDATION_FIELDS],
    }


# ── 第一章地基硬门（2026-11-10）────────────────────────────────────────────
# 作者要求：地基未完，禁止开始第一章。三个 HTTP 入口在开 worker/模型/任务之前、
# run_pipeline 在 agentic 分流之前，复用**同一个只读判据**；force/from_step 不绕过。
# 真假之分（历史会话 深度审查同款口径）：本函数只读盘上事实，不是防线唯一处——
# 入口与流水线各自调用它，任一处都不能被前端/直连 POST 跳过。
# 不门章 2：章 2 沿用原有规则（prev_unfinished 等），不受新增门影响。

def foundation_block(root: Path | str, chapter: int) -> dict | None:
    """章 1 地基未完成 → 统一错误体；已完成或非章 1 → None（不拦）。

    配置缺失/损坏时返回结构化结果（**不抛**）：入口据此回 400（与既有「不是书」
    同码），不让作者看到 500。
    """
    try:
        if int(chapter or 0) != 1:
            return None
    except (TypeError, ValueError):
        return None
    root = Path(root)
    if not (root / "loom.toml").is_file():
        # 入口通常在 _project_root 已拦成 400；这里防御性给同码结构，避免缺键。
        progress = _all_fields_missing()
        return {
            "error": f"{root} 不是 loom 项目(没有 loom.toml)。",
            "code": "not_a_book",
            "missing": progress["缺失项"],
            "missing_details": progress["缺失项详情"],
        }
    from . import config as C
    try:
        C.load_config(root)
    except Exception:
        return {
            "error": f"{root} 的 loom.toml 读不出来（配置可能损坏）。"
                     f"先修好这本书的配置文件，再开始第一章。",
            "code": "config_unreadable",
            "missing": [],
            "missing_details": [],
        }
    progress = foundation_progress(root)
    if progress["done"]:
        return None
    details = progress["缺失项详情"]
    names = "、".join(d["问"] for d in details) or "、".join(progress["缺失项"])
    return {
        "error": f"第一章还开不了工：地基还差 {names}。"
                 f"在领航员里补齐地基（书名 / 一句话设定 / 每章目标字数）再开始第一章。",
        "code": "foundation_incomplete",
        "missing": progress["缺失项"],
        "missing_details": details,
    }


def save_foundation(root: Path | str, answers: dict[str, str]) -> None:
    """把地基答案写入 Config。

    answers: {field_id: 用户回复}。只写必填项;选填项只在本字段有回复时写入。
    """
    from . import config as C
    import dataclasses

    root = Path(root)
    cfg = C.load_config(root)
    updates: dict = {}
    for f in FOUNDATION_FIELDS:
        if f["id"] not in answers:
            continue
        raw = answers[f["id"]].strip()
        if not raw and f["必填"]:
            continue
        k = f["key"]
        if k == "chapter_chars":
            try:
                updates[k] = int(raw)
            except ValueError:
                pass
        else:
            updates[k] = raw
    if updates:
        new_cfg = dataclasses.replace(cfg, **updates)
        C.save_config(root, new_cfg)


# ── 世界信息充足度（软门，2026-11-11 用户裁定 / v3 终稿）─────────────────
# 与地基硬门**严格分开**：硬门拒绝、不能越过；软门只提示、作者确认即放行。
# 顺序固定：先过硬门，再评软门（硬门未过时不弹"确定吗"）。
# 只在「开始织第一章」这个时点评估：不后台扫描、不调模型、不做完成度百分比。

WORLD_ASK = "世界信息还不够完善，确定现在开始织第一章吗？"

# v3 第三节的 A 级维度 → 承载它的文档（文档级结构判定，零内容推理）。
# 维度识别按需求方 2026-11-11 裁定：小节存在性 + 占位词 + 草案标记，不做相关性判断。
WORLD_A_DIMENSIONS: tuple[dict, ...] = (
    {"role": "设定师", "field": "核心约束与代价",
     "docs": ("外置大脑/设定集/10-世界观/力量体系.md",)},
    {"role": "设定师", "field": "主要势力与地理骨架",
     "docs": ("外置大脑/设定集/10-世界观/地理与势力.md",)},
    {"role": "大纲师", "field": "主线目标与核心阻力",
     "docs": ("外置大脑/全书大纲.md",)},
    {"role": "大纲师", "field": "分卷走向",
     "docs": ("外置大脑/卷纲.md",)},
    {"role": "大纲师", "field": "关键转折点",
     "docs": ("外置大脑/卡章纲.md",)},
    {"role": "大纲师", "field": "至少一条伏笔及其回收位置",
     "docs": ("外置大脑/卡章纲.md",)},
    {"role": "战斗设计师", "field": "实力刻度与胜负代价",
     "docs": ("外置大脑/设定集/30-战斗/战斗体系.md",)},
    {"role": "情绪节奏设计师", "field": "全书情绪基调",
     "docs": ("外置大脑/分析/情绪曲线.md",)},
    {"role": "情绪节奏设计师", "field": "爽点/痛点的大致位置",
     "docs": ("外置大脑/分析/情绪曲线.md",)},
)

# 词表集中定义（需求要求：以后加词只改一处）。
# D 档（占位/未填）：模板提示语、显式待补标记
_PLACEHOLDER_WORDS = ("占位示例", "换成你自己的", "待补", "TODO", "TBD", "（略）", "(略)")
# B 档（成篇但未定稿）
_DRAFT_WORDS = ("草案", "暂定", "待定", "初稿")

_GRADE_A, _GRADE_B, _GRADE_C, _GRADE_D = "A", "B", "C", "D"
_THIN_CHARS = 20          # 少于此实字 = 碎片（C）
_THIN_RATIO = 0.35        # 低于同文档其它小节中位数的这个比例 = 明显偏短（C）


def _doc_sections(text: str) -> list[tuple[str, str]]:
    """把文档切成 (标题, 正文) 小节；无 `##` 小节时整篇算一节（标题取首个 `#` 行）。"""
    lines = str(text or "").splitlines()
    body_lines = [ln for ln in lines if not ln.lstrip().startswith(">")]  # 引用块是说明,不算正文
    sections: list[tuple[str, str]] = []
    cur_title, cur_body = "", []
    for ln in body_lines:
        if ln.lstrip().startswith("#"):
            if cur_title or "".join(cur_body).strip():
                sections.append((cur_title, "\n".join(cur_body)))
            cur_title, cur_body = ln.strip().lstrip("#").strip(), []
        else:
            cur_body.append(ln)
    if cur_title or "".join(cur_body).strip():
        sections.append((cur_title, "\n".join(cur_body)))
    return sections


def _fields_all_empty(body: str) -> bool:
    """`- 标签:` 形式但冒号后都没填 → 视为未填（D）。"""
    import re

    bullets = [ln.strip() for ln in str(body or "").splitlines()
               if ln.strip().startswith(("-", "*"))]
    if not bullets:
        return False
    for b in bullets:
        m = re.match(r"^[-*]\s*(.*)$", b)
        rest = (m.group(1) if m else b).strip()
        parts = re.split(r"[:：]", rest, maxsplit=1)
        value = (parts[1] if len(parts) > 1 else "").strip()
        if value:
            return False
    return True


def _grade_doc(text: str) -> str:
    """单份文档的结构档位：A 成篇已确认 / B 成篇草案 / C 碎片 / D 占位或缺失。"""
    sections = _doc_sections(text)
    body = "\n".join(b for _, b in sections).strip()
    if not body:
        return _GRADE_D
    if any(w in body for w in _PLACEHOLDER_WORDS):
        return _GRADE_D
    if _fields_all_empty(body):
        return _GRADE_D
    if any(w in body for w in _DRAFT_WORDS):
        return _GRADE_B
    visible = "".join(ch for ch in body if not ch.isspace())
    if len(visible) < _THIN_CHARS:
        return _GRADE_C
    lengths = sorted(len("".join(ch for ch in b if not ch.isspace())) for _, b in sections if b.strip())
    if lengths:
        median = lengths[len(lengths) // 2]
        if median and len(visible) < median * _THIN_RATIO and len(visible) < _THIN_CHARS * 3:
            return _GRADE_C
    return _GRADE_A


def _world_dimension_grades(root: Path) -> tuple[list[dict], str]:
    """按「角色 × A 级维度」给文档结构档位（A/B/C/D）。任何异常都不抛。

    零推理纪律：只看**小节/字段是否存在、正文是否非空、有无占位词与草案标记**，
    不判断内容是否切题。判不出的按 D（宁可提示补，不假装完善），并在 error 记原因。
    """
    grades: list[dict] = []
    problems: list[str] = []
    root = Path(root)
    for dim in WORLD_A_DIMENSIONS:
        best = _GRADE_D
        for rel in dim["docs"]:
            p = root / rel
            try:
                if not p.exists():
                    continue
                if not p.is_file():
                    # 「存在但不是个可读文件」（被同名的目录/坏链接占位）必须留痕：
                    # 否则它在报告里与「根本没写」长得一样，作者查不出为什么判 D。
                    problems.append(f"{rel}:非文件(不可读)")
                    best = _GRADE_D
                    continue
                # A 优于 D：取「最高档」= ABCD 序号最小者，故用负序号做 max。
                best = max(best, _grade_doc(p.read_text(encoding="utf-8", errors="replace")),
                           key=lambda g: -"ABCD".index(g))
            except Exception as exc:      # noqa: BLE001 - 单份读不了不拖累其它维度
                problems.append(f"{rel}:{type(exc).__name__}")
                best = _GRADE_D
        grades.append({"role": dim["role"], "field": dim["field"],
                       "grade": best, "docs": list(dim["docs"])})
    return grades, ("结构无法判定：" + "；".join(problems) if problems else "")


def _ledger_conflicts(root: Path) -> list[dict]:
    """只读结构化账本，检出「同实体同属性不同取值」。不调模型、不做语义推理。"""
    from . import facts_ledger as fl

    items = fl.load(root)
    if not items:
        return []
    out: list[dict] = []
    seen: set = set()
    for c in fl.compare(items, items):
        key = (str(c["实体"]), str(c["属性"]),
               tuple(sorted([str(c["既有"]["取值"]), str(c["新的"]["取值"])])))
        if key in seen:
            continue
        seen.add(key)
        out.append({"entity": c["实体"], "attr": c["属性"],
                    "old": c["既有"]["取值"], "new": c["新的"]["取值"],
                    "status_pair": c["类型"]})
    return out


def world_readiness(root: Path | str, chapter: int) -> dict:
    """章 1 世界信息充足度（软门）。四契约键恒在；**任何异常都不抛**。

    异常时 `ok=True` + `error` 非空并照常放行——软门是提醒不是阻止，判定自身出问题
    不该演化成"作者写不了第一章"（规格第四节）。
    """
    result = {"applies": False, "ok": True, "missing": [], "draft_only": [],
              "conflicts": [], "error": ""}
    try:
        if int(chapter or 0) != 1:
            return result
        result["applies"] = True
        root = Path(root)
        grades, gerr = _world_dimension_grades(root)
        # 映射（需求裁定）：A 级维度判 C/D → missing；判 B → draft_only；判 A → 不出现。
        result["missing"] = [{"role": g["role"], "field": g["field"], "level": "A"}
                             for g in grades if g["grade"] in (_GRADE_C, _GRADE_D)]
        result["draft_only"] = [{"role": g["role"], "field": g["field"], "grade": "B"}
                                for g in grades if g["grade"] == _GRADE_B]
        if gerr:
            result["error"] = gerr
        result["conflicts"] = _ledger_conflicts(root)
        result["ok"] = not result["missing"] and not result["conflicts"]
    except Exception as exc:      # noqa: BLE001 - 判定自身出问题不得阻塞作者
        result.update(ok=True, missing=[], draft_only=[], conflicts=[],
                      error=f"{type(exc).__name__}: {exc}")
        import logging
        logging.getLogger("loom.world").warning("世界信息判定失败，按放行处理: %s", result["error"])
    return result


def world_block(root: Path | str, chapter: int) -> dict | None:
    """软门提示体：未达标时返回待确认结构，否则 None（与 foundation_block 对称）。"""
    r = world_readiness(root, chapter)
    if not r.get("applies") or r.get("ok"):
        return None
    return {"code": "world_incomplete", "missing": r["missing"],
            "draft_only": r["draft_only"], "conflicts": r["conflicts"],
            "问": WORLD_ASK}


def record_world_confirmation(root: Path | str, chapter: int, readiness: dict, *,
                              choice: str = "continue", now: str = "") -> bool:
    """追加一条开织确认记录（`外置大脑/设定集/开织确认记录.jsonl`，书间隔离）。

    只追加；失败返回 False 并留 warn，**绝不因为记不上就不让作者写**。
    """
    import json
    import logging
    import time
    try:
        from . import paths
        p = Path(root) / paths.SET_DIR / "开织确认记录.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": now or time.strftime("%Y-%m-%d %H:%M:%S"),
               "book": str(Path(root)), "chapter": int(chapter or 0),
               "foundation": "ok",
               "world_ok": bool((readiness or {}).get("ok")),
               "missing": (readiness or {}).get("missing") or [],
               "draft_only": (readiness or {}).get("draft_only") or [],
               "conflicts": (readiness or {}).get("conflicts") or [],
               "author_choice": choice}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:      # noqa: BLE001
        logging.getLogger("loom.world").warning("开织确认记录没写进去: %s", exc)
        return False
