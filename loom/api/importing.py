"""loom.api.importing —— /api/import/* /pending* /volplan/* /snapshot/* /card/* /entities/*(从 server.py 原样搬家)。"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import usecases
from .. import usecases as _uc
from ..config import load_config
from . import _common
from ._common import (_card_rest_hint, _fail, _log_op, _project_root)

router = APIRouter()


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
class CardExpandBody(BaseModel):
    root: str
    size: int = 50       # 一块多少章
    lo: int = 0          # 联动链③b「重排这一块」:指定区间(0 = 不指定,走 next_batch)
    hi: int = 0

@router.get("/api/card/coverage")
def api_card_coverage(root: str):
    """卡章纲铺到哪了:按卷报「已铺/未铺」。**纯只读。**"""
    from .. import volplan

    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    return volplan.coverage(r)

        # 原样带上具体码(deepseek_auth / key_missing / insufficient_balance …):
        # 压成 backend_failed 会让前端丢掉「检查 API Key」那半句提示(N-4)
@router.get("/api/pending")
def api_pending(root: str):
    """这本书有哪些 AI 产物还没被人确认过(章纲/卷纲...)。只读,不花钱。"""
    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import volplan as _vp
    cur = _vp.pending(r)
    return {"ok": True, "待确认": cur, "有几种": len(cur)}

@router.post("/api/pending/confirm")
def api_pending_confirm(b: dict):
    """人看过了:清掉标记(不传 kind 就清全部)。**只清标记,不动任何正文/章纲。**"""
    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import volplan as _vp
    kind = body.get("kind") or None
    left = _vp.confirm_pending(r, kind)
    return {"ok": True, "说明": "已记下:这些内容算你确认过了。", "还剩": left}

# 预览与真跑必须同一个切法:两边默认值不一样(3000 / 1000)时,报出来的章数和
# 缺号跟真跑完全对不上,而人据此决定花不花钱(交叉审查 P1)。
@router.post("/api/import/preview")
def api_import_preview(b: dict):
    """切章预览:给一份写到一半的 TXT,报「几章/缺哪号/认没认出卷」。

    **纯只读、零模型调用、不落盘** —— 作者先看切得对不对,再决定要不要花钱抽章纲。
    只认本地已存在的 .txt/.md,大小上限 80MB(再大就是拿别的东西误当小说了)。
    """
    from .. import import_txt

    raw = str((b or {}).get("path") or "").strip().strip('"')
    if not raw:
        return JSONResponse({"error": "要给文件路径(它得在这台机器上)。", "code": "no_path"},
                            status_code=400)
    path = Path(raw)
    # 先看后缀再看在不在:后缀不对时说「我不要这个类型」,比说「没找到」有用得多。
    if path.suffix.lower() not in (".txt", ".md"):
        return JSONResponse({"error": "只吃 .txt / .md —— 小说原稿一般是这两种。",
                             "code": "bad_type"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"error": f"没找到这个文件:{raw}", "code": "not_found"},
                            status_code=400)
    if path.stat().st_size > 80 * 1024 * 1024:
        return JSONResponse({"error": f"这个文件 {path.stat().st_size // 1048576}MB,太大了(上限 80MB)。",
                             "code": "too_big"}, status_code=400)
    try:
        chunk = int((b or {}).get("chunk") or IMPORT_CHUNK)
    except (TypeError, ValueError):
        chunk = 3000
    chunk = max(500, min(chunk, 20000))
    try:
        return import_txt.preview(path, chunk=chunk)
    except Exception as e:      # noqa: BLE001 - 编码怪、读不动都要给人话,不是裸 500
        return JSONResponse({"error": f"读不了这个文件:{type(e).__name__}: {e}",
                             "code": "read_failed"}, status_code=400)

@router.post("/api/volplan/volumes")
def api_volplan_volumes(b: dict):
    """归纳各卷的「卷名 / 该卷主线」(逆推半截稿的第二步)。

    不带 `confirm` 只报账:要归纳几卷、会调几次模型。带 `confirm` 才真跑。
    只填**空着的**字段,人写过的一个字不动。
    """
    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import volplan as _vp
    if not body.get("confirm"):
        return JSONResponse({"ok": True, "预计调用次数": "按卷数算(每卷一次)",
                             "说明": "会给每卷填「卷名 / 该卷主线」,人写过的字段不动;现在还没花钱。"})
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有别的任务在跑", "code": "busy"}, status_code=409)
    t0 = time.monotonic()
    try:
        from ..routes import get_backend_for as _gbf   # 局部导入:别指望别的函数替它导过
        backend = _gbf(load_config(r), "大纲")
        res = _vp.rebuild_volumes(r, backend)
        _log_op(r, 0, "归纳卷纲", bool(res.get("归纳了几卷")), t0,
                f"归纳 {res.get('归纳了几卷')} 卷,{res.get('调用次数')} 次调用")
        return JSONResponse({**res, "ok": bool(res.get("归纳了几卷"))})
    except Exception as e:      # noqa: BLE001
        _log_op(r, 0, "归纳卷纲", False, t0, str(e)[:200])
        return JSONResponse({"error": f"归纳卷纲失败:{e}", "code": "backend_failed"}, status_code=502)
    finally:
        lock.release()

@router.post("/api/volplan/overview")
def api_volplan_overview(b: dict):
    """归纳全书主线(第三步),落在《全书大纲》里一个「AI 归纳,待你确认」的小节。

    **只替换这一节**,节后人写的内容一个字不动(上一版会整段删,交叉审查 P0)。
    """
    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import volplan as _vp
    if not body.get("confirm"):
        return JSONResponse({"ok": True, "预计调用次数": 1,
                             "说明": "会调 1 次模型写一段全书主线草稿,暂时还没花钱。"})
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有别的任务在跑", "code": "busy"}, status_code=409)
    t0 = time.monotonic()
    try:
        from ..routes import get_backend_for as _gbf   # 局部导入:别指望别的函数替它导过
        backend = _gbf(load_config(r), "大纲")
        res = _vp.rebuild_overview(r, backend)
        _log_op(r, 0, "归纳全书大纲", bool(res.get("已写入")), t0,
                f"{res.get('调用次数')} 次调用")
        return JSONResponse({**res})
    except Exception as e:      # noqa: BLE001
        _log_op(r, 0, "归纳全书大纲", False, t0, str(e)[:200])
        return JSONResponse({"error": f"归纳全书大纲失败:{e}", "code": "backend_failed"},
                            status_code=502)
    finally:
        lock.release()

@router.get("/api/snapshot/list")
def api_snapshot_list(root: str = ""):
    """列这本书拍过的快照(只读)。"""
    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import snapshot as _snap
    return JSONResponse({"ok": True, "快照": _snap.snapshots(r)})

@router.post("/api/snapshot/make")
def api_snapshot_make(b: dict):
    """拍一张快照:只读稿子,不改任何正文。"""
    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import snapshot as _snap
    try:
        res = _snap.make(r, note=str(body.get("note") or ""))
    except Exception as e:      # noqa: BLE001
        return JSONResponse({"error": f"拍快照失败:{e}", "code": "snapshot_failed"}, status_code=500)
    return JSONResponse({**res}, status_code=200 if res.get("ok") else 400)

@router.get("/api/snapshot/verify")
def api_snapshot_verify(root: str = "", name: str = ""):
    """只读校验:某一版快照和盘上还一不一致。"""
    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import snapshot as _snap
    return JSONResponse({**_snap.verify(r, name)})

@router.post("/api/snapshot/restore")
def api_snapshot_restore(b: dict):
    """恢复某一版快照。**要照着快照名输入确认**;恢复前会自动再拍一张保命。"""
    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import snapshot as _snap
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有别的任务在跑,先停了再恢复",
                             "code": "busy"}, status_code=409)
    try:
        res = _snap.restore(r, str(body.get("name") or ""), str(body.get("typed") or ""))
    except Exception as e:      # noqa: BLE001
        return JSONResponse({"error": f"恢复失败:{e}", "code": "restore_failed"}, status_code=500)
    finally:
        lock.release()
    if not res.get("ok"):
        code = "need_typed" if res.get("需要输入") else "restore_failed"
        return JSONResponse({**res, "code": code}, status_code=400)
    return JSONResponse({**res})

@router.get("/api/entities")
def api_entities_list(root: str = ""):
    """实体的正名 / 别名 / id。**纯只读。**"""
    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import entities as _ent
    ents = _ent.load(r)
    return JSONResponse({"ok": True, "条数": len(ents),
                         "实体": [{"id": e.id, "正名": e.name, "别名": list(e.aliases),
                                   "类型": getattr(e, "kind", "")} for e in ents]})

@router.post("/api/entities/build")
def api_entities_build(b: dict):
    """从材料里扫出实体。`write=false` 只报数(默认),`write=true` 才落表。"""
    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import entities as _ent
    res = _ent.build(r, write=bool(body.get("write")))
    return JSONResponse({**res})

@router.post("/api/entities/rename")
def api_entities_rename(b: dict):
    """改名 + 全库替换。**不带 typed 只返回预览**(不写一个字);typed 要照着旧名输入。"""
    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    from .. import entities as _ent
    old = str(body.get("old") or "").strip()
    new = str(body.get("new") or "").strip()
    typed = str(body.get("typed") or "").strip()
    if not old or not new:
        return JSONResponse({"error": "要给出「旧名」和「新名」", "code": "missing"},
                            status_code=400)
    if typed != old:
        # R19「需输入确认」:全库替换是破坏性操作,输错名字一步都不许动
        prev = _ent.rename_preview(r, old, new)
        if not prev.get("ok"):
            return JSONResponse({**prev}, status_code=400)
        return JSONResponse({**prev, "已执行": False, "需要输入": old,
                             "code": "need_typed",
                             "说明": f"预览如上。真要改,把「{old}」照着输进 typed 再调一次。"},
                            status_code=400)
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有别的任务在跑,先停了再改名", "code": "busy"},
                            status_code=409)
    try:
        res = _ent.rename(r, old, new, confirm=True)
    finally:
        lock.release()
    return JSONResponse({**res})

@router.post("/api/import/cancel")
def api_import_cancel(b: dict):
    """让正在跑的「抽章纲」在下一章之前停下。已抽完的章不重抽,再点一次就能接着来。"""
    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    flag = r / ".导入取消"
    try:
        flag.write_text("停\n", encoding="utf-8")
    except OSError as e:
        return JSONResponse({"error": f"写不下取消标记:{e}", "code": "write_failed"},
                            status_code=500)
    return JSONResponse({"ok": True, "说明": "已经叫停了:它会在下一章开始前停手,已抽完的章保留。"})

@router.post("/api/import/run")
def api_import_run(b: dict):
    """拿一份写到一半的稿子,逐章抽章纲,**会花钱**(每章一次调用)。

    两段式,故意不合并:
    - 没带 `confirm: true` = **只报账**:告诉你这本书几章、按这个抽法要调多少次模型。
    - 带了 `confirm: true` 才真跑;拿写锁、记运行账本、失败给 502 而不是裸 500。
    """
    from .. import import_txt, volplan
    from ..config import load_config
    from ..routes import get_backend_for

    body = b or {}
    try:
        r = _project_root(body.get("root"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)

    raw = str(body.get("path") or "").strip().strip('"')
    if not raw:
        return JSONResponse({"error": "要给文件路径(它得在这台机器上)。", "code": "no_path"},
                            status_code=400)
    path = Path(raw)
    if path.suffix.lower() not in (".txt", ".md"):
        return JSONResponse({"error": "只吃 .txt / .md —— 小说原稿一般是这两种。",
                             "code": "bad_type"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"error": f"没找到这个文件:{raw}", "code": "not_found"}, status_code=400)

    try:
        text, enc = import_txt.read_text(path)
    except UnicodeDecodeError as e:
        return JSONResponse({"error": f"这个文件的编码认不出来({e.reason}),换成 UTF-8 / GBK 再试。",
                             "code": "bad_encoding"}, status_code=400)
    except OSError as e:
        return JSONResponse({"error": f"读不动这个文件:{e.strerror or e}。", "code": "unreadable"},
                            status_code=400)
    chunk = max(500, min(int(body.get("chunk") or IMPORT_CHUNK), 20000))  # 与预览同一个常量
    info = import_txt.analyze(text, chunk=chunk)
    chunks = import_txt.chapter_texts(text, chunk=chunk)
    # 疑似目录页(正文太短的章)不参与抽章纲 —— 目录页没有剧情,抽它纯烧钱。
    tiny = {c["号"] for c in chunks if len("".join((c.get("正文") or "").split())) < 30}
    chunks = [c for c in chunks if c["号"] not in tiny]
    have = {n for n, t in volplan.card_state(r).items() if t}
    todo = [c for c in chunks if int(c.get("号") or 0) not in have]
    mode = str(body.get("mode") or "全部")
    recent = max(1, int(body.get("recent") or 30))
    step = max(1, int(body.get("step") or 10))
    if mode not in ("只抽最近", "抽样", "全部"):
        # 静默当全量是最贵的错法:拼错一个词就多花几十倍的钱。宁可报错。
        return JSONResponse({"error": f"不认识的抽法:{mode}", "code": "bad_mode",
                             "可选": ["只抽最近", "抽样", "全部"]}, status_code=400)
    if mode == "只抽最近":
        todo = todo[-recent:]
    elif mode == "抽样":
        todo = [c for i, c in enumerate(todo) if i % step == 0]

    plan = {"文件": str(path), "编码": enc, "切法": info["切法"], "总章数": info["章数"],
            "缺号": info["缺号"][:20], "疑似目录": sorted(tiny)[:20],
            "章前字数": info.get("章前字数", 0), "章前样例": info.get("章前样例", ""),
            "切章字数": chunk,
            "要抽的章数": len(todo), "预计调用次数": len(todo), "抽法": mode}
    if not body.get("confirm"):
        plan["说明"] = (f"这次不花钱,只报账:这本书 {info['章数']} 章,按「{mode}」要抽 "
                      f"{len(todo)} 章 = {len(todo)} 次模型调用。要真跑,带 confirm:true 再来一次。")
        return JSONResponse(plan)

    # 写盘与花钱都在写锁之内。以前是「先落正文、后拿锁」:抢不到锁回 409 时,
    # 盘上其实已经多了 N 章正文 —— 报告与真实写入对不上,而且写盘发生在写锁之外(交叉审查 P1)。
    from .. import usecases as _uc

    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再来(正文一个字都没落)。",
                             "code": "busy"}, status_code=409)
    t0 = time.monotonic()

    if not todo:
        # 没有要抽的,但正文还是要落(有人只想把稿子接进书里,不想再花调用)。
        _b = {"ok": False, "说明": "没带 write_body"}
        try:
            if body.get("write_body", True):
                _b = import_txt.write_chapters(r, chunks)
        except Exception as e:      # noqa: BLE001
            _b = {"ok": False, "说明": f"落正文失败:{e}"}
        finally:
            lock.release()
        return JSONResponse({**plan, **_card_rest_hint(r), "ok": True, "抽了几章": 0, "正文": _b,
                             "说明": "该抽的都抽过了,不用再花钱。"})

    # ① 先把正文落盘(不花钱、不发模型):稿子得先进书,「接着写」才接得上前文。
    body_res = {"ok": False, "说明": "这次不落正文(带了 write_body=false)"}
    if body.get("write_body", True):
        try:
            body_res = import_txt.write_chapters(r, chunks)
        except Exception as e:      # noqa: BLE001 - 落正文失败要说清,但不该连带毁掉抽章纲
            body_res = {"ok": False, "说明": f"落正文失败:{type(e).__name__}: {e}"}

    # 单次上限:一本书几百上千章,一次全抽就是几百次付费调用。默认 200 章,可改小。
    max_chapters = max(1, min(int(body.get("max_chapters") or 200), 2000))
    flag = r / ".导入取消"

    try:
        cfg = load_config(r)
        backend = get_backend_for(cfg, "写作")
        try:
            flag.unlink()                       # 开跑前清掉上次的取消标记
        except OSError:
            pass
        res = volplan.rebuild_from_chapters(r, chunks, backend, mode=mode, recent=recent, step=step,
                                            max_chapters=max_chapters,
                                            should_stop=lambda: flag.exists())
        # 账本不许撒谎:一章都没落 / 有章没抽出来,都算「没完全成」,并把数字写进备注。
        _ok = bool(res.get("抽了几章")) and not res.get("未抽出章数")
        _log_op(r, 0, "逆推章纲", _ok, t0,
                f"{res.get('抽了几章')}/{res.get('要抽的章数')} 章落地,"
                f"{res.get('调用次数')} 次调用,{res.get('未抽出章数')} 章没读出来")
        # 一章都没落地 = 失败(常见原因:没配 key / 网络不通)。回 200 会让人以为抽成了。
        if not res.get("抽了几章") and res.get("未抽出章数"):
            _first = (res.get("未抽出") or [{}])[0].get("原因") or "模型没给成形的一行"
            return JSONResponse({**plan, **res, "正文": body_res,
                                 "error": f"一章都没抽出来:{_first}", "code": "backend_failed"},
                                status_code=502)
        return JSONResponse({**plan, **res, "正文": body_res,
                             **_card_rest_hint(r)})
    except Exception as e:      # noqa: BLE001 - 统一成 502 + 人话
        _log_op(r, 0, "逆推章纲", False, t0, str(e)[:200])
        # 正文可能在失败前就已经落好了 —— 说清楚,别让人以为白导一次。
        return JSONResponse({"error": f"抽章纲失败:{e}", "code": "backend_failed",
                             "正文": body_res}, status_code=502)
    finally:
        try:
            flag.unlink()                    # 跑完/失败都清掉,别让下次一开跑就停
        except OSError:
            pass
        lock.release()

@router.post("/api/card/expand")
def api_card_expand(b: CardExpandBody):
    """铺一块章纲(50 章),**会花钱**(一次模型调用)。人写过的行绝不覆盖。"""
    from .. import volplan
    from ..config import load_config
    from ..routes import get_backend_for

    try:
        r = _project_root(b.root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    size = max(1, min(int(b.size or 50), 120))     # 一块最多 120 章,再大必然塌方
    # 联动链③b「重排这一块」:把区间钉死。next_batch 是「从第一卷起找第一处缺行」,
    # 拿它做「重排第 51-100 章」会铺到别的块去——所以指定了 lo/hi 就走 batch_for。
    if b.lo and b.hi and b.hi >= b.lo:
        nb = volplan.batch_for(r, b.lo, b.hi, cap=120)
    else:
        nb = volplan.next_batch(r, size)
    if not nb.get("ok"):
        return nb
    # 付费 + 写盘:必须拿这本书的写锁,否则连点两次就是两次模型调用、两份章行互相盖
    # (交叉审查 P1-4)。拿不到 → 409,让人等一会儿,不是 500。
    from .. import usecases as _uc
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再铺。", "code": "busy"}, status_code=409)
    t0 = time.monotonic()
    try:
        sys_prompt, usr = volplan.build_prompt(r, nb)
        cfg = load_config(r)
        backend = get_backend_for(cfg, "大纲")
        out = backend.complete(sys_prompt, usr, max_chars=max(2000, nb["章数"] * 120))
        landed = volplan.apply_batch(r, out or "")
        after = volplan.next_batch(r, size)
        _落地 = int(landed.get("落了几行", 0) or 0)
        # 账本记的是「有没有真落进卡章纲」:模型只回旁白时 落了几行=0,这时候记 ok=True 就是假账
        _log_op(r, 0, "铺章纲", _落地 > 0, t0,
                f'{nb["起"]}-{nb["止"]} 落 {_落地} 章' + ("" if _落地 else "(模型没给出可落的行)"))
        return {"ok": True, "这块": {"起": nb["起"], "止": nb["止"], "章数": nb["章数"]},
                "落盘": landed, "下一块": after,
                "说明": "铺完这块,人写过的行一个字没动;确认这块内容再铺下一块。"}
    except Exception as e:      # noqa: BLE001 - 付费失败也要记账,再换成 502
        _log_op(r, 0, "铺章纲", False, t0, f"{type(e).__name__}: {e}"[:200])
        return JSONResponse({"error": f"模型调用失败:{e}", "code": "backend_failed"}, status_code=502)
    finally:
        lock.release()

