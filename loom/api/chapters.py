"""loom.api.chapters —— /api/chapters/* /api/chapter/*(从 server.py 原样搬家)。"""
from __future__ import annotations

import json
import queue
import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import cancel, events, paths, usecases
from ..backends import LoomBackendError, list_models
from ..config import load_config
from ..fsutil import atomic_write_text
from . import _common
from ._common import (_bad_chapter, _chapter_nums, _checked_root, _err_json, _fail,
                      _project_root, _state)

router = APIRouter()

def get_backend(cfg):
    from .. import server as _srv
    return _srv.get_backend(cfg)


def cheap_backend(cfg):
    from .. import server as _srv
    return _srv.cheap_backend(cfg)



# 项目全景响应体单一真相在 usecases.project_state,server 只留薄壳
# ----------------------------- 项目 -----------------------------
    # 模式终身绑定：导入的旧书要**继承源 loom.toml 的顶层
    # writing_mode**——模式跟书走，不因导入丢失（源书设了「教学」，导入后还是「教学」）。
    # 填错路径时以前是 500 纯文本(前端 api() 只读 JSON)⇒ 人看到一句看不懂的话。
# ----------------------------- 文件 -----------------------------
    # 客户端上次读到的那一版的 sha256。给了就做「乐观锁」:对不上说明磁盘上被别处改过,
    # 回 409 needs_merge 并把当前内容一起给回去,让人自己决定要不要覆盖。
    # 判护栏要用「落盘后真实的相对路径」,不是请求里那串字符:
    # 正文/./.原稿/x / 正文/../正文/.原稿/x / 正文//.原稿/x 归一后才现形(交叉审查 P0-2)。
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
    # ② 乐观锁:客户端带上它上次读到的版本 hash,磁盘上已经变了就停下来问人。
    # ③ 真拿锁:上面的 is_busy 只是预检,预检与写盘之间还有窗口,流水线正好在这一瞬开工,
    #    就是「谁最后落盘谁赢」。拿到锁只占几毫秒。豁免落点见 ①(只有正文/卡章纲需要锁)。
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
# ----------------------------- 后端配置 -----------------------------
    # key 住在 ~/.loom/.env(load_config 平时会顺带装进 env;欢迎页没有书可读,这里显式装一次)
# 目录浏览（浏览器模式选文件夹用；WebView2 窗口仍走系统选择框）。
# 安全口径：只列目录不列文件、不跟随符号链接出根之外没有限制（本机单用户桌面应用，
# 与 /api/file 同口径——服务本来就是本地信任进程）。
# ---- 用户级默认后端(接入模型:连一次全局记住,新书自动继承)----
# 行动模式 + 记忆库长程维护开关（用户级偏好，prefs.json）。
    # 行动模式热生效：写进进程环境，工具限流当下读到
# ── 书房的槽表 ───────────────────────
# 书房没有当前书，所以槽表存在 `~/.loom/backend.json` 的 routes 字段（见 userconf.load_user_routes）。
# 不复用 `/api/routes`：那个强依赖 root（读那本书的 loom.toml），而书房根本没有当前书。
            # 界面按书内那套排，所以把工具协议也带上。
class ChapterOpBody(BaseModel):
    root: str
    n: int
    direction: str | None = None

