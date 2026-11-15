"""loom.api.partner —— /api/partner/* /api/navigator/*(从 server.py 原样搬家)。"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import cancel, partner, usecases
from ..backends import LoomBackendError
from ..config import load_config
from . import _common
from ._common import (_err_json, _project_root)
from .projects import RootBody

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
@router.get("/api/partner/pending")
def api_partner_pending(root: str = ""):
    """还没被拍板的候选卡（书房伙伴角标的数据源）。

    口径：有 proposal、且没有同 id 的 confirm 事件。**跨通道**统计
    （领航员 `当前.jsonl` + 各角色的 `agent-<角色>.jsonl`），因为卡可能出自任何一处。

    `root` **可选**：书房首页没有"当前书"，所以不传时
    **汇总书架上所有书**；每条 item 都带 `root`，点进候选卡时用它调 confirm 接口。

    返回里的 **`存在`**：指的是「**这次查询的那本书**在不在」——传 root 时如实反映；
    **汇总查询（不传 root）恒为 `true`**，那个 true 表示"这次汇总拿到了"，不是"某本书在"；
    书那一层要看 `items[].root`。（字段名会骗人：写成只在 false 时给键，会让 `if (j.存在)` 把真书判假。）
    """
    from .. import partner_store as _ps, userconf

    if root:
        # **显式传一本不存在/已搬走的书，不该是 400**。
        # 这是"读一个数"的只读端点——书没了就等于没有待拍板，不是客户端错误；
        # 真报 400，调用方就得为"这不是错误"写一条错误分支，违背"接口挂了要像没事"。
        # 但也**不能静默**：给一句 warning，界面能说人话（与 study/library 的 warnings 同风格）。
        try:
            roots = [Path(_project_root(root))]
        except HTTPException:
            return {"ok": True, "count": 0, "items": [], "存在": False,
                    "warning": "这本书没找到（可能被移走或改名了），所以没有待拍板的卡。"}
    else:
        roots = []
        for p in userconf.list_projects():
            r = Path(p.get("root", "") or "")
            if r.is_dir() and (r / "loom.toml").is_file():      # 只算还活着的书
                roots.append(r)

    items: list[dict] = []
    total = 0
    for r in roots:
        props = _ps.pending_proposals(r)
        total += len(props)
        for e in props:
            if len(items) >= 50:        # 汇总时给个上限，角标只要数字
                break
            brief = _ps.brief(str(e.get("content") or e.get("内容") or e.get("slot") or ""), 40)
            items.append({"id": e.get("id"), "kind": e.get("kind") or "提设定",
                          "角色": e.get("角色") or "", "root": str(r), "摘要": brief})
    # `存在` 两种情形**都要带**：只在不存在的分支里给 `false`，
    # 会让「`if (j.存在)` 才算真书」这种写法把真书判成假。汇总查询（不传 root）恒为 true：
    # 那个 true 描述的是"这次汇总拿到了"，不是"某本书在"——书那一层看 `items[].root`。
    return {"ok": True, "count": total, "items": items, "存在": True}

    # ── 下线原实现（保留备回）：下面永远走不到，仅作历史留痕 ──
        # 「这份提案写不成」通常是数据不全/写盘失败 → 400 能读的话
    # P2-2/P2-3:一个请求最多 50 章(自己勾的区间也一样),章号不许 < 1
            # 一次最多 BLOCK 章(50)。夹断要说出来,不然人以为整段都核过了(交叉审查 P2-2')
        # 老前端读 d.chapters / d.path;新字段照给,两边都不折腾
        # 缺 key 这种事后端已经写好了中文指引,别让它变成裸 500
# ----------------------------- 书房伙伴(对话) -----------------------------
class PartnerSayBody(BaseModel):
    root: str
    text: str

class PartnerConfirmBody(BaseModel):
    root: str
    id: str

@router.post("/api/partner/say")
def partner_say(b: PartnerSayBody):
    """对话一轮,ndjson 流(user/assistant/assistant_delta/tool/result/proposal/error)。
    锁裁量红线(spec §10):独立 partner 轮锁,完全不碰书写锁——织章持写锁跑几分钟期间,
    say 照样能聊,只挡两次并发 say 交错写同一 .伙伴对话/当前.jsonl。backend 用主模型
    (get_backend),不走 cheap_backend:对话是主体验。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    root = Path(b.root)
    lock = usecases.acquire_partner_lock(root)   # 拿不到 → ProjectBusyError(partner_busy) → 409

    q: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def worker():
        ts = time.strftime("%Y%m%d-%H%M%S")
        from .. import agent_status
        agent_status.mark_busy("领航员")      # 侧栏指示灯
        try:
            cfg = load_config(root)
            from .. import routes as _routes_mod
            partner.run_turn(root, b.text, _routes_mod.get_backend_for(cfg, "对话"), emit=q.put, ts=ts,
                             should_cancel=cancel.is_set)
        except LoomBackendError as e:
            # partner 流信封是 {"t":...,"ts":...}(非 /api/write 的 type-keyed),error 事件跟随;
            # code 透传,前端据此附可操作提示;e.code 为 None 时不出 code 键
            err_ev = {"t": "error", "text": str(e), "ts": ts}
            if e.code:
                err_ev["code"] = e.code
            q.put(err_ev)
        except (ValueError, FileNotFoundError) as e:
            q.put({"t": "error", "text": str(e), "ts": ts})
        except Exception as e:  # 兜底,别让流挂死
            q.put({"t": "error", "text": f"意外错误:{e}", "ts": ts})
        finally:
            agent_status.mark_busy("领航员", False)
            lock.release()   # 锁跟着 worker 走(响应流着,轮还没完),在哨兵 None 之前放
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        try:
            while True:
                ev = q.get()
                if ev is None:
                    break
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        finally:
            # 客户端断连(前端 abort)→ 生成器被关闭 → 置取消旗,worker 下一轮边界停并放锁。
            # 正常收尾(哨兵 None break)时 worker 已 return,置旗是无副作用的 no-op(spec §10.3)。
            cancel.set()

    return StreamingResponse(stream(), media_type="application/x-ndjson")

