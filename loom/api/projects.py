"""loom.api.projects —— /api/version /genres /project* /projects* /doctor /backup /file /history* /backend* /fs* /prefs /study/routes /ready /debug/injection /memory/count /writing_mode /config(从 server.py 原样搬家)。"""
from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictInt

from .. import cancel, events, paths, usecases
from ..backends import (PROVIDERS, LoomBackendError, list_models, probe as probe_backend, provider_catalog, validate_model)
from ..config import (CONCURRENCY_MAX, CONCURRENCY_MIN, Config, clamp_concurrency,
                      load_config, parse_context_len, save_config, set_provider_key)
from ..doctor import report, run_checks
from ..fsutil import snapshot_file, atomic_write_text, list_history, safe_join, snapshot_chapter
from ..scaffold import available_genres
from ..scaffold import init as scaffold_init
from . import _common
from ._common import (_chapter_nums, _checked_root, _err_json, _is_project,
                      _project_root, _shelve, _state)
from .. import usecases as _uc

router = APIRouter()

def get_backend(cfg):
    from .. import server as _srv
    return _srv.get_backend(cfg)


def cheap_backend(cfg):
    from .. import server as _srv
    return _srv.cheap_backend(cfg)



# 项目全景响应体单一真相在 usecases.project_state,server 只留薄壳
# ----------------------------- 项目 -----------------------------
class CreateBody(BaseModel):
    name: str
    parent: str
    genre: str | None = None
    idea: str = ""        # 一句话设定(可空):存 loom.toml,AI 铺设定底稿的种子
    platform: str = ""    # 目标平台(可空):写立项卡平台行,定违禁词自检基线
    outline: str = ""     # 全书大纲(可空):建书时填就落进 外置大脑/全书大纲.md(裁定 Q5)
    stop_after: str = ""  # 一章跑到哪一站就停(空=跑满五步):大纲师=只到细纲 / 写手=只到初稿

class RootBody(BaseModel):
    root: str

@router.get("/api/version")
def version():
    from .. import __version__
    return {"version": __version__}

@router.get("/api/genres")
def genres():
    return {"genres": available_genres()}

@router.get("/api/project/check")
def project_check(root: str):
    """这本书还在不在?**纯只读,不读稿子内容**。

    真机回归教训:书目录被挪走后,界面还开着那本旧书,文风档案/导出这些
    需要写盘的动作一个个回后端原始报错("不是 loom 项目(没有 loom.toml)")。
    前端拿这个端点先问一句,好给出人话提示并把动作禁掉。
    """
    p = Path(root).expanduser()
    return {"ok": True, "root": str(p),
            "exists": p.is_dir(),
            "is_book": (p / "loom.toml").is_file()}

# (_shelve 单点在 _common.py——交叉审查 P2-1：此处原有一份同体副本，删)

@router.post("/api/project/create")
def create_project(b: CreateBody):
    try:
        root = scaffold_init(b.name, Path(b.parent).expanduser(), b.genre,
                             idea=b.idea, platform=b.platform, stop_after=b.stop_after, outline=b.outline)
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    from .. import userconf
    userconf.apply_default_to_new_book(root)   # 新书继承用户级默认后端(连一次全局记住,不用每本重填)
    return _shelve(root, _state(root))

@router.get("/api/projects")
def projects_list():
    """书架:注册过的书 + 是否还在磁盘上 + 章数(欢迎页点选用)。"""
    from .. import paths as _paths
    from .. import userconf
    out = []
    for p in userconf.list_projects():
        root = Path(p.get("root", ""))
        ok = (root / "loom.toml").is_file()
        chapters = 0
        if ok:
            try:
                chapters = len(_paths.chapter_numbers(root))
            except Exception:
                pass
        out.append({**p, "exists": ok, "chapters": chapters})
    return {"projects": out}

@router.post("/api/projects/forget")
def projects_forget(b: RootBody):
    from .. import userconf
    try:
        userconf.forget_project(b.root)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return JSONResponse({"ok": False, "error": f"书架登记未更新：{exc}"}, status_code=409)
    return {"ok": True}

class ImportScanBody(BaseModel):
    folder: str

class ImportCommitBody(BaseModel):
    folder: str
    name: str
    parent: str
    routing: dict = {}

@router.post("/api/project/import/scan")
def import_scan(b: ImportScanBody):
    """扫资料夹里的 .md 和 .txt,按文件名启发路由(不落盘),供前端出确认屏。"""
    from .. import importer
    folder = Path(b.folder).expanduser()
    if not folder.is_dir():
        return JSONResponse({"error": f"{folder} 不是文件夹或不存在。"}, status_code=400)
    names = sorted(p.name for p in folder.rglob("*") if p.suffix.lower() in (".md", ".txt") and p.is_file())
    if not names:
        return JSONResponse({"error": "这个文件夹里没有 .md 或 .txt 文件。"}, status_code=400)
    routed = importer.route_files(names)
    unknown = routed.pop("unknown")
    return {"ok": True, "name_suggest": folder.name, "routed": routed, "unknown": unknown}