@router.post("/api/chapter/insert")
def chapter_insert(b: ChapterOpBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        return {**usecases.chapter_insert(b.root, b.n), **{"state": _state(Path(b.root))}}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.post("/api/chapter/move")
def chapter_move(b: ChapterOpBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        return {**usecases.chapter_move(b.root, b.n, b.direction or "up"), **{"state": _state(Path(b.root))}}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 自定义供应商必须说清模型 id:空串发上去,上游只会回「未知模型 可用:…」,而人看不出是自己把框清空了
    # (真机教训:模型框空着还能点保存 —— 一次手滑就把 model 写成空,整套按用途的槽全废)。
    # 读-改-写要原子:不然和 /api/routes(改同一个文件)并发时,刚设的槽会被悄悄写回旧值(N-2)
        # 只覆盖界面上真改的那几项,其余**原样继承**(P0-1:原来在这里新建只填 6 个字段的 Config,
        # 于是 [routes] 八槽、[search]、一句话设定、停在哪一站、agent模式、便宜模型、伏笔章距、
        # 除虫开关,全被整文件重写静默抹掉)。
            # key 按供应商的 key_env(PROVIDERS 注册表声明)各落各行:DeepSeek/智谱/Kimi/…互不覆盖
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
@router.get("/api/chapters/tree")
def chapters_tree(root: str):
    root_p = Path(_project_root(root))
    from .. import volplan as _vp
    from .. import paths as _paths
    from ..config import load_config
    cfg = load_config(root_p)
    have = set(_paths.chapter_numbers(root_p))   # 已有正文的章号
    vols = _vp.volume_ranges(root_p)
    cards = _vp.card_state(root_p)               # 章号 → 卡章纲内容（空=未排）
    max_ch = 1200
    out = []
    prev_ch_has_body = True   # 「上一章有正文」的初始语义：第1章永远可写
    for vi, v in enumerate(vols):
        lo, hi = v.get("起"), v.get("止")
        if lo is None or hi is None:
            out.append({"卷": v.get("卷"), "起": None, "止": None, "状态": "卷纲缺起止",
                        "章": [], "引导": "这一卷没写「起止:第a-b章」，先找大纲师补。"})
            continue
        chapters = []
        for n in range(int(lo), int(hi) + 1):
            if n > max_ch:
                break
            done = n in have
            # 卷内顺序：章 n 可写 = 前一章已有正文；
            # 跨卷解锁（裁定原话「第一卷没写完，就可以写第二卷第一章」）：
            # **每卷第一章无条件可写**——卷首不受上一卷进度牵制（卷首是大纲师的排程起点，
            # 卡章纲没排也能先织，大纲师可边写边补）。
            is_vol_first = (n == int(lo))
            writable = (not done) and (prev_ch_has_body or is_vol_first) and n <= max_ch
            has_outline = bool(cards.get(n))
            chapters.append({
                "章": n, "状态": "已完成" if done else ("可写" if writable else "锁定"),
                "有卡章纲": has_outline,
                # ③混合流:该章有审稿留痕(.审稿留痕/第N章.md 非空)→ 前端亮「审」徽标
                "有审稿": _paths.review_note_path(root_p, n).is_file(),
                "可写": writable,
                "路径": str(_paths.chapter_path(root_p, n)) if done else ""})
            prev_ch_has_body = done
        vol_done = sum(1 for c in chapters if c["状态"] == "已完成")
        out.append({"卷": v.get("卷"), "起": int(lo), "止": int(hi),
                    "状态": f"{vol_done}/{len(chapters)}",
                    "章": chapters, "引导": ""})
    # 没有正式卷 → 返回真正的空数组；不要造「未命名卷」占位卡。
    guide = "" if out else "还没排卷纲。先找领航员或大纲师确认卷纲，才会生成分卷结构。"
    return {"卷": out, "引导": guide, "writing_mode": cfg.writing_mode,
            "已完成判定": "六棒全跑完（润色师产出即完成）"}

# ── 全面引导注册表 ──────────────────────────────────
# GET /api/guides/pending：当前该触发的引导（when=True 且 done_when=False 且未 seen）。
# POST /api/guides/seen {id}：标记已读/已完成（永不重复轰炸）。
# POST /api/guides/wizard-done：首启引导完成（开过一本书即算）。
    # context=import-folder → 前端在动作入口显式拉取，允许 front_triggered 型出现
# ── 调试端点（批 D 场景 6 断言用）─────────────────────────────────────
# GET /api/debug/injection → ask.py 的 LAST_INJECTION（最近一次自动记忆注入统计）
# GET /api/memory/count?root= → 该书记忆库条数（superseded_by IS NULL，本书+全局）
# 写作模式读写
# 写作模式读写（全局默认+书独立覆盖，模型设置对话框用）。
# GET 返回 {global_mode, book_mode(可能空=未覆盖), effective}；PUT 书级覆盖（写 loom.toml）。
# 编织进行中查询：前端切档前调 /api/write/busy?root=…，进行中→弹警告。
    # 真跑回归教训:白名单曾硬编码三档，漏了四档新增的「剧作」——
    # 前端下拉加了第四档后 PUT 会被 400 拒掉。改引 WRITING_MODES 单源。
    # 书级覆盖：写进 loom.toml（writing_mode 字段）
# 本章细纲（卡章纲）读取（树里点「纲」徽标要能看大纲）。
# 返回 {章, 有, 内容}——内容取自 外置大脑/卡章纲.md 里该章的行（volplan.card_state 口径）。
@router.get("/api/chapters/outline")

def chapter_outline(root: str, chapter: int):

    root_p = Path(_project_root(root))

    from .. import volplan as _vp

    cards = _vp.card_state(root_p)

    body = cards.get(int(chapter), "")

    return {"章": int(chapter), "有": bool(body.strip()), "内容": body}

# ── 手动+审查混合流 ────────────────────────────────
# 辅助/教学模式：作者手动填细纲/正文 → 可手动跑「责任编辑」「编辑总监」单棒。 
# 单棒运行 = run_pipeline(from_step=棒名, stop_after=棒名)——断点续跑机制白捡： 
# 作者手动改的细纲/正文落产物位后，上游指纹变化让下游自然重算。
# 手动注入 = 作者内容直接落 paths.chapter_path / outline_path（产物位即真相源）。
class RunStepBody(BaseModel):

    root: str

    chapter: int

    step: str

    confirm_world: bool = False   # 软门未达标时，作者确认继续

@router.post("/api/chapter/run-step")

def run_step(b: RunStepBody):

    """手动跑单棒（辅助/教学模式）。从指定棒起跑、跑完即停。"""

    b.root = str(_project_root(b.root))

    root = Path(b.root)

    bad = _bad_chapter(b.chapter)

    if bad:

        return bad

    # 第一章地基硬门（2026-11-10）：开 worker/模型/任务之前统一阻断。

    from ..navigator import foundation_block

    _fb = foundation_block(root, b.chapter)

    # T1（2026-11-11）：编辑类单棒 + 第一章正文已落盘非空 = 审稿，放行地基门。
    if _fb and not usecases.review_pass_ok(root, b.chapter, b.step):

        from ._common import _foundation_response

        return _foundation_response(_fb)

    from ..agents import STEPS

    _legal = [s.role for s in STEPS]

    if b.step not in _legal:

        return JSONResponse({"error": f"没有「{b.step}」这一棒。合法：{chr(47).join(_legal)}"},

                            status_code=400)

    # 教学模式只允许审查两棒（教学=作者手写，AI 只打分纠错不代笔）

    from ..config import load_config

    cfg = load_config(root)

    if cfg.writing_mode == "教学" and b.step not in ("责任编辑", "编辑总监"):

        return JSONResponse({"error": "教学模式下只能手动跑「责任编辑」或「编辑总监」。"},

                            status_code=403)

    # 世界信息软门（2026-11-11）：硬门之后、开 worker 之前；未达标是提示不是拒绝。

    from ._common import _world_gate

    _wg = _world_gate(root, b)

    if _wg:

        return _wg

    lock = usecases.acquire_lock(root)

    cancel.clear(root)

    q: queue.Queue = queue.Queue()



    def worker():

        try:

            usecases.write_chapter(root, b.chapter, q.put, force=True, slow=0.25,

                                   from_step=b.step, stop_after=b.step,

                                   should_stop=lambda: cancel.pending(root))

        except LoomBackendError as e:

            q.put(events.error(str(e), code=e.code))

        except (ValueError, FileNotFoundError) as e:

            q.put(events.error(str(e)))

        except Exception as e:

            q.put(events.error(f"意外错误:{e}"))

        finally:

            lock.release()

            q.put(None)



    threading.Thread(target=worker, daemon=True).start()



    def stream():

        while True:

            ev = q.get()

            if ev is None:

                break

            yield json.dumps(ev, ensure_ascii=False) + "\n"



    return StreamingResponse(stream(), media_type="application/x-ndjson")

class ManualBody(BaseModel):

    root: str

    chapter: int

    kind: str          # "正文" | "细纲"

    origin: str = "unknown"
    content: str

@router.post("/api/chapter/manual")

def chapter_manual(b: ManualBody):

    """作者手写内容落流水线产物位（辅助/教学模式）。

    字数软要求：目标字数 <50% 或 >150% 返回 warning（不硬拦）。"""

    b.root = str(_project_root(b.root))

    root = Path(b.root)

    bad = _bad_chapter(b.chapter)

    if bad:

        return bad

    from .. import paths as _paths

    from ..fsutil import atomic_write_text

    if b.kind == "正文":

        p = _paths.chapter_path(root, b.chapter)

    elif b.kind == "细纲":

        p = _paths.outline_path(root, b.chapter)

    else:

        return JSONResponse({"error": "kind 只能是 正文 或 细纲。"}, status_code=400)

    p.parent.mkdir(parents=True, exist_ok=True)

    atomic_write_text(p, b.content)
    from .. import edit_origins
    edit_origins.record(root, p.relative_to(root).as_posix(), b.origin, content=b.content)

    # 字数软诊断（不硬拦）

    from ..config import load_config

    cfg = load_config(root)

    target = cfg.chapter_chars or 0

    n = len(b.content.strip())

    warning = ""

    if target and n < target * 0.5:

        warning = f"字数严重不足：当前 {n} 字，目标约 {target} 字（不足一半）。"

    elif target and n > target * 1.5:

        warning = f"字数严重超出：当前 {n} 字，目标约 {target} 字（超过一半以上）。"

    return {"ok": True, "path": str(p), "字数": n, "warning": warning}

    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 教学模式=AI 完全不代笔。后端硬拦（前端按钮也隐藏，
    # 但不能只靠前端——直接 POST 照样能触发）。返回 403 + 人话。
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # ① 卡章纲这一行填没填
    # ② 本章分镜细纲
    # ③ 上一章的账入没入(不入账,后面的连续性检测就失明)
    # ④ 悬空伏笔
    # ⑤ 顶层两份规划:全书大纲看有没有实质内容;卷纲看**切得出卷**没有
    #   (出厂模板里 `## 第一卷` 只是占位:卷名与主线都没填 → budget.volumes 不算分过卷)
class RootOnly(BaseModel):
    root: str

class ChapterStatusBody(BaseModel):
    root: str
    chapter: int
    status: str

class LeaseBody(BaseModel):
    root: str
    chapter: int
    owner: str = ""
    action: str = "acquire"     # acquire | release
    ttl: int = 0

@router.get("/api/chapter/state")
def api_chapter_state(root: str, chapter: int = 0):
    """单章状态 + 谁在写它。**只读。**"""
    from .. import chapterstate as cs

    rows = cs.load(root)
    if chapter:
        r = rows.get(chapter)
        return {"ok": True, "章": chapter, "状态": r.status if r else "未写",
                "脏": bool(r and r.dirty), "脏因": r.reason if r else "",
                "写者": cs.holder(root, chapter),
                "变过的材料": cs.diff(root, chapter)}
    return {"ok": True, "表": [{"章": n, "状态": r.status, "脏": r.dirty, "脏因": r.reason,
                                "写者": cs.holder(root, n)} for n, r in sorted(rows.items())],
            "在": cs.STATUS_REL}

@router.get("/api/chapter/dirty")
def api_chapter_dirty(root: str):
    """哪些已写章节的上游材料变过。**纯只读**(不写表、不跑模型)。"""
    from .. import chapterstate as cs

    p, bad = _checked_root(root)
    if bad:
        return bad
    return {"ok": True, "脏章": cs.dirty_now(p), "落盘": False}

@router.post("/api/chapter/sync")
def api_chapter_sync(b: RootOnly):
    """把上游变更写进状态表(打脏标)。**只打标,不重跑**——一个模型调用都不会发。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    from .. import chapterstate as cs

    return {"ok": True, **cs.sync(b.root)}

@router.post("/api/chapter/status")
def api_chapter_status(b: ChapterStatusBody):
    """改章状态。**脏章不许定稿**(唯一硬闸);其余都是提示。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    from .. import chapterstate as cs

    r = cs.set_status(b.root, b.chapter, b.status)
    return r if r.get("ok") else {**r, "ok": False}

@router.post("/api/chapter/lease")
def api_chapter_lease(b: LeaseBody):
    """抢/放一章的写租约:两个窗口不会同时写同一章。租约到期自动失效,不会把书锁死。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    from .. import chapterstate as cs

    if b.action == "release":
        return cs.release(b.root, b.chapter, b.owner)
    return cs.acquire(b.root, b.chapter, b.owner, b.ttl or cs.LEASE_TTL)

@router.get("/api/chapter/impact")
def api_chapter_impact(root: str):
    """R8 影响面:改上级之后哪些章要重算、大概几次调用。**只算,不跑。**"""
    from .. import chapterstate as cs

    p, bad = _checked_root(root)
    if bad:
        return bad
    return {"ok": True, **cs.impact(p)}

        # 缺 key 时后端写好的中文指引原来被裸 500 吃掉(本轮审计按调用图扫出来的最后一处)
            # 并发上限(2–8,全局闸):
            # 不再只管"议事时几个角色在跑",而是所有会走模型的调用共用一个水龙头
            # (见 loom/concurrency.py,闸装在 backends 层)。
class ChapterDeleteBody(BaseModel):
    root: str
    chapter: int = 0
    n: int = 0            # 老前端发的是 n;两个都收,免得 422
    reason: str = ""

@router.post("/api/chapter/delete")
def api_chapter_delete(b: ChapterDeleteBody):
    """删一章 = 软删到 正文/.回收站/(整章产物一起走),可一键还原。"""
    root = _project_root(b.root)
    n = b.chapter or b.n
    if not n:
        raise HTTPException(status_code=400, detail="没说要删第几章(chapter 或 n)。")
    bad = _bad_chapter(n)
    if bad:
        return bad
    # 删不存在的章要**报错**,不能静默回 200。
    # 回 200 等于告诉作者「删掉了」,而其实什么都没发生 —— 他以为那一章没了,还去回收站找。
    #
    # ⚠️ 顺序:这条存在性判断**必须排在「这本书正忙」之后**。忙的时候仍然要回 409 project_busy
    # (码与文案由下面的写锁统一给),不能被「章不存在」抢先 —— 否则同一本书在织章期间点删除,
    # 作者看到的会是「没有这一章」而不是「正在写作中」,更难理解。所以这里先探一次忙,
    # 不忙才判存在性;真忙就原样往下走,让 delete_chapter 抛 ProjectBusyError。
    from .. import paths as _paths
    from .. import usecases as _uc_del
    if not _uc_del.is_busy(root):
        if not _paths.chapter_path(root, n).is_file():
            return JSONResponse({"error": f"第 {n} 章没有正文,没什么可删的。",
                                 "code": "chapter_missing"}, status_code=400)
    return {**usecases.delete_chapter(root, n, reason=b.reason), "state": _state(root)}

        # 原样带上具体码(deepseek_auth / key_missing / insufficient_balance …):
        # 压成 backend_failed 会让前端丢掉「检查 API Key」那半句提示(N-4)
# 预览与真跑必须同一个切法:两边默认值不一样(3000 / 1000)时,报出来的章数和
# 缺号跟真跑完全对不上,而人据此决定花不花钱(交叉审查 P1)。
    # 先看后缀再看在不在:后缀不对时说「我不要这个类型」,比说「没找到」有用得多。
        # R19「需输入确认」:全库替换是破坏性操作,输错名字一步都不许动
    # 疑似目录页(正文太短的章)不参与抽章纲 —— 目录页没有剧情,抽它纯烧钱。
        # 静默当全量是最贵的错法:拼错一个词就多花几十倍的钱。宁可报错。
    # 写盘与花钱都在写锁之内。以前是「先落正文、后拿锁」:抢不到锁回 409 时,
    # 盘上其实已经多了 N 章正文 —— 报告与真实写入对不上,而且写盘发生在写锁之外(交叉审查 P1)。
        # 没有要抽的,但正文还是要落(有人只想把稿子接进书里,不想再花调用)。
    # ① 先把正文落盘(不花钱、不发模型):稿子得先进书,「接着写」才接得上前文。
    # 单次上限:一本书几百上千章,一次全抽就是几百次付费调用。默认 200 章,可改小。
        # 账本不许撒谎:一章都没落 / 有章没抽出来,都算「没完全成」,并把数字写进备注。
        # 一章都没落地 = 失败(常见原因:没配 key / 网络不通)。回 200 会让人以为抽成了。
        # 正文可能在失败前就已经落好了 —— 说清楚,别让人以为白导一次。
    # 联动链③b「重排这一块」:把区间钉死。next_batch 是「从第一卷起找第一处缺行」,
    # 拿它做「重排第 51-100 章」会铺到别的块去——所以指定了 lo/hi 就走 batch_for。
    # 付费 + 写盘:必须拿这本书的写锁,否则连点两次就是两次模型调用、两份章行互相盖
    # (交叉审查 P1-4)。拿不到 → 409,让人等一会儿,不是 500。
        # 账本记的是「有没有真落进卡章纲」:模型只回旁白时 落了几行=0,这时候记 ok=True 就是假账
    # 逐篇蒸馏:每篇各蒸一份,库里累积多份
    # 付费 + 写档案:同 /api/card/expand,拿写锁、记运行账本、失败给 502 而不是裸 500(交叉审查 P1-4)。
# ── 0.9.0 B2 分块蒸馏端点(设计稿 v2.1 §9) ──────────────────────────────
    # 原文重切+账本重建(chapter_hash 锚定,增量只蒸新/变块)
    # 原文.md 不改名(新版流程仍用它);蒸馏产物改名 .v0旧版 保护(人工修改不丢)
    # 迁移即分块(零 LLM):作者立刻能看到块清单,「开始蒸馏」只蒸内容
        # **显式传一本不存在/已搬走的书，不该是 400**。
        # 这是"读一个数"的只读端点——书没了就等于没有待拍板，不是客户端错误；
        # 真报 400，调用方就得为"这不是错误"写一条错误分支，违背"接口挂了要像没事"。
        # 但也**不能静默**：给一句 warning，界面能说人话（与 study/library 的 warnings 同风格）。
    # `存在` 两种情形**都要带**：只在不存在的分支里给 `false`，
    # 会让「`if (j.存在)` 才算真书」这种写法把真书判成假。汇总查询（不传 root）恒为 true：
    # 那个 true 描述的是"这次汇总拿到了"，不是"某本书在"——书那一层看 `items[].root`。
    # ── 下线原实现（保留备回）：下面永远走不到，仅作历史留痕 ──
        # 「这份提案写不成」通常是数据不全/写盘失败 → 400 能读的话
    # P2-2/P2-3:一个请求最多 50 章(自己勾的区间也一样),章号不许 < 1
            # 一次最多 BLOCK 章(50)。夹断要说出来,不然人以为整段都核过了(交叉审查 P2-2')
        # 老前端读 d.chapters / d.path;新字段照给,两边都不折腾
        # 缺 key 这种事后端已经写好了中文指引,别让它变成裸 500
# ----------------------------- 书房伙伴(对话) -----------------------------
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
            # partner 流信封是 {"t":...,"ts":...}(非 /api/write 的 type-keyed),error 事件跟随;
            # code 透传,前端据此附可操作提示;e.code 为 None 时不出 code 键
            # 客户端断连(前端 abort)→ 生成器被关闭 → 置取消旗,worker 下一轮边界停并放锁。
            # 正常收尾(哨兵 None break)时 worker 已 return,置旗是无副作用的 no-op(spec §10.3)。
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
# ----------------------------- 新架构:领航员+速记本+上下文守卫 -----------------------------
# ── 14 角色系统 API ──────────────────────────────────────
# ── 作者智库一对一对话 ───────────────────
    # 落库后自动续一轮。true = 这一轮不是作者说的,是"接着上一轮往下走"(不记 user 事件)
# ── 议事例会(一句需求 → 多位角色并发分析)──────────────
# 与 /api/agent/chat 的关系:那条是「一对一」,这条是「一对多」。
# 落盘:.伙伴对话/议事/<轮次>.jsonl(合并流水)+ 各角色自己的 agent-<角色>.jsonl(带 via=议事)。
# ── 逐章步骤(侧边栏按章列六棒 + 人工单步启动)────
class ChapterStepBody(BaseModel):
    root: str
    chapter: int
    step: str = ""        # 空 = 从第一棒开始跑到头
    confirm_world: bool = False   # 软门未达标时，作者确认继续

    # ① 网关/模型可达 + 模型名真在清单里 —— 上次那个 502「目标模型 … 启动失败」就是这一条
    # ② 角色提示词文件缺 —— load_agent 会直接抛,写章在第 1 棒就崩
        # ③ 目标字数太大 → 输出容易被截断/超时
    # ④ 对话历史太长 → 本地模型上下文塞爆
    # ④b 上一章偏短(裁定乙:提示不拦)
    # ⑤ 写作指纹还是中性默认 → 写出来不会像你
@router.get("/api/chapter/steps")
def chapter_steps_ep(root: str, chapter: int):
    """某章的六棒各自做到哪一步了。**纯只读**:账本 + 产物文件,不调模型。"""
    from .. import agents as _ag, runlog
    root = Path(root)
    done = {}
    try:
        for e in runlog.entries(root, chapter):
            if e.get("ok"):
                done[e.get("role", "")] = e
    except Exception:      # noqa: BLE001 - 账本读不到就当没做过
        pass
    outline = (root / "正文" / ".细纲" / f"第{chapter}章.md")
    body = None
    for p in (root / "正文" / f"第{chapter}章.md",):
        if p.is_file():
            body = p
    steps = []
    for spec in _ag.STEPS:      # STEPS 是 StepSpec 列表(不是元组),角色名在 .role
        role = getattr(spec, "role", None) or (spec["role"] if isinstance(spec, dict) else str(spec))
        e = done.get(role)
        steps.append({"名字": role, "做过": bool(e),
                      "字数": (e or {}).get("chars_out") or 0,
                      "耗时ms": (e or {}).get("ms") or 0})
    prev_missing = False
    prev_chars = 0
    prev_target = 0
    prev_short = False
    if chapter > 1:
        prev_p = root / "正文" / f"第{chapter - 1}章.md"
        prev_missing = not prev_p.is_file()
        if not prev_missing:
            try:
                prev_chars = len(prev_p.read_text(encoding="utf-8"))
            except OSError:
                prev_chars = 0
            try:
                prev_target = int(getattr(load_config(root), "chapter_chars", 0) or 0)
            except Exception:      # noqa: BLE001
                prev_target = 0
            # 裁定(乙):**字数低于目标 60% 只提示,不拦**——
            # 「文件存在」太松(200 字半截也算),「六棒全做过」又会把"我就想先写第 3 章"堵死。
            if prev_target and prev_chars < prev_target * 0.6:
                prev_short = True
    return {"ok": True, "chapter": chapter, "steps": steps,
            "已有细纲": outline.is_file(), "已有正文": body is not None,
            "上一章没弄完": prev_missing,
            "上一章字数": prev_chars, "上一章目标": prev_target, "上一章偏短": prev_short}

@router.post("/api/chapter/step")
def chapter_step_ep(b: ChapterStepBody):
    """只跑一棒(流式,信封同 /api/write)。step 给了就 from_step=stop_after=那一棒。"""
    from .. import agents as _ag
    b.root = str(_project_root(b.root))
    root = Path(b.root)
    bad = _bad_chapter(b.chapter)
    if bad:
        return bad
    # 第一章地基硬门（2026-11-10）：空 step（=全流程）与显式单棒都在开 worker 前统一阻断。
    from ..navigator import foundation_block
    _fb = foundation_block(root, b.chapter)
    # T1（2026-11-11）：编辑类单棒 + 第一章正文已落盘非空 = 审稿，放行地基门。
    if _fb and not usecases.review_pass_ok(root, b.chapter, b.step):
        from ._common import _foundation_response
        return _foundation_response(_fb)
    legal = [(getattr(x, "role", None) or (x["role"] if isinstance(x, dict) else str(x)))
             for x in _ag.STEPS]
    step = (b.step or "").strip()
    if step and step not in legal:
        return JSONResponse({"error": f"没有这一棒:{step}(合法:{' / '.join(legal)})"}, status_code=400)
    # 上一章还没弄完就别开新的
    if b.chapter > 1 and not (root / "正文" / f"第{b.chapter - 1}章.md").is_file():
        return JSONResponse({"error": f"第{b.chapter - 1} 章还没弄完,先把上一章写出来再动第{b.chapter} 章。",
                             "code": "prev_unfinished"}, status_code=409)

    # 世界信息软门（2026-11-11）：硬门之后、开 worker 之前；未达标是提示不是拒绝。
    from ._common import _world_gate
    _wg = _world_gate(root, b)
    if _wg:
        return _wg

    lock = usecases.acquire_lock(root)
    cancel.clear(root)
    q: queue.Queue = queue.Queue()

    def worker():
        try:
            cfg = load_config(root)
            be = get_backend(cfg)
            _ag.run_pipeline(
                root, b.chapter, be, cfg, progress=q.put, resume=False,
                from_step=step or None, stop_after=step or None,
                backend_for=lambda r: usecases.backend_for_role(root, cfg, r))
        except LoomBackendError as e:
            q.put(events.error(str(e), code=e.code))
        except (ValueError, FileNotFoundError) as e:
            q.put(events.error(str(e)))
        except Exception as e:      # noqa: BLE001
            q.put(events.error(f"意外错误:{e}"))
        finally:
            lock.release()
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        try:
            while True:
                ev = q.get()
                if ev is None:
                    break
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except GeneratorExit:
            cancel.set()
            raise

    return StreamingResponse(stream(), media_type="application/x-ndjson")

