"""章状态机(R9):每章的 status / 脏标 / 租约,外加 R8 的影响面分析。

**真相仍是 md**:`外置大脑/章状态.md` 一张表,人能看能改。机器基线放 `正文/.状态/第N章.materials.json`
(隐藏、可丢:丢了只是暂时检测不到「上游变过」,书一点没事——这是刻意的降级方式)。

三个状态:

| 状态 | 含义 | 谁能写 |
|---|---|---|
| 未写 | 正文目录里还没有这一章 | — |
| 草稿 | 有正文,还没人确认 | 流水线落盘后自动打 |
| 定稿 | 作者确认过了 | 只有人能标;**脏章拒绝** |

**脏 = 这一章的上游材料变过**(卡章纲/卷纲/全书大纲/世界观/人物卡/本章细纲),不是「正文被手改」——
后者 `ledger.chapter_drifted` 早就有了,两者别混。

**只打脏,不自动重跑**(R9 的硬要求):标脏只影响「能不能定稿」与「影响面分析」,一个模型调用都不会发。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .fsutil import atomic_write_text

STATUS_REL = paths.brain_rel("章状态")
BASE_DIR = f"{paths.BODY_DIR}/.状态"        # 机器基线(隐藏;删了不会丢任何内容)

STATUSES = ("未写", "草稿", "定稿")

_HEAD = """# 章状态

