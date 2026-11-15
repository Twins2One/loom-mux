"""书房 · 知识库只读层。

**这是 0.8.0 前端壳的第一块地基，刻意做成完全新增、不依赖任何既有模块。**
理由（作者 2026-10-26）：工程以稳定为主，先让新东西自己站稳，再谈接线。

职责只有一个：把磁盘上的知识库目录读成一份结构化清单（分类 → 科目 → 册）。
不做编辑、不做写入、不缓存——每次调用现场扫，保证看到的就是盘上的。

目录约定（三层，见 docs/需求文档-Loom-mux-0.8.0-书房.md）：

    <根>/
      教材/      ← 网文创作夜课 25 册等
      语料库/    ← 叙事理论、蒸馏产物
      素材/      ← 个人图书馆：笔记、转写、拆书产出
      _索引.md / _映射表.json   ← 下划线开头 = 元数据，对用户隐藏

**兼容期**：如果根下还没有那三个分类目录（当前就是这种状态），
就把所有一级科目目录当作「教材」返回。这样三层结构将来落地时无需改代码。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

# 分类目录名 → 稳定键。顺序即界面顺序。
CATEGORIES: list[tuple[str, str]] = [
    ("教材", "textbook"),
    ("语料库", "corpus"),
    ("素材", "material"),
]

# 兜底分类：根下没有分类目录时，一级科目都归到这里
_FALLBACK_KEY = "textbook"
_FALLBACK_NAME = "教材"

# 册文件后缀
_VOLUME_SUFFIXES = (".md",)

# 只读的元数据文件前缀（对用户隐藏）
_HIDDEN_PREFIX = "_"


def library_root() -> Path:
    """知识库根目录。**口径唯一收在 `loom/libroot.py`**，这里只是转一手。

    2026-11-11 作者裁定：不再硬编码 `E:\\Book\\知识库`，默认落在 Loom 根目录下的
    `知识库/`；设置里可改路径、可迁移（见 libroot.migrate）。环境变量
    `LOOM_LIBRARY_ROOT` 仍然最优先，所以测试隔离与临时迁移不受影响。
    """
    from .. import libroot

    return libroot.library_root()


def _is_hidden(name: str) -> bool:
    return name.startswith(_HIDDEN_PREFIX)


def _volume_info(path: Path, subject: str, category: str) -> dict[str, Any]:
    """一册的元信息。只做 stat，不读正文——读正文太慢，交给按需接口。"""
    try:
        st = path.stat()
    except OSError:
        return {}
    size = st.st_size
    return {
        "name": path.stem,
        "file": path.name,
        "path": str(path),
        "subject": subject,
        "category": category,
        "bytes": size,
        # 中文 UTF-8 约 3 字节/字，据此粗估字数；仅用于界面显示"约多大"，不作精确统计
        "chars_estimate": int(size / 3),
        "mtime": int(st.st_mtime),
    }


def _scan_subject(subject_dir: Path, category: str) -> dict[str, Any] | None:
    """扫一个科目目录，返回该科目的册清单。空目录返回 None（不显示空科目）。"""
    volumes: list[dict[str, Any]] = []
    try:
        entries = sorted(subject_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file():
            continue
        if _is_hidden(entry.name):
            continue
        if entry.suffix.lower() not in _VOLUME_SUFFIXES:
            continue
        info = _volume_info(entry, subject_dir.name, category)
        if info:
            volumes.append(info)
    if not volumes:
        return None
    total = sum(v["chars_estimate"] for v in volumes)
    return {
        "name": subject_dir.name,
        "category": category,
        "count": len(volumes),
        "chars_estimate": total,
        "volumes": volumes,
    }


def _scan_category(category_dir: Path, key: str, name: str) -> dict[str, Any] | None:
    """扫一个分类目录（教材/语料库/素材），返回其下科目清单。"""
    subjects: list[dict[str, Any]] = []
    try:
        entries = sorted(category_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        if _is_hidden(entry.name):
            continue
        subject = _scan_subject(entry, key)
        if subject:
            subjects.append(subject)
    if not subjects:
        return None
    return {
        "key": key,
        "name": name,
        "dir": str(category_dir),
        "count": sum(s["count"] for s in subjects),
        "subjects": subjects,
    }


def scan_library(root: Path | None = None) -> dict[str, Any]:
    """扫描整个知识库，返回 分类 → 科目 → 册 的三层结构。

    永不抛异常：根不存在、无权限、单个目录读失败，都以"返回空/跳过"处理，
    并在结果里带上 warnings，方便前端提示而不是白屏。
    """
    base = Path(root) if root is not None else library_root()
    started = time.time()
    warnings: list[str] = []
    categories: list[dict[str, Any]] = []

    if not base.exists():
        return {
            "root": str(base),
            "exists": False,
            "categories": [],
            "warnings": [f"知识库根目录不存在：{base}"],
            "scanned_ms": 0,
        }

    has_three_layer = any((base / name).is_dir() for name, _ in CATEGORIES)

    if has_three_layer:
        # 三层结构已就位：按分类读
        for name, key in CATEGORIES:
            cdir = base / name
            if not cdir.is_dir():
                continue
            cat = _scan_category(cdir, key, name)
            if cat:
                categories.append(cat)
    else:
        # 兼容期：根下直接就是科目目录，全部当作「教材」
        subjects: list[dict[str, Any]] = []
        try:
            entries = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            warnings.append(f"读取根目录失败：{exc}")
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            if _is_hidden(entry.name):
                continue
            subject = _scan_subject(entry, _FALLBACK_KEY)
            if subject:
                subjects.append(subject)
        if subjects:
            categories.append({
                "key": _FALLBACK_KEY,
                "name": _FALLBACK_NAME,
                "dir": str(base),
                "count": sum(s["count"] for s in subjects),
                "subjects": subjects,
                "flat": True,   # 标记：这是兼容模式读出来的
            })
            warnings.append("知识库尚未分层（教材/语料库/素材），当前把一级科目全部归入「教材」")

    total_volumes = sum(c["count"] for c in categories)
    return {
        "root": str(base),
        "exists": True,
        "layered": has_three_layer,
        "categories": categories,
        "total_volumes": total_volumes,
        "warnings": warnings,
        "scanned_ms": int((time.time() - started) * 1000),
    }


def read_volume(path: str | Path, max_bytes: int = 4 * 1024 * 1024) -> dict[str, Any]:
    """按需读一册的正文。超过 max_bytes 拒绝——防止误点巨型文件把进程拖住。

    只放行 .md。知识库根下还有 _索引.md、_映射表.json、迁移记录等文件，
    读接口若来者不拒，等于把整个知识库目录开成下载口子。

    读不到、后缀不对、太大，一律返回 {"error": ...}，绝不返回半截正文。
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {"error": f"文件不存在：{p}"}
    if p.suffix.lower() not in _VOLUME_SUFFIXES:
        return {"error": f"只允许读取 {'/'.join(_VOLUME_SUFFIXES)} 文件：{p.name}"}
    try:
        size = p.stat().st_size
        if size > max_bytes:
            return {"error": f"文件过大（{size} 字节），超过 {max_bytes} 上限"}
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": f"读取失败：{exc}"}
    except UnicodeDecodeError:
        try:
            text = p.read_text(encoding="utf-8-sig")
        except Exception as exc:  # noqa: BLE001 - 兜底，宁可报错也不返回半截
            return {"error": f"编码无法识别：{exc}"}
    return {
        "name": p.stem,
        "path": str(p),
        "text": text,
        "bytes": size,
    }
