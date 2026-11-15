"""loom.api.writing —— /api/seed /learn* /brain* /outline/regen /chapter/debug /evolve/revert /rewrite* /write* /guides/*(从 server.py 原样搬家)。"""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import cancel, events, paths, usecases
from ..backends import LoomBackendError, list_models
from ..config import load_config
from . import _common
from ._common import (_bad_chapter, _chapter_nums, _err_json, _fail, _project_root, _state)

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
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
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
class SeedBody(BaseModel):
    root: str
    text: str | None = None
    inherit: str | None = None
    reference: str | None = None

@router.post("/api/seed")
def seed(b: SeedBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    root = Path(b.root)
    try:
        usecases.seed_fingerprint(root, text=b.text or "", inherit=b.inherit, reference=b.reference or "")
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)
    return _state(root)

class ChapterBody(BaseModel):
    root: str
    chapter: int

@router.post("/api/learn")
def learn(b: ChapterBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    root = Path(b.root)
    bad = _bad_chapter(b.chapter)
    if bad:
        return bad
    try:
        cfg = load_config(root)
        rep = usecases.learn_chapter(root, b.chapter, get_backend(cfg),
                                     appraisal_backend=cheap_backend(cfg))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)
    # LearnReport → 响应字段逐字节兼容(前端 app.js 零改动)
    return {"ok": True,
            "fingerprint": rep.fingerprint,
            "卡章纲": rep.card,
            "changes": rep.changes,
            "世界观补充": rep.world_supp,
            "人物卡补充": rep.chars_supp,
            "warn": rep.warn}   # 软提示:疑似把嗓音磨短/丢 anchor,可一键撤销

class DraftBody(BaseModel):
    root: str
    idea: str = ""

@router.post("/api/brain/draft")
def brain_draft(b: DraftBody):
    """从书名+题材+一句话设定,AI 起草 世界观/人物卡/卡章纲 初稿(只覆盖空白/模板,不动你写的)。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    root = Path(b.root)
    try:
        res = usecases.draft_brain(root, b.idea)
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)
    return {"ok": True, "written": list(res["written"].keys()),
            "skipped": res["skipped"], "state": _state(root)}

class BrainRWBody(BaseModel):
    root: str
    rel: str
    full_text: str
    span: str = ""
    instruction: str = ""

@router.post("/api/brain/rewrite")
def brain_rewrite_section(b: BrainRWBody):
    """设定文件 AI 改写:只生成候选、不落盘(作者在编辑器确认后走既有保存链路)。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    from ..brainedit import check_rel, rewrite_section
    root = Path(b.root)
    try:
        check_rel(b.rel)   # 守卫先行:rel 不合法就别碰 config/backend,报错才是守卫文案而非 key 缺失
        out = rewrite_section(root, b.rel, b.full_text, b.span, b.instruction, get_backend(load_config(root)))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)
    return {"ok": True, "rewritten": out}

@router.post("/api/brain/continue")
def brain_continue_section(b: BrainRWBody):
    """设定文件 AI 续写:同上,只生成不落盘。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    from ..brainedit import check_rel, continue_section
    root = Path(b.root)
    try:
        check_rel(b.rel)
        out = continue_section(root, b.rel, b.full_text, b.instruction, get_backend(load_config(root)))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)
    return {"ok": True, "continued": out}

@router.post("/api/outline/regen")
def outline_regen(b: ChapterBody):
    """重新生成第 N 章细纲(设定师→大纲师),覆盖 正文/.细纲/第N章.md 并返回。不碰正文。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    root = Path(b.root)
    bad = _bad_chapter(b.chapter)
    if bad:
        return bad
    try:
        text = usecases.regen_outline(root, b.chapter)
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)
    return {"ok": True, "outline": text}

@router.post("/api/chapter/debug")
def chapter_debug(b: ChapterBody):
    """手动除虫第 N 章:跨章连续性报告(非阻断)+ 本章状态入账。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        rep = usecases.debug_chapter(Path(b.root), b.chapter)
    except (LoomBackendError, ValueError, FileNotFoundError, OSError) as e:
        return _err_json(e)
    return {"ok": True, **rep}

@router.post("/api/learn/revert")
def learn_revert(b: ChapterBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    p = usecases.learn_revert(Path(b.root), b.chapter)
    if p is None:
        return JSONResponse({"error": "没有可撤销的 learn 备份(可能已撤过)。"}, status_code=400)
    return {"ok": True, "fingerprint": p.read_text(encoding="utf-8")}

class EvolveRevertBody(BaseModel):
    root: str
    角色: str

@router.post("/api/evolve/revert")
def evolve_revert_ep(b: EvolveRevertBody):
    """撤销某人格最近一次「学改法」落盘(终审④:`evolve.propose`/`partner_confirm` 承诺的
    「可见性 + 可撤销」红线里「可撤销」那半此前没有生产调用点)。没有快照(没落过 / 已撤过)
    是正常业务态,回可读的 400,不是 500。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        p = usecases.evolve_revert(Path(b.root), b.角色)
    except (ValueError, FileNotFoundError) as e:
        return _err_json(e)
    if p is None:
        return JSONResponse({"error": "没有可撤销的学改法备份(可能已撤过,或还没落过)。"}, status_code=400)
    return {"ok": True, "persona": p.read_text(encoding="utf-8")}

