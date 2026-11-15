"""创作旅程状态机:领航员访谈的阶段表 + 完成谓词 + 薄游标。

设计(docs/superpowers/specs/2026-07-10-journey-partner-design.md、ADR 0013):
- 出题以文件现状为准,不以问答历史为准;游标可丢弃(坏了当无,谓词重推)。
- 创作产出只落外置大脑 md(单一真相);游标只存不可派生的最少字段。
- 拓扑住代码侧表(同 agents.STEPS 模式),不下放用户可编辑文件。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .backends import LoomBackendError
from .fsutil import atomic_write_text
from .guard import DRAFT_SECTION, validate_output
from .parse import is_substantive, parse_journey_card
from .state import load_state, save_state

_MAX_QUESTIONS = 4   # 每段题数预算(交互从简;代码侧常量,不进 loom.toml)


@dataclass(frozen=True)
class StageSpec:
    key: str                  # 阶段名(面板展示 + 游标键)
    goal: str                 # 出题目标一句话,进领航员 user prompt
    reads: tuple[str, ...]    # 出题上下文(rel;也是缓存卡签名源;目录=读整目录)
    land: str                 # 落盘模式:field | sections | card_lines | seed
    target: str = ""          # field/card_lines 的目标文件;sections 的单文件形态
    target_dir: str = ""      # sections 的目录形态
    slot_order: tuple[str, ...] = ()   # 槽位扫描的容器 stem 优先序(round-robin 轮转用;空=按现有顺序)


STAGES: tuple[StageSpec, ...] = (
    StageSpec("立项", "问清这本书的定位:平台/分区/题材/对标意图/为什么选它",
              (paths.PROJECT_CARD_REL,), "field", target=paths.PROJECT_CARD_REL),
    StageSpec("世界观", "问出核心世界观:力量体系、金手指及其代价、关键地理与势力",
              (paths.WORLD_REL, paths.WORLD_DIR_REL), "sections",
              target=paths.WORLD_REL, target_dir=paths.WORLD_DIR_REL,
              slot_order=("一句话定位", "力量体系", "金手指", "地理与势力", "冰山真相")),
    StageSpec("人物", "问出主角与关键配角/反派:名字、动机、底牌、软肋",
              (paths.CHARS_REL, paths.CHARS_DIR_REL), "sections",
              target=paths.CHARS_REL, target_dir=paths.CHARS_DIR_REL,
              slot_order=("主角", "配角", "反派")),
    StageSpec("卷纲", "问出全书分几卷:每卷从第几章到第几章、该卷主线、卷内小高潮、卷末钩子",
              (paths.VOLUMES_REL,), "sections",
              target=paths.VOLUMES_REL, target_dir=f"{paths.BRAIN_DIR}/卷纲",
              slot_order=("第一卷", "第二卷", "第三卷")),
    StageSpec("卡章纲", "问出开局钩子、前 5 章一句话章纲、全书大弧(明确:这 5 章只许推进开局,"
                        "不许把大弧跑完;章纲按行写,后面几卷的章纲另批铺)",
              (paths.CARD_REL,), "card_lines", target=paths.CARD_REL),
    StageSpec("voice", "喂 2-3 段你的真实样本让指纹像你(走 seed,不出题)",
              (), "seed"),
)

_CARD_FIELDS = ("分区", "题材", "对标意图", "为什么选它")
_CARD_LINE_RE = re.compile(r"^-\s*第(\d+)章[:：][ \t]*\S", re.M)

# 每段给用户看的「这段要定什么」向导(区别于喂 LLM 的 spec.goal);面板副标题与降级卡都用它
_STAGE_HINT = {
    "立项": "这本书发哪个平台、什么分区题材、对标哪本书、为什么选它",
    "世界观": "力量体系、金手指及其代价、关键的地理与势力",
    "人物": "主角是谁(名字/动机/底牌/软肋),以及关键的配角、反派",
    "卷纲": "这本书分几卷、每卷从第几章到第几章、每卷要完成什么、卷末拿什么收",
    "卡章纲": "开局钩子、前几章一句话章纲、全书大弧(这几章只走开局,别提前收线)",
    "voice": "喂 2-3 段你写过的样本,让指纹更像你(可跳过)",
}

_NAME_SEP = ("·", "・", "•")   # 与 agents._NAME_SEP 同一口径:专名册只认带分隔符的「类型·名字」标题
_PROTAG_HEAD_RE = re.compile(r"^##\s*主角\s*[·・•]\s*\S", re.M)
_VOLUME_HEAD_RE = re.compile(r"^##\s*第\s*[0-9一二三四五六七八九十百]+\s*卷")


def _volumes_done(root: Path) -> bool:
    """卷纲做完的判据:至少一卷的「卷名」或「该卷主线」有实质。

    **不能用「文件非空」**:模板里自带 `## 第一卷` 和 `- 起止:第1-120章`,一判非空就永远算做完
    (真机实测过——建完书卷纲直接亮 ✓)。模板占位不算填过。
    """
    p = root / paths.VOLUMES_REL
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8")
    for chunk in re.split(r"^##\s+", text, flags=re.M)[1:]:
        head, _, body = chunk.partition("\n")
        if not _VOLUME_HEAD_RE.match(f"## {head.strip()}"):
            continue
        for line in body.splitlines():
            s = line.strip().lstrip("-").strip()
            key, _, val = s.partition(":")
            if key.strip() in ("卷名", "该卷主线") and is_substantive(val):
                return True
    return False
# 「门禁放」:卡章纲原来是 must,但它含「全书大弧」——
# 短篇和「只想先写一章试试」的作者根本用不上,却把他锁在门外。现在只留三格硬门槛:
# 立项(写给谁)、世界观(规矩)、人物(谁在走)。卷纲与卡章纲都变成「建议先打,可跳」。
# 跳过卡章纲时,写章前的 preflight 会提示「没有开篇钩子/前5章纲」——提醒但不拦。
_GATE_STAGES = ("立项", "世界观", "人物")   # voice / 卷纲 / 卡章纲 不进门禁


def _all_h2(text: str) -> list[tuple[str, str]]:
    """粗切 (H2标题, 段落体) 列表——救导入立项卡的非标准 H2(如「## 来自:xxx」)。"""
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"^##\s+", text, flags=re.M)[1:]:
        head, _, body = chunk.partition("\n")
        out.append((head.strip(), body))
    return out