@router.post("/api/partner/confirm")
def partner_confirm_ep(b: PartnerConfirmBody):
    """拍板落盘(非流式,走真书写锁):找 proposal → 落进对应槽位 → 记 confirm 事件。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        return usecases.partner_confirm(Path(b.root), b.id, ts=time.strftime("%Y%m%d-%H%M%S"))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)

# ----------------------------- 新架构:领航员+速记本+上下文守卫 -----------------------------

class RouteBody(BaseModel):
    text: str
    root: str | None = None   # 可选,指定项目根;不填则用内置默认规则

@router.post("/api/navigator/route")
def navigator_route_ep(b: RouteBody):
    """根据用户输入文本,返回应路由到的角色名。
    
    若传入 root,优先读项目的「领航员-路由规则.md」;无则用内置规则。
    """
    from ..navigator import route as _route, route_from_project as _rfp
    if b.root:
        role = _rfp(b.root, b.text)
    else:
        role = _route(b.text)
    return {"role": role}

@router.post("/api/navigator/suggest")
def navigator_suggest_ep(b: RouteBody):
    """「该找谁」建议：返回可能不止一个角色，前端只提示、不自动开会话。

    命中的关键词越长越靠前；一个都没命中 → ["领航员"]（它自己接）。
    """
    from ..navigator import route_multi as _rm, route_multi_from_project as _rmp
    if b.root:
        roles = _rmp(_project_root(b.root), b.text)
    else:
        roles = _rm(b.text)
    return {"candidates": roles}

@router.get("/api/navigator/foundation")
def navigator_foundation_ep(root: str):
    """返回地基完成进度:哪些字段已填,哪些还空着。"""
    root_path = _project_root(root)
    from ..navigator import foundation_progress as _fp
    return _fp(root_path)

class FoundationSaveBody(BaseModel):
    root: str
    answers: dict[str, str]

@router.post("/api/navigator/foundation/save")
def navigator_foundation_save_ep(b: FoundationSaveBody):
    """把地基答案写入 Config。"""
    root_path = _project_root(b.root)
    from ..navigator import save_foundation as _sf
    _sf(root_path, b.answers)
    return {"ok": True}

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
    # 立项卡.md → 00-立项.md
@router.get("/api/navigator/foundation/fields")
def navigator_fields_ep():
    """返回地基字段定义(问法+key),供前端渲染填表单。"""
    from ..navigator import FOUNDATION_FIELDS as _fields
    return {"fields": _fields}

@router.post("/api/partner/new")
def partner_new_ep(b: RootBody):
    """归档当前伙伴对话、另起一段(走真书写锁;不动书内容,只挪 jsonl 文件)。"""
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        return usecases.partner_new(Path(b.root), stamp=time.strftime("%Y%m%d-%H%M%S"))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)

@router.get("/api/partner/history")
def partner_history_ep(root: str):
    """纯读派生视图,无锁(同 journey_state)。"""
    return usecases.partner_history(Path(root))