@router.post("/api/project/import/commit")
def import_commit(b: ImportCommitBody):
    """按确认后的 routing 机械落盘成新书,入书架,返回全景 + 降级小结。"""
    from .. import importer
    folder = Path(b.folder).expanduser()
    if not folder.is_dir():
        return JSONResponse({"error": f"{folder} 不是文件夹或不存在。"}, status_code=400)
    if not (b.name or "").strip():
        return JSONResponse({"error": "先给这本书起个名字。"}, status_code=400)
    routing = {bk: list(b.routing.get(bk, [])) for bk in importer.BUCKETS}
    try:
        root = importer.import_folder(folder, b.name.strip(), routing, Path(b.parent).expanduser())
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        return _err_json(e)
    from .. import userconf
    userconf.apply_default_to_new_book(root)   # 同 create:新书继承用户级默认后端
    # 模式终身绑定：导入的旧书要**继承源 loom.toml 的顶层
    # writing_mode**——模式跟书走，不因导入丢失（源书设了「教学」，导入后还是「教学」）。
    try:
        import tomllib as _tl
        src_toml = folder / "loom.toml"
        if src_toml.is_file():
            _wm = _tl.loads(src_toml.read_text(encoding="utf-8")).get("writing_mode")
            if _wm:
                from ..usecases import set_book_writing_mode
                set_book_writing_mode(root, str(_wm))
    except Exception:
        pass  # 源没写模式/解析失败 → 落默认，不阻断导入
    summary = importer.import_summary(root, routing)
    state = _shelve(root, _state(root))        # 同 create_project:顶层就是 project_state
    return {**state, "summary": summary}       # webui: enterProject(d) 吃 state 字段、d.summary 取小结

@router.post("/api/project/open")
def open_project(b: RootBody):
    root = Path(b.root).expanduser()
    if not _is_project(root):
        return JSONResponse({"error": f"{root} 不是 loom 项目(没有 loom.toml)。"}, status_code=400)
    try:
        return _shelve(root, _state(root))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.get("/api/project/state")
def project_state(root: str):
    # 填错路径时以前是 500 纯文本(前端 api() 只读 JSON)⇒ 人看到一句看不懂的话。
    try:
        return _state(_project_root(root))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)

@router.get("/api/doctor")
def doctor(root: str):
    """只读启动自检:检查 key/后端命令/agent/外置大脑齐不齐。"""
    return report(run_checks(Path(root)))

@router.post("/api/backup")
def backup(b: RootBody):
    from ..archive import backup_project
    try:
        return backup_project(Path(b.root).expanduser())
    except (FileNotFoundError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)

# ----------------------------- 文件 -----------------------------
class FileBody(BaseModel):
    root: str
    rel: str
    content: str
    # 客户端上次读到的那一版的 sha256。给了就做「乐观锁」:对不上说明磁盘上被别处改过,
    # 回 409 needs_merge 并把当前内容一起给回去,让人自己决定要不要覆盖。
    base_hash: str = ""
    origin: str = "unknown"
    force: bool = False          # 人看完冲突后明确要覆盖,才允许             

@router.get("/api/file")
def read_file(root: str, rel: str):
    try:
        p = safe_join(str(_project_root(root)), rel)   # root 先必须是书,再挡穿越
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not p.exists():
        return JSONResponse({"error": f"文件不存在:{rel}"}, status_code=404)
    raw = p.read_bytes()
    import hashlib as _hl
    return {"rel": rel, "content": raw.decode("utf-8"), "hash": _hl.sha256(raw).hexdigest()}

@router.put("/api/file")
def write_file(b: FileBody):
    try:
        p = safe_join(str(_project_root(b.root)), b.rel)   # root 先必须是书,再挡穿越
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    from .. import guardrail as _gr
    # 判护栏要用「落盘后真实的相对路径」,不是请求里那串字符:
    # 正文/./.原稿/x / 正文/../正文/.原稿/x / 正文//.原稿/x 归一后才现形(交叉审查 P0-2)。
    try:
        _root_abs = Path(_project_root(b.root)).resolve()
        _canon = p.resolve().relative_to(_root_abs).as_posix()
    except ValueError:
        return JSONResponse({'error': '这个路径不在书目录内。', 'code': 'out_of_book'}, status_code=400)
    _why = _gr.protected(_canon)
    if _why:                       # 禁止级位置(原稿/回收站/指纹)不接受手写覆盖
        return JSONResponse({"error": _why, "code": "protected"}, status_code=403)
    # ① 批量任务跑着的时候不许整份写:抽章纲/铺章纲会一行行改卡章纲,这时自动保存一盖,
    #    刚跑出来的几十行就没了。宁可让这次保存失败并明确告诉人,也不静默丢数据。
    #
    # 真机回归教训:这里原来**无条件**抢锁,把「PUT /api/file 豁免写锁」
    # 这条红线作废了(红线写在 usecases.py 模块 docstring、本文件 54-55 行、navigator spec:23)。
    # 后果:整章生成要跑几分钟,期间作者存一下世界观必然 409「正在写作中」——
    # 这是作者面向的操作,必须活。现在按落点分:
    #   · 正文/ 章节文件:照旧要锁(写章期间整份覆盖正文会打掉流水线的产出);
    #   · 卡章纲:照旧要锁(批量任务正在逐行重写它,整份盖会吞掉新行);
    #   · 其余(外置大脑/设定集、人物卡、文风参考…)一律豁免 —— 这些不在流水线的写路径上。
    from .. import usecases as _uc2
    _needs_lock = _canon.startswith("正文/") or _canon.endswith("卡章纲.md")
    if _needs_lock and _uc2.is_busy(_root_abs):
        return JSONResponse({"error": "这本书正有任务在跑(导入/铺章纲),先等它跑完再改。",
                             "code": "busy"}, status_code=409)
    # ② 乐观锁:客户端带上它上次读到的版本 hash,磁盘上已经变了就停下来问人。
    if b.base_hash and p.is_file() and not b.force:
        import hashlib as _hl
        _cur = _hl.sha256(p.read_bytes()).hexdigest()
        if _cur != b.base_hash:
            return JSONResponse({"error": "磁盘上这份文件在别处被改过了,要不要用你这一版盖掉?",
                                 "code": "needs_merge",
                                 "current": p.read_text(encoding="utf-8"),
                                 "current_hash": _cur}, status_code=409)
    # ③ 真拿锁:上面的 is_busy 只是预检,预检与写盘之间还有窗口,流水线正好在这一瞬开工,
    #    就是「谁最后落盘谁赢」。拿到锁只占几毫秒。豁免落点见 ①(只有正文/卡章纲需要锁)。
    _lock = _uc2.try_lock(_root_abs) if _needs_lock else None
    if _needs_lock and _lock is None:
        return JSONResponse({"error": "这本书正有任务在跑(导入/铺章纲),先等它跑完再改。",
                             "code": "busy"}, status_code=409)
    try:
        snapshot_file(str(_root_abs), _canon)   # 覆盖前留一版历史:正文章节按章号存,其它 .md 按文件名存
        atomic_write_text(p, b.content)        # 原子写盘:崩溃/断电不会把正文截成空或半截
        from .. import edit_origins
        edit_origins.record(_root_abs, _canon, b.origin, content=b.content)
    finally:
        if _lock is not None:
            _lock.release()
    import hashlib as _hl2
    return {"ok": True, "hash": _hl2.sha256(p.read_bytes()).hexdigest()}

