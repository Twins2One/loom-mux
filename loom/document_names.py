"""User-confirmed names for character/custom documents; fixed structural paths stay fixed."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .fsutil import safe_join


class RenameError(ValueError):
    def __init__(self, message: str, code: str = "invalid_name", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def renameable(rel: str) -> bool:
    parts = Path(rel.replace("\\", "/")).parts
    return (len(parts) == 4 and parts[:2] == ("外置大脑", "设定集")
            and parts[2] in ("20-人物", "50-自定义")
            and parts[3].lower().endswith(".md")
            and parts[3].casefold() not in ("readme.md", "索引.md", "成长档案.md")
            and not parts[3].startswith((".", "_")))


def rename_document(root: Path, rel: str, name: str, base_hash: str, confirmed: bool) -> dict:
    """Keep content bytes and directory intact; an existing destination always wins.

    Caller holds the project lock. A hard link reserves the new name exclusively;
    only then is the old directory entry retired. A filesystem without hard-link
    support reports failure, leaving the original document untouched.
    """
    if not confirmed:
        raise RenameError("请先在浮窗确认旧文件名和新文件名。", "confirmation_required")
    source = safe_join(str(root), rel)
    canonical = source.relative_to(root.resolve()).as_posix()
    if not renameable(canonical):
        raise RenameError("此文件属于固定结构，请使用人物或自定义设定文档的改名入口。", "fixed_document", 403)
    if not source.is_file() or source.is_symlink():
        raise RenameError("原文件不存在或不是普通文档。", "not_found", 404)
    name = str(name or "")
    if (not name or name != name.strip() or name.endswith((".", " "))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or not name.lower().endswith(".md") or name.startswith(".")
            or len(name) > 160
            or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name)):
        raise RenameError("请输入有效的 Markdown 文件名（保留 .md，不能含路径或特殊字符）。")
    if source.parent.name == "20-人物":
        source_type = re.split(r"[·・•]", source.stem, maxsplit=1)
        if len(source_type) == 2:
            expected = source_type[0] + "·"
            new_type = re.split(r"[·・•]", Path(name).stem, maxsplit=1)
            if (len(new_type) != 2 or new_type[0] != source_type[0] or not new_type[1].strip()
                    or "未命名" in Path(name).stem):
                raise RenameError(f"请保留人物类型前缀「{expected}」，后面填写人物姓名。", "character_type_required")
    new_rel = source.with_name(name).relative_to(root.resolve()).as_posix()
    if not renameable(new_rel):
        raise RenameError("这个文件名留给固定结构使用。", "fixed_document", 403)
    current_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if not base_hash or base_hash != current_hash:
        raise RenameError("文件已变化，请重新打开后再确认改名。", "stale_file", 409)
    if name == source.name:
        return {"ok": True, "rel": canonical, "hash": current_hash, "unchanged": True}
    target = source.with_name(name)
    try:
        os.link(source, target)
    except FileExistsError:
        raise RenameError("同目录已存在这个文件名，请换一个。", "name_exists", 409)
    except OSError as exc:
        raise RenameError("文件系统暂不支持安全改名，原文件保持原状。", "rename_unavailable", 409) from exc
    try:
        source.unlink()
    except OSError:
        target.unlink()
        raise RenameError("原文件正被占用，改名未完成，请稍后重试。", "file_busy", 409)
    return {"ok": True, "rel": new_rel, "hash": current_hash}
