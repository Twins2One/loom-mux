"""全面引导注册表（2026-11-03 批 C，需求访谈三问之三）。

**设计原则**（外部 AI 审阅定稿）：
- 引导不是硬编码文案，是**结构化数据**——新功能上线 = 加一条注册表项，引导随功能走
- 每条引导 = 触发条件（when）+ 出口动作（who/say）+ 完成判定（done_when）
- **done_when 在后端判定**——后端知道"用户是否开过一本书/填没填凭据"，前端只展示
- 完成即出队：`seen` 记录存 prefs，永不重复轰炸

**数据形态**（guides.json，随包分发；用户 seen 状态存 prefs）：
    [{"id", "when": {type, ...}, "who", "say", "done_when": {type, ...},
      "hint": 可选短版（设置项悬浮说明用）, "version_added"}]

**when/done_when 的 type 判定器**（_check）：
- wizard_not_done        prefs.wizard_done != true
- books_count_gte {n}    书架书数 >= n
- has_bili_credential    prefs/环境有 SESSDATA
- no_volume_outline      当前书卷纲缺起止（需要 root）
- has_volume_outline     当前书卷纲有起止
- style_archive_empty    文风档案 0 个
- style_archive_exists   文风档案 >= 1
- import_used_once       prefs.guides_seen 里有 import-folder
"""

from __future__ import annotations

import json
from pathlib import Path

_DEFAULTS = [
    {
        "id": "first-shelf",
        "title": "欢迎来到书房",
        "when": {"type": "wizard_not_done"},
        "who": "书房伙伴",
        "say": (
            "欢迎来到书房。三句话讲清这里是什么：左边【自习室】是教材和素材——"
            "写作分析课教你怎么写，个人图书馆存你拆过的案例；【织布机】是你写书的地方，"
            "开一本书从立项走到正文。卡住了就叫我，或者找下面两位：知识顾问学视频查资料，"
            "语料分析师蒸文风。想让我学 B 站视频的话，设置里先填一下 B 站凭据（一分钟）。"
        ),
        "done_when": {"type": "books_count_gte", "n": 1},
        "version_added": "0.9.0",
    },
    {
        "id": "bili-credential",
        "title": "学视频前先填 B 站凭据",
        "when": {"type": "never"},   # 由取稿失败代码里主动标记（见 server 取稿失败分支）
        "who": "知识顾问",
        "say": (
            "取 B 站视频需要凭据（B 站 CC → 设置里那格 SESSDATA，只存在这台电脑，"
            "不进软件包、不上传）。填上我就能取稿了。"
        ),
        "done_when": {"type": "has_bili_credential"},
        "hint": "学视频前先在设置里填 B 站凭据（SESSDATA）",
        "version_added": "0.9.0",
    },
    {
        "id": "no-volume",
        "title": "这本书还没排卷纲",
        "when": {"type": "no_volume_outline"},
        "who": "领航员",
        "say": "这本书还没排卷纲。先找大纲师把卷纲排出来（每卷从第几章到第几章），分卷分章才立得起来。",
        "done_when": {"type": "has_volume_outline"},
        "version_added": "0.9.0",
    },
    {
        "id": "style-archive-empty",
        "title": "蒸一份文风档案",
        "when": {"type": "style_archive_empty"},
        "who": "语料分析师",
        "say": (
            "文风档案还是空的。把我给您蒸馏吧：贴几段您喜欢的文本（或导入文档），"
            "我蒸出写手参考——写作时会自动喂给写手。"
        ),
        "done_when": {"type": "style_archive_exists"},
        "hint": "先蒸一份文风档案，写作时才会带您的味道",
        "version_added": "0.9.0",
    },
    {
        "id": "import-folder",
        "title": "导入已有资料夹",
        "when": {"type": "front_triggered"},   # 前端在首次点开「＋已有资料夹」时主动调 seen 触发展示
        "who": "书房伙伴",
        "say": "「已有资料夹」用来导入您以前写的书：选旧书文档根目录，我自动认出世界观/人物/正文并归位；认不出的会列出来让您处理。",
        "done_when": {"type": "import_used_once"},
        "version_added": "0.9.0",
    },
]


def _guides_path() -> Path:
    from .config import user_config_dir

    return user_config_dir() / "guides.json"


def _seen(root: Path | str | None = None) -> list[str]:
    """已 seen 的引导 id 列表（存 prefs 同目录的 guides_seen.json；全局，不分书）。"""
    p = _guides_path().parent / "guides_seen.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def mark_seen(gid: str) -> None:
    p = _guides_path().parent / "guides_seen.json"
    try:
        seen = _seen(None)
        if gid not in seen:
            seen.append(gid)
            from .fsutil import atomic_write_text

            atomic_write_text(p, json.dumps(seen, ensure_ascii=False, indent=1))
    except Exception:
        pass