class RenameFileBody(BaseModel):
    root: str
    rel: str
    name: str
    base_hash: str = ""
    confirm: bool = False


@router.post("/api/file/rename")
def rename_file(b: RenameFileBody):
    from ..document_names import RenameError, rename_document
    try:
        root = Path(_project_root(b.root)).resolve()
        lock = usecases.try_lock(root)
        if lock is None:
            return JSONResponse({"error": "本书有任务在运行，请稍后改名。", "code": "busy"}, status_code=409)
        try:
            return rename_document(root, b.rel, b.name, b.base_hash, b.confirm)
        finally:
            lock.release()
    except RenameError as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=exc.status)
    except ValueError as exc:
        return JSONResponse({"error": str(exc), "code": "invalid_path"}, status_code=400)


@router.get("/api/history")
def history(root: str, rel: str):
    return {"versions": list_history(root, rel)}

class RestoreBody(BaseModel):
    root: str
    rel: str
    id: str

@router.post("/api/history/restore")
def history_restore(b: RestoreBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        content = usecases.history_restore(b.root, b.rel, b.id)  # 回滚前会先把当前版本也存一份
    except (ValueError, FileNotFoundError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "content": content}

# ----------------------------- 后端配置 -----------------------------
class ConfigBody(BaseModel):
    root: str
    provider: str
    model: str
    chapter_chars: int
    base_url: str | None = None
    api_key: str | None = None
    context_len: StrictInt = 0

@router.get("/api/backend/probe")
def backend_probe(provider: str):
    return probe_backend(provider)

@router.get("/api/backend/ping")
def backend_ping():
    """欢迎页用的「模型还活着吗」。**只读、不花 token**:打一次 GET /models,不发生成请求。

    每次打开欢迎页自动测一次,不可用就让接入卡片红闪两下提醒。
    探的是**用户级默认后端**(欢迎页还没开书,没有书级配置可读)。
    返回 {ok, models, error};拿不到模型列表就算不可用(连不上 / 鉴权失败 / 模型名不存在)。
    """
    from ..backends import list_models
    from .. import userconf
    d = userconf.load_default_backend()
    provider = (d.get("provider") or "deepseek").lower()
    # key 住在 ~/.loom/.env(load_config 平时会顺带装进 env;欢迎页没有书可读,这里显式装一次)
    try:
        from dotenv import load_dotenv
        from ..config import user_env_path
        load_dotenv(user_env_path(), override=True)
    except Exception:      # noqa: BLE001 - 没有 .env 就按无 key 试(有的端点不需要)
        pass
    key_env = (PROVIDERS.get(provider) or {}).get("key_env", "")
    api_key = os.environ.get(key_env, "") if key_env else ""
    try:
        r = list_models(provider, base_url=d.get("base_url") or "", api_key=api_key or "")
    except Exception as e:      # noqa: BLE001 - 探活失败只回报,不抛
        return {"ok": False, "provider": provider, "error": f"{type(e).__name__}: {e}"}
    models = r.get("models") or []
    good = bool(r.get("ok")) and bool(models)
    return {"ok": good, "provider": provider, "model": d.get("model", ""), "models": len(models),
            "error": "" if good else (r.get("message") or "没拿到模型列表")}

@router.get("/api/fs/dirs")
def fs_dirs():
    """建书页「放在哪个文件夹」的候选:桌面 / 文档 / 下载 / 家目录的**绝对路径**。

    浏览器拿不到系统文件夹选择器,但服务端知道真实路径 —— 给前端做 datalist 候选,
    作者点一下就能填对,不用手打。只读,不列目录内容。
    """
    home = Path.home()
    cand = [("桌面", home / "Desktop"), ("文档", home / "Documents"),
            ("下载", home / "Downloads"), ("家目录", home)]
    out = []
    for label, p in cand:
        if p.is_dir():
            out.append({"label": label, "path": str(p)})
    return {"ok": True, "dirs": out}

# 目录浏览（浏览器模式选文件夹用；WebView2 窗口仍走系统选择框）。
# 安全口径：只列目录不列文件、不跟随符号链接出根之外没有限制（本机单用户桌面应用，
# 与 /api/file 同口径——服务本来就是本地信任进程）。
class BrowseBody(BaseModel):

    path: str = ""          # 空=常用位置列表

@router.post("/api/fs/browse")

def fs_browse(b: BrowseBody):

    """列某个目录的子目录（供前端目录浏览浮窗导航选文件夹）。

    path 空 → 返回常用位置（桌面/文档/下载/家目录/盘符根）。"""

    if not b.path.strip():

        home = Path.home()

        import string as _s

        drives = [Path(f"{c}:\\") for c in _s.ascii_uppercase

                  if Path(f"{c}:\\").exists()]

        cand = [("桌面", home / "Desktop"), ("文档", home / "Documents"),

                ("下载", home / "Downloads"), ("家目录", home)] + [\

                (p.anchor, p) for p in drives]

        out = [{"label": lb, "path": str(p)} for lb, p in cand if p.is_dir()]

        return {"ok": True, "current": "", "parent": None, "dirs": out}

    p = Path(b.path).expanduser()

    if not p.is_dir():

        return JSONResponse({"error": f"{p} 不是文件夹。"}, status_code=400)

    sub = []

    try:

        for c in sorted(p.iterdir()):

            if c.is_dir() and not c.name.startswith("."):

                sub.append({"label": c.name, "path": str(c)})

    except OSError as e:

        return JSONResponse({"error": f"读不了这个目录:{e}"}, status_code=400)

    parent = str(p.parent) if p.parent != p else None

    return {"ok": True, "current": str(p), "parent": parent, "dirs": sub}

# ---- 用户级默认后端(接入模型:连一次全局记住,新书自动继承)----
class DefaultBackendBody(BaseModel):
    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    context_len: StrictInt = 0

@router.get("/api/backend/default")
def get_default_backend():
    """欢迎页「接入模型」回显:当前用户级默认后端 + 各家 key 是否已设 + 供应商目录。"""
    from .. import userconf
    d = userconf.load_default_backend()
    keys_set = {pid: (spec["kind"] == "cli" or userconf.default_key_is_set(pid))
                for pid, spec in PROVIDERS.items()}
    declared = max(0, int(d.get("context_len", 0) or 0))
    return {"provider": d.get("provider", "deepseek"), "model": d.get("model", ""),
            "base_url": d.get("base_url", ""), "has_default": userconf.has_default(),
            "context_len": declared, "context_limit": declared or 128000,
            "context_len_source": "手填" if declared else "默认",
            "keys_set": keys_set, "providers": provider_catalog()}

# 行动模式 + 记忆库长程维护开关（用户级偏好，prefs.json）。
@router.get("/api/prefs")
def get_prefs():
    from .. import userconf
    return userconf.load_prefs()

class PrefsBody(BaseModel):
    mode: str
    memory_startup: bool = False

@router.put("/api/prefs")
def put_prefs(b: PrefsBody):
    from .. import userconf
    if b.mode not in ("主动思考", "默认", "安静"):
        return JSONResponse({"error": "行动模式只能是 主动思考 / 默认 / 安静。"}, status_code=400)
    userconf.save_prefs(b.mode, b.memory_startup)
    # 行动模式热生效：写进进程环境，工具限流当下读到
    import os
    os.environ["LOOM_MEMORY_MODE"] = b.mode
    return {"ok": True, "prefs": userconf.load_prefs()}

@router.put("/api/backend/default")
def put_default_backend(b: DefaultBackendBody):
    from .. import userconf
    if b.provider == "openai_compat" and not (b.base_url or "").strip():
        return JSONResponse({"error": "选了「OpenAI 兼容(自定义)」要先填 base_url 再保存。"}, status_code=400)
    try:
        context_len = parse_context_len(b.context_len)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    userconf.save_default_backend(b.provider, b.model, (b.base_url or "").strip(),
                                  b.api_key, context_len)
    warn = validate_model(b.provider, b.model)
    return {"ok": True, "model_warning": warn}

class ModelsBody(BaseModel):
    root: str | None = None
    provider: str
    base_url: str | None = None
    api_key: str | None = None

@router.post("/api/backend/models")
def backend_models(b: ModelsBody):
    """「拉取可用模型」:OpenAI 兼容的实时打 GET /models,CLI 类返回预设。只读、不耗 token。"""
    if b.root:
        load_config(Path(b.root))   # 让项目 .env 里的 key 进 os.environ,拉取时能用上
    return list_models(b.provider, base_url=b.base_url or "", api_key=b.api_key or "")

# ── 书房的槽表 ───────────────────────
# 书房没有当前书，所以槽表存在 `~/.loom/backend.json` 的 routes 字段（见 userconf.load_user_routes）。
# 不复用 `/api/routes`：那个强依赖 root（读那本书的 loom.toml），而书房根本没有当前书。
@router.get("/api/study/routes")
def study_routes_get() -> dict:
    """书房用得上的那几个槽（不把全部10 个槽摆出来）。"""
    from .. import userconf as _uc
    from ..backends import PROVIDERS as _P
    from ..routes import PURPOSES as _PUR

    WANT = ("知识顾问", "文风学习")
    d = _uc.load_default_backend()
    from ..config import context_len_info
    base = {"provider": (d.get("provider") or ""), "model": (d.get("model") or ""),
        "base_url": (d.get("base_url") or "")}
    base.update(context_len_info(d.get("context_len", 0), provider=base["provider"],
                                 model=base["model"], purpose="默认"))
    saved = _uc.load_user_routes()
    rows = []
    for purpose in WANT:
        spec = saved.get(purpose) or {}
        rows.append({
            "用途": purpose,
            "说明": _PUR.get(purpose, ""),
            "跟随默认": not (spec.get("provider") or spec.get("model") or spec.get("base_url")
                              or int(spec.get("context_len", 0) or 0)),
            "provider": spec.get("provider") or base["provider"],
            "model": spec.get("model") or base["model"],
            "base_url": spec.get("base_url") or base["base_url"],
            "context_len": int(spec.get("context_len", 0) or base["context_len"] or 0),
            "context_limit": int(spec.get("context_len", 0) or base["context_limit"]),
            "context_len_source": ("用途手填" if int(spec.get("context_len", 0) or 0) > 0
                                   else base["context_len_source"]),
            # 界面按书内那套排，所以把工具协议也带上。
            "工具协议": spec.get("tool_protocol") or "",
            "写明": {"provider": spec.get("provider") or "",
                "model": spec.get("model") or "",
                "base_url": spec.get("base_url") or "",
                "tool_protocol": spec.get("tool_protocol") or "",
                "context_len": int(spec.get("context_len", 0) or 0)}, 
        })
    providers = [{"id": k,
        "label": (v or {}).get("label", k),
        "default_model": (v or {}).get("default_model", ""),
        "needs_key": bool((v or {}).get("needs_key"))}
        for k, v in _P.items()]
    return {"ok": True, "默认": base, "槽": rows, "供应商": providers}

class StudyRouteBody(BaseModel):
    purpose: str
    provider: str = ""
    model: str = ""
    base_url: str = ""
    context_len: StrictInt = 0

@router.post("/api/study/routes")
def study_routes_set(b: StudyRouteBody) -> dict:
    """改一个书房槽；全空 = 恢复「跟随默认」。"""
    from .. import userconf as _uc
    try:
        context_len = parse_context_len(b.context_len)
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return _uc.save_user_route(b.purpose, provider=b.provider, model=b.model,
        base_url=b.base_url, context_len=context_len)

    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
@router.put("/api/config")
def update_config(b: ConfigBody):
    root = Path(b.root)
    base_url = (b.base_url or "").strip()
    if b.provider == "openai_compat" and not base_url:
        return JSONResponse({"error": "选了「OpenAI 兼容(自定义)」要先填这家供应商的 base_url(接口地址),再保存。"},
                            status_code=400)
    # 自定义供应商必须说清模型 id:空串发上去,上游只会回「未知模型 可用:…」,而人看不出是自己把框清空了
    # (真机教训:模型框空着还能点保存 —— 一次手滑就把 model 写成空,整套按用途的槽全废)。
    if b.provider == "openai_compat" and not (b.model or "").strip():
        return JSONResponse({"error": "模型名不能为空:先点「拉取可用模型」从服务器清单里选一个,"
                                      "或手动填一个服务器上真实存在的模型 id。", "code": "model_required"},
                            status_code=400)
    # 读-改-写要原子:不然和 /api/routes(改同一个文件)并发时,刚设的槽会被悄悄写回旧值(N-2)
    lock = _uc.try_lock(root)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再改配置。", "code": "project_busy"},
                            status_code=409)
    try:
        try:
            cfg = load_config(root)
        except (ValueError, OSError) as e:      # noqa: BLE001 - 读坏了要说清,不许裸 500(N-3')
            return JSONResponse({"error": f"这本书的 loom.toml 读不了:{e}", "code": "bad_config"},
                                status_code=400)
        # 只覆盖界面上真改的那几项,其余**原样继承**(P0-1:原来在这里新建只填 6 个字段的 Config,
        # 于是 [routes] 八槽、[search]、一句话设定、停在哪一站、agent模式、便宜模型、伏笔章距、
        # 除虫开关,全被整文件重写静默抹掉)。
        try:
            context_len = parse_context_len(b.context_len)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        save_config(root, dataclasses.replace(cfg, provider=b.provider, model=b.model,
                                              base_url=base_url, chapter_chars=b.chapter_chars,
                                              context_len=context_len))
        if b.api_key:
            # key 按供应商的 key_env(PROVIDERS 注册表声明)各落各行:DeepSeek/智谱/Kimi/…互不覆盖
            key_env = (PROVIDERS.get(b.provider) or {}).get("key_env")
            if key_env:
                set_provider_key(root, key_env, b.api_key.strip())
        st = _state(root)
        warn = validate_model(b.provider, b.model)   # 软提示:只提示,不阻断保存
        if warn:
            st["model_warning"] = warn
        return st
    finally:
        lock.release()

