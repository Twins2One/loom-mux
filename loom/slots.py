"""槽位扫描器:把外置大脑的骨架行读成可寻址的槽位(容器×键)。

槽位真相 = 书里的文件正文,不是代码侧的表:作者删掉一行骨架,那个槽就没了。
纯派生、零存储、零模型、每次现算。五种 at:line/h2/row(本文件)+ filename/file(见下)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .journey import StageSpec, _CARD_FIELDS, _h2_body
from .parse import _EMPTY_ROW_RE, _PAREN_SPAN_RE, is_substantive

_ROW_RE = re.compile(r"^-\s*([^:：(（]+?)\s*(?:[(（][^)）]*[)）])?\s*[:：](.*)$")
# 捕获:键(冒号/括注前的名)、括注后冒号后的正文。hint 另取括注原文。
_HINT_RE = re.compile(r"[(（]([^)）]*)[)）]")
_PLATFORM_RE = re.compile(r"^平台[ \t]*[:：][ \t]*(.*)$", re.M)

# 立项卡四个 H2 格的 hint(素材来自模板占位 loom/templates/外置大脑/立项卡.md,提炼成短句):
# 模板本身对这四格没有像 row 槽那样的行内括注可抓,之前一直是空 hint——真机暴露的缺陷正是
# 由此而来(模型不知道「分区」是什么,把平台名塞进分区槽)。
_CARD_FIELD_HINTS = {
    "分区": "投稿分区/题材归类(如玄幻·东方玄幻),不是平台",
    "题材": "核心题材标签,如重生+复仇+宗门流",
    "对标意图": "想写成哪本书那种爽感节奏",
    "为什么选它": "选这个定位的理由/初心备忘",
}


def _preview(val: str) -> str:
    return val.strip()[:24]


def _row_slots(root: Path, rel: str) -> list[Slot]:
    """一个骨架行文件 → row 槽(每 - 键: 行一个)。"""
    p = root / rel
    if not p.is_file():
        return []
    out: list[Slot] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        key = m.group(1).strip()
        val = m.group(2).strip()
        # hint 只从「键到冒号」的模板部分找括注,不进冒号后的用户值——用同一次 _ROW_RE 匹配
        # 定位那个终结键的冒号(m.start(2) 前一个字符),不用字符串切分:括注内本就可能含冒号
        # (如「核心功能(补主角哪块短板:资源…):」),按冒号切分会把这类括注切断。
        before_colon = line[:m.start(2) - 1]
        hm = _HINT_RE.search(before_colon)
        # 「空表单行」判定复用 is_substantive 的口径:剥括注后冒号后无实字 = 空
        filled = bool(_PAREN_SPAN_RE.sub("", line).split("：")[-1].split(":")[-1].strip()) or bool(val)
        out.append(Slot(id=f"{rel}#{key}", label=key[:10], container=rel, at="row",
                        key=key, hint=hm.group(1).strip() if hm else "",
                        filled=filled, preview=_preview(val)))
    return out


@dataclass(frozen=True)
class Slot:
    id: str        # "<容器rel>#<键>"
    label: str     # ≤10 字,实体容器带前缀
    container: str # 容器文件 rel
    at: str        # "line"|"h2"|"row"|"filename"|"file"
    key: str       # 行/H2 键;filename="@name";file="@body"
    hint: str      # 模板括注原文(喂 prompt,不上屏)
    filled: bool
    preview: str   # 已填值前 24 字


def _project_slots(root: Path) -> list[Slot]:
    """立项卡:平台行(line)+ 四个 H2(h2)。"""
    rel = paths.PROJECT_CARD_REL
    p = root / rel
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    out: list[Slot] = []
    for f in _CARD_FIELDS:
        body = _h2_body(text, f)
        out.append(Slot(id=f"{rel}#{f}", label=f, container=rel, at="h2", key=f,
                        hint=_CARD_FIELD_HINTS.get(f, ""), filled=is_substantive(body), preview=_preview(body)))
    # 平台挪到立项最后一格(FB-平台):建书不再逼选/预填平台,领航员先聊分区/题材/对标(创作的),
    # 平台留最后、可选可跳(它只影响违禁词自检松紧,非阻断;作者不填就用默认档)。
    pm = _PLATFORM_RE.search(text)
    plat_val = pm.group(1).strip() if pm else ""
    out.append(Slot(id=f"{rel}#平台", label="平台", container=rel, at="line", key="平台",
                    hint="发哪个平台(可选)", filled=bool(plat_val), preview=_preview(plat_val)))
    return out


def _container_slots(root: Path, rel: str, *, entity: bool) -> list[Slot]:
    """一个容器文件 → 槽位。entity=True(人物)时未命名出 filename 槽、压住 row。"""
    p = root / rel
    if not p.is_file():
        return []
    stem = Path(rel).stem
    text = p.read_text(encoding="utf-8", errors="replace")
    if entity and "未命名" in stem:
        role = stem.split("·")[0].split("・")[0].split("•")[0]
        return [Slot(id=f"{rel}#@name", label=f"给{role}起个名字"[:10], container=rel,
                     at="filename", key="@name", hint="改文件名为「类型·名字」,逐字直送写手",
                     filled=False, preview="")]
    rows = _row_slots(root, rel)
    if rows:
        if entity:   # 实体容器:label 带名字前缀「林潜 · 软肋」
            name = stem
            for sep in ("·", "・", "•"):
                if sep in name:
                    name = name.split(sep, 1)[-1]
                    break
            rows = [Slot(id=s.id, label=f"{name} · {s.key}"[:10], container=s.container,
                         at=s.at, key=s.key, hint=s.hint, filled=s.filled, preview=s.preview)
                    for s in rows]
        return rows
    # 无 row、无可寻址骨架 → file 兜底
    quote = next((l[1:].strip() for l in text.splitlines() if l.startswith(">")), "")
    return [Slot(id=f"{rel}#@body", label=stem[:10], container=rel, at="file", key="@body",
                 hint=quote, filled=is_substantive(text), preview=_preview(text))]


def _dir_container_slots(root: Path, dir_rel: str, order: tuple[str, ...], *, entity: bool) -> list[Slot]:
    """目录段(世界观/人物):按 order 收各容器槽,再 round-robin 交错未填槽。"""
    per: list[list[Slot]] = []
    files = [f for f in paths.brain_dir_files(root, dir_rel) if f.name != paths.GROWTH_NAME]
    def rank(f: Path) -> int:
        for i, stem in enumerate(order):
            if f.stem.startswith(stem):
                return i
        return len(order)
    for f in sorted(files, key=rank):
        per.append(_container_slots(root, f"{dir_rel}/{f.name}", entity=entity))
    # round-robin:逐容器取第 k 个,交错拼;filled 与未填分开(未填优先展示由消费方决定,这里只保稳定序)
    out: list[Slot] = []
    for k in range(max((len(x) for x in per), default=0)):
        for x in per:
            if k < len(x):
                out.append(x[k])
    return out


def _volume_slots(root: Path, spec: StageSpec) -> list[Slot]:
    """卷纲:按 StageSpec.slot_order 合成可寻址的 H2 槽（第一卷/第二卷/第三卷）。

    卷纲是「每卷一节 + 节内 `- 起止:第A-B章`」的固定形态,不能走行式/文件兜底:
    没有槽位时大纲师无法「提设定」,领航员落盘也只能生成无起止的散节。
    """
    p = root / paths.VOLUMES_REL
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    order = spec.slot_order or ("第一卷", "第二卷", "第三卷")
    out: list[Slot] = []
    for f in order:
        body = _h2_body(text, f)
        out.append(Slot(id=f"{paths.VOLUMES_REL}#{f}", label=f[:10], container=paths.VOLUMES_REL,
                        at="h2", key=f, hint="每卷写:- 起止:第A-B章 / - 卷名: / - 该卷主线:",
                        filled=is_substantive(body), preview=_preview(body)))
    return out


def stage_slots(root: Path, spec: StageSpec) -> list[Slot]:
    if spec.key == "立项":
        return _project_slots(root)
    if spec.key == "世界观":
        return _dir_container_slots(root, paths.WORLD_DIR_REL, spec.slot_order, entity=False)
    if spec.key == "人物":
        return _dir_container_slots(root, paths.CHARS_DIR_REL, spec.slot_order, entity=True)
    if spec.key == "卡章纲":
        return _row_slots(root, paths.CARD_REL)   # 章行「- 第N章:」/「- 大弧:」都是 row 形态
    if spec.key == "卷纲":
        return _volume_slots(root, spec)
    return []   # voice:P2 需要时再加



# ─────────────────────── 新落点（地基卡里没有的项）───────────────────────────────
# :「很多角色想写设定地基范围外的项目时,限于地基项有限无法落盘,
# 同时又无法自动生成对应项的位置」⇒ 允许**按作者的想法长出新项**,两条路:
#   A. 已有文件里**新加一节**:`<设定集里已有的 .md>#<新小节名>` → 末尾追加 `## 小节名`
#   B. **新建一份文件**:`外置大脑/设定集/50-自定义/<名字>.md#<小节名|@body>`
# 两条都只出候选卡,作者拍板才落盘(与提设定同一条红线);落盘在 journey._land_new_slot。
_ALLOWED_NEW_ROOT = paths.SET_DIR + "/"
_NEW_KEY_MAX = 24
# 行式骨架容器不接受「追加 H2」:它们的结构是「- 键: 值」的行,塞 H2 会把结构搅乱
# (人物卡还要靠 H2 认人名)。这些文件里想加东西,应该走 B 新建一份。
_ROW_SKELETON_NAMES = {"00-立项.md", "20-人物卡.md", "卡章纲.md", "卷纲.md", "全书大纲.md",
                       "写作指纹.md", "状态账本.md", "文风参考.md", "违禁词.md"}


def _check_new_rel(rel: str) -> None:
    """新落点的路径体检。只放行「设定集内的 .md」,其余一律人话拒绝。"""
    if "\\" in rel:
        raise ValueError("落点路径要用正斜杠「/」,不要用反斜杠。")
    # 2026-10-25:报告类产物落 外置大脑/分析/(分析目录同样放行)
    if not (rel.startswith(_ALLOWED_NEW_ROOT) or rel.startswith(paths.ANALYSIS_DIR_REL + "/")):
        raise ValueError(f"新项只能落在「{paths.SET_DIR}/」里面;正文、agents、别的目录都不行。")
    if not rel.endswith(".md"):
        raise ValueError("新项必须是 .md 文件。")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("落点路径里有空段或「..」,不允许。")
    if any(p.startswith(".") for p in parts):
        raise ValueError("落点路径里不能有以「.」开头的段(那是引擎内部目录)。")
    if len(Path(rel).stem) > _NEW_KEY_MAX:
        raise ValueError(f"文件名太长了(最多 {_NEW_KEY_MAX} 字),起短一点。")


def plan_new_slot(root: Path, slot_id: str) -> dict:
    """把「地基里没有的落点」判成一个可新建的计划;不合法抛 ValueError(人话)。

    返回 {"kind": "h2"|"file", "rel": ..., "key": ...}。
    """
    rel, _, key = str(slot_id or "").partition("#")
    key = key.strip()
    _check_new_rel(rel)
    p = root / rel
    # 人物目录里不许凭空造文件:那边靠「类型·名字.md」的文件名当专名册,乱建会污染名册。
    if rel.startswith(paths.SET_CHARS_DIR_REL + "/"):
        raise ValueError("人物不要直接新建文件(会污染专名册)——要加人物,用「看地基」里那张"
                         "「给某人起个名字」的槽;要记别的东西,落 50-自定义。")
    if p.exists():
        # A 类:文件已在 → 只能在里面**新加一节**(已有的节走正常提设定)。
        if not p.is_file():
            raise ValueError("这个落点是个目录,不是文件。")
        if p.name in _ROW_SKELETON_NAMES:
            raise ValueError(f"「{p.name}」是「- 键: 值」这种行式骨架,不能往里塞小节;"
                             f"想加新东西请新建到 {paths.SET_CUSTOM_DIR_REL}/。")
        if not key or key == "@body":
            raise ValueError(f"「{p.name}」已经存在;要往里加内容,得给一个新小节名"
                             f"(写成 {rel}#小节名)。")
        return {"kind": "h2", "rel": rel, "key": key}
    # B 类:文件不存在 → 只允许在 50-自定义 里新建
    # 2026-10-25:报告类产物(战斗设计/质量报告/问题清单)落在 外置大脑/分析/,
    # 与语料分析、情绪曲线同一个「派生分析」目录 —— 不污染设定集这个事实源。
    if rel.startswith(paths.ANALYSIS_DIR_REL + "/"):
        stem = Path(rel).stem
        if not stem:
            raise ValueError("新项要有名字。")
        return {"kind": "file", "rel": rel, "key": key if key and key != "@body" else ""}
    if not rel.startswith(paths.SET_CUSTOM_DIR_REL + "/"):
        raise ValueError(f"新建文件只能建在「{paths.SET_CUSTOM_DIR_REL}/」下面;"
                         f"其它分区要先有那份文件(或用「看地基」里的现成落点)。")
    stem = Path(rel).stem
    if stem in ("", ".", ".."):
        raise ValueError("新项要有名字(文件名去掉 .md 不能是空)。")
    return {"kind": "file", "rel": rel, "key": key if key and key != "@body" else ""}