def _stage_spec(key: str) -> StageSpec:
    for s in STAGES:
        if s.key == key:
            return s
    raise ValueError(f"未知阶段:{key}")


# ---- 完成谓词(全部文件派生,零存储) ----

def _h2_body(text: str, title: str) -> str:
    m = re.search(rf"^##\s*{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", text, flags=re.M | re.S)
    return m.group(1) if m else ""


def _project_card_done(root: Path) -> bool:
    """四格任一实质;或救导入——任一非模板 H2 段有实质(整卡兜底,不误吃模板占位/平台默认行)。"""
    p = root / paths.PROJECT_CARD_REL
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8")
    if any(is_substantive(_h2_body(text, f)) for f in _CARD_FIELDS):
        return True
    return any(is_substantive(body) for head, body in _all_h2(text) if head not in _CARD_FIELDS)


def _protagonist_done(root: Path) -> bool:
    """至少一张「主角·名字」实质卡;只填反派/未命名/占位不算(同专名册口径)。"""
    form = paths.brain_form(root, paths.CHARS_REL, paths.CHARS_DIR_REL)
    if form == "dir":
        return any(f.name != paths.GROWTH_NAME and f.stem.startswith("主角")
                   and any(s in f.stem for s in _NAME_SEP) and "未命名" not in f.stem
                   and is_substantive(f.read_text(encoding="utf-8"))
                   for f in paths.brain_dir_files(root, paths.CHARS_DIR_REL))
    if form == "file":
        p = root / paths.CHARS_REL
        return p.is_file() and bool(_PROTAG_HEAD_RE.search(p.read_text(encoding="utf-8")))
    return False


def _rel_has_content(root: Path, rel: str) -> bool:
    p = root / rel
    if p.is_dir():
        return any(f.name != paths.GROWTH_NAME and is_substantive(f.read_text(encoding="utf-8"))
                   for f in sorted(p.glob("*.md")))
    return p.is_file() and is_substantive(p.read_text(encoding="utf-8"))


def stage_done(root: Path, spec: StageSpec) -> bool:
    if spec.land == "seed":
        return load_state(root).get("fingerprint_source", "default") != "default"
    if spec.land == "field":
        return _project_card_done(root)
    if spec.land == "card_lines":
        p = root / spec.target
        if not p.is_file():
            return False
        text = p.read_text(encoding="utf-8")
        return bool(_CARD_LINE_RE.search(text)) or is_substantive(text)   # 放宽:段落式章纲也算
    if spec.key == "人物":
        return _protagonist_done(root)   # 硬判主角,面板与门禁同口径(只填反派不算过)
    if spec.key == "卷纲":
        return _volumes_done(root)       # 模板自带占位,不能拿「文件非空」当做完
    return any(_rel_has_content(root, rel) for rel in spec.reads)


# ---- 薄游标(挂 .loom_state.json 的 journey 键;整键可丢弃) ----

def _journey(st: dict) -> dict:
    j = st.get("journey")
    if not isinstance(j, dict):
        j = {}
    j.setdefault("skips", {})
    j.setdefault("asked", {})   # {stage: 已出题数}
    j.setdefault("card", None)  # 待答卡缓存 {stage,sig,question,options,...}
    j.setdefault("focus", "")   # goto 显式聚焦(空=按顺序派生)
    return j