# ----------------------------- seed / learn -----------------------------
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # LearnReport → 响应字段逐字节兼容(前端 app.js 零改动)
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
# ----------------------------- 局部重写 -----------------------------
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
# ----------------------------- write(流式) -----------------------------
# ── 写作视图侧栏的分卷分章树 ─────────────────────────
# 数据源：volplan.volume_ranges（卷起止）+ card_state（卡章纲）+ 正文目录（已完成）。
# 可写判定：卷内顺序（前一章有正文）；跨卷解锁（卷一起止内最后一章有正文 → 下一卷第1章可写）。
# 教学模式：可写章仍返回 writing_mode="教学"，前端隐藏编织按钮只留 ✎。
            # 卷内顺序：章 n 可写 = 前一章已有正文；
            # 跨卷解锁（裁定原话「第一卷没写完，就可以写第二卷第一章」）：
            # **每卷第一章无条件可写**——卷首不受上一卷进度牵制（卷首是大纲师的排程起点，
            # 卡章纲没排也能先织，大纲师可边写边补）。
                # ③混合流:该章有审稿留痕(.审稿留痕/第N章.md 非空)→ 前端亮「审」徽标
    # 没有卷纲 → 空态引导
# ── 全面引导注册表 ──────────────────────────────────
# GET /api/guides/pending：当前该触发的引导（when=True 且 done_when=False 且未 seen）。
# POST /api/guides/seen {id}：标记已读/已完成（永不重复轰炸）。
# POST /api/guides/wizard-done：首启引导完成（开过一本书即算）。
    # context=import-folder → 前端在动作入口显式拉取，允许 front_triggered 型出现
