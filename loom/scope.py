"""R20 跨书作用域:什么必须留在书里,什么可以全机共用。

**为什么这件事会丢书**(审计 A-9):如果 canon(人物/设定/状态账本)被做成全局共享,
那么**新书会继承上一本书的正史**——新书第一页就带着别人的世界观,而且是被平台自动注入的,
作者根本不知道。反过来,如果范文库/提示词每本书都要重配一遍,那「连一次全局记住」就成了空话。

| 作用域 | 项 | 铁律 |
|---|---|---|
| **书内**(绝不跨书) | 状态账本 / 实体词典 / 卡章纲 / 卷纲 / 全书大纲 / 人物 / 世界观 / 写作指纹 / AI 原稿 | 新书**零继承**;路径必须在这本书的根目录下 |
| **全局**(可覆盖、可关) | 范文库 / 提示词 / 违禁词 | 全机一份,**书内同名文件优先**(可见、可关) |

**开关落在书里**:`外置大脑/共享开关.md` 一张表。全局项默认开,但每本书可以单独关掉其中任意一项
——「可见」(列出路径与来源)与「可关」是一起的:不给开关的共享,等于偷偷替作者做决定。
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .config import user_config_dir
from .fsutil import atomic_write_text

SHARED_REL = paths.brain_rel("共享开关")

# 书内项:相对路径固定在这本书里。写死成常量而不是字符串散落各处。
BOOK_ITEMS: dict[str, str] = {
    "状态账本": paths.STATEBOOK_REL,
    "实体词典": paths.brain_rel("实体词典"),
    "卡章纲": paths.CARD_REL,
    "卷纲": paths.VOLUMES_REL,
    "全书大纲": paths.OVERVIEW_REL,
    "人物": paths.CHARS_DIR_REL,
    "世界观": paths.WORLD_DIR_REL,
    "写作指纹": paths.FINGERPRINT_REL,
    "AI 原稿": paths.SNAPSHOT_DIR,
}

# 全局项:(是目录吗, 全局目录下的名字, 书内覆盖时的文件名)
GLOBAL_ITEMS: dict[str, tuple[bool, str]] = {
    "范文库": (True, "范文库"),
    "提示词": (True, "提示词"),
    "违禁词": (False, "违禁词.md"),
}

_HEAD = """# 共享开关

> 全局项(范文库 / 提示词 / 违禁词)默认全机共用一份。这本书想单独关掉哪一项,把「开」改成「关」。
> **书内项(状态账本/人物/世界观/指纹…)不在这张表里**——它们永远只属于这本书,不可能跨书。
"""


def shared_root() -> Path:
    """全机共享目录(默认 ~/.loom/shared,`LOOM_HOME` 可覆盖)。"""
    return user_config_dir() / "shared"


def global_path(kind: str) -> Path:
    is_dir, name = GLOBAL_ITEMS[kind]
    return shared_root() / name


def book_path(root: Path | str, kind: str) -> Path:
    """书内项的落点。**一定在这本书的根目录下**(这是「新书零继承 canon」的结构保证)。"""
    if kind not in BOOK_ITEMS:
        raise KeyError(f"「{kind}」不是书内项")
    return Path(root) / BOOK_ITEMS[kind]


def book_override(root: Path | str, kind: str) -> Path:
    """全局项的书内覆盖:放 `外置大脑/<名字>` 就以本书为准(WYSIWYG,人可改)。"""
    if kind not in GLOBAL_ITEMS:
        raise KeyError(f"「{kind}」不是全局项")
    # 直接用目录拼,不过 brain_rel:后者会给没后缀的名字补 .md,
    # 于是「目录型的范文库」被写成 范文库.md、而「文件型的违禁词」又被砍成没后缀,
    # 两头都错(审查 B 的 P1)。名字原样用,该有 .md 的项自己在表里写全。
    _is_dir, _name = GLOBAL_ITEMS[kind]
    return Path(root) / paths.BRAIN_DIR / _name


def _split(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def load_switch(root: Path | str) -> dict[str, bool]:
    """读共享开关:项名 → 是否启用。没写过的项默认**开**(共用),老书零影响。"""
    p = Path(root) / SHARED_REL
    out = {k: True for k in GLOBAL_ITEMS}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _split(line)
        if len(cells) < 2 or cells[0] not in GLOBAL_ITEMS:
            continue
        out[cells[0]] = _switch_value(cells[1], default=True)[0]
    return out


def render_switch(switch: dict[str, bool]) -> str:
    out = [_HEAD, "", "| 项 | 开关 | 说明 |", "|---|---|---|"]
    for k in GLOBAL_ITEMS:
        out.append(f"| {k} | {'开' if switch.get(k, True) else '关'} | 全机共用一份,书内可覆盖 |")
    return "\n".join(out) + "\n"


def set_enabled(root: Path | str, kind: str, on: bool) -> dict:
    """开/关一个全局项(写进这本书的共享开关表)。书内项没有开关——它们本来就不可能跨书。"""
    if kind not in GLOBAL_ITEMS:
        return {"ok": False, "reason": f"「{kind}」不是全局项,没有开关可关。"}
    switch = load_switch(root)
    switch[kind] = bool(on)
    p = Path(root) / SHARED_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, render_switch(switch))
    return {"ok": True, "项": kind, "开": bool(on), "表": SHARED_REL}


def resolve(root: Path | str, kind: str) -> dict:
    """一个项在这本书里到底读哪份。"""
    root = Path(root)
    if kind in BOOK_ITEMS:
        p = book_path(root, kind)
        return {"项": kind, "作用域": "书内", "来源": "书内", "路径": str(p),
                "在": p.exists(), "已关": False, "可关": False,
                "说明": "只属于这本书,新书不会继承"}
    is_dir, _name = GLOBAL_ITEMS[kind]
    on = load_switch(root).get(kind, True)
    ov = book_override(root, kind)
    has_ov = ov.exists()
    src = "书内覆盖" if has_ov else "全局"
    p = ov if has_ov else global_path(kind)
    return {"项": kind, "作用域": "全局", "来源": src, "路径": str(p), "在": p.exists(),
            "已关": not on, "可关": True, "有书内覆盖": has_ov,
            "说明": "全机共用一份;书内同名文件优先"}


def visible(root: Path | str) -> dict:
    """全表的可见视图:书内项在前(永远是书内),全局项在后(带开关与来源)。"""
    return {"书内": [resolve(root, k) for k in BOOK_ITEMS],
            "全局": [resolve(root, k) for k in GLOBAL_ITEMS],
            "共享目录": str(shared_root()),
            "开关表": SHARED_REL}


_OFF = ("关", "关闭", "false", "否", "0", "off", "no", "×", "✗", "disable", "禁用")
_ON = ("开", "开启", "true", "是", "1", "on", "yes", "√", "✓", "enable", "启用")


def _switch_value(raw: str, *, default: bool = True) -> tuple[bool, bool]:
    """返回 (值, 是否认得出)。认不出的**记下来**,不假装读懂了。"""
    v = (raw or "").strip().lower()
    if v in _OFF:
        return False, True
    if v in _ON:
        return True, True
    return default, False