def journey_state(root: Path) -> dict:
    st = load_state(root)
    j = _journey(st)
    stages = [{"key": s.key, "land": s.land, "hint": _STAGE_HINT.get(s.key, ""),
               "done": stage_done(root, s),
               "skipped": bool(j["skips"].get(s.key)),
               "asked": int(j["asked"].get(s.key, 0))}
              for s in STAGES]
    open_keys = [x["key"] for x in stages if not x["done"] and not x["skipped"]]
    focusable = [x["key"] for x in stages if not x["skipped"]]   # 回头改压过 done,只避 skip(I1)
    current = j["focus"] if j["focus"] in focusable else ""
    pending = j["card"] or {}
    if not current and pending.get("stage") in open_keys:
        current = pending["stage"]   # 未答的卡钉住本段(哪怕本段预算已满)
    if not current:
        nxt = next((x for x in stages if x["key"] in open_keys and x["asked"] < _MAX_QUESTIONS), None)
        current = nxt["key"] if nxt else ""
    card = j["card"] if (j["card"] and j["card"].get("stage") == current) else None
    return {"stages": stages, "current": current or None, "card": card}


def goto(root: Path, stage: str, *, skip: bool = False) -> dict:
    _stage_spec(stage)   # 未知段名即 ValueError
    if skip and stage in _GATE_STAGES:
        return goto(root, stage, skip=False)   # 门禁段不许跳过,静默降级为「聚焦本段」,不写 skip 标记
    view = journey_state(root)
    if not skip and stage == view["current"]:
        return view   # 已在本段:幂等返回,不作废待答卡、不清预算(防误点重复计费,I2)
    st = load_state(root)
    j = _journey(st)
    if skip:
        j["skips"][stage] = True
        if j["focus"] == stage:
            j["focus"] = ""
    else:
        j["skips"].pop(stage, None)
        j["focus"] = stage
        j["asked"][stage] = 0   # 回头改=重开本段预算
    if j["card"] and j["card"].get("stage") == stage:
        j["card"] = None        # 换段/跳段即作废待答卡
    st["journey"] = j
    save_state(root, st)
    return journey_state(root)


# ---- 领航员(第六个角色;项目文件优先、包内模板回退——同 deconstruct._load_skill 先例) ----

def _navigator_system(root: Path) -> str:
    from .agents import _parse_frontmatter   # 薄别名惯例(同 draft.py 之于 parse.py)
    local = root / "agents" / "领航员.md"
    path = local if local.exists() else Path(__file__).parent / "templates" / "agents" / "领航员.md"
    _, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return body


# ---- 出题 ----

_NAV_MAX_CHARS = 300   # 一张卡的输出预算(短步骤,防思考型模型吃空正文)


def _sig(root: Path, spec: StageSpec) -> str:
    """缓存卡签名 = 阶段源文件全文哈希;文件一动签名即失配 → 重出题(防旧卡问馊问题)。"""
    h = hashlib.sha256()
    for rel in spec.reads:
        p = root / rel
        files = sorted(p.glob("*.md")) if p.is_dir() else ([p] if p.is_file() else [])
        for f in files:
            h.update(f.read_text(encoding="utf-8").encode("utf-8"))
    return h.hexdigest()[:12]


def _stage_context(root: Path, spec: StageSpec) -> str:
    from .agents import _read_files, _noop   # 复用:剥占位、跳空文件、目录展开
    return _read_files(root, list(spec.reads), _noop)


def _premise(root: Path, spec: StageSpec) -> str:
    """给领航员的「这本书是关于什么」铺垫:建书一句话设定 + 已填的其它设定段(current 段除外,
    那份在『资料现状』里)。让选项贴这本书、顺着已定的往下问——而不是给崇祯故事出修真选项。"""
    from .agents import _read_files, _noop
    from .config import load_config
    parts: list[str] = []
    idea = (load_config(root).idea or "").strip()
    if idea:
        parts.append(f"【这本书的一句话设定】\n{idea}")
    other_reads: list[str] = []
    for s in STAGES:
        if s.key == spec.key or s.land == "seed":
            continue
        other_reads.extend(s.reads)
    established = _read_files(root, other_reads, _noop)   # 已剥占位/跳空文件;空段自然不出现
    if established.strip():
        parts.append(f"【已经定下的设定(顺着它问,别自相矛盾)】\n{established}")
    return "\n\n".join(parts)


def _nav_trace(root: Path, *, stage: str, sig: str, why: str, backend, raw: str, code: str = "") -> None:
    """出题降级才留痕(.审稿留痕/领航员留痕.md):现场证据,新条目在前,保最近 20 条。

    三条硬边界:绝不进 .loom_state.json(游标要薄且可丢弃,ADR 0013);绝不进外置大脑
    (会被 agent 读到污染设定);绝不被读回来派生状态(题从文件现状推导,不从问答历史推导)。
    留痕失败绝不影响出题(fire-and-forget,同 events 纪律)。"""
    from datetime import datetime
    try:
        p = root / paths.NAV_TRACE_REL
        old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""   # 人可改的文件:GBK 存回也不能让留痕从此静默全丢(读完即 UTF-8 重写,自愈)
        entries = [b for b in re.split(r"(?=^## )", old, flags=re.M) if b.startswith("## ")][:19]
        quoted = "\n".join("  > " + l for l in (raw.strip() or "(无原始回复)")[:500].splitlines())
        entry = (f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · {stage}\n"
                 f"- sig: {sig}\n- 结果: {why}\n"
                 f"- 后端: {type(backend).__name__} / {getattr(backend, 'model', '') or '—'}\n"
                 + (f"- code: {code}\n" if code else "")
                 + f"- 原始回复(截断 500 字):\n{quoted}\n\n")
        head = "# 领航员留痕\n\n> 出题失败的现场记录:查「为什么降级」用。loom 只追加、不读回;整个文件可删。\n\n"
        atomic_write_text(p, head + entry + "".join(entries))
    except Exception:
        pass