# ── 调试端点（批 D 场景 6 断言用）─────────────────────────────────────
# GET /api/debug/injection → ask.py 的 LAST_INJECTION（最近一次自动记忆注入统计）
# GET /api/memory/count?root= → 该书记忆库条数（superseded_by IS NULL，本书+全局）
from pydantic import BaseModel as _BM

class _EmptyBody(_BM):
    path: str = ""

@router.get("/api/debug/injection")
def debug_injection():
    from ..study import ask as _ask
    return dict(getattr(_ask, "LAST_INJECTION", {}))

@router.get("/api/memory/health")
def memory_health(root: str = ""):
    """设置栏记忆库面板读数（T7）。不可支持项显式 None + unsupported，不显示虚构 0。"""
    from .. import memory as _m
    try:
        return _m.health(_project_root(root) if root else "")
    except Exception as e:      # noqa: BLE001 - 面板不许把界面弄崩
        return {"总条数": None, "已向量化": None, "维度": None, "过时": None,
                "置顶": None, "软删除": None,
                "范围": {"本书": None, "全局": None},
                "unsupported": {"置顶": "读数失败", "软删除": "读数失败"},
                "引擎": {"可用": None, "缺": None, "已加载": None,
                         "last_err": "%s: %s" % (type(e).__name__, e),
                         "形态": "进程内 ONNX"},
                "错误": "%s: %s" % (type(e).__name__, str(e)[:120])}


