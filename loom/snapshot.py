# -*- coding: utf-8 -*-
"""R18:全库快照 / 一键恢复。

设计取舍(都写在这里,免得以后有人当 bug 改回去):
- **快照只装稿子**:`正文/`, `外置大脑/` 里的 .md,加上 `loom.toml`。备份目录、指纹历史、
  待确认标记这类**派生/临时**文件不进快照 —— 它们能重建,进去只会把快照撑大。
- **恢复永不删文件**:快照里没有的文件一律不动(可能是人后来写的)。多出来的会在返回值里报出来。
- **恢复前先给自己拍一张**:万一恢复错了还能回去。这是「可撤销」那级护栏的要求。
- **恢复要输入快照名**:R19 的「需输入确认」那级,别让人手滑点一下就回退整本书。
- **绝不碰 `.快照/` 自己**:否则一次恢复会把历史全填成同一份。
"""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

SNAP_REL = "外置大脑/.快照"
_ROOT_FILES = ("loom.toml",)
# `.原稿` 不在这里:它是 learn 的 diff 基线(rewrite.py 的「快照 vs 正文」句级 diff),
# 不是能重建的派生文件 —— 排掉它,快照就"全库"名不副实,learn 失明时也救不回来。
_SKIP_DIRS = {".快照", ".批量备份", ".指纹历史", ".回收站", ".拆书"}


def _book(root: Path | str) -> Path:
    r = Path(root)
    if not (r / "loom.toml").is_file():
        raise ValueError(f"{r} 不是 loom 项目(没有 loom.toml)")
    return r


def _collect(root: Path) -> list[Path]:
    """要进快照的文件(相对路径),顺序稳定才便于比对。"""
    out: list[Path] = []
    for name in _ROOT_FILES:
        p = root / name
        if p.is_file():
            out.append(p)
    for top in ("正文", "外置大脑"):
        d = root / top
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            rel = p.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts):
                continue
            out.append(p)
    return sorted(out, key=lambda p: str(p.relative_to(root)))


def make(root: Path | str, note: str = "") -> dict:
    """拍一张快照。**只读稿子,不改任何正文。**"""
    r = _book(root)
    files = _collect(r)
    if not files:
        return {"ok": False, "说明": "这本书还没有任何 .md 稿子,没什么可拍的"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = r / SNAP_REL / stamp
    n = 0
    while dest.exists():                      # 同一秒拍两张:加后缀,别互相覆盖
        n += 1
        dest = r / SNAP_REL / f"{stamp}-{n}"
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    for p in files:
        rel = p.relative_to(r)
        tgt = dest / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, tgt)
        total += p.stat().st_size
    manifest = dest / "MANIFEST.tsv"
    manifest.write_text("\n".join(f"{p.relative_to(r)}\t{p.stat().st_size}" for p in files) + "\n",
                        encoding="utf-8")
    return {"ok": True, "名称": dest.name, "文件数": len(files), "字节": total,
            "说明": f"拍了 {len(files)} 个文件(共 {total // 1024} KB)" + (f";{note}" if note else "")}


def snapshots(root: Path | str) -> list[dict]:
    r = _book(root)
    base = r / SNAP_REL
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        mf = d / "MANIFEST.tsv"
        try:
            lines = [ln for ln in mf.read_text(encoding="utf-8").splitlines() if ln.strip()]
            size = sum(int(ln.rsplit("\t", 1)[1]) for ln in lines if "\t" in ln)
        except (OSError, ValueError):
            lines, size = [], 0
        out.append({"名称": d.name, "文件数": len(lines), "字节": size,
                    "时间": time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime))})
    return out


def snap_dir(root: Path | str, name: str):
    """快照名 → 快照目录。**名字必须是单层目录名**,不许带路径。

    为什么非卡不可:`r / SNAP_REL / "C:/Windows"` 在 pathlib 里会被绝对路径**顶掉**,
    于是恢复会把书外目录里的文件一个个拷进书里;verify 更松,连 typed 都不看。
    「用户照着名字打字」是给人看的闸门,不能当作唯一防线。
    """
    r = _book(root)
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or ":" in name:
        return None
    base = (r / SNAP_REL)
    if not base.is_dir():
        return None
    try:
        base_r = base.resolve()
        p2 = (base / name).resolve()
    except OSError:
        return None
    if p2 == base_r or base_r not in p2.parents:
        return None          # 越界(含 ..、符号链接绕路)
    return p2 if p2.is_dir() else None


def restore(root: Path | str, name: str, typed: str = "") -> dict:
    """把某一版快照盖回盘上。**恢复前自动给自己再拍一张。**"""
    r = _book(root)
    name = (name or "").strip()
    if not name:
        return {"ok": False, "说明": "要说清恢复哪一版"}
    if typed.strip() != name:
        # R19「需输入确认」:输错名字就不动盘,别让人手滑回退整本书
        return {"ok": False, "说明": f"确认名不对:请照着输入「{name}」", "需要输入": name}
    src = snap_dir(r, name)
    if src is None:
        return {"ok": False, "说明": f"没有这一版快照:{name}(快照名只能是单层目录名,不能带路径)"}
    safety = make(r, note="恢复前自动留底")
    files = [p for p in src.rglob("*") if p.is_file() and p.name != "MANIFEST.tsv"]
    written, same = 0, 0
    for p in files:
        rel = p.relative_to(src)
        tgt = r / rel
        try:
            if tgt.is_file() and tgt.read_bytes() == p.read_bytes():
                same += 1
                continue
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, tgt)
            written += 1
        except OSError as e:
            return {"ok": False, "说明": f"恢复中途失败({rel}):{e}", "恢复了几文件": written,
                    "还原前快照": safety.get("名称")}
    return {"ok": True, "恢复了几文件": written, "没变几文件": same,
            "还原前快照": safety.get("名称"),
            "说明": f"从「{name}」恢复了 {written} 个文件(另有 {same} 个本来就和快照一样)。"
                    f"快照里没有的文件一律没动 —— 那些可能是你后来写的。"}


def verify(root: Path | str, name: str) -> dict:
    """只读校验:快照里每个文件是否还在盘上、内容是否一致。"""
    r = _book(root)
    src = snap_dir(r, name)
    if src is None:
        return {"ok": False, "说明": f"没有这一版快照:{name}(快照名只能是单层目录名,不能带路径)"}
    missing, differ, ok = [], [], 0
    for p in src.rglob("*"):
        if not p.is_file() or p.name == "MANIFEST.tsv":
            continue
        rel = p.relative_to(src)
        tgt = r / rel
        if not tgt.is_file():
            missing.append(str(rel))
        elif hashlib.sha256(tgt.read_bytes()).digest() != hashlib.sha256(p.read_bytes()).digest():
            differ.append(str(rel))
        else:
            ok += 1
    return {"ok": not missing and not differ, "一致": ok, "缺": missing[:20], "不同": differ[:20]}
