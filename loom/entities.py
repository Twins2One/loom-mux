"""实体词典(R2):角色 / 地点 / 物品 / 功法 / 组织,每个实体有稳定 id、正名、别名。

**真相 = `外置大脑/实体词典.md` 里的一张表格**(md 唯一真源,R1)。索引是内存派生、永不落盘,
所以「删掉索引一条命令重建」天然成立,「改名全库替换」也不必维护任何缓存。

四条纪律(都是被踩过的坑换来的):

1. **id 永不改**。改名只动「正名/别名」两列,id 一个字都不动——引用面靠 id,不靠名字。
   所以改名的成本是 O(替换出现次数),不是 O(重建引用)。
2. **抽取不调 LLM**。人物来自 `外置大脑/人物/` 的**文件名**(Loom 的既有约定:文件名就是专名),
   其余四类由人在表里写。宁可漏,不可猜错——错词进了「全库替换」是真误伤。
3. **改名是破坏性操作**(R19 二级:需确认)。默认只给预览(哪些文件、多少处、示例行),
   `confirm=True` 才落盘;单字别名直接拒绝(单汉字必然撞词:苏醒/苏州/复苏)。
4. **只改作者可见的文字**,不碰 `.原稿` / `.历史` / `.半稿` / `.轨迹` / `.回收站`。
   那是 learn 的基线——替换进去会把 AI 原稿改成「作者手改过」的形状,污染指纹(ADR-0001)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .fsutil import atomic_write_text

TABLE_REL = paths.brain_rel("实体词典")

# 类型 → (前缀, 中文名)。前缀进 id,所以**顺序即稳定**(别重排、别改名,id 会跟着变)。
KINDS: dict[str, str] = {
    "人物": "p",
    "地点": "l",
    "物品": "i",
    "功法": "g",
    "组织": "o",
}

# 改名时扫这些地方(相对项目根),**不扫**隐藏目录(见模块头第 4 条)。
_SCOPE_GLOBS = (
    "正文/*.md",
    "正文/.细纲/*.md",
    f"{paths.BRAIN_DIR}/*.md",
    f"{paths.WORLD_DIR_REL}/*.md",
    f"{paths.CHARS_DIR_REL}/*.md",
)
# 人物目录里不是「人」的文件(成长档案是聚合页,占位示例不算实体)
_NOT_PERSON = {"成长档案", "未命名", "主角·未命名", "反派·未命名", "配角·未命名"}

_HEAD = """# 实体词典

