"""R19 破坏性操作的三级护栏:可撤销 / 需输入确认 / 禁止。

**为什么要有这个文件**:审计 A-8 的原话是「这个平台会丢书」。丢书不是 bug,是**动作没有分级**
——删一章和删一本书在代码里长一个样,前端也不敢问「你确定吗」,因为问了也拦不住真删。

**三级的分界(判据是「后悔药在不在」,不是「危不危险」)**:

| 级别 | 判据 | 例子 |
|---|---|---|
| **可撤销** | 事后能一键还原(回收站 / 版本历史 / 指纹备份) | 删章、撤销 learn、恢复历史版本 |
| **需输入确认** | 不可自动还原,但用户明确知道自己在做什么 → 要打字确认 + 给预览 | 批量改写正文、清空回收站、重建卡章纲 |
| **禁止** | 做了就**永久失去**证据链,任何界面都不给入口 | 删整本书目录、改 `.原稿`(learn 基线)、直接改指纹文件 |

**宁可升档**:不认识的动作用「需输入确认」,不给「可撤销」的错觉。

**软删的落点是 `paths.CHAPTER_ARTIFACTS`**——那张表里每一行都是一章在盘上的产物
(正文/原稿/账本/细纲/轨迹/历史/留痕/指纹备份)。只挪正文不挪账本 = 删掉第 3 章后
新的第 3 章会重放旧第 3 章的产物(串章,表里那句注释就是踩过的坑)。
"""

from __future__ import annotations

import json
from datetime import datetime
import shutil
import time
from pathlib import Path

from . import paths
from .fsutil import atomic_write_text

LEVELS = ("可撤销", "需输入确认", "禁止")

# 动作 → 级别 + 确认词 + 说明。动词用中文短名,前端按这个名字问。
ACTIONS: dict[str, dict] = {
    "删章": {"级别": "可撤销", "说明": "整章产物移进 正文/.回收站/,可一键还原"},
    "撤销指纹学习": {"级别": "可撤销", "说明": "指纹历史里有 learn 前的备份"},
    "恢复历史版本": {"级别": "可撤销", "说明": "覆盖前已留快照"},
    "AI 修": {"级别": "可撤销", "说明": "落盘前留一版快照,可整章回滚"},
    "合入外部改稿": {"级别": "可撤销", "说明": "写前留快照;外部改动已并进合并结果,不会丢"},
    "批量改写正文": {"级别": "需输入确认", "确认词": "确认批量改写", "说明": "同时改多处,逐句预览后再动手"},
    "清空回收站": {"级别": "需输入确认", "确认词": "确认清空回收站", "说明": "清空后不可还原"},
    "重建卡章纲": {"级别": "需输入确认", "确认词": "确认重建", "说明": "会覆盖现有章纲里人写的部分"},
    "清空审稿留痕": {"级别": "需输入确认", "确认词": "确认清空", "说明": "历史报告会一起消失"},
    "改模型与供应商": {"级别": "需输入确认", "确认词": "确认修改", "说明": "写错会让写章直接失败(可改回来)"},
    "删除整本书": {"级别": "禁止", "说明": "整本目录一旦删掉,回收站救不回来;要删请在文件管理器里自己动手"},
    "改 AI 原稿快照": {"级别": "禁止", "说明": "learn 的基线:动了它,指纹会把 AI 的稿当成人手改"},
    "改指纹文件": {"级别": "禁止", "说明": "护城河本体:只允许通过 learn/撤销 两条路改动"},
    "改回收站内容": {"级别": "禁止", "说明": "回收站是还原的唯一凭据,不许程序化改写"},
}


# 这些位置**禁止**用手写端点直接覆盖(R19 的「禁止」级):它们是悔棋的底、或学出来的真相。
# 要改它们只能走专门的动作(用历史还原 / 重新 learn),不能拿个 PUT 就盖掉。
PROTECTED_REL = ("正文/.原稿", "正文/.回收站", "正文/.历史", "外置大脑/写作指纹.md")


