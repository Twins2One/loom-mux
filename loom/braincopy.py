"""把**另一本书**的「违禁词」「文风参考」抄到这本书里。

作者 2026-10-18 裁定:不做全局违禁词库/全局范文库(R20 的全局接线撤掉),
只做**单本**的,外加一个「从另一本书复制」的便捷口。理由也说得通:一类题材的
禁词与文风往往是一套的,但别的题材不该被这套绑住 —— 于是「复制」比「全局」更贴用。

三条纪律:
  ① **只抄这两个文件**,别的什么都不碰(不碰正文、不碰账本、不碰 loom.toml);
  ② 目标已存在时**默认不覆盖**,返回两边的字节数让人自己判断;
  ③ 不写任何「来源标记」进正文内容 —— 文件会进提示词,注入标记等于污染语气。
     来源信息只出现在接口返回值里,由界面显示。
"""

from __future__ import annotations

from pathlib import Path

from .fsutil import atomic_write_text
from .paths import BANNED_REL, brain_rel

# kind → 书内相对路径。新增种类时**必须**同时给 paths 里的常量,别在这里硬写字面量。
KINDS: dict[str, str] = {
    "违禁词": BANNED_REL,
    "文风参考": brain_rel("文风参考"),
}
MAX_BYTES = 2 * 1024 * 1024      # 200 万字符级别就够用;再大多半是贴错了文件


def _rel(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"不认识的种类「{kind}」(只认 {' / '.join(KINDS)})")
    return KINDS[kind]


def _is_book(root: Path | str) -> bool:
    return (Path(root) / "loom.toml").is_file()


def _template_text(rel: str) -> str | None:
    """这本书「刚建好、还没被人动过」时,该文件的内容就是模板原文。读不到就返回 None。"""
    from .scaffold import TEMPLATES_DIR

    f = TEMPLATES_DIR / rel
    try:
        return f.read_text(encoding="utf-8") if f.is_file() else None
    except (OSError, UnicodeDecodeError):
        return None


def _is_untouched(dst: Path, rel: str) -> bool:
    """目标文件是否仍是模板原样(= 没人填过)。"""
    tpl = _template_text(rel)
    if tpl is None or not dst.is_file():
        return False
    try:
        return dst.read_text(encoding="utf-8") == tpl
    except (OSError, UnicodeDecodeError):
        return False


def candidates(root: Path | str, kind: str = "") -> dict:
    """书架上**除这本书以外**、含有该文件的书。纯只读。"""
    from .userconf import list_projects

    me = Path(root).resolve()
    kinds = [kind] if kind else list(KINDS)
    for k in kinds:
        _rel(k)      # 校验种类
    out = []
    for p in list_projects():
        try:
            r = Path(p.get("root") or "").resolve()
        except (OSError, ValueError):
            continue
        if not r.name or r == me or not _is_book(r):
            continue
        have = {k: (r / KINDS[k]).is_file() for k in kinds}
        if not any(have.values()):
            continue
        item = {"名字": p.get("title") or r.name, "root": str(r),
                "有": [k for k, v in have.items() if v]}
        for k in kinds:
            f = r / KINDS[k]
            if f.is_file():
                try:
                    item[k + "字节"] = f.stat().st_size
                except OSError:
                    pass
        out.append(item)
    return {"ok": True, "候选": out, "落盘": False}


def copy_from(root: Path | str, from_root: Path | str, kind: str, *,
              overwrite: bool = False) -> dict:
    """把 from_root 的这个文件抄进 root。目标已存在且没让覆盖 → 拒绝并报两边字节。"""
    dst_root = Path(root).resolve()
    src_root = Path(from_root).resolve()
    rel = _rel(kind)
    if not _is_book(dst_root):
        return {"ok": False, "code": "not_a_book", "reason": f"{dst_root} 不是一本书(没有 loom.toml)。"}
    if not _is_book(src_root):
        return {"ok": False, "code": "src_not_a_book",
                "reason": f"{src_root} 不是一本书,不敢从那里抄。"}
    if src_root == dst_root:
        return {"ok": False, "code": "same_book", "reason": "不能从同一本书抄自己。"}
    src = src_root / rel
    if not src.is_file():
        return {"ok": False, "code": "src_missing",
                "reason": f"那本书里没有「{kind}」({rel})。"}
    try:
        text = src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {"ok": False, "code": "src_unreadable", "reason": f"读不出来:{e}"}
    if len(text.encode("utf-8")) > MAX_BYTES:
        return {"ok": False, "code": "too_big",
                "reason": f"那个文件超过 {MAX_BYTES // 1024 // 1024} MB,多半不是{kind}。"}
    dst = dst_root / rel
    # 「已有内容」要排除**模板原样**:新书一建就带着这两份空模板,那不是人的内容。
    _had = dst.is_file() and not _is_untouched(dst, rel)
    if _had and not overwrite:
        have = dst.stat().st_size
        return {"ok": False, "code": "target_exists",
                "reason": (f"这本书里已经有「{kind}」了(现有 {have} 字节,"
                           f"要抄过来的这份 {src.stat().st_size} 字节)。"
                           "确认要覆盖再点一次。"),
                "现有字节": have, "来源字节": src.stat().st_size}
    atomic_write_text(dst, text)
    return {"ok": True, "种类": kind, "文件": rel, "字节": len(text.encode("utf-8")),
            "来自": str(src_root), "覆盖了原有的": bool(_had and overwrite),
            "盖掉了模板": bool(dst.is_file() and not _had),
            "可撤销": False,
            "说明": "只抄了这一个文件;要退回去,去那本书里重新抄一份,或手写覆盖。"}