def _registry() -> list[dict]:
    """注册表：包内缺省 + 用户自定义覆盖（guides.json 可选）。"""
    out = list(_DEFAULTS)
    try:
        p = _guides_path()
        if p.is_file():
            extra = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(extra, list):
                known = {g.get("id") for g in out}
                for g in extra:
                    if isinstance(g, dict) and g.get("id") not in known:
                        out.append(g)
    except Exception:
        pass
    return out


def _count_books() -> int:
    try:
        from . import userconf

        return sum(1 for p in userconf.list_projects() if p.get("root"))
    except Exception:
        return 0


def _has_bili() -> bool:
    import os

    if os.environ.get("LOOM_BILIBILI_SESSDATA", "").strip():
        return True
    try:
        from .config import user_config_dir

        env = user_config_dir() / ".env"
        if env.is_file() and "SESSDATA" in env.read_text(encoding="utf-8", errors="replace"):
            return True
    except Exception:
        pass
    return False


def _volume_outline_state(root: Path | str | None) -> str | None:
    """返回 "有起止"/"缺起止"/None（无书或无卷纲文件）。"""
    if not root:
        return None
    try:
        from . import volplan

        vols = volplan.volume_ranges(Path(root))
        if not vols:
            return None
        return "有起止" if any(v.get("起") is not None for v in vols) else "缺起止"
    except Exception:
        return None


def _style_archive_count() -> int:
    try:
        from .config import user_config_dir

        base = user_config_dir() / "style"
        if not base.is_dir():
            # 2026-11-11：不再硬编码某台机器的知识库路径，跟 libroot 同口径
            from . import libroot

            base = libroot.library_root() / "语料库"
        if not base.is_dir():
            return 0
        return sum(1 for d in base.iterdir() if d.is_dir() and (d / "写手参考.md").is_file())
    except Exception:
        return 0


def _check(cond: dict | None, root: Path | str | None) -> bool:
    """判定一个 when/done_when 条件。未知 type = False（保守，宁可不触发/不算完成）。"""
    if not cond or not isinstance(cond, dict):
        return False
    t = cond.get("type")
    if t == "wizard_not_done":
        try:
            from . import userconf

            return not userconf.load_prefs().get("wizard_done", False)
        except Exception:
            return True
    if t == "books_count_gte":
        return _count_books() >= int(cond.get("n", 1))
    if t == "has_bili_credential":
        return _has_bili()
    if t == "no_volume_outline":
        # 2026-11-03（作者截图裁定）：书房页没有"当前书"上下文，不该触发书级引导——
        # 无 root 时返回 False（不触发），书级引导只在织布机（有 root）里出现。
        if not root:
            return False
        return _volume_outline_state(root) in ("缺起止", None)
    if t == "has_volume_outline":
        if not root:
            return False
        return _volume_outline_state(root) == "有起止"
    if t == "style_archive_empty":
        return _style_archive_count() == 0
    if t == "style_archive_exists":
        return _style_archive_count() > 0
    if t == "import_used_once":
        return "import-folder" in _seen(None)
    if t == "front_triggered":
        # when=never 的兄弟型：注册表条件恒真，但**只在动作入口被前端主动拉取时展示**
        # （showGuideCardOnce），不出现在常规轮询的打扰队列里。
        # 2026-11-03 修正：goImportFolder 拉 pending?context=import-folder 期望拿到它，
        # 恒 False 会让说明卡永远不出现（实测钉死）。
        return True
    if t == "never":
        return False
    return False


def pending(root: Path | str | None = None, *, include_front: bool = False) -> list[dict]:
    """当前该触发的引导（when=True 且 done_when=False 且未 seen），按注册表顺序。"""
    seen = set(_seen(None))
    out = []
    for g in _registry():
        gid = str(g.get("id") or "")
        if not gid or gid in seen:
            continue
        is_front = (g.get("when") or {}).get("type") == "front_triggered"
        if is_front and not include_front:
            continue   # 动作入口型引导只在入口被显式拉取时展示，不进常规轮询
        if _check(g.get("when"), root) and not _check(g.get("done_when"), root):
            out.append({"id": gid, "title": g.get("title", ""), "who": g.get("who"), "say": g.get("say"),
                        "hint": g.get("hint", "")})
    return out


def mark_wizard_done() -> None:
    """首启引导完成（开过一本书即算）。"""
    try:
        from . import userconf
        from .fsutil import atomic_write_text

        d = userconf.load_prefs()
        d["wizard_done"] = True
        p = userconf.user_config_dir() / "prefs.json"
        atomic_write_text(p, json.dumps(d, ensure_ascii=False, indent=1))
    except Exception:
        pass