# ----------------------------- 局部重写 -----------------------------
class RewriteBody(BaseModel):
    root: str
    chapter: int
    full_text: str
    span: str
    instruction: str = ""

@router.post("/api/rewrite")
def rewrite(b: RewriteBody):
    """只重写选中段(整章作上下文,按写作指纹的嗓音);不落盘,返回候选。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    from ..rewrite import rewrite_span
    root = Path(b.root)
    try:
        out = rewrite_span(root, b.chapter, b.full_text, b.span, b.instruction, get_backend(load_config(root)))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)
    return {"rewritten": out}

class RewriteApplyBody(BaseModel):
    root: str
    chapter: int
    content: str
    old_span: str
    new_span: str

@router.post("/api/rewrite/apply")
def rewrite_apply(b: RewriteApplyBody):
    """应用重写:落盘正文 + 外科式同步 .原稿 快照(守 learn 不被 AI 重写污染)。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    usecases.rewrite_apply(Path(b.root), b.chapter, b.content, b.old_span, b.new_span)
    return {"ok": True}

# ----------------------------- write(流式) -----------------------------
class WriteBody(BaseModel):
    root: str
    chapter: int
    force: bool = False
    from_step: str = ""   # R24:从哪一步起重算(空=正常续跑;填「润色师」=只重跑润色那一棒)
    confirm_world: bool = False   # 软门（世界信息充足度）未达标时，作者确认继续

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
@router.get("/api/guides/pending")
def guides_pending(root: str = "", context: str = ""):
    from .. import guides as _g
    # context=import-folder → 前端在动作入口显式拉取，允许 front_triggered 型出现
    include_front = context == "import-folder"
    rp = Path(root).expanduser() if root else None
    return {"pending": _g.pending(rp, include_front=include_front)}

class GuideSeenBody(BaseModel):
    id: str

@router.post("/api/guides/seen")
def guides_seen(b: GuideSeenBody):
    from .. import guides as _g
    _g.mark_seen(b.id)
    return {"ok": True, "id": b.id}

@router.post("/api/guides/wizard-done")
def guides_wizard_done():
    from .. import guides as _g
    _g.mark_wizard_done()
    return {"ok": True}

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
# ── 手动+审查混合流 ────────────────────────────────
# 辅助/教学模式：作者手动填细纲/正文 → 可手动跑「责任编辑」「编辑总监」单棒。 
# 单棒运行 = run_pipeline(from_step=棒名, stop_after=棒名)——断点续跑机制白捡： 
# 作者手动改的细纲/正文落产物位后，上游指纹变化让下游自然重算。
# 手动注入 = 作者内容直接落 paths.chapter_path / outline_path（产物位即真相源）。
    # 教学模式只允许审查两棒（教学=作者手写，AI 只打分纠错不代笔）
    # 字数软诊断（不硬拦）