def norm_rel(rel: str) -> str:
    """把 rel 归一成「正斜杠、无空段、无 . / ..」的形状。

    **为什么必须有它**:`protected()` 原来直接拿原始 rel 去比前缀,而落盘用的是 `resolve` 后的路径
    ⇒ 写 `正文/./.原稿/第1章.md` 或 `正文/../正文/.原稿/…` 就能绕过「禁止级」护栏(交叉审查的 P0)。
    比前缀这件事必须发生在**归一化之后**。`..` 一律按「往上退一级」处理,退到根就丢掉。
    """
    parts: list[str] = []
    for seg in (rel or "").replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def protected(rel: str) -> str:
    """rel 落在禁止手写的位置上就返回那一句理由,否则空串。"""
    r = norm_rel(rel)
    for p in PROTECTED_REL:
        if r == p or r.startswith(p + "/"):
            return (f"「{p}」是护栏里的「禁止」级位置(悔棋的底/学出来的真相),"
                    "不接受手写覆盖。要改请走历史还原或重新 learn。")
    return ""


def level_of(action: str) -> str:
    """没登记的动作按「需输入确认」处理:宁可多问一句,不给「可撤销」的错觉。"""
    return (ACTIONS.get(action) or {}).get("级别", "需输入确认")


def confirm_word(action: str) -> str:
    return (ACTIONS.get(action) or {}).get("确认词", f"确认{action}")


def check(action: str, *, count: int = 0, preview: list[str] | None = None) -> dict:
    """动手前问一次:能不能做、要不要打字确认、批量的话先看预览。"""
    lvl = level_of(action)
    item = ACTIONS.get(action) or {}
    out = {"动作": action, "级别": lvl, "允许": lvl != "禁止",
           "说明": item.get("说明", "未登记的动作,按需确认处理"),
           "预览": (preview or [])[:50], "预览条数": len(preview or []),
           "需要确认": lvl == "需输入确认"}
    if lvl == "禁止":
        out["理由"] = "这个动作会让证据链永久断裂,平台不提供入口。"
    elif lvl == "需输入确认":
        out["确认词"] = confirm_word(action)
        out["要确认什么"] = f"请输入「{out['确认词']}」后才会执行"
    if count:
        out["涉及"] = f"{count} 项"
    return out


def verify(action: str, token: str) -> dict:
    """核对用户打的字。**可撤销与禁止都不需要确认词**:前者靠回收站,后者根本不执行。"""
    lvl = level_of(action)
    if lvl == "禁止":
        return {"ok": False, "级别": lvl, "reason": "这个动作被禁止,打什么字都不执行。"}
    if lvl == "可撤销":
        return {"ok": True, "级别": lvl,
                "reason": "可撤销动作不需要打字确认(回收站/历史里能还原)。"}
    want = confirm_word(action)
    got = (token or "").strip()
    if got != want:
        return {"ok": False, "级别": lvl, "reason": f"确认词不对:要输入「{want}」。", "期望": want}
    return {"ok": True, "级别": lvl}


# ---------------------------------------------------------------- 软删 / 回收站
def _trash_root(root: Path | str) -> Path:
    return Path(root) / paths.TRASH_DIR


def soft_delete_chapter(root: Path | str, n: int, *, reason: str = "") -> dict:
    """删一章 = 整章产物一起挪进回收站(不真删)。**没有正文就拒绝**,不做空动作。"""
    root = Path(root)
    if not paths.chapter_path(root, n).is_file():
        return {"ok": False, "reason": f"第 {n} 章没有正文,没什么可删的。"}
    # time.strftime 不认 %f(那是 datetime 的);用 datetime 撑到微秒,免得同一秒两次删同章号落进同一个盒子把旧稿覆盖掉
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    box = _trash_root(root) / f"第{n}章-{stamp}"
    moved: list[dict] = []
    for dir_rel, name_tpl, is_dir in paths.CHAPTER_ARTIFACTS:
        src = root / dir_rel / name_tpl.format(n=n)
        if not src.exists():
            continue
        dst = box / dir_rel / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append({"原路径": f"{dir_rel}/{src.name}", "在回收站里": f"{dir_rel}/{src.name}"})
    manifest = {"章": n, "时间": stamp, "原因": reason or "手动删除",
                "产物": moved, "还原办法": "调 restore_chapter(按 manifest 原路挪回)"}
    atomic_write_text(box / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return {"ok": True, "章": n, "移走": len(moved), "回收站": str(paths.TRASH_DIR) + f"/第{n}章-{stamp}",
            "可撤销": True}


def list_trash(root: Path | str) -> list[dict]:
    """回收站里有什么(按时间新→旧)。坏 manifest 跳过,不要让界面崩。"""
    box = _trash_root(root)
    if not box.is_dir():
        return []
    out = []
    for d in sorted(box.iterdir(), reverse=True):
        mf = d / "manifest.json"
        if not d.is_dir() or not mf.is_file():
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"名字": d.name, "章": m.get("章"), "时间": m.get("时间"),
                    "原因": m.get("原因", ""), "产物数": len(m.get("产物") or [])})
    return out