def next_card(root: Path, backend) -> dict:
    view = journey_state(root)
    cur = view["current"]
    if cur is None:
        return {"card": None, "state": view}
    spec = _stage_spec(cur)
    if spec.land == "seed":
        return {"card": {"stage": "voice", "static": "seed"}, "state": view}

    st = load_state(root)
    j = _journey(st)
    sig = _sig(root, spec)
    cached = j["card"]
    if cached and cached.get("stage") == cur and cached.get("sig") == sig and not cached.get("degraded"):
        return {"card": cached, "state": view}   # 降级卡不吃缓存,每次点「下一题」都真重试(I3)

    left = max(0, _MAX_QUESTIONS - int(j["asked"].get(cur, 0)))
    premise = _premise(root, spec)
    user = (f"阶段:{spec.key}\n目标:{spec.goal}\n这一段还能问 {left} 题。\n\n"
            + (premise + "\n\n" if premise else "")
            + f"资料现状:\n{_stage_context(root, spec) or '(全部空白)'}")
    parsed = None
    raw = ""
    why = ""       # 降级原因:backend_error|unparsed|few_options|false_exhausted(留痕+查因;成卡时为空)
    err_code = ""
    try:
        raw = backend.complete(_navigator_system(root), user, max_chars=_NAV_MAX_CHARS)
        parsed = parse_journey_card(raw)
        if parsed is None:
            why = "unparsed"
    except LoomBackendError as e:
        why, err_code = "backend_error", (e.code or "")   # 断网/超时 → 降级卡,旅程不卡死

    if parsed and parsed.get("exhausted"):
        if stage_done(root, spec):
            j["skips"][cur] = True   # 真做完了才跳;游标可丢,丢了最多重问一次
            j["card"] = None
            st["journey"] = j
            save_state(root, st)
            return next_card(root, backend) if journey_state(root)["current"] else \
                {"card": None, "state": journey_state(root)}
        parsed, why = None, "false_exhausted"   # 没做完却报无题 → 模型误判,降级兜底,别死锁

    if parsed and len(parsed["options"]) < 2:
        parsed, why = None, "few_options"   # 契约 2-4 个候选:独苗不成卡(否则烧预算+缓存钉死+无重试钮)

    if parsed:
        card = {"stage": cur, "sig": sig, "question": parsed["question"],
                "options": parsed["options"]}
        if "field" in parsed:
            card["field"] = parsed["field"]
        j["asked"][cur] = int(j["asked"].get(cur, 0)) + 1   # 只有成卡才烧预算
    else:
        card = {"stage": cur, "sig": sig, "options": [], "degraded": True, "why": why,
                "question": f"「{spec.key}」要定:{_STAGE_HINT.get(cur, spec.key)}。领航员这次没出上题,直接写你想定的。"}
        _nav_trace(root, stage=cur, sig=sig, why=why, backend=backend, raw=raw, code=err_code)
    j["card"] = card
    st["journey"] = j
    save_state(root, st)
    return {"card": card, "state": journey_state(root)}


# ---- 答案落盘(作者拍板过的答案=人写主体;人写优先,答案绝不丢) ----

_DIGEST_SYSTEM = (
    "你是写作伙伴的记录员。把作者对一个创作问题的回答,整理成要落进资料文件的正式文本。\n"
    "红线:只整形作者说的内容,绝不添加作者没说的设定事实;不加客套、不加解释。\n"
    "输出格式严格按任务里的要求。"
)
_DIGEST_MAX_CHARS = 400


def land_answer(root: Path, answer: str, backend) -> dict:
    answer = (answer or "").strip()
    if not answer:
        raise ValueError("答案是空的。")
    st = load_state(root)
    j = _journey(st)
    card = j["card"]
    if not card or "static" in card:
        raise ValueError("没有待答的问题卡,先出题。")
    spec = _stage_spec(card["stage"])
    if spec.land == "field":
        landed = _land_field(root, card.get("field", ""), answer)
    elif spec.land == "sections":
        landed = _land_sections(root, spec, card["question"], answer, backend)
    else:   # card_lines
        landed = _land_card_lines(root, card["question"], answer, backend)
    j["card"] = None
    st["journey"] = j
    save_state(root, st)
    result = {"landed": landed, "state": journey_state(root)}
    if spec.key == "卷纲":
        from . import volplan
        parsed = [v for v in volplan.volume_ranges(root) if v.get("起") and v.get("止")]
        if not parsed:
            result["warning"] = ("卷纲已落盘,但没解析到「- 起止:第A-B章」;"
                                 "补上这一行,章节树才会出现分卷。")
    return result