> 一章同时只有一个状态:**未写 / 草稿 / 定稿**。
> 「脏」= 这一章的上游材料(章纲/卷纲/书纲/设定/细纲)变过,建议重算;**脏章不能定稿**。
> 「写者」是正在生成这一章的窗口;租约到期自动失效,不会把书锁死。
> 这张表可以手改:改状态就等于你亲自确认过。
"""

_COLS = ("章", "状态", "脏", "脏因", "写者", "租到")


@dataclass
class Row:
    chapter: int
    status: str = "未写"
    dirty: bool = False
    reason: str = ""
    owner: str = ""
    lease_until: float = 0.0

    def as_row(self) -> list[str]:
        until = time.strftime("%m-%d %H:%M", time.localtime(self.lease_until)) if self.lease_until else ""
        return [str(self.chapter), self.status, "脏" if self.dirty else "", self.reason, self.owner, until]


# ---------------------------------------------------------------- md 表读写
def _split(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_table(text: str) -> dict[int, Row]:
    rows: dict[int, Row] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _split(line)
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        n = int(cells[0])
        rows[n] = Row(n,
                      cells[1] if cells[1] in STATUSES else "未写",
                      len(cells) > 2 and cells[2] == "脏",
                      cells[3] if len(cells) > 3 else "",
                      cells[4] if len(cells) > 4 else "",
                      0.0)   # 租到只给人看;机读用 .状态 里的 lease.json,不解析「%m-%d %H:%M」
    return rows


def render_table(rows: dict[int, Row]) -> str:
    out = [_HEAD, "", "| " + " | ".join(_COLS) + " |", "|" + "|".join(["---"] * len(_COLS)) + "|"]
    for n in sorted(rows):
        out.append("| " + " | ".join(c.replace("|", "／") for c in rows[n].as_row()) + " |")
    return "\n".join(out) + "\n"


def load(root: Path | str) -> dict[int, Row]:
    p = Path(root) / STATUS_REL
    return parse_table(p.read_text(encoding="utf-8")) if p.is_file() else {}


def save(root: Path | str, rows: dict[int, Row]) -> Path:
    p = Path(root) / STATUS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, render_table(rows))
    return p


# ---------------------------------------------------------------- 基线:上游材料指纹
def materials(root: Path | str, n: int) -> dict[str, str]:
    """这一章的上游材料 → {相对路径: sha12}。**只看会进 prompt 的东西**,不看正文本身。

    范围刻意窄:章纲/卷纲/书纲/世界观/人物/本章细纲。改正文不算上游变(那是 ledger.chapter_drifted 的事)。
    """
    root = Path(root)
    rels = [paths.CARD_REL, paths.VOLUMES_REL, paths.OVERVIEW_REL]
    out: dict[str, str] = {}
    for rel in rels:
        p = root / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    # 单文件形态(老书 / 手工建档:世界观.md、人物卡.md)与目录形态都要算。
    # 以前只扫目录形态,于是老书改了世界观/人物卡不打脏,定稿闸也拦不住(交叉审查 P1-1)。
    for file_rel, dir_rel in ((paths.WORLD_REL, paths.WORLD_DIR_REL),
                              (paths.CHARS_REL, paths.CHARS_DIR_REL)):
        fp = root / file_rel
        if fp.is_file():
            out[file_rel] = hashlib.sha256(fp.read_bytes()).hexdigest()[:12]
        for p in paths.brain_dir_files(root, dir_rel):
            out[str(p.relative_to(root)).replace("\\", "/")] = \
                hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    op = paths.outline_path(root, n)
    if op.is_file():
        out[paths.OUTLINE_DIR + f"/第{n}章.md"] = hashlib.sha256(op.read_bytes()).hexdigest()[:12]
    return out


def _base_path(root: Path | str, n: int) -> Path:
    return Path(root) / BASE_DIR / f"第{n}章.materials.json"


def stamp(root: Path | str, n: int, *, status: str = "草稿") -> dict:
    """把当前材料指纹记成基线,并把这一章标成 status、清掉脏标。**写流水线落盘后调它。**"""
    root = Path(root)
    p = _base_path(root, n)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(materials(root, n), ensure_ascii=False, indent=1))
    rows = load(root)
    r = rows.get(n) or Row(n)
    r.status = status if status in STATUSES else "草稿"
    r.dirty = False
    r.reason = ""
    rows[n] = r
    save(root, rows)
    return {"章": n, "状态": r.status, "脏": False}


def diff(root: Path | str, n: int) -> list[str]:
    """与基线比:返回变过的材料路径。没基线 → 空(宽容:老章不判脏,不吓人)。"""
    root = Path(root)
    p = _base_path(root, n)
    if not p.is_file():
        return []
    try:
        base = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    now = materials(root, n)
    return sorted(k for k in set(base) | set(now) if base.get(k) != now.get(k))


def dirty_now(root: Path | str) -> list[dict]:
    """**纯只读**:哪些已写章节的上游变了,变的是哪些文件。不碰盘。"""
    root = Path(root)
    rows = load(root)
    out: list[dict] = []
    for n in _written(root):
        changed = diff(root, n)
        if not changed:
            continue
        row = rows.get(n)
        out.append({"章": n,
                    "状态": row.status if row else "草稿",
                    "变过的材料": changed,
                    "建议": f"重算第 {n} 章(或确认无需重算后标定稿)"})
    return out


def sync(root: Path | str) -> dict:
    """把上游变更写进表(脏 + 脏因)。**只打脏标,不发任何模型调用。**"""
    root = Path(root)
    rows = load(root)
    hits = dirty_now(root)
    for h in hits:
        n = h["章"]
        r = rows.get(n) or Row(n)
        r.dirty = True
        names = "、".join(Path(x).name for x in h["变过的材料"][:3])
        more = " 等" if len(h["变过的材料"]) > 3 else ""
        r.reason = f"上游材料变过:{names}{more}"
        if r.status == "未写":
            r.status = "草稿"
        rows[n] = r
    save(root, rows)
    return {"新打脏": [h["章"] for h in hits], "共脏": sum(1 for r in rows.values() if r.dirty)}


# ---------------------------------------------------------------- 状态 / 定稿闸
def set_status(root: Path | str, n: int, status: str) -> dict:
    """改状态。**脏章不许定稿**——这是 R9 的唯一硬闸(其余都是提示)。"""
    if status not in STATUSES:
        return {"ok": False, "reason": f"不认识的状态:{status}(只认 {' / '.join(STATUSES)})"}
    root = Path(root)
    rows = load(root)
    r = rows.get(n) or Row(n)
    if status == "定稿":
        changed = diff(root, n)
        if r.dirty or changed:
            return {"ok": False, "code": "dirty",
                    "reason": f"第 {n} 章的上游材料变过({('、'.join(Path(x).name for x in changed[:3]) or r.reason)}),"
                              f"先重算或先在表里确认,再定稿。"}
        if not paths.chapter_path(root, n).is_file():
            return {"ok": False, "code": "no_text", "reason": f"第 {n} 章还没有正文,不能定稿。"}
        r.dirty = False
        r.reason = ""
    r.status = status
    rows[n] = r
    save(root, rows)
    return {"ok": True, "章": n, "状态": status}


# ---------------------------------------------------------------- 租约(两个窗口不撞车)
LEASE_TTL = 900   # 15 分钟;生成一章通常远短于此


def _lease_path(root: Path | str) -> Path:
    return Path(root) / BASE_DIR / "lease.json"


def _leases(root: Path | str) -> dict[str, dict]:
    p = _lease_path(root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_leases(root: Path | str, d: dict[str, dict]) -> None:
    p = _lease_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(d, ensure_ascii=False, indent=1))


def holder(root: Path | str, n: int) -> str:
    """当前持有第 n 章租约的写者;过期/没人 → 空串。"""
    le = _leases(root).get(str(n))
    if not le or le.get("until", 0) < time.time():
        return ""
    return le.get("owner", "")


def acquire(root: Path | str, n: int, owner: str, ttl: int = LEASE_TTL) -> dict:
    """抢租约。同一个 owner 重入 = 续期;**别人持有且未过期 → 拒绝**。"""
    if not owner:
        return {"ok": False, "reason": "要说明是谁在写(owner 不能为空)。"}
    cur = holder(root, n)
    if cur and cur != owner:
        return {"ok": False, "code": "busy", "holder": cur,
                "reason": f"第 {n} 章正在被「{cur}」写,别两个窗口同时写一章。"}
    d = _leases(root)
    d[str(n)] = {"owner": owner, "until": time.time() + max(30, ttl)}
    _save_leases(root, d)
    rows = load(root)
    r = rows.get(n) or Row(n)
    r.owner, r.lease_until = owner, d[str(n)]["until"]
    rows[n] = r
    save(root, rows)
    return {"ok": True, "章": n, "写者": owner, "秒": int(d[str(n)]["until"] - time.time())}


def release(root: Path | str, n: int, owner: str) -> dict:
    """放手。只放自己的:别人的租约你放不掉(否则一个窗口能踢掉另一个)。"""
    cur = holder(root, n)
    if cur and cur != owner:
        return {"ok": False, "reason": f"第 {n} 章不是你在写(写者:{cur}),放不掉。"}
    d = _leases(root)
    d.pop(str(n), None)
    _save_leases(root, d)
    rows = load(root)
    r = rows.get(n) or Row(n)
    r.owner, r.lease_until = "", 0.0
    rows[n] = r
    save(root, rows)
    return {"ok": True, "章": n}


# ---------------------------------------------------------------- R8 影响面分析
def impact(root: Path | str, *, per_chapter: int = 0) -> dict:
    """改上级之后:哪些章要重算、大概花多少次调用。**只算,不跑。**"""
    from . import agents   # 局部导入:避免与 agents→本题 的循环

    root = Path(root)
    hits = dirty_now(root)
    steps = len(agents.PIPELINE)
    cost = sum(per_chapter or steps for _ in hits)
    return {"要重算的章": [h["章"] for h in hits],
            "每章工序数": steps,
            "预计调用次数": cost,
            "说明": "这是下限:gate 复审与除虫可能再加几次。跑不跑由你定,引擎不会自己重跑。",
            "明细": hits}


def _written(root: Path | str) -> list[int]:
    out: list[int] = []
    d = Path(root) / paths.BODY_DIR
    if not d.is_dir():
        return out
    for p in d.glob("第*章.md"):
        s = p.stem
        if s.startswith("第") and s.endswith("章") and s[1:-1].isdigit():
            out.append(int(s[1:-1]))
    return sorted(out)