@router.post("/api/memory/recover")
def memory_recover():
    """手动「修复」= 重新加载模型 + 重新探测依赖（不是重启服务）。"""
    from .. import memory as _m
    return _m.recover()


# ── 记忆库修复/抢救（用户裁定②·三按钮之②③；2026-11-12 需求方派单）────────────
# ②重建记忆索引 = 可取消后台任务（start/status/cancel 三端点）；
# ③导出记忆数据 = 只读抢救（单端点）。与 ①recover（重载引擎）严格分开。
@router.post("/api/memory/rebuild/start")
def memory_rebuild_start(root: str = ""):
    """启动重建（按钮②）。root 给=只收该书；不给=收所有书。已有重建在跑则拒。"""
    from .. import memory_repair as _mr
    try:
        r = _project_root(root) if root else ""
        return _mr.rebuild_index(str(r) if r else None)
    except HTTPException:
        raise
    except Exception as e:      # noqa: BLE001 - 面板不许把界面弄崩
        return {"ok": False, "结果": "%s: %s" % (type(e).__name__, str(e)[:160])}


@router.get("/api/memory/rebuild/status")
def memory_rebuild_status():
    """读重建进度（N/M）与结果/取消/失败原因。"""
    from .. import memory_repair as _mr
    try:
        return _mr.rebuild_status()
    except Exception as e:      # noqa: BLE001
        return {"status": "idle", "错误": "%s: %s" % (type(e).__name__, str(e)[:120])}


@router.post("/api/memory/rebuild/cancel")
def memory_rebuild_cancel():
    """取消重建：新库丢弃、旧库原样。"""
    from .. import memory_repair as _mr
    try:
        return _mr.cancel_rebuild()
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "结果": "%s: %s" % (type(e).__name__, str(e)[:160])}


@router.post("/api/memory/export")
def memory_export(root: str = ""):
    """导出记忆数据（按钮③）。只读：不写书、不写库。"""
    from .. import memory_repair as _mr
    try:
        r = _project_root(root) if root else ""
        return _mr.export_memory(str(r) if r else None)
    except HTTPException:
        raise
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "结果": "%s: %s" % (type(e).__name__, str(e)[:160])}


@router.get("/api/library/root")
def library_root_status():
    """设置页「知识库位置」：当前根、来源（env/配置/默认）、是否存在、可否回退。"""
    from .. import libroot
    try:
        return libroot.status()
    except Exception as e:      # noqa: BLE001 - 面板不许把界面弄崩
        return {"根": None, "来源": None, "存在": None, "默认": None,
                "回退目标": None, "可回退": False,
                "错误": "%s: %s" % (type(e).__name__, str(e)[:120])}