def _replace_h2_body(text: str, title: str, new_body: str) -> str:
    """占位格 → 换成答案;已有人写内容 → 在格尾追加(人写优先);缺格 → 文末补格。"""
    old = _h2_body(text, title)
    if not re.search(rf"^##\s*{re.escape(title)}\s*$", text, flags=re.M):
        return text.rstrip() + f"\n\n## {title}\n{new_body}\n"
    if is_substantive(old):
        replacement = old.rstrip() + f"\n\n{new_body}\n"
    else:
        replacement = f"\n{new_body}\n"
    return re.sub(rf"(^##\s*{re.escape(title)}\s*$).*?(?=^##\s|\Z)",
                  lambda m: m.group(1) + replacement + "\n", text, count=1, flags=re.M | re.S)


# ⚠️ 卡片机 legacy 落盘链与伙伴 `_land_slot`(line 分支)共用此 helper——退役卡片机时勿删。
def _land_field(root: Path, field: str, answer: str) -> str:
    p = root / paths.PROJECT_CARD_REL
    text = p.read_text(encoding="utf-8") if p.is_file() else "# 立项卡\n"
    if field == "平台":
        # 平台行整行替换是裁量:答题即作者本人拍板换值(区别于 H2 格的人写只追加)
        text, n = re.subn(r"^平台[:：].*$", f"平台:{answer}", text, count=1, flags=re.M)
        if not n:
            text = text.rstrip() + f"\n\n平台:{answer}\n"
    else:
        if field not in _CARD_FIELDS:
            field = "题材"   # 领航员出了怪格名 → 落最通用的格,答案绝不丢
        text = _replace_h2_body(text, field, answer)
    atomic_write_text(p, text)
    return paths.PROJECT_CARD_REL


_SLOT_ROW_FILL_RE_TMPL = r"^(-\s*{key}\s*(?:[(（][^)）]*[)）])?\s*[:：])(.*)$"

# 卷纲的可解析起止行(与 volplan._RANGE 同口径:引擎按它切卷)
_VOL_RANGE_RE = re.compile(r"^\s*-\s*起止\s*[:：]\s*第?\s*(\d+)\s*[-–—~～至到]\s*第?\s*(\d+)\s*章?", re.M)
_VOL_NUM_RE = re.compile(r"第?\s*(\d+)\s*章?\s*[-–—~～至到]\s*第?\s*(\d+)\s*章")


def _volume_body(key: str, answer: str, *, fallback: str = "") -> str:
    """卷纲每卷的落盘体:保证有一行可解析的「- 起止:第A-B章」。

    digest/模型没写起止时,先从本节找「第A-B章/第A章到第B章」区间机械补上,
    再从原始答案找;连区间都没有才明确提示。绝不静默落一节没有起止的卷
    —— 那会让章节树永远显示「卷纲缺起止」。
    """
    ans = (answer or "").strip()
    if _VOL_RANGE_RE.search(ans):
        return ans
    for src in (ans, fallback or ""):
        m = _VOL_NUM_RE.search(src)
        if m:
            return f"- 起止:第{m.group(1)}-{m.group(2)}章\n{ans}"
    return ans + "\n\n> 这卷还没写「- 起止:第A-B章」;补上这一行,分卷树才会立起来。"



