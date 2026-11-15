"""知识库根目录：**单一解析口径** + 可回退的迁移。

背景（作者 2026-11-11 裁定）：知识库不再硬编码 `E:\\Book\\知识库`，
默认落在 **Loom 根目录** 下的 `知识库/`；设置里可改路径、可迁移已有知识库。
硬编码那台机器的目录也是开源时的隐私暴露点，这次一并解决。

三个消费方原本各自写死一份默认值（`study/library.py`、`guides.py`、`stylepack.py`），
迟早漂——这里收成一处，它们只许调 `library_root()`。

**优先级**（高 → 低）：
1. 环境变量 `LOOM_LIBRARY_ROOT`（测试隔离 / 临时迁移）
2. 用户配置 `LOOM_HOME/library.json` 的 `root`
3. 默认：`LOOM_HOME/知识库`

配置文件独立于 `prefs.json`，不跟行动模式/长程维护那两个字段挤在一起——
`save_prefs()` 是整体覆盖写的，混进去会被它悄悄抹掉。

**迁移规则（作者要求逐条守住）**：
- 只做「复制 + 校验 + 切换」，绝不动源目录；指针切换前必须先校验通过。
- 校验逐文件比大小与 sha256，条数/字节/摘要三项全对才算通过。
- 切换只写 `library.json`；失败一律不改指针，于是回退 = 把指针切回 `previous`。
- 源与目标不得互为父子（会把知识库搬进自己肚子里）。
- 目标是已存在的非空目录时拒绝——不拿别人的目录当靶子。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .config import user_config_dir
from .fsutil import atomic_write_text

# 迁移/切指针的互斥锁（进程内）。迁移是真搬数据的长动作，不允许两个一起跑。
_SWITCH_LOCK = threading.Lock()

# 默认落点：Loom 根目录（= LOOM_HOME）下的这个目录名
DEFAULT_DIRNAME = "知识库"

# 配置文件名（与 backend.json / prefs.json 同层）
_CONFIG_NAME = "library.json"

# 校验时读文件的块大小
_CHUNK = 1 << 20

# 历史默认落点候选（作者 2026-11-11：不要硬编码单一值，做成候选列表便于以后追加）。
# **只放"已知"位置，不做全盘扫描**——多一个候选就是多一次磁盘探测，
# 而猜错会把别人的目录当成知识库，提示出来的迁移就是在搬错东西。
# 追加新位置就加一行；检测按顺序取第一个"有内容且互不包含"的候选。
# ⚠️ 这些是**历史默认位置**，只供迁移检测（老用户的书原来建在这里）——
# 它们**不是**当前默认值；当前默认见 `DEFAULT_DIRNAME`（Loom 根目录下）。
LEGACY_CANDIDATES: tuple[str, ...] = (
    r"E:\Book\知识库",
)


# ---------- 解析 ----------

def loom_root() -> Path:
    """Loom 根目录（配置家）。与 userconf/backends 用的是同一个 LOOM_HOME。"""
    return user_config_dir()


def default_root() -> Path:
    """默认知识库：Loom 根目录 / 知识库。"""
    return loom_root() / DEFAULT_DIRNAME


def load_config() -> dict:
    """读 library.json；没有/坏 JSON → {}（当没配过，不做迁移）。"""
    p = user_config_dir() / _CONFIG_NAME
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:   # noqa: BLE001 - 坏配置当没配过，不能拖死开书
        return {}


def _save_config(d: dict) -> None:
    atomic_write_text(user_config_dir() / _CONFIG_NAME,
                      json.dumps(d, ensure_ascii=False, indent=2))


def configured_root() -> Path | None:
    """用户配置里的知识库根；没配过返回 None。"""
    raw = str(load_config().get("root") or "").strip()
    return Path(raw) if raw else None


def library_root() -> Path:
    """知识库根目录（三处消费方的唯一口径）。

    注意：**不保证目录存在**——不存在时上游该按"空知识库"处理，而不是报错。
    """
    raw = os.environ.get("LOOM_LIBRARY_ROOT")
    if raw and raw.strip():
        return Path(raw.strip())
    cfg = configured_root()
    if cfg is not None:
        return cfg
    return default_root()


def source() -> str:
    """当前根目录来自哪一层：env / 配置 / 默认。面板要如实说，不能瞎猜。"""
    raw = os.environ.get("LOOM_LIBRARY_ROOT")
    if raw and raw.strip():
        return "env"
    return "配置" if configured_root() is not None else "默认"


def status() -> dict:
    """设置页要的一段：当前根、来源、是否存在、回退目标。

    `可回退` 只在 previous 确实存在且不等于当前时才为 True——
    不做"按钮亮着但按了没反应"的东西。
    """
    cfg = load_config()
    root = library_root()
    prev_raw = str(cfg.get("previous") or "").strip()
    prev = Path(prev_raw) if prev_raw else None
    return {
        "根": str(root),
        "来源": source(),
        "存在": root.is_dir(),
        "默认": str(default_root()),
        "回退目标": str(prev) if prev else None,
        "可回退": bool(prev and prev != root and prev.is_dir()),
        "迁移时间": cfg.get("migrated_at"),
    }


# ---------- 目录体检 ----------

def _fingerprint(base: Path) -> dict:
    """目录指纹：文件数、总字节、逐文件 (相对路径 → (大小, sha256))。

    `_` 开头的元数据文件也算——迁移漏掉它们，界面上的索引就对不上了。
    """
    files: dict[str, tuple[int, str]] = {}
    total = 0
    if not base.is_dir():
        return {"文件数": 0, "字节": 0, "文件": files}
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        try:
            h = hashlib.sha256()
            size = 0
            with p.open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    h.update(chunk)
                    size += len(chunk)
        except OSError:
            files[rel] = (-1, "")      # 读不了 ≠ 没问题，指纹里如实留痕
            continue
        files[rel] = (size, h.hexdigest())
        total += size
    return {"文件数": len(files), "字节": total, "文件": files}


def _overlaps(a: Path, b: Path) -> bool:
    """互为父子（或同一目录）返回 True。"""
    try:
        ra, rb = a.resolve(), b.resolve()
    except OSError:
        ra, rb = a.absolute(), b.absolute()
    return ra == rb or ra in rb.parents or rb in ra.parents


# ---------- 迁移 ----------

def migrate(target: Path | str, *, source_root: Path | str | None = None) -> dict:
    """公开入口：**同一时刻只允许一个迁移/切换在跑**，第二个直接拒（不排队、不并发复制）。

    为什么必须挡：两个并发 migrate 会各自建暂存目录、各自全量复制，
    同一秒内还会撞同一个暂存名；真撞上就是"搬了俩、谁也没说清哪个算数"。
    这是进程内锁（Loom 是本地单进程应用，够用）；跨进程并发不在本锁范围内。
    """
    if not _SWITCH_LOCK.acquire(blocking=False):
        return {"ok": False, "结果": "已有迁移或切换正在进行，请等它结束再试",
                "源": str(source_root or ""), "目标": str(target), "暂存": None}
    try:
        return _migrate_locked(target, source_root=source_root)
    finally:
        _SWITCH_LOCK.release()


def _migrate_locked(target: Path | str, *, source_root: Path | str | None = None) -> dict:
    """把知识库迁到 target：复制 → 校验 → 切换。**任何一步不过都不改指针。**

    返回 `{ok, 结果, 源, 目标, 校验, 回退目标, 暂存}`；从不抛异常。
    源目录始终保持原样（作者要求：不能只改指针不动数据；源留着，回退才成立）。

    `source_root` 必须显式给：首次启动迁移时"当前知识库"还是那个空的新默认位置，
    拿它当源会就地失败——真正要搬的是探测到的旧库。
    省略时仍退回"当前知识库根"，但**只要这个回退值不可用，就明确说"未指定迁移源"**，
    不再让它伪装成"源与目标不能是同一个目录"这种看不出根因的报错。
    """
    dst = Path(str(target)).expanduser()
    explicit = source_root is not None and str(source_root).strip() != ""
    src = Path(str(source_root)) if explicit else library_root()
    out: dict = {"ok": False, "源": str(src), "目标": str(dst), "暂存": None}

    if not str(target).strip():
        return {**out, "结果": "没给目标路径"}
    if not explicit and (not src.is_dir() or _overlaps(src, dst)):
        why = "不存在" if not src.is_dir() else "与目标相同或互相包含"
        return {**out, "结果": f"未指定迁移源（source）：当前知识库根是 {src}，它{why}。"
                                f"请显式传入要搬的那个知识库路径。"}
    if _overlaps(src, dst):
        return {**out, "结果": "源与目标不能是同一个目录，也不能互相包含"}
    if not src.is_dir():
        return {**out, "结果": f"源知识库不存在：{src}"}
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        return {**out, "结果": f"目标已存在且非空，拒绝覆盖：{dst}"}

    # 1) 复制到暂存目录（同一父目录下，便于最后一步原子改名）
    staging = dst.parent / f".{dst.name}.迁移中-{int(time.time())}"
    out["暂存"] = str(staging)
    try:
        staging.mkdir(parents=True, exist_ok=False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        for p in src.rglob("*"):
            rel = p.relative_to(src)
            if p.is_dir():
                (staging / rel).mkdir(parents=True, exist_ok=True)
            elif p.is_file():
                (staging / rel).parent.mkdir(parents=True, exist_ok=True)
                (staging / rel).write_bytes(p.read_bytes())
    except OSError as e:
        # 不删暂存：让用户能看见搬了一半的东西在哪，自行处置
        return {**out, "结果": f"复制失败：{e}", "校验": None}

    # 2) 校验：逐文件大小 + sha256，条数/字节/摘要三项全对
    a, b = _fingerprint(src), _fingerprint(staging)
    bad = [k for k in a["文件"] if a["文件"][k] != b["文件"].get(k)]
    missing = [k for k in a["文件"] if k not in b["文件"]]
    extra = [k for k in b["文件"] if k not in a["文件"]]
    ok = (a["文件数"] == b["文件数"] and a["字节"] == b["字节"] and not bad)
    checks = {
        "源文件数": a["文件数"], "目标文件数": b["文件数"],
        "源字节": a["字节"], "目标字节": b["字节"],
        "不一致": bad[:20], "缺": missing[:20], "多": extra[:20],
        "通过": ok,
    }
    out["校验"] = checks
    if not ok:
        return {**out, "结果": "校验未通过，指针未改动（暂存目录保留待查）"}

    # 3) 切换：暂存改名成正式目录，再写指针
    try:
        if dst.is_dir():                     # 可能是刚才校验过的空目录
            dst.rmdir()
        os.replace(staging, dst)
        out["暂存"] = None
    except OSError as e:
        return {**out, "结果": f"就位失败（校验已通过，指针未改）：{e}"}

    prev = str(library_root())
    cfg = load_config()
    cfg.update({"root": str(dst), "previous": prev, "migrated_at": int(time.time())})
    try:
        _save_config(cfg)
    except OSError as e:
        return {**out, "结果": f"数据已在目标位，但写入配置失败：{e}"}

    return {**out, "ok": True, "结果": "已迁移并切换", "回退目标": prev}


def repoint(target: Path | str) -> dict:
    """只改指针（不搬数据）：用户自己已经把知识库放好了，或只是想切回旧位置。

    要求目标目录已存在——指到一个空地方，界面会显示"没有知识库"，
    那不是错误，但用户多半是打错路径了，得当场说清楚。
    （2026-11-16 独立复审 P2-3 指出：首启弹层的「用新位置」会因此成为死路。
     不改这里的契约——auto 建目录会让打错的路径凭空长出文件夹——
     改成 detect_migration 回传「新位置存在」，由前端禁用该按钮并说明。）
    迁移进行中会被拒（否则指针在搬运途中被改走，搬完不知道该落到谁身上）。
    """
    if not _SWITCH_LOCK.acquire(blocking=False):
        return {"ok": False, "结果": "已有迁移或切换正在进行，请等它结束再试"}
    try:
        dst = Path(str(target)).expanduser()
        if not str(target).strip():
            return {"ok": False, "结果": "没给目标路径"}
        if not dst.is_dir():
            return {"ok": False, "结果": f"目标目录不存在：{dst}"}
        prev = str(library_root())
        cfg = load_config()
        cfg.update({"root": str(dst), "previous": prev})
        try:
            _save_config(cfg)
        except OSError as e:
            return {"ok": False, "结果": f"写入配置失败：{e}"}
        res: dict = {"ok": True, "结果": "已切换", "根": str(dst), "回退目标": prev}
        # 切到一个空目录 = 界面马上变成"没有知识库"。不是错误（用户可能就是先建个空的），
        # 但必须当场说出来，否则作者会以为自己的书没了。
        try:
            if not any(dst.iterdir()):
                res["提醒"] = ("目标目录是空的，切换后知识库会是空的（旧数据仍在原位置，"
                               "可用「回退」切回去）。要搬内容请用「迁移」而不是「切换」。")
        except OSError:
            pass
        return res
    finally:
        _SWITCH_LOCK.release()


def revert() -> dict:
    """回退到上一次的根（迁移失败/新位置不满意时用）。数据不动，只切指针。"""
    if not _SWITCH_LOCK.acquire(blocking=False):
        return {"ok": False, "结果": "已有迁移或切换正在进行，请等它结束再试"}
    try:
        cfg = load_config()
        prev = str(cfg.get("previous") or "").strip()
        if not prev:
            return {"ok": False, "结果": "没有可回退的位置"}
        if not Path(prev).is_dir():
            return {"ok": False, "结果": f"上一次的位置已不存在：{prev}"}
        cur = str(library_root())
        cfg["root"] = prev
        cfg["previous"] = cur
        try:
            _save_config(cfg)
        except OSError as e:
            return {"ok": False, "结果": f"写入配置失败：{e}"}
        return {"ok": True, "结果": "已回退", "根": prev, "回退目标": cur}
    finally:
        _SWITCH_LOCK.release()


# ---------- 首次启动：旧知识库迁移提示 ----------
# 作者裁定 2026-11-11「两者都做」：(a) 首次启动检测提示 + (b) 设置内迁移入口。
# 这里只负责 (a) 的检测与"不用了"的记忆；真迁移仍走 migrate()，由作者确认后触发。
#
# 克制保留：**不自动搬数据、不自动删旧数据、不做全盘搜索**——只探已知候选列表。
# 检测纯文件系统动作，不调模型、不耗 token；任何异常一律当"没有可迁移项"，绝不阻塞启动。


def _has_content(p: Path) -> bool:
    """目录里是否至少有一个文件（空目录不算"有内容"）。找到第一个就停，不扫全库。"""
    if not p.is_dir():
        return False
    try:
        for q in p.rglob("*"):
            if q.is_file():
                return True
    except OSError:
        return False
    return False


def _empty_or_absent(p: Path) -> bool:
    """不存在，或存在但是空目录。"""
    if not p.exists():
        return True
    if not p.is_dir():
        return False        # 同名文件挡路：不算空，宁可不动
    try:
        return not any(p.iterdir())
    except OSError:
        return False


def _dismissed_roots() -> set[str]:
    out: set[str] = set()
    for d in load_config().get("migration_dismissed") or []:
        if isinstance(d, dict) and d.get("old_root"):
            out.add(str(d["old_root"]))
    return out


def detect_migration() -> dict:
    """首次启动检测：是否该提示"把旧知识库迁到新位置"。

    三个条件**同时**成立才提示（作者要求）：
    1. 从未配置过（`library.json` 里没有 `root`）；
    2. 某个旧候选目录存在且有内容；
    3. 新默认位置不存在或为空。

    另加两道自我保护：新旧路径相同或互为父子 ⇒ 不需要迁移；
    作者点过"不用了"的候选 ⇒ 不再提。

    返回 `{code, old_root, new_root, 问, 候选, 原因}`；`code` 只会是
    `library_migration_available` 或 `none`，从不抛异常。
    """
    base: dict = {"code": "none", "old_root": None, "new_root": None,
                  "问": "", "候选": [], "可迁移": False}
    try:
        new_root = default_root()
        base["new_root"] = str(new_root)
        # 2026-11-16（独立复审 P2-3）：弹层要据此决定「用新位置」能不能点 ——
        # repoint 要求目标已存在，而这里"不存在"也算可迁移，前端不知道就会给个必失败的按钮。
        try:
            base["新位置存在"] = new_root.is_dir()
        except OSError:
            base["新位置存在"] = False
        if os.environ.get("LOOM_LIBRARY_ROOT"):
            return {**base, "原因": "知识库位置已被环境变量指定"}
        if configured_root() is not None:
            return {**base, "原因": "已配置过知识库位置"}

        if not _empty_or_absent(new_root):
            return {**base, "原因": "新默认位置已有内容"}

        dismissed = _dismissed_roots()
        cands: list[str] = []
        for raw in LEGACY_CANDIDATES:
            old = Path(raw)
            if str(old) in dismissed:
                continue
            if _overlaps(old, new_root):      # 同一个目录/互相包含：搬它没有意义
                continue
            if not _has_content(old):
                continue
            cands.append(str(old))
        if not cands:
            return {**base, "原因": "没有发现旧知识库"}

        old_root = cands[0]
        return {
            **base,
            "code": "library_migration_available",
            "old_root": old_root,
            "问": f"发现旧知识库（{old_root}），要迁移到新位置吗？",
            "候选": cands,
            "可迁移": True,
        }
    except Exception as e:      # noqa: BLE001 - 检测失败不许挡住启动
        return {**base, "原因": "检测失败：%s" % type(e).__name__}


def dismiss_migration(old_root: str) -> dict:
    """记住"不用了"：这个候选以后不再提示。别的候选日后出现仍会提。"""
    raw = str(old_root or "").strip()
    if not raw:
        return {"ok": False, "结果": "没给旧位置"}
    try:
        cfg = load_config()
        lst = [d for d in (cfg.get("migration_dismissed") or []) if isinstance(d, dict)]
        if not any(str(d.get("old_root")) == raw for d in lst):
            lst.append({"old_root": raw, "at": int(time.time())})
        cfg["migration_dismissed"] = lst
        _save_config(cfg)
    except OSError as e:
        return {"ok": False, "结果": f"写入配置失败：{e}"}
    return {"ok": True, "结果": "已记住，不再提示", "不再提示": raw}