@router.post("/api/write")
def write(b: WriteBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    root = Path(b.root)
    bad = _bad_chapter(b.chapter)
    if bad:
        return bad
    # 第一章地基硬门（2026-11-10）：地基未完禁止开始第一章。放在旧门之前，
    # 让章 1 缺项统一报 foundation_incomplete（旧 brain 门在章 2 / 地基齐全时照旧生效）。
    from ..navigator import foundation_block
    block = foundation_block(root, b.chapter)
    if block:
        from ._common import _foundation_response
        return _foundation_response(block)
    rej = usecases.write_precheck(root, b.chapter, b.force, b.from_step)  # 已存在/drifted/force 三态
    if rej:
        return JSONResponse(rej, status_code=409)

    # 教学模式=AI 完全不代笔。后端硬拦（前端按钮也隐藏，
    # 但不能只靠前端——直接 POST 照样能触发）。返回 403 + 人话。
    from ..config import load_config, writing_mode_to_stop_after
    cfg = load_config(root)
    if cfg.writing_mode == "教学":
        return JSONResponse({"error": "这本书处于「教学」模式：AI 不代笔正文。要启用编织，"
                                     "请在设置里把 AI 介入程度切回辅助或 FullAuto。"},
                            status_code=403)

    # 世界信息软门（2026-11-11）：硬门之后、开 worker 之前。未达标是**提示**不是拒绝，
    # 作者带 confirm_world=true 重发即放行并落确认记录。
    from ._common import _world_gate
    _wg = _world_gate(root, b)
    if _wg:
        return _wg

    lock = usecases.acquire_lock(root)   # 拿不到 → ProjectBusyError → 409(流式场景锁随 worker)
    cancel.clear(root)   # 消费掉上一次的「停」标记,否则它会误伤这一次

    # T3 结构化事实账本：**开织之前**再比对一次（用户 2026-11-11 裁定：两个时机都比对）。
    # 用已落盘的设定集正文做 AI 抽取 → 比对账本 → 冲突只提示不阻塞。任何异常吞掉。
    _fact_events: list[dict] = []
    try:
        from .. import facts_ledger as _fl
        from .. import paths as _paths
        _src = ""
        for _rel in (_paths.SET_CARD_REL, _paths.CARD_REL):
            _f = root / _rel
            if _f.is_file():
                _src += _f.read_text(encoding="utf-8", errors="replace") + "\n"
        if _src.strip():
            _cfg = load_config(root)
            # 必须把真实后端传下去：不传 ⇒ check 内部只能退化为 0 条抽取，
            # 「没有冲突」就成了「压根没比」的假绿（用户裁定 P1-b）。
            try:
                _backend = cheap_backend(_cfg) or get_backend(_cfg)
            except Exception as _be:
                _backend = None   # 抽取会自己打一行 stderr 说明未提供后端
                print(f"[开织前事实比对] 取抽取后端失败：{type(_be).__name__}: {_be}",
                      file=sys.stderr, flush=True)
            _chk = _fl.check(root, text=_src, backend=_backend,
                             source=f"开织前/第{b.chapter}章")
            if _chk.get("冲突"):
                _fact_events.append(events.info(
                    "结构化事实账本发现 " + str(len(_chk["冲突"])) + " 处取值不一致（只提示，不影响写作）："
                    + "；".join(_fl.conflict_lines(_chk["冲突"]))))
    except Exception as _e:
        # 不阻塞写作，但不能静默：否则「账本整块没跑」和「账本没发现冲突」一回事
        print(f"[开织前事实比对] 本次跳过：{type(_e).__name__}: {_e}", file=sys.stderr, flush=True)
        _fact_events = []

    q: queue.Queue = queue.Queue()
    for _ev in _fact_events:      # 先排进流头，作者一开织就能看见
        q.put(_ev)

    def worker():
        try:
            usecases.write_chapter(root, b.chapter, q.put, force=b.force, slow=0.25,
                                   from_step=b.from_step,
                                   should_stop=lambda: cancel.pending(root))
        except LoomBackendError as e:
            q.put(events.error(str(e), code=e.code))   # code 透传,前端据此附可操作提示
        except (ValueError, FileNotFoundError) as e:
            q.put(events.error(str(e)))
        except Exception as e:  # 兜底,别让流挂死
            q.put(events.error(f"意外错误:{e}"))
        finally:
            lock.release()   # 锁跟着 worker 走(响应流着,写还没完),在哨兵 None 之前放
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")

class CancelBody(BaseModel):
    root: str
    chapter: int = 0

@router.post("/api/write/cancel")
def write_cancel(b: CancelBody):
    """叫停正在写的这一章。语义 (a):**只停后续步骤**,不打断在途的那一次请求。

    所以回执要老实:当前这一棒仍会写完(它已经在路上);下一棒不再开始;
    已跑完那一棒的稿子落 `正文/.半稿/第N章.txt`,正式正文一个字节不动。
    """
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    cancel.request(b.root)
    return {"ok": True, "cancelled": True, "chapter": b.chapter,
            "note": "当前这一棒会写完;下一棒不再开始。半稿落在 正文/.半稿。"}

    # ① 卡章纲这一行填没填
    # ② 本章分镜细纲
    # ③ 上一章的账入没入(不入账,后面的连续性检测就失明)
    # ④ 悬空伏笔
    # ⑤ 顶层两份规划:全书大纲看有没有实质内容;卷纲看**切得出卷**没有
    #   (出厂模板里 `## 第一卷` 只是占位:卷名与主线都没填 → budget.volumes 不算分过卷)
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
        # 缺 key 时后端写好的中文指引原来被裸 500 吃掉(本轮审计按调用图扫出来的最后一处)
            # 并发上限(2–8,全局闸):
            # 不再只管"议事时几个角色在跑",而是所有会走模型的调用共用一个水龙头
            # (见 loom/concurrency.py,闸装在 backends 层)。
    # 删不存在的章要**报错**,不能静默回 200。
    # 回 200 等于告诉作者「删掉了」,而其实什么都没发生 —— 他以为那一章没了,还去回收站找。
    #
    # ⚠️ 顺序:这条存在性判断**必须排在「这本书正忙」之后**。忙的时候仍然要回 409 project_busy
    # (码与文案由下面的写锁统一给),不能被「章不存在」抢先 —— 否则同一本书在织章期间点删除,
    # 作者看到的会是「没有这一章」而不是「正在写作中」,更难理解。所以这里先探一次忙,
    # 不忙才判存在性;真忙就原样往下走,让 delete_chapter 抛 ProjectBusyError。
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
class BrainCopyBody(BaseModel):
    root: str
    from_root: str
    kind: str                      # 违禁词 / 文风参考
    overwrite: bool = False

@router.get("/api/brain/copyable")
def api_brain_copyable(root: str, kind: str = ""):
    """书架上哪些书有「违禁词 / 文风参考」可以抄。**纯只读。**"""
    from .. import braincopy

    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    try:
        return braincopy.candidates(r, kind)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.post("/api/brain/copy")
def api_brain_copy(b: BrainCopyBody):
    """从另一本书抄一份「违禁词 / 文风参考」过来。目标已存在 → 默认不覆盖。"""
    from .. import braincopy

    try:
        return braincopy.copy_from(_project_root(b.root), b.from_root, b.kind,
                                   overwrite=b.overwrite)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

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
    # ① 网关/模型可达 + 模型名真在清单里 —— 上次那个 502「目标模型 … 启动失败」就是这一条
    # ② 角色提示词文件缺 —— load_agent 会直接抛,写章在第 1 棒就崩
        # ③ 目标字数太大 → 输出容易被截断/超时
    # ④ 对话历史太长 → 本地模型上下文塞爆
    # ④b 上一章偏短(裁定乙:提示不拦)
    # ⑤ 写作指纹还是中性默认 → 写出来不会像你
            # 裁定(乙):**字数低于目标 60% 只提示,不拦**——
            # 「文件存在」太松(200 字半截也算),「六棒全做过」又会把"我就想先写第 3 章"堵死。
    # 上一章还没弄完就别开新的
# ── 候选卡换落点（审查跟进项①）────────────────────────────
    # before 要按新格子重算，否则落库时的快照比对会误判 stale
# ── 旧结构 → 设定集（裁定「乙」：作者点了才搬）────────────
class MigrateLegacyBody(BaseModel):
    root: str

@router.post("/api/brain/migrate-legacy")
def brain_migrate_legacy_ep(b: MigrateLegacyBody):
    """把旧结构（世界观/、人物/、立项卡.md）**复制**进设定集，原文件保留。

    只复制、不移动、不删除——最坏情况是设定集里多一份副本，作者随时能删。
    已存在同名文件则跳过（不覆盖作者后来改过的内容）。
    """
    import shutil as _shutil

    from .. import paths
    root = _project_root(b.root)
    out = {"copied": [], "skipped": []}

    def _copy_tree(legacy_dir: Path, set_dir: Path, label: str):
        if not legacy_dir.is_dir():
            return
        set_dir.mkdir(parents=True, exist_ok=True)
        for f in sorted(legacy_dir.glob("*.md")):
            tgt = set_dir / f.name
            if tgt.exists():
                out["skipped"].append(f"{label}/{f.name}")
                continue
            _shutil.copy2(f, tgt)
            out["copied"].append(f"{label}/{f.name}")

    _copy_tree(root / "外置大脑" / "世界观", root / paths.SET_WORLD_DIR_REL, "10-世界观")
    _copy_tree(root / "外置大脑" / "人物", root / paths.SET_CHARS_DIR_REL, "20-人物")

    # 立项卡.md → 00-立项.md
    legacy_card = root / "外置大脑" / "立项卡.md"
    target_card = root / paths.SET_CARD_REL
    if legacy_card.is_file():
        if target_card.exists():
            out["skipped"].append("00-立项.md")
        else:
            target_card.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(legacy_card, target_card)
            out["copied"].append("00-立项.md")

    out["ok"] = True
    return out