def _land_slot(root: Path, slot_id: str, answer: str) -> str:
    """按 slot 的 at 类型定址落盘。slot_id = "<容器rel>#<键>"。守落盘三铁律。

    P1 覆盖 line/h2/row;filename/file 见 Task 5。slot_id 必须来自 stage_slots(防越界)。"""
    from .slots import stage_slots
    answer = answer.strip()
    if not answer:
        raise ValueError("答案不能为空")
    rel, _, key = slot_id.partition("#")
    # 从 slot 集合定位 at(单一真相:落点类型由扫描器说了算,不在这里重新推断)
    slot = next((s for spec in STAGES for s in stage_slots(root, spec) if s.id == slot_id), None)
    if slot is None:
        raise ValueError(f"未知槽位:{slot_id}")
    p = root / rel
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    if slot.at == "line":       # 平台行:复用 _land_field 的整行替换裁量
        return _land_field(root, "平台", answer)
    if slot.at == "h2":
        # 卷纲必须带可解析起止(否则章节树永远「缺起止」);其余 H2 落点原样。
        body = _volume_body(key, answer) if rel == paths.VOLUMES_REL else answer
        atomic_write_text(p, _replace_h2_body(text, key, body))
        return rel

    if slot.at == "row":
        pat = re.compile(_SLOT_ROW_FILL_RE_TMPL.format(key=re.escape(key)), re.M)
        if slot.key.startswith("第") or key == "大弧":   # 卡章纲行走既有 _apply_card_lines(内部已落盘)
            return _apply_card_lines(root, f"- {key}:{answer}", fallback=answer)
        new, n = pat.subn(lambda m: f"{m.group(1)}{answer}", text, count=1)
        if not n:   # 理论兜底:守卫已保证槽位存在→行通常在;仅防扫描与落盘间文件被并发改(TOCTOU)
            new = text.rstrip() + f"\n- {key}:{answer}\n"
        atomic_write_text(p, new)
        return rel
    if slot.at == "filename":
        from .draft import _FN_BAD
        name = _FN_BAD.sub("·", answer.splitlines()[0].strip())[:12] or "未命名"
        role = Path(rel).stem.split("·")[0].split("・")[0].split("•")[0]
        target = p.with_name(f"{role}·{name}.md")
        if target.exists() and is_substantive(target.read_text(encoding="utf-8", errors="replace")):
            raise ValueError(f"已经有一个叫「{name}」的{role}了——换个名字,或先合并那张卡")
        if target.exists():   # 占位残卡,让位
            target.unlink()
        p.rename(target)
        return str(target.relative_to(root))
    if slot.at == "file":
        joiner = "\n" if text.endswith("\n") else "\n\n"
        atomic_write_text(p, (text.rstrip() + joiner + answer + "\n") if text.strip() else f"{answer}\n")
        return rel
    raise ValueError(f"P1 未支持的落点类型:{slot.at}(filename/file 见 Task 5)")


def _land_new_slot(root: Path, slot_id: str, answer: str) -> str:
    """落一个**地基卡里原来没有**的新项()。

    两种形状,由 `slots.plan_new_slot` 判定:
    - `h2`  —— 文件已在,在里面**追加一节**(`## 小节名` + 正文);
    - `file`—— 文件不在,新建到 `外置大脑/设定集/50-自定义/<名字>.md`(可带 `## 小节`)。

    只追加/新建,**永不覆盖**已有正文:同一个小节名若在落盘前被别人抢先写了(TOCTOU),
    就当新内容追加在末尾,不替换任何人的字。落盘后与老路径一样返回相对路径。
    """
    from .slots import plan_new_slot

    answer = answer.strip()
    if not answer:
        raise ValueError("内容不能为空")
    plan = plan_new_slot(root, slot_id)
    rel, key = plan["rel"], plan["key"]
    p = root / rel
    if plan["kind"] == "h2":
        text = p.read_text(encoding="utf-8", errors="replace")
        block = f"## {key}\n{answer}\n"
        atomic_write_text(p, text.rstrip() + "\n\n" + block if text.strip() else block)
        return rel
    # kind == "file":新建。目录可能还不存在(50-自定义 第一次用时)。
    p.parent.mkdir(parents=True, exist_ok=True)
    title = Path(rel).stem
    if key:
        body = f"# {title}\n\n## {key}\n{answer}\n"
    else:
        body = f"# {title}\n\n{answer}\n"
    if p.exists():   # 极小窗口:提案挂起期间别人已经建了同名文件 → 追加,不覆盖
        old = p.read_text(encoding="utf-8", errors="replace")
        extra = f"## {key}\n{answer}\n" if key else answer + "\n"
        atomic_write_text(p, old.rstrip() + "\n\n" + extra)
        return rel
    atomic_write_text(p, body)
    return rel


def _land_fingerprint(root: Path | str, section: str, content: str) -> str:
    """把一条「指纹增补」落进 `外置大脑/写作指纹.md` 的指定小节()。

    为什么单独一条落盘链(而不是复用 `_land_slot` / `_land_new_slot`):
    写作指纹不在设定集里,也不接受「追加一个 H2」那种形状 —— 它的小节是**固定的五节**,
    多一节就没人读了。所以这里只做一件事:**把那几行插进已有小节的末尾**,
    老内容一个字不动(只增不删,和 learn 同一条铁律),写盘前过 `guard_write(FINGERPRINT)`,
    指纹万一被搞坏就整次不写、旧文件原样留着。

    `content` 是模型给的多行文本:anchor 例句会逐行变成 `> 句子`,其余小节逐行变成 `- 规则`。
    """
    from .fingerprint import neutral_default, resolve_section
    from .guard import FINGERPRINT, guard_write

    root = Path(root)
    title = resolve_section(section)
    body = _fingerprint_lines(content, title == "anchor 例句")
    if not body:
        raise ValueError("改指纹的「内容」是空的,没有可落的行。")
    path = root / paths.FINGERPRINT_REL
    old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else neutral_default()
    new = _insert_into_section(old, title, body)
    if new == old:
        raise ValueError(f"这几行已经在「{title}」里了,不用重复落。")
    guard_write(path, new, FINGERPRINT)     # 校验不过就抛,旧指纹原样保留
    return paths.FINGERPRINT_REL