> 角色 / 地点 / 物品 / 功法 / 组织。**这张表就是唯一真源**(md 为真相,索引可随时重建)。
> id 一旦生成**永不改**:改名只改正名与别名,id 不动。
> 一行一个实体;别名用中文逗号分隔。第二列「类型」只认这五个词:人物、地点、物品、功法、组织。
"""

_COLS = ("id", "类型", "正名", "别名", "首次出现")


@dataclass
class Ent:
    id: str
    kind: str
    name: str
    aliases: list[str] = field(default_factory=list)
    first: str = ""

    def as_row(self) -> list[str]:
        return [self.id, self.kind, self.name, "、".join(self.aliases), self.first]


# ---------------------------------------------------------------- md 表格 ↔ Ent
def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_table(text: str) -> list[Ent]:
    """解析词典表格。**宽容**:表格缺列、分隔行、空行都不炸——作者会手改这张表。"""
    out: list[Ent] = []
    seen_ids: set[str] = set()
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_row(line)
        if len(cells) < 3:
            continue
        if cells[0].lower() == "id" or set(cells[0]) <= set("-: "):
            continue
        eid, kind, name = cells[0], cells[1], cells[2]
        if not eid or not name or kind not in KINDS:
            continue
        if eid in seen_ids:      # 手抄重复行:保留第一条,不静默合并
            continue
        seen_ids.add(eid)
        aliases = [a for a in re.split(r"[、,，/\s]+", cells[3]) if a] if len(cells) > 3 else []
        out.append(Ent(eid, kind, name, aliases, cells[4] if len(cells) > 4 else ""))
    return out


def render_table(ents: list[Ent]) -> str:
    """渲染回 md。**往返稳定**(parse(render(x)) == x):否则每次保存都会改动用户的表。"""
    lines = [_HEAD, "", "| " + " | ".join(_COLS) + " |", "|" + "|".join(["---"] * len(_COLS)) + "|"]
    for e in ents:
        cells = [c.replace("|", "／") for c in e.as_row()]   # 竖线会截断表格
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def load(root: Path | str) -> list[Ent]:
    p = Path(root) / TABLE_REL
    return parse_table(p.read_text(encoding="utf-8")) if p.is_file() else []


def save(root: Path | str, ents: list[Ent]) -> Path:
    p = Path(root) / TABLE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, render_table(ents))
    return p


# ---------------------------------------------------------------- 别名 / 索引
# 别名派生要认的复姓。**比 `continuity` 的 8 个多**(那边只认慕容/南宫/欧阳/西门/上官/端木/独孤/诸葛,
# 且用「字符包含」判断)。这里放宽不会影响出稿:实体词典的别名只用于改名与盘点;
# 要不要让 continuity 也认这 18 个(会让它多报一些「仅以别名出现」的案子)留给作者裁定。
_COMPOUND_SURNAMES = set("欧阳 司马 上官 诸葛 东方 令狐 慕容 独孤 公孙 长孙 宇文 皇甫 尉迟 夏侯 端木 南宫 西门")


def name_aliases(name: str) -> list[str]:
    """从正名派生别名。规则与 `continuity` 的既有做法**逐条相同**(去姓给名 / 复姓去姓 /
    「·」后段 / 姓+称谓),只多认几个复姓。**不含单字**(单汉字必然撞词:苏醒/苏州/复苏)。"""
    out: list[str] = []
    if len(name) >= 2:
        out.append(name[1:])
        if name[0] in _COMPOUND_SURNAMES:
            out.append(name[2:])
    if "·" in name:
        out.append(name.split("·")[-1])
    surname = name[0]
    out += [surname + suf for suf in ("姑娘", "公子", "前辈", "兄", "老", "某")]
    return [a for a in dict.fromkeys(out) if len(a) >= 2 and a != name]


def index(ents: list[Ent]) -> tuple[dict[str, Ent], list[str]]:
    """正名与别名 → 实体。返回 (索引, 冲突说明)。

    冲突(同一串指向两个 id)不静默解决:留在第二个返回值里,由人决定。**故意不自动合并**——
    合并实体是不可逆的信息损失,而「报出来让人裁」的成本很低。
    """
    ix: dict[str, Ent] = {}
    clash: list[str] = []
    for e in ents:
        for key in [e.name, *e.aliases]:
            if key in ix and ix[key].id != e.id:
                clash.append(f"「{key}」同时指向 {ix[key].id} 与 {e.id}")
                continue
            ix[key] = e
    return ix, clash


# ---------------------------------------------------------------- 抽取(只读)
def _person_name(stem: str) -> str:
    """文件名 → 专名。`主角·沈砚` → 沈砚;`主角` → 主角。"""
    return stem.split("·")[-1].strip() if "·" in stem else stem.strip()


def scan(root: Path | str) -> dict:
    """只读盘点:材料里有哪些实体、词典里多了/少了什么。**不写任何文件**。"""
    root = Path(root)
    ents = load(root)
    known = {e.name for e in ents}

    people: list[str] = []
    for p in paths.brain_dir_files(root, paths.CHARS_DIR_REL):
        if p.suffix != ".md" or p.stem in _NOT_PERSON:
            continue
        nm = _person_name(p.stem)
        if nm and nm not in people:
            people.append(nm)

    have = {e.name for e in ents if e.kind == "人物"}
    _, clash = index(ents)
    return {
        "词典条目": len(ents),
        "材料里的人物": people,
        "词典缺的人物": [n for n in people if n not in have],   # 该补
        "词典多的人物": [n for n in have if n not in people],   # 材料里没这张卡(可能删过卡)
        "别名冲突": clash,
        "词典里没有的类别": [k for k in KINDS if not any(e.kind == k for e in ents)],
    }


def build(root: Path | str, *, write: bool = False) -> dict:
    """把材料里的人物补进词典。保留已有 id 与别名,只新增。

    `write=False`(默认)只返回「会新增几条」,不动盘。
    """
    root = Path(root)
    ents = load(root)
    info = scan(root)
    added: list[Ent] = []
    # id = 前缀 + 三位序号,序号取**当前最大值 +1**(不是「第一个空号」):
    # 第一个空号会让「删掉 p-002 之后新加的人」拿到 p-002,而旧 p-002 可能已经被
    # 正文/留痕引用过——同一个 id 先后指向两个实体,比号段不连续危险得多。
    used = {e.id for e in ents}
    seq = _max_seq(used, KINDS["人物"])
    for nm in info["词典缺的人物"]:
        seq += 1
        eid = f"{KINDS['人物']}-{seq:03d}"
        while eid in used:
            seq += 1
            eid = f"{KINDS['人物']}-{seq:03d}"
        used.add(eid)
        added.append(Ent(eid, "人物", nm, name_aliases(nm), ""))
    if write and added:
        save(root, ents + added)
    return {
        "新增": [{"id": e.id, "类型": e.kind, "正名": e.name, "别名": e.aliases} for e in added],
        "已写盘": bool(write and added),
        "提示": "非人物类别(地点/物品/功法/组织)不自动抽取:错词进了全库替换会真误伤,请在表里手写。",
    }


# ---------------------------------------------------------------- 改名(需确认)
def _scope_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for g in _SCOPE_GLOBS:
        out.extend(p for p in root.glob(g) if p.is_file())
    # 词典自身最后改(先改正文,表最后落,顺序无关但便于定位)
    return sorted({p for p in out})


def _max_seq(used: set[str], prefix: str) -> int:
    mx = 0
    head = f"{prefix}-"
    for eid in used:
        if eid.startswith(head):
            try:
                mx = max(mx, int(eid[len(head):]))
            except ValueError:
                continue
    return mx


def _keys_for(root: Path | str, old: str) -> list[str]:
    """要替换的词:old 可能是 id、正名或别名。**单字一律拒绝**。"""
    for e in load(root):
        if old in (e.id, e.name, *e.aliases):
            return [k for k in dict.fromkeys([e.name, *e.aliases]) if len(k) >= 2]
    return [old] if len(old) >= 2 else []


def rename_preview(root: Path | str, old: str, new: str) -> dict:
    """预览:哪些文件、多少处、示例。**不写盘。**"""
    root = Path(root)
    if len(new.strip()) < 2:
        return {"ok": False, "reason": "新名字少于 2 个字:单字/空名必然撞词,拒绝。"}
    keys = _keys_for(root, old)
    if not keys:
        return {"ok": False, "reason": "这个名字少于 2 个字:单字/空名必然撞词,拒绝。"}
    in_dict = any(old in (e.id, e.name, *e.aliases) for e in load(root))
    files: list[dict] = []
    total = 0
    for p in _scope_files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        n = sum(text.count(k) for k in keys)
        if not n:
            continue
        total += n
        sample = ""
        for k in keys:
            i = text.find(k)
            if i >= 0:
                sample = text[max(0, i - 12): i + len(k) + 12].replace("\n", " ")
                break
        files.append({"文件": str(p.relative_to(root)).replace("\\", "/"), "处": n, "示例": sample})
    return {"ok": True, "旧": old, "新": new, "要替换的词": keys,
            # 「词典」= 正名与别名一起换(这是改名的本意);「字面」= 词典里没有,只换这一串字。
            # 分开报是因为后果不同:字面替换不会修正它的别名,可能留下半新半旧的名字。
            "来源": "词典" if in_dict else "字面(词典里没有这个实体,只换这串字)",
            "合计处数": total, "文件": files,
            "范围": "正文 / 细纲 / 外置大脑(不含 .原稿/.历史/.半稿/.轨迹/.回收站)",
            "需确认": True}


def rename(root: Path | str, old: str, new: str, *, confirm: bool = False) -> dict:
    """改名。`confirm=False` 只返回预览——**破坏性操作不给默认执行**(R19 二级)。"""
    prev = rename_preview(root, old, new)
    if not prev.get("ok") or not confirm:
        prev["已执行"] = False
        return prev
    root = Path(root)
    keys = prev["要替换的词"]
    new = new.strip()
    # ① 一次替换,不是串行替换:新名里若含某个旧别名(「林晚 → 晚晴」而别名是「晚」),
    #    串行替换会把刚写进去的新字再换一遍,变成「晚晴晴」。
    keys_sorted = sorted({k for k in keys if k and k != new}, key=len, reverse=True)
    pat = re.compile("|".join(re.escape(k) for k in keys_sorted)) if keys_sorted else None
    touched, changed, done, missed = 0, 0, [], []
    for p in _scope_files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # ② 读不动的文件不许静默跳过:GBK 老稿改不到,人得知道,不然以为改全了
            missed.append({"文件": str(p.relative_to(root)).replace("\\", "/"),
                           "原因": type(e).__name__})
            continue
        t2 = pat.sub(new, text) if pat else text
        if t2 == text:
            continue
        try:
            atomic_write_text(p, t2)
        except OSError as e:
            # ③ 写到一半失败要回滚:前面几个文件已经变成新名字了,不能留半新半旧
            back = 0
            for q, old_text in done:
                try:
                    atomic_write_text(q, old_text)
                    back += 1
                except OSError:
                    pass
            return {"ok": False, "已执行": False, "已写几文件": len(done),
                    "回滚了几文件": back,
                    "说明": f"写到 {p.name} 时失败,已把前面 {back} 个文件退回原样:{e}"}
        done.append((p, text))
        touched += 1
        changed += len(pat.findall(text)) if pat else 0
    # 表格最后改:id 不动,只把正名/别名里的旧词换掉
    ents = load(root)
    for e in ents:
        if e.name in keys:
            e.name = new
        e.aliases = list(dict.fromkeys(new if a in keys else a for a in e.aliases))
        e.aliases = [a for a in e.aliases if a != e.name]
    save(root, ents)
    return {"ok": True, "已执行": True, "改了文件": touched,
            "实际处数": changed, "预览处数": prev.get("合计处数"),
            "没读动": missed[:20], "没读动几文件": len(missed),
            "说明": ("有些文件读不动(编码/权限),那些地方没改到:"
                     + "、".join(x["文件"] for x in missed[:5])) if missed else "范围内都改到了。",
            "id 未变": True}


# ---------------------------------------------------------------- 重建(R1)
# ---------------------------------------------------------------- 按 50 章一块取词(裁定 6③)
# 粒度跟「铺章纲」同一个单位(作者定的 50 章一块)。意义:块内任何一章都能看到这块里
# 出现过的专名(而不是只看本章),于是称呼/专名不会在块内漂;块换一次,推送内容才换一次。
BLOCK = 50


def block_of(chapter_n: int, span: int = BLOCK) -> tuple[int, int]:
    """第 N 章属于哪一块,返回 (起章, 止章)。第 1-50 章 = (1, 50)。"""
    n = max(1, int(chapter_n))
    span = max(1, int(span))
    lo = (n - 1) // span * span + 1
    return lo, lo + span - 1


def block_text(root: Path | str, chapter_n: int, span: int = BLOCK) -> str:
    """这一块的章纲原文(只抽「- 第N章:…」那几行),没有就返回空串。"""
    from . import paths as _paths
    p = Path(root) / _paths.CARD_REL
    if not p.is_file():
        return ""
    lo, hi = block_of(chapter_n, span)
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*[-*]\s*第\s*(\d+)\s*章\s*[:：]", line)
        if m and lo <= int(m.group(1)) <= hi:
            out.append(line)
    return "\n".join(out)


def subset_for_block(root: Path | str, chapter_n: int, span: int = BLOCK) -> tuple[list[Ent], tuple[int, int]]:
    """实体词典里,**被这一块章纲提到过**的条目(正名或别名命中都算)。

    返回 (条目, (起, 止))。命中用整词串匹配,不做模糊——宁可少推,不可推错。
    """
    lo, hi = block_of(chapter_n, span)
    text = block_text(root, chapter_n, span)
    if not text:
        return [], (lo, hi)
    ents = load(root)
    ix, _clash = index(ents)
    hit: dict[str, Ent] = {}
    for term, e in ix.items():
        if len(term) >= 2 and term in text:
            hit.setdefault(e.id, e)
    order = {e.id: i for i, e in enumerate(ents)}
    return sorted(hit.values(), key=lambda e: order.get(e.id, 0)), (lo, hi)


def rebuild(root: Path | str, *, create: bool = False) -> dict:
    """R1 的一条命令重建:实体索引是纯内存派生,「重建」= 重新解析一遍表。

    删掉任何派生文件后跑它:表在 → 索引立刻回来;表不在(且 create=True)→ 从材料抽人物建表。
    """
    root = Path(root)
    p = root / TABLE_REL
    if not p.is_file():
        if not create:
            return {"表": str(TABLE_REL), "存在": False, "条目": 0,
                    "提示": "词典还没建;用 --create 从 人物/ 目录抽一份初稿。"}
        b = build(root, write=True)
        ents = load(root)
        return {"表": str(TABLE_REL), "存在": True, "条目": len(ents), "新建": len(b["新增"])}
    ents = load(root)
    ix, clash = index(ents)
    return {"表": str(TABLE_REL), "存在": True, "条目": len(ents),
            "索引词条": len(ix), "冲突": clash,
            "可重建": True}