class LibraryRootBody(_BM):
    root: str = ""
    migrate: bool = False
    source: str = ""     # 迁移源（首次启动时应传探测到的 old_root，而非"当前根"）


@router.post("/api/library/root")
def library_root_set(body: LibraryRootBody):
    """改知识库位置。**两条路语义不同，别当成同一个调用复用**：

    - `migrate=false` → `repoint`：**只改指针，不搬数据**。目标目录必须已存在
      （通常是你自己已经把知识库放好了，或从别处拷完了）。没有"源"这个概念。
    - `migrate=true`  → `migrate`：**复制 + 逐文件校验 + 切换**。必须显式给 `source`（要搬的源），
      `root` 是目标。校验不过不改指针，源目录不动。
    - `revert`（另一个端点）→ 把指针切回上一次的位置。

    `migrate=true` 但没给 `source` ⇒ **明确报错，不静默拿"当前知识库根"顶上**：
    首次启动时那个根正是还空着的新默认位置，静默兜底会让迁移走不动且看不出根因。
    两种都返回 `{ok, 结果, ...}`；从不 500——失败要让界面能照原话说出来。
    """
    from .. import libroot
    try:
        if body.migrate:
            src = (body.source or "").strip()
            if not src:
                return {"ok": False,
                        "结果": "迁移必须指定源目录：请传 source（要搬的那个知识库路径）。"
                                "这里不能用“当前知识库根”兜底——首次启动时它正是那个还空着的新位置，"
                                "迁移会走不动。只是想把指针指到已备好的目录，请传 migrate:false。"}
            return libroot.migrate(body.root, source_root=src)
        return libroot.repoint(body.root)
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "结果": "%s: %s" % (type(e).__name__, str(e)[:160])}


@router.get("/api/library/migration")
def library_migration_detect():
    """首次启动检测：要不要提示迁移旧知识库。

    前端启动时调一次；`code=="none"` 就什么都别弹（`原因` 只用于排查，不上界面）。
    纯文件系统检查，不调模型、不耗 token；检测失败一律返回 none，不阻塞启动。
    """
    from .. import libroot
    try:
        return libroot.detect_migration()
    except Exception as e:      # noqa: BLE001 - 双保险：任何意外都不许挡住启动
        return {"code": "none", "old_root": None, "new_root": None, "问": "",
                "候选": [], "可迁移": False,
                "原因": "检测失败：%s" % type(e).__name__}


class LibraryDismissBody(_BM):
    old_root: str = ""


@router.post("/api/library/migration/dismiss")
def library_migration_dismiss(body: LibraryDismissBody):
    """作者选「不用了」→ 记住这个旧位置，以后不再提示（只记标记，不动数据）。"""
    from .. import libroot
    try:
        return libroot.dismiss_migration(body.old_root)
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "结果": "%s: %s" % (type(e).__name__, str(e)[:160])}


@router.post("/api/library/root/revert")
def library_root_revert():
    """回退到上一次的位置（只切指针，不动数据）。"""
    from .. import libroot
    try:
        return libroot.revert()
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "结果": "%s: %s" % (type(e).__name__, str(e)[:160])}


@router.get("/api/memory/count")
def memory_count(root: str):
    from .. import memory as _m
    rp = Path(_project_root(root))
    norm = _m._norm_root(rp)
    with _m._LOCK:
        conn = _m._connect()
        try:
            n_book = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE superseded_by IS NULL AND book = ?",
                (norm,),
            ).fetchone()[0]
            n_global = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE superseded_by IS NULL AND book = ''",
            ).fetchone()[0]
        finally:
            conn.close()
    return {"ok": True, "本书": n_book, "全局": n_global, "条数": n_book + n_global}

# 写作模式读写
# 写作模式读写（全局默认+书独立覆盖，模型设置对话框用）。
# GET 返回 {global_mode, book_mode(可能空=未覆盖), effective}；PUT 书级覆盖（写 loom.toml）。
# 编织进行中查询：前端切档前调 /api/write/busy?root=…，进行中→弹警告。
class WritingModeBody(BaseModel):
    root: str
    mode: str

@router.get("/api/writing_mode")
def get_writing_mode(root: str = ""):
    from .. import userconf as _uc
    g = _uc.load_prefs().get("writing_mode", "FullAuto")
    book = ""
    if root:
        try:
            book = load_config(Path(_project_root(root))).writing_mode
        except Exception:
            book = ""
    effective = book or g
    busy = False
    if root:
        try:
            from .. import usecases as _uc2
            busy = _uc2.lock_held(Path(_project_root(root)))
        except Exception:
            busy = False
    return {"global_mode": g, "book_mode": book, "effective": effective, "busy": busy}

@router.put("/api/writing_mode")
def put_writing_mode(b: WritingModeBody):
    # 真跑回归教训:白名单曾硬编码三档，漏了四档新增的「剧作」——
    # 前端下拉加了第四档后 PUT 会被 400 拒掉。改引 WRITING_MODES 单源。
    from ..config import WRITING_MODES as _WM
    if b.mode not in _WM:
        return JSONResponse({"error": "写作模式只有 " + " / ".join(_WM) + "。"}, status_code=400)
    # 书级覆盖：写进 loom.toml（writing_mode 字段）
    from .. import usecases as _uc2
    try:
        _uc2.set_book_writing_mode(Path(_project_root(b.root)), b.mode)
    except Exception as e:
        return JSONResponse({"error": f"写不进去：{e}"}, status_code=500)
    return {"ok": True, "mode": b.mode}