def _fingerprint_lines(content: str, quote: bool) -> list[str]:
    """多行内容 → 指纹里的行(anchor 用 `>`,其余用 `-`;空行/已有标记清理掉)。"""
    out: list[str] = []
    for raw in (content or "").splitlines():
        s = raw.strip().lstrip("->*•").strip()
        if not s or s.startswith(("(", "（")):    # 「(待填充)」这类占位不是规则
            continue
        out.append(f"> {s}" if quote else f"- {s}")
    return out


def _find_section_span(lines: list[str], title: str) -> tuple[int, int] | None:
    """在指纹正文里定位某个小节的 [标题行下标, 结尾下标(不含)]。

    标题按 `resolve_section` 认(老书的标题可能没有「 / 高频表达」后缀),取第一处。
    """
    from .fingerprint import resolve_section

    start = -1
    for i, line in enumerate(lines):
        t = line.strip()
        if not t.startswith("## "):
            continue
        if start == -1:
            try:
                if resolve_section(t[3:].strip()) == title:
                    start = i
                    continue
            except ValueError:
                continue
        else:
            return start, i            # 走到下一节了
    if start == -1:
        return None
    return start, len(lines)


def _insert_into_section(text: str, title: str, body: list[str]) -> str:
    """把 body 插到该小节最后一行实质内容之后;小节不存在就在末尾补一节。

    幂等:已经在那一节里的行先剔掉,剔完没剩就原样返回(= 不写盘)。
    老内容只增不删,也不重排 —— 作者手写的顺序不许被模型打乱。
    """
    lines = text.rstrip("\n").splitlines()
    span = _find_section_span(lines, title)
    if span is None:
        tail = [f"## {title}"] + body
        return "\n".join(lines + ([""] if lines else []) + tail) + "\n"
    start, end = span
    present = {l.strip() for l in lines[start + 1:end]}
    fresh = [b for b in body if b not in present]
    if not fresh:
        return text
    # 从节尾往前退,跳过空行,插在最后一行内容后面
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    return "\n".join(lines[:at] + fresh + lines[at:]) + "\n"


def _digest(backend, question: str, answer: str, format_ask: str) -> str:
    """一次消化调用;失败或过不了 guard 返回空串(调用方落原答案兜底)。"""
    user = f"问题:{question}\n作者的回答:{answer}\n\n{format_ask}只用作者给的信息。"
    try:
        body = backend.complete(_DIGEST_SYSTEM, user, max_chars=_DIGEST_MAX_CHARS)
    except LoomBackendError:
        return ""
    return "" if validate_output(body, DRAFT_SECTION) else body


_H2_SPLIT_RE = re.compile(r"^(?=## )", re.M)


def _split_h2(body: str) -> list[str]:
    """消化产物 → H2 节列表(每项含「## 标题」头);H2 前导语丢弃,与 _write_sections_into_dir 口径一致。"""
    return [p for p in _H2_SPLIT_RE.split(body) if p.strip().startswith("## ")]


def _fallback_title(question: str) -> str:
    return re.sub(r'[\\/:*?"<>|。,]', "", question)[:12] or "访谈补充"


def _land_sections(root: Path, spec: StageSpec, question: str, answer: str, backend) -> str:
    # 卷纲是固定单文件 + 每卷 h2 + 「- 起止:」行的形态,单独走一条落盘链;
    # 通用目录/文件形态要么把卷写散成独立文件,要么丢起止。
    if spec.key == "卷纲":
        return _land_volume_sections(root, spec, question, answer, backend)
    from .draft import _write_sections_into_dir   # 只写空白/模板文件,人写的一律不碰

    body = _digest(backend, question, answer,
                   "整理成 markdown:每个主题一节,以「## 标题」开头(标题即文件名,如「## 金手指」「## 主角·林潜」),标题下写正文。")
    if not body:
        body = f"## {_fallback_title(question)}\n{answer}"
    segs = _split_h2(body)
    if not segs:   # 消化产物没按格式出 H2 → 整段当一节,绝不丢
        segs = [f"## {_fallback_title(question)}\n{body.strip()}"]
    form = paths.brain_form(root, spec.target, spec.target_dir)
    if form != "file":
        written: list[str] = []
        leftover: list[str] = []
        for seg in segs:   # 逐节落盘:哪节撞上人写成品,哪节进兜底——绝不整批丢
            got = _write_sections_into_dir(root, spec.target_dir, "\n" + seg,
                                           drop_unnamed=(spec.key == "人物"))
            if got:
                written.extend(got)
            else:
                leftover.append(seg.strip())
        rel = f"{spec.target_dir}/访谈补充.md"      # 同名文件已是人写成品 → 兜底追加,绝不覆盖
        if leftover:
            p = root / rel
            old = p.read_text(encoding="utf-8") if p.is_file() else "# 访谈补充\n"
            atomic_write_text(p, old.rstrip() + "\n\n" + "\n\n".join(leftover) + "\n")
        if written:
            return f"{spec.target_dir}/{written[0]}.md"
        return rel
    p = root / spec.target                          # 单文件形态的老书
    old = p.read_text(encoding="utf-8") if p.is_file() else ""
    atomic_write_text(p, (old.rstrip() + "\n\n" if old.strip() else "") + body.strip() + "\n")
    return spec.target


