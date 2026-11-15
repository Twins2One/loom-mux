"""loom.api.style —— /api/style/*(含 b2 七端点,从 server.py 原样搬家)。"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import usecases
from .. import usecases as _uc
from . import _common
from ._common import (_fail, _log_op, _project_root)

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
class StyleRawBody(BaseModel):
    root: str = ""           # 可选：书房没有当前书，档案本来就住知识库
    name: str = "主档案"     # 档案(库)名
    filename: str            # 文档标题
    text: str

class StylePackBody(BaseModel):
    root: str = ""           # 可选：书房没有当前书，用用户级默认后端
    name: str = "主档案"
    # 逐篇蒸馏:每篇各蒸一份,库里累积多份
    docs: list[str] = []     # 要蒸的文档标题(不传 = 空,接口会拒绝)
    detail: str = "中"       # 蒸馏细致度:粗 / 中 / 细(不是注入量)

class StyleImportBody(BaseModel):
    root: str = ""           # 可选：书房没有当前书，档案本来就住知识库
    name: str = "主档案"
    paths: list[str] = []

class StyleDocBody(BaseModel):
    root: str = ""           # 可选：同上
    name: str = "主档案"
    title: str

class StylePointerBody(BaseModel):
    root: str
    name: str = "主档案"
    strength: str = "中"     # 注入量:弱 / 中 / 强(不是蒸馏细致度)

@router.get("/api/style/packs")
def api_style_packs(root: str, name: str = "主档案"):
    """文风文档库:每篇的字数、蒸没蒸过、什么时候蒸的。**纯只读。**"""
    from .. import stylepack

    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    return {"ok": True, "档案": stylepack.list_packs(r),
            "库": stylepack.list_library(r, name),
            "在用": stylepack.read_pointer(r),
            "细致度档": list(stylepack.DETAIL_LEVELS),
            "注入量档": list(stylepack.STRENGTH_BUDGET),
            "落盘": False}

@router.post("/api/style/raw")
def api_style_raw(b: StyleRawBody):
    """往文档库里加一篇(粘贴的短文档也走这条)。原样保留。"""
    from .. import stylepack

    try:
        return stylepack.add_doc(_project_root(b.root) if b.root else None, b.name, b.filename, b.text)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.post("/api/style/import")
def api_style_import(b: StyleImportBody):
    """把外面选中的文件复制进文档库(不移动原件,同名自动加后缀)。"""
    from .. import stylepack

    try:
        return stylepack.import_docs(_project_root(b.root) if b.root else None, b.name, b.paths)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.post("/api/style/delete")
def api_style_delete(b: StyleDocBody):
    """从库里删一篇(原文 + 该篇蒸馏产物 + 状态一起清,再重算合并档案)。"""
    from .. import stylepack

    try:
        return stylepack.delete_doc(_project_root(b.root) if b.root else None, b.name, b.title)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.post("/api/style/distill")
def api_style_distill(b: StylePackBody):
    """**逐篇**蒸馏(每篇一次调用 ⇒ 会花 N 次)。输出不合格的那一篇不落盘。

    `root` 可选：书房没有当前书，
    档案本来就住 `知识库\语料库` 下，所以不传 root 时**不拿书的写锁**、用用户级默认后端。
    """
    from .. import stylepack
    from ..config import load_config
    from ..routes import get_backend_for

    r = None
    if b.root:
        try:
            r = _project_root(b.root)
        except HTTPException as e:
            return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    if not [x for x in (b.docs or []) if str(x).strip()]:
        return JSONResponse({"error": "先勾选要蒸馏的文档。", "code": "no_selection"}, status_code=400)
    # 付费 + 写档案:同 /api/card/expand,拿写锁、记运行账本、失败给 502 而不是裸 500(交叉审查 P1-4)。
    from .. import usecases as _uc
    lock = None
    if r is not None:
        lock = _uc.try_lock(r)
        if lock is None:
            return JSONResponse({"error": "这本书正有任务在写,等它跑完再蒸。", "code": "busy"}, status_code=409)
    t0 = time.monotonic()
    try:
        if r is not None:
            cfg = load_config(r)
        else:
            from ..study.ask import studio_config
            cfg = studio_config()        # 书房：用户级默认后端（欢迎页「接入模型」连的那个）
        backend = get_backend_for(cfg, "文风学习")   # R21 有这一槽,就该用它,别借「审稿」
        out = stylepack.distill_docs(r, b.name, b.docs, backend,
                                     model=getattr(cfg, 'model', '') or '', detail=b.detail)
        if r is not None:
            _log_op(r, 0, f"文风蒸馏×{len(b.docs)}", bool(out.get("ok")), t0,
                    str(out.get("reason") or out.get("code") or ""))
        return out
    except Exception as e:      # noqa: BLE001 - 付费失败也要记账,再换成 502
        if r is not None:
            _log_op(r, 0, "文风蒸馏", False, t0, f"{type(e).__name__}: {e}"[:200])
        return JSONResponse({"error": f"模型调用失败:{e}", "code": "backend_failed"}, status_code=502)
    finally:
        if lock is not None:
            lock.release()

@router.post("/api/style/pointer")
def api_style_pointer(b: StylePointerBody):
    """指定用哪个档案、**注入量**给多少(弱/中/强)。注意这不是蒸馏细致度。"""
    from .. import stylepack

    try:
        return stylepack.set_pointer(_project_root(b.root), b.name, b.strength)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.get("/api/style/archives")
def api_style_archives(root: str = ""):
    """文风档案清单。

    档案住在 `<知识库>/语料库/<档案名>/`（汇总到知识库，不跟书走），
    三层是 `原文/` `蒸馏原稿/` `写手参考.md`。这里**只报份数，不带正文**——点开再取。

    `root` **可选**：书房首页没有"当前书"，而档案本来就跨书共享，
    所以不传 root 也照样能列。传了也不会变——保留这个参数只为**向后兼容**，别再依赖它。
    盘的现状是空的（从没建过档案），正常返回 `[]`；**不造数据**。
    """
    from .. import stylepack

    return {"ok": True, "root": str(stylepack.archives_root()),
            "archives": stylepack.list_archives(_project_root(root) if root else None)}

@router.get("/api/style/library")
def api_style_library(name: str = "", root: str = ""):
    """一份文风档案的细目：每篇字数、蒸没蒸过、细致度。

    形状与 `stylepack.list_library` **逐字一致**（中文键）——薄封装，不另起一套英文键再映射，
    免得将来两边漂。`root` 可选：书房首页没有当前书，档案本来就跨书共享。
    """
    from .. import stylepack

    nm = (name or "").strip() or stylepack.DEFAULT_LIB
    try:
        return {"ok": True, **stylepack.list_library(_project_root(root) if root else None, nm)}
    except ValueError as e:                 # 名字带路径分隔符等
        return {"ok": False, "error": str(e)}

@router.get("/api/style/file")
def api_style_file(name: str = "", layer: str = "", file: str = "", root: str = ""):
    """读档案某一层的正文。

    `layer` 只认 `原文` / `蒸馏原稿` / `写手参考`：
      · 前两层按篇读，`file` 必须是**该层目录下的裸文件名**（.md）
      · `写手参考` 是一整个文件，`file` 忽略
    越界一律拒：名字带分隔符 / `file` 带目录 / 解析后不在档案目录里 / 不是 .md / 超 2 MB。
    **只读**：这里不提供任何写入口。
    """
    from .. import stylepack

    nm = (name or "").strip() or stylepack.DEFAULT_LIB
    lay = (layer or "").strip()
    if lay not in ("原文", "蒸馏原稿", "写手参考"):
        return {"ok": False, "error": "层名只认 原文 / 蒸馏原稿 / 写手参考。"}
    try:
        base = stylepack.pack_dir(_project_root(root) if root else None, nm)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if lay == "写手参考":
        p = base / stylepack.ARCHIVE_FILE
    else:
        raw = (file or "").strip()
        if not raw or Path(raw).name != raw or not raw.lower().endswith(".md"):
            return {"ok": False, "error": "只能读该层目录下的 .md 文件。"}
        p = base / (stylepack.DOCS_SUB if lay == "原文" else stylepack.DISTILLED_SUB) / raw

    try:
        rp, rb = p.resolve(), base.resolve()
        if not rp.is_relative_to(rb):        # 双保险：符号链接/`..` 绕出去的一律拒
            return {"ok": False, "error": "路径越界。"}
    except OSError:
        return {"ok": False, "error": "路径不可解析。"}
    if not p.is_file():
        return {"ok": False, "error": "这一层还没有这份文件。"}
    if p.stat().st_size > stylepack.MAX_DOC_BYTES:
        return {"ok": False, "error": "文件超过 2 MB，界面不读整份。"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": f"打不开：{e}"}
    return {"ok": True, "text": text, "chars": len(text)}

# ── 0.9.0 B2 分块蒸馏端点(设计稿 v2.1 §9) ──────────────────────────────
class B2DistillBody(BaseModel):
    name: str                    # 档案名
    deep: bool = False           # 深度蒸馏(多层粗到细)
    root: str = ""               # 可选,书房不传

@router.post("/api/style/b2/status")
def api_b2_status(b: B2DistillBody):
    """块账本状态:块数/进度/failed 数/pending 数/成本预告(启动前显示用)。"""
    from ..study.chunker import load_index
    from ..study.distiller import pending_count
    from pathlib import Path as _P
    from .. import stylepack as _sp
    try:
        base = _sp.pack_dir(_project_root(b.root) if b.root else None, b.name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    idx = load_index(base)
    blocks = idx.get("blocks", [])
    pending = pending_count(base)
    return {"ok": True, "块数": len(blocks),
            "已蒸": sum(1 for x in blocks if x.get("status") == "distilled"),
            "failed": sum(1 for x in blocks if x.get("status") == "failed"),
            "human_edited": sum(1 for x in blocks if x.get("status") == "human_edited"),
            "待蒸": pending,
            "预计调用": f"≈{pending}~{pending * 2 + 6} 次(深度+验收全开)"}

@router.post("/api/style/b2/run")
def api_b2_run(b: B2DistillBody):
    """分块蒸馏执行(串行,阻塞直到整批完成/中止——前端用流式感知进度时改 SSE 再说)。"""
    from ..study import chunker as _c
    from ..study import distiller as _d
    from .. import stylepack as _sp
    from ..config import load_config
    from ..routes import get_backend_for
    try:
        base = _sp.pack_dir(_project_root(b.root) if b.root else None, b.name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    orig = base / "原文.md"
    if not orig.is_file():
        return {"ok": False, "error": "档案里没有 原文.md。"}
    # 原文重切+账本重建(chapter_hash 锚定,增量只蒸新/变块)
    _c.rebuild_index(base, orig)
    _c.write_chunk_files(base, orig)
    try:
        cfg = load_config(_project_root(b.root)) if b.root else None
        if cfg is None:
            from ..study.ask import studio_config
            cfg = studio_config()
        backend = get_backend_for(cfg, "文风学习")
        stats = _d.run_block_distill(base, orig, backend, deep=b.deep)
        return {"ok": True, **stats}
    except Exception as e:   # noqa: BLE001
        return JSONResponse({"error": f"蒸馏失败:{e}"}, status_code=502)

@router.post("/api/style/b2/merge")
def api_b2_merge(b: B2DistillBody):
    """合并:ONNX 去重→归纳(分批)→落蒸馏原稿(human_edited 保护/版本链)。"""
    from ..study import merger as _m
    from .. import stylepack as _sp
    from ..config import load_config
    from ..routes import get_backend_for
    try:
        base = _sp.pack_dir(_project_root(b.root) if b.root else None, b.name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        cfg = load_config(_project_root(b.root)) if b.root else None
        if cfg is None:
            from ..study.ask import studio_config
            cfg = studio_config()
        backend = get_backend_for(cfg, "文风学习")
        return {"ok": True, **_m.merge_distills(base, backend)}
    except Exception as e:   # noqa: BLE001
        return JSONResponse({"error": f"合并失败:{e}"}, status_code=502)

@router.post("/api/style/b2/cards")
def api_b2_cards(b: B2DistillBody):
    """压卡:蒸馏原稿 → 六节卡片集写手参考.md(禁用清单全量转录)。"""
    from ..study import merger as _m
    from .. import stylepack as _sp
    from ..config import load_config
    from ..routes import get_backend_for
    try:
        base = _sp.pack_dir(_project_root(b.root) if b.root else None, b.name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        cfg = load_config(_project_root(b.root)) if b.root else None
        if cfg is None:
            from ..study.ask import studio_config
            cfg = studio_config()
        backend = get_backend_for(cfg, "文风学习")
        cards = _m.build_cards(base, backend)
        return {"ok": True, "chars": len(cards)}
    except Exception as e:   # noqa: BLE001
        return JSONResponse({"error": f"压卡失败:{e}"}, status_code=502)

@router.get("/api/style/b2/verify")
def api_b2_verify(name: str, root: str = ""):
    """L1 覆盖率(零 LLM,自动)。"""
    from ..study import verify as _v
    from .. import stylepack as _sp
    try:
        base = _sp.pack_dir(_project_root(root) if root else None, name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **_v.l1_coverage(base, base / "原文.md")}

@router.post("/api/style/b2/l2")
def api_b2_l2(b: B2DistillBody):
    """L2 对比仿写(1 次调用,作者目测蒸馏度)。"""
    from ..study import verify as _v
    from .. import stylepack as _sp
    from ..config import load_config
    from ..routes import get_backend_for
    try:
        base = _sp.pack_dir(_project_root(b.root) if b.root else None, b.name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        cfg = load_config(_project_root(b.root)) if b.root else None
        if cfg is None:
            from ..study.ask import studio_config
            cfg = studio_config()
        backend = get_backend_for(cfg, "文风学习")
        return _v.l2_imitate(base, backend)
    except Exception as e:   # noqa: BLE001
        return JSONResponse({"error": f"仿写失败:{e}"}, status_code=502)

@router.post("/api/style/b2/migrate")
def api_b2_migrate(b: B2DistillBody):
    """旧档案迁移(作者裁定⑤乙):旧三主文件改 .v0旧版 永不自动删;原文走新流程。"""
    from .. import stylepack as _sp
    from pathlib import Path as _P
    try:
        base = _sp.pack_dir(_project_root(b.root) if b.root else None, b.name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if (base / "chunks").is_dir():
        return {"ok": False, "error": "已是新版结构,不用迁移。"}
    moved = []
    # 原文.md 不改名(新版流程仍用它);蒸馏产物改名 .v0旧版 保护(人工修改不丢)
    for fname in ("蒸馏原稿.md", "写手参考.md"):
        f = base / fname
        if f.is_file():
            f.rename(base / (fname + ".v0旧版"))
            moved.append(fname)
    # 迁移即分块(零 LLM):作者立刻能看到块清单,「开始蒸馏」只蒸内容
    from ..study import chunker as _c
    n = _c.write_chunk_files(base, base / "原文.md")
    _c.rebuild_index(base, base / "原文.md")
    return {"ok": True, "moved": moved, "块数": n,
            "提示": "旧版已保留(.v0旧版 后缀,永不自动删)。请点「开始蒸馏」走新版流程,"
                    "完成后对照新旧内容再点「确认收下」;不确认也停在新版,可手动换回。"}

