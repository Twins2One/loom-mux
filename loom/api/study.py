"""loom.api.study —— /api/knowledge/* /websrc/* /align/* /brain/copyable /brain/copy(从 server.py 原样搬家)。"""
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
class KnowledgeAskBody(BaseModel):
    root: str
    question: str
    inside: str = ""        # 书内材料节选(前端从打开的文件里带过来,省一次读盘)
    attached: str = ""      # 作者自己贴的资料
    web: str = ""           # 联网检索结果(带网址,来源标 [联网])
    evidence: bool = False  # 跨章取证(裁定 3):先让「长上下文检索」槽从章目录里挑相关章
    browse: bool = False    # 让顾问自己上网抓页(裁定 2 的「浏览器」);没配就用不了

class KnowledgeAcceptBody(BaseModel):
    root: str
    question: str
    proposal: dict
    attached: str = ""
    web: str = ""

@router.get("/api/knowledge")
def api_knowledge(root: str):
    """这本书有哪些知识条目、反向索引几行。**纯只读。**"""
    from .. import knowledge

    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    return {**knowledge.listing(r), "落盘": False}

@router.post("/api/knowledge/ask")
def api_knowledge_ask(b: KnowledgeAskBody):
    """知识顾问出一份**提案**(会花钱,走「深度逻辑推理」槽)。**不落盘** —— 勾选才入账。

    知识顾问已下线（真花钱入口加下线闸）。
    下线 ≠ 删代码——knowledge.ask 整条链路保留（正式版可能回来），只是入口应答
    明确告知已下线；只读的 /api/knowledge 列表保留（旧书已有条目还能看）。"""
    return JSONResponse(
        {"error": "知识顾问已下线：这个入口不再发起模型调用。"
                  "已有的知识条目仍可在「查资料」里只读查看；旧书数据不受影响。",
         "code": "knowledge_retired"},
        status_code=410)
    # ── 下线原实现（保留备回）：下面永远走不到，仅作历史留痕 ──
    from ..config import load_config
    from .. import knowledge
    from .. import usecases as _uc
    from ..routes import get_backend_for

    try:
        r = _project_root(b.root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    if not (b.question or "").strip():
        return JSONResponse({"error": "先写下要问什么。", "code": "no_question"}, status_code=400)
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再问。", "code": "busy"}, status_code=409)
    t0 = time.monotonic()
    try:
        cfg = load_config(r)
        backend = get_backend_for(cfg, "深度逻辑推理")
        rtr = get_backend_for(cfg, "长上下文检索") if (b.evidence or b.browse) else None
        out = knowledge.ask(r, b.question, backend, inside=b.inside, attached=b.attached,
                            web=b.web, retrieve_backend=rtr, evidence=b.evidence,
                            browse_backend=rtr, browse_now=b.browse)
        _log_op(r, 0, "知识顾问", bool(out.get("ok")), t0, str(out.get("说明") or ""))
        return out
    except Exception as e:      # noqa: BLE001 - 付费失败也要记账
        _log_op(r, 0, "知识顾问", False, t0, f"{type(e).__name__}: {e}"[:200])
        return _fail(e, "知识顾问")
    finally:
        lock.release()

@router.post("/api/knowledge/accept")
def api_knowledge_accept(b: KnowledgeAcceptBody):
    """人勾选后入账:写 `外置大脑/知识/<主题>.md`,并重建反向索引。"""
    from .. import knowledge

    try:
        r = _project_root(b.root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再入账。", "code": "busy"},
                            status_code=409)
    try:
        return knowledge.accept(r, b.question, b.proposal or {}, attached=b.attached, web=b.web)
    except (KeyError, ValueError, TypeError, OSError) as e:
        # 「这份提案写不成」通常是数据不全/写盘失败 → 400 能读的话
        return JSONResponse({"error": f"没能写进知识库:{e}", "code": "accept_failed"},
                            status_code=400)
    except Exception as e:      # noqa: BLE001 - 其余按「自家 bug」处理,不冒充用户错(N-5)
        return _fail(e, "知识入账")
    finally:
        lock.release()

class WebSearchBody(BaseModel):
    root: str
    query: str
    limit: int = 0

class WebFetchBody(BaseModel):
    root: str = ""          # 抓网页不依赖具体哪本书;给了就按它的配置提示
    url: str
    max_chars: int = 8000

@router.get("/api/websrc")
def api_websrc(root: str):
    """搜索服务配没配(不显示 key 的值)。**纯只读、不联网。**"""
    from .. import websrc

    return websrc.status(root)

@router.post("/api/websrc/search")
def api_websrc_search(b: WebSearchBody):
    """问外面那个搜索服务(会联网)。**没配就明说没配,绝不编结果。**"""
    from .. import websrc

    try:
        _project_root(b.root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    out = websrc.search(b.root, b.query, limit=b.limit)
    if out.get("code") in ("not_configured", "no_query"):
        return JSONResponse(out, status_code=400)
    if not out.get("ok"):
        return JSONResponse(out, status_code=502)
    return out

@router.post("/api/websrc/fetch")
def api_websrc_fetch(b: WebFetchBody):
    """内置的「浏览器」:抓一个网页正文,削成纯文本。只许 http/https,内网不碰。"""
    from .. import websrc

    out = websrc.fetch_page(b.url, max_chars=max(500, min(int(b.max_chars or 8000), 40000)))
    if not out.get("ok"):
        code = out.get("code")
        return JSONResponse(out, status_code=400 if code == "bad_url" else 502)
    return out

class AlignReviewBody(BaseModel):
    root: str
    chapter: int = 0        # 从哪一章算起(按 50 章一块);0 = 第 1 章
    lo: int = 0             # 也可以直接给区间(0 表示按章算块)
    hi: int = 0
    span: int = 50

class AlignAcceptBody(BaseModel):
    root: str
    report: dict

@router.get("/api/align")
def api_align(root: str):
    """这本书已经存过哪些「大纲符合度」复核文档。**纯只读。**"""
    from .. import align

    try:
        r = _project_root(root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    return {**align.listing(r), "落盘": False}

@router.post("/api/align/review")
def api_align_review(b: AlignReviewBody):
    """领航员复核「章纲要的 vs 正文写的」(会花钱,走「长上下文检索」槽)。**只出提案,不落盘。**"""
    from ..config import load_config
    from .. import align
    from .. import usecases as _uc
    from ..routes import get_backend_for

    try:
        r = _project_root(b.root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    # P2-2/P2-3:一个请求最多 50 章(自己勾的区间也一样),章号不许 < 1
    _asked_hi = 0
    if b.lo and b.hi and b.hi >= b.lo:
        _asked_hi = int(b.hi)
        lo = max(1, int(b.lo))
        hi = min(max(1, int(b.hi)), lo + align.BLOCK - 1)
    else:
        span = max(1, min(int(b.span or align.BLOCK), align.BLOCK))
        lo, hi = align.block_range(max(1, int(b.chapter or 1)), span)
    if lo < 1 or hi < lo:
        return JSONResponse({"error": "章号得从第 1 章起。", "code": "bad_chapter"}, status_code=400)
    _clamped = bool(_asked_hi and _asked_hi > hi)
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再复核。", "code": "busy"}, status_code=409)
    t0 = time.monotonic()
    try:
        cfg = load_config(r)
        backend = get_backend_for(cfg, "长上下文检索")
        out = align.review(r, lo, hi, backend)
        if _clamped and isinstance(out, dict):
            # 一次最多 BLOCK 章(50)。夹断要说出来,不然人以为整段都核过了(交叉审查 P2-2')
            out["提示"] = (f"一次最多核 {align.BLOCK} 章:这次核的是第 {lo}-{hi} 章,"
                          f"你要的到第 {_asked_hi} 章;后面的再点一次。")
            out["被夹到上限"] = True
        if out.get("ok"):
            _log_op(r, 0, "大纲符合度复核", True, t0, f"第{lo}-{hi}章 {len(out.get('偏离章') or [])} 章偏离")
        else:
            _log_op(r, 0, "大纲符合度复核", False, t0, str(out.get("reason") or ""))
        return out
    except Exception as e:      # noqa: BLE001 - 付费失败也要记账
        _log_op(r, 0, "大纲符合度复核", False, t0, f"{type(e).__name__}: {e}"[:200])
        return _fail(e, "大纲复核")
    finally:
        lock.release()

@router.post("/api/align/accept")
def api_align_accept(b: AlignAcceptBody):
    """人点过才存:写 `外置大脑/复核/大纲符合度-第LO-HI章.md`。**不改章纲、不改正文。**"""
    from .. import align

    try:
        r = _project_root(b.root)
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)
    lock = _uc.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再存。", "code": "busy"},
                            status_code=409)
    try:
        return align.accept(r, b.report or {})
    except (KeyError, ValueError, TypeError, OSError) as e:
        return JSONResponse({"error": f"没能存这份复核:{e}", "code": "accept_failed"},
                            status_code=400)
    except Exception as e:      # noqa: BLE001 - 其余按「自家 bug」处理(N-5)
        return _fail(e, "大纲复核存档")
    finally:
        lock.release()