def _land_volume_sections(root: Path, spec: StageSpec, question: str, answer: str, backend) -> str:
    """领航员答完卷纲 → 落进卷纲单文件,每卷一节且带可解析起止。

    与普通 sections 的差异:卷纲只认一个文件（`外置大脑/卷纲.md`）,每卷 `## 第N卷`,
    节内必须有 `- 起止:第A-B章`;重复回答同一卷走 `_replace_h2_body`（人写只追加,不新开重复节）。
    """
    first_title = spec.slot_order[0] if spec.slot_order else "第一卷"
    body = _digest(
        backend, question, answer,
        "整理成 markdown:每卷一节,以「## 第N卷」开头(例如「## 第一卷」);"
        "每节第一行必须写「- 起止:第A-B章」,其后可写「- 卷名:」「- 该卷主线:」"
        "「- 卷内节奏:」「- 卷末钩子:」。不要输出其它标题层级,不要写解释。")
    if not body:
        body = f"## {first_title}\n{answer}"
    segs = _split_h2(body)
    if not segs:   # 消化产物没按格式出 H2 → 整段当第一卷,绝不丢
        segs = [f"## {first_title}\n{body.strip()}"]
    p = root / spec.target
    text = p.read_text(encoding="utf-8") if p.is_file() else "# 卷纲\n"
    for seg in segs:
        head, _, rest = seg.partition("\n")
        key = head[3:].strip() if head.startswith("## ") else first_title
        text = _replace_h2_body(text, key, _volume_body(key, rest, fallback=answer))
    atomic_write_text(p, text)
    return spec.target


def _bulleted(answer: str) -> str:
    """答案逐行 bullet 化,兜底用(消化异常/过不了 guard 两处同用)。"""
    return "\n".join(f"- {l.strip()}" for l in answer.splitlines() if l.strip())


# ⚠️ 卡片机 legacy 落盘链与伙伴 `_land_slot`(row 分支,章纲行)共用此 helper——退役卡片机时勿删。
def _apply_card_lines(root: Path, body: str, *, fallback: str = "") -> str:
    """把已整形的「- 第N章:…」body 落卡章纲(填空章行/追加缺章行/非章行精确判重;人写行绝不覆盖)。
    啥都没落且 fallback 非空 → 追加 _bulleted(fallback) 兜底(land_answer 传原 answer 保「答案绝不丢」;
    诊断候选传空=撞车即丢弃,不强塞 AI 内容)。"""
    p = root / paths.CARD_REL
    text = p.read_text(encoding="utf-8") if p.is_file() else "# 卡章纲\n"
    landed_any = False   # 消化产物哪怕不合规(散文/漏行),答案也绝不静默丢(C1)
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("- ") or line == "-":
            continue
        m = re.match(r"^-\s*第(\d+)章[:：]\s*(.*)$", line)
        if m and m.group(2).strip():
            n, content = m.group(1), m.group(2).strip()
            empty_pat = re.compile(rf"^-\s*第{n}章[:：]\s*$", re.M)
            if empty_pat.search(text):
                text = empty_pat.sub(f"- 第{n}章:{content}", text, count=1)
                landed_any = True
            elif not re.search(rf"^-\s*第{n}章[:：]\s*\S", text, flags=re.M):
                text = text.rstrip() + f"\n- 第{n}章:{content}\n"
                landed_any = True
            # 已有人写内容的章行 → 跳过,绝不覆盖
        elif re.search(rf"^{re.escape(line)}\s*$", text, flags=re.M) is None:
            # 整行精确匹配判重(而非子串):新行是已有行前缀时不再被误吞
            text = text.rstrip() + f"\n{line}\n"
            landed_any = True
    if not landed_any and fallback.strip():
        text = text.rstrip() + f"\n{_bulleted(fallback)}\n"
    atomic_write_text(p, text)
    return paths.CARD_REL


def _land_card_lines(root: Path, question: str, answer: str, backend) -> str:
    body = _digest(backend, question, answer,
                   "整理成卡章纲行:每行「- 第N章:这章完成什么+章末钩子」;不属于具体某章的规划(如全书大弧),输出「- 大弧:一句话」。")
    if not body:
        body = _bulleted(answer)
    return _apply_card_lines(root, body, fallback=answer)   # 答案绝不丢:兜底用原 answer,不是 digest 产物


def writing_unlocked(root: Path) -> tuple[bool, list[str]]:
    """起书完整性:立项+世界观+主角+卡章纲 是否齐;返回 (解锁?, 缺项 STAGES key 列表)。纯文件派生。"""
    missing = [s.key for s in STAGES if s.key in _GATE_STAGES and not stage_done(root, s)]
    return (not missing, missing)