# 本章细纲（卡章纲）读取（树里点「纲」徽标要能看大纲）。
# 返回 {章, 有, 内容}——内容取自 外置大脑/卡章纲.md 里该章的行（volplan.card_state 口径）。
# ── 手动+审查混合流 ────────────────────────────────
# 辅助/教学模式：作者手动填细纲/正文 → 可手动跑「责任编辑」「编辑总监」单棒。 
# 单棒运行 = run_pipeline(from_step=棒名, stop_after=棒名)——断点续跑机制白捡： 
# 作者手动改的细纲/正文落产物位后，上游指纹变化让下游自然重算。
# 手动注入 = 作者内容直接落 paths.chapter_path / outline_path（产物位即真相源）。
    # 教学模式只允许审查两棒（教学=作者手写，AI 只打分纠错不代笔）
    # 字数软诊断（不硬拦）
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 教学模式=AI 完全不代笔。后端硬拦（前端按钮也隐藏，
    # 但不能只靠前端——直接 POST 照样能触发）。返回 403 + 人话。
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
@router.get("/api/ready")
def api_ready(root: str, chapter: int = 0):
    """素材准备度(R7):**纯只读组装**,列出「这一章开写前缺什么」。

    **绝不挂进写稿前置**——它是说明,不是门槛:带缺项照样能写。为此硬禁三件会写盘/要 LLM 的东西:
    `continuity.scan_chapter`(会写留痕与账本)、`diagnose.scan`(要 LLM)、`fingerprint.learn`。
    响应带 `落盘: False`,前端据此不把它当「应用」。
    """
    from .. import budget, hooks, journey, statebook
    from ..parse import is_substantive

    try:
        r = _project_root(root)     # 填错路径给 400 + JSON,不再是 500 纯文本(交叉审查 UI-P1-5)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    cfg = load_config(r)
    nums = _chapter_nums(r)
    n = chapter or (max(nums) + 1 if nums else 1)
    missing: list[dict] = []
    ok_items: list[str] = []

    # ① 卡章纲这一行填没填
    card = r / paths.CARD_REL
    line = ""
    if card.is_file():
        for ln in card.read_text(encoding="utf-8").splitlines():
            if re.match(rf"^-\s*第{n}章\s*[:：]", ln.strip()):
                line = ln.strip()
                break
    task = re.sub(rf"^-\s*第{n}章\s*[:：]", "", line).strip() if line else ""
    if task:
        ok_items.append("章纲行")
    else:
        missing.append({"项": "章纲行", "说明": f"卡章纲里第 {n} 章还没有一句话任务",
                        "在": str(paths.CARD_REL)})

    # ② 本章分镜细纲
    op = paths.outline_path(r, n)
    if op.is_file() and is_substantive(op.read_text(encoding="utf-8")):
        ok_items.append("分镜细纲")
    else:
        missing.append({"项": "分镜细纲", "说明": f"第 {n} 章的分镜细纲还没生成(写手会用章纲硬写)",
                        "在": paths.OUTLINE_DIR})

    # ③ 上一章的账入没入(不入账,后面的连续性检测就失明)
    if n > 1:
        book = r / paths.STATEBOOK_REL
        has = book.is_file() and statebook.has_section(book.read_text(encoding="utf-8"), n - 1)
        if has:
            ok_items.append("上章已入账")
        else:
            missing.append({"项": "上章未入账", "说明": f"第 {n - 1} 章还没写进状态账本(人设状态/剧情线会漂)",
                            "在": paths.STATEBOOK_REL})

    # ④ 悬空伏笔
    stale = hooks.stale(r, n, cfg.foreshadow_distance)
    if stale:
        missing.append({"项": "伏笔悬空", "说明": f"有 {len(stale)} 条伏笔超过 {cfg.foreshadow_distance} 章没推进",
                        "例": [i.desc for i in stale[:3]]})

    # ⑤ 顶层两份规划:全书大纲看有没有实质内容;卷纲看**切得出卷**没有
    #   (出厂模板里 `## 第一卷` 只是占位:卷名与主线都没填 → budget.volumes 不算分过卷)
    ov = r / paths.OVERVIEW_REL
    if ov.is_file() and is_substantive(ov.read_text(encoding="utf-8")):
        ok_items.append("全书大纲")
    else:
        missing.append({"项": "全书大纲未填", "说明": "全书大纲空着:大纲师只能按章纲那一句硬写",
                        "在": paths.OVERVIEW_REL})
    vols = budget.volumes(r)
    if vols:
        ok_items.append(f"已分卷({len(vols)} 卷)")
    else:
        missing.append({"项": "卷纲未分卷", "说明": "卷纲里还没有「- 起止:第A-B章」,引擎不知道卷界(不猜)",
                        "在": paths.VOLUMES_REL})
    gate_ok, gate_missing = journey.writing_unlocked(r)

    return {"ok": True, "chapter": n, "落盘": False,
            "可开写": gate_ok, "门槛缺项": gate_missing,
            "缺项": missing, "齐备": ok_items, "已分卷": len(vols),
            "提示": "这些只是提醒:带缺项也能写。写稿前不会拦你。"}