def restore_chapter(root: Path | str, name: str) -> dict:
    """一键还原。**先全查再全挪**:任何一处目标已存在就整批拒绝,绝不覆盖现有内容。"""
    root = Path(root)
    box = _trash_root(root) / name
    mf = box / "manifest.json"
    if not mf.is_file():
        return {"ok": False, "reason": f"回收站里没有「{name}」。"}
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "reason": "这条回收站记录的清单坏了,不敢乱挪。"}
    raw = m.get("产物") or []
    entries, broken = [], 0
    for e in raw:
        if not isinstance(e, dict) or not e.get("原路径") or not e.get("在回收站里"):
            broken += 1
            continue
        # 清单是盘上的文件,可能被手改过。原路径 带 ../ 就能把文件挪到书外面去
        # (交叉审查 P1),所以**先归一化、再拒绝任何仍然带 .. 的条目**。
        _orig = str(e["原路径"]).replace("\\", "/")
        _segs = [x for x in _orig.split("/") if x not in ("", ".")]
        # 清单里出现 .. 一律拒收(不是「归一化后就照做」):归一化会把开头的 .. 丢掉,
        # 结果文件被挪到书里另一个位置,人还以为「还原成功」。
        if ".." in _segs:
            broken += 1
            continue
        _norm = norm_rel(_orig)
        if not _norm:
            broken += 1
            continue
        entries.append({**e, "原路径": _norm})
    if broken:
        return {"ok": False, "code": "manifest_bad",
                "reason": f"这条回收站记录的清单里有 {broken} 行缺字段,不敢照着挪。"
                          "请手工打开「正文/.回收站/」把文件挪回去。"}

    # 「先全查再全挪」要**两边都查**:只查目标侧,一旦上次挪到一半断了,剩下的永远回不去
    # (目标已经有文件 → 每次都报 clash → 死锁)。所以:
    #   · 回收站里已经没有、目标已经有的 → 上次已经挪回去了,跳过(幂等);
    #   · 回收站里有、目标也有 → 真冲突,整批拒绝;
    #   · 回收站里没有了、目标也没有 → 文件不知去向,整批拒绝并点名。
    plan, clash, lost, done = [], [], [], []
    for e in entries:
        src = box / e["在回收站里"]
        dst = root / e["原路径"]
        if src.exists() and dst.exists():
            clash.append(e["原路径"])
        elif not src.exists() and dst.exists():
            done.append(e["原路径"])
        elif not src.exists() and not dst.exists():
            lost.append(e["原路径"])
        else:
            plan.append((src, dst, e["原路径"]))
    if clash:
        return {"ok": False, "code": "clash",
                "reason": "这些位置已经有文件了,还原会覆盖现有内容:" + "、".join(clash[:3])
                          + "。请先把现有的改名或删掉。"}
    if lost:
        return {"ok": False, "code": "lost",
                "reason": "这些文件在回收站里也找不到了:" + "、".join(lost[:3])
                          + "。清单记过它们,但没有实体文件,不敢当成还原成功。"}
    for src, dst, _ in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    shutil.rmtree(box, ignore_errors=True)
    return {"ok": True, "章": m.get("章"), "挪回": len(plan),
            "上次已挪回": len(done),
            "正文": str(paths.chapter_path(root, int(m.get("章") or 0)))}


def purge_trash(root: Path | str, *, token: str = "") -> dict:
    """清空回收站:不可还原,所以**必须打字确认**。"""
    v = verify("清空回收站", token)
    if not v.get("ok"):
        return {"ok": False, "reason": v.get("reason")}
    box = _trash_root(root)
    n = len([d for d in box.iterdir() if d.is_dir()]) if box.is_dir() else 0
    shutil.rmtree(box, ignore_errors=True)
    return {"ok": True, "清掉": n}
