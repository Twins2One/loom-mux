"""loom.api.agents —— /api/agents* /agent/* /scratchpad* /slots /panel/* /preflight /proposal/retarget /fullreview/*(从 server.py 原样搬家)。"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import cancel, events, usecases
from ..backends import PROVIDERS, LoomBackendError, list_models
from ..config import load_config
from . import _common
from ._common import (_bad_chapter, _checked_root, _err_json, _fail, _log_op, _project_root)
from .projects import RootBody

router = APIRouter()


def _known_role(role):
    from ..collab import _ALL_ROLES
    if role not in _ALL_ROLES:
        raise HTTPException(status_code=404, detail=f"未知角色: {role}")

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
@router.get("/api/agents")
def agents_list_ep():
    """返回所有角色的元数据（不含提示词正文）。"""
    from ..agent_api import list_agents as _la
    return _la()

class AgentMetaBody(BaseModel):
    role: str

@router.get("/api/agent/meta")
def agent_meta_ep(role: str):
    """返回角色的元数据（不含提示词正文）。"""
    from ..agent_api import agent_meta as _am
    meta = _am(role)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"未知角色: {role}")
    return meta

@router.get("/api/scratchpads")
def scratchpads_list_ep(root: str):
    """返回项目里已有速记本的角色名列表。"""
    from ..agent_api import list_scratchpad_roles as _lsr
    return {"roles": _lsr(_project_root(root))}

@router.get("/api/slots")
def slots_ep(root: str, container: str = ""):
    """列出某容器文件的所有可写格子（丁方案的「选落点」用）。container 为空 = 全书所有格子。

    只读、纯派生。丁方案要求作者先点选落点，模型不再生成它——这个接口就是给那个「点选」用的。
    """
    from .. import journey as journey_mod
    from .. import slots as slots_mod
    root_path = _project_root(root)
    out = []
    for spec in journey_mod.STAGES:
        for s in slots_mod.stage_slots(root_path, spec):
            if container and s.container != container:
                continue
            out.append({
                "id": s.id,
                "label": getattr(s, "label", "") or "",
                "container": s.container,
                "key": getattr(s, "key", "") or "",
                "at": getattr(s, "at", "") or "",
                "filled": bool(getattr(s, "filled", False)),
                "preview": getattr(s, "preview", "") or "",
            })
    return {"slots": out}

@router.get("/api/scratchpad/{role}")
def scratchpad_load_ep(role: str, root: str, max_entries: int = 5):
    """读取角色速记本的最后 max_entries 条。"""
    from ..agent_api import get_scratchpad as _gs
    return _gs(role, _project_root(root), max_entries=max_entries)

class ScratchpadAppendBody(BaseModel):
    root: str
    role: str = ""
    content: str
    note: str = ""

@router.post("/api/scratchpad/{role}")
def scratchpad_append_ep(role: str, b: ScratchpadAppendBody):
    """追加一条记录到角色速记本。"""
    from ..agent_api import append_scratchpad as _as
    return _as(role, _project_root(b.root), b.content, b.note)

# ── 旧书单角色模板补缺（人物侧写师）───────────────────
class RoleTemplateBody(BaseModel):
    root: str
    role: str = "人物侧写师"
    confirmation_ticket: str = ""


@router.get("/api/agent/template/status")
def agent_template_status_ep(root: str, role: str = "人物侧写师"):
    from .. import role_template_install as _rti
    return _rti.inspect(_project_root(root), role)


@router.post("/api/agent/template/confirm")
def agent_template_confirm_ep(b: RoleTemplateBody):
    from .. import role_template_install as _rti
    try:
        return _rti.confirm(_project_root(b.root), b.role)
    except _rti.InstallError as exc:
        return JSONResponse({"ok": False, "code": exc.code, "error": str(exc)}, status_code=exc.status)


@router.post("/api/agent/template/install")
def agent_template_install_ep(b: RoleTemplateBody):
    from .. import role_template_install as _rti
    try:
        return _rti.install(_project_root(b.root), b.confirmation_ticket, b.role)
    except _rti.InstallError as exc:
        return JSONResponse({"ok": False, "code": exc.code, "error": str(exc)}, status_code=exc.status)


# ── 作者智库一对一对话 ───────────────────
class AgentChatBody(BaseModel):
    root: str
    role: str
    text: str = ""
    locked_slot: str = ""   # 丁方案：作者指定要改的那一格（形如 外置大脑/设定集/...md#键）；空=自由模式
    # 落库后自动续一轮。true = 这一轮不是作者说的,是"接着上一轮往下走"(不记 user 事件)
    resume: bool = False
    # ── 任务准备架构（2026-11-08,§13.2:老客户端缺字段=只定位服务端当前代际,不任意认领）──
    conversation_id: str = ""       # 另起对话的代际;空 = 服务端当前代际「默认」
    task_id: str = ""               # 承接既有任务;空 = 服务端定位/新建
    decision_event_id: str = ""     # 接受后续轮携带的决策事件(§8,关联原任务)

@router.post("/api/agent/chat")
def agent_chat_ep(b: AgentChatBody):
    """与某个作者智库角色聊一轮。ndjson 流，信封同 /api/partner/say（t 键）。"""
    from ..agent_api import agent_meta as _am
    b.root = str(_project_root(b.root))
    if _am(b.role) is None:
        raise HTTPException(status_code=404, detail=f"未知角色: {b.role}")
    root = Path(b.root)
    # 必须早于队列/线程/backend/任务准备/历史：旧书缺模板是前置状态，不是一轮失败。
    from .. import role_template_install as _rti
    _preflight = _rti.chat_preflight(root, b.role)
    if _preflight is not None:
        return JSONResponse(_preflight, status_code=409)
    q: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def worker():
        ts = time.strftime("%Y%m%d-%H%M%S")
        from .. import agent_status
        agent_status.mark_busy(b.role)      # 侧栏指示灯:这个角色正在思考
        try:
            from .. import agent_chat
            from .. import routes as routes_mod
            cfg = load_config(root)
            agent_chat.run_turn(root, b.role, b.text, routes_mod.get_backend_for(cfg, "对话"),
                                emit=q.put, ts=ts, should_cancel=cancel.is_set,
                                locked_slot=b.locked_slot,
                                protocol=routes_mod.preferred_tool_protocol(cfg),
                                resume=b.resume,
                                conversation_id=b.conversation_id,
                                task_id=b.task_id,
                                decision_event_id=b.decision_event_id)
        except Exception as e:      # noqa: BLE001
            q.put({"t": "error", "text": f"{type(e).__name__}: {e}", "ts": ts})
        finally:
            agent_status.mark_busy(b.role, False)
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

@router.post("/api/agent/chat/new")
def agent_chat_new_ep(b: AgentChatBody):
    """归档与某个智库角色的对话、另起一段。

    只挪 `.伙伴对话/agent-<角色>.jsonl`,不动书内容;旧段仍留档可查。
    """
    from ..agent_api import agent_meta as _am
    b.root = str(_project_root(b.root))
    if _am(b.role) is None:
        raise HTTPException(status_code=404, detail=f"未知角色: {b.role}")
    # 另起对话的身份边界(2026-11-08 复审用例):旧对话里非终态任务(ready/waiting_author…)
    # 必须先 paused——否则 active_only 过滤不了它们,另起后刷新会把旧任务当"当前任务"回显/承接。
    # paused 保留盘上可追溯(§8 旧任务留档),turn_prepare 不承接 paused(§5 状态机)。
    from ..paths import TASKS_DIR as _TASKS_DIR
    from .. import task_state as _ts
    try:
        # 2026-11-08 午：另起对话只暂停**该角色自己**的旧任务（跨角色不误伤）。
        _ts.pause_active_tasks(Path(b.root) / _TASKS_DIR / "默认",
                               reason="另起一段对话,旧任务归档暂停", role=b.role)
    except Exception as e:      # noqa: BLE001 - 归档失败不挡"另起"主流程,但要在响应里可见
        import logging
        logging.getLogger("loom.task").warning("另起对话暂停旧任务失败: %s", e)
    try:
        return usecases.agent_new(Path(b.root), b.role, stamp=time.strftime("%Y%m%d-%H%M%S"))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)
    except Exception as e:      # noqa: BLE001 - 撞锁等 → 409 让前端说人话
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=409)

# ── 议事例会(一句需求 → 多位角色并发分析)──────────────
# 与 /api/agent/chat 的关系:那条是「一对一」,这条是「一对多」。
# 落盘:.伙伴对话/议事/<轮次>.jsonl(合并流水)+ 各角色自己的 agent-<角色>.jsonl(带 via=议事)。
class PanelPlanBody(BaseModel):
    root: str
    text: str = ""

class PanelSayBody(BaseModel):
    root: str
    text: str = ""
    roles: list[str] = []     # 空 = 让领航员点名;非空 = 沿用这一批人(省钱,名单也不再变)
    repick: bool = False      # true = 哪怕给了 roles 也重新点名

class PanelWrapBody(BaseModel):
    root: str
    panel: str

def _panel_gate(root):
    from ..journey import writing_unlocked
    ready, missing = writing_unlocked(Path(root))
    if not ready:
        return JSONResponse({"error": "先完成地基，再叫大家来议。", "code": "foundation_incomplete",
                             "missing": missing}, status_code=409)
    return None


@router.post("/api/panel/plan")
def panel_plan_ep(b: PanelPlanBody):
    """让领航员点名该找谁(**花 1 次调用**)。只出名单,不派发、不开始议事。"""
    from .. import roundtable
    from .. import routes as routes_mod

    root = _project_root(b.root)
    block = _panel_gate(root)
    if block is not None:
        return block
    cfg = load_config(root)
    try:
        backend = routes_mod.get_backend_for(cfg, "对话")
        out = roundtable.pick(root, b.text, backend)
    except LoomBackendError as e:
        return _err_json(e)
    except Exception as e:      # noqa: BLE001 - 点不出名不该让界面崩
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, "roles": out["roles"], "source": out["source"], "note": out["note"],
            "并发上限": roundtable.cap(cfg), "参会上限": roundtable.MAX_ROLES,
            "可参会": list(roundtable.PANEL_ROLES), "落盘": False}

@router.post("/api/panel/say")
def panel_say_ep(b: PanelSayBody):
    """开一场议事:先点名(除非名单已给),再并发派发。ndjson 流,每条事件带 role。

    **钱**:点名 1 次 + 每位参会角色 1 轮(谁再问同事,每人最多 +2)。
    作者按下发送 = 授权这一次;所以名单、人数、并发上限都先流给前端,别让人蒙着付钱。
    """
    from .. import roundtable
    from .. import routes as routes_mod

    b.root = str(_project_root(b.root))
    root = Path(b.root)
    block = _panel_gate(root)
    if block is not None:
        return block
    q: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def worker():
        ts = time.strftime("%Y%m%d-%H%M%S")
        try:
            cfg = load_config(root)
            roles = [r for r in (b.roles or []) if r in roundtable.PANEL_ROLES]
            note, source = "", ""
            if b.repick or not roles:
                backend = routes_mod.get_backend_for(cfg, "对话")
                picked = roundtable.pick(root, b.text, backend if b.text.strip() else None)
                roles = [x["role"] for x in picked["roles"]]
                note, source = picked["note"], picked["source"]
            if not roles:
                q.put({"t": "error", "text": "没点出可参会的角色 —— 换个说法,或先手动选人。"})
                q.put(None)
                return
            roundtable.run_panel(
                root, b.text, roles, cfg,
                get_backend_for=routes_mod.get_backend_for,
                emit=q.put, ts=ts, should_cancel=cancel.is_set,
                protocol=routes_mod.preferred_tool_protocol(cfg),
                note=note, source=source,
            )
        except LoomBackendError as e:
            ev = {"t": "error", "text": str(e)}
            if e.code:
                ev["code"] = e.code
            q.put(ev)
        except Exception as e:      # noqa: BLE001 - 兜底,别让流挂死
            q.put({"t": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
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

@router.post("/api/panel/wrapup")
def panel_wrapup_ep(b: PanelWrapBody):
    """领航员收口(作者点按钮才跑,多花 1 次调用)。ndjson 流,通常就一条 assistant。"""
    from .. import roundtable
    from .. import routes as routes_mod

    b.root = str(_project_root(b.root))
    root = Path(b.root)
    q: queue.Queue = queue.Queue()

    def worker():
        try:
            cfg = load_config(root)
            backend = routes_mod.get_backend_for(cfg, "对话")
            roundtable.wrapup(root, b.panel, backend, q.put,
                              ts=time.strftime("%Y%m%d-%H%M%S"))
        except LoomBackendError as e:
            ev = {"t": "error", "text": str(e)}
            if e.code:
                ev["code"] = e.code
            q.put(ev)
        except Exception as e:      # noqa: BLE001
            q.put({"t": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")

@router.get("/api/agents/status")
def api_agents_status(root: str):
    """侧栏角色指示灯。**纯只读、不调模型**。

    两盏灯都是事实,不是猜:
    - 在思考:这条进程里该角色的工作线程正在跑(进程重启即清零,那也是事实)。
    - 有欠账:它最后一条是"我说的",作者之后没再回它 —— 直接从对话流水尾巴读。
    """
    from .. import agent_status

    root_path = _project_root(root)
    return {"ok": True, "角色": agent_status.snapshot(root_path),
            "读于": agent_status.stamp(), "落盘": False}

@router.get("/api/panel/history")
def panel_history_ep(root: str, panel: str = ""):
    """不传 panel → 列出最近的几场议事;传了 → 读回那一场的全部事件。**纯只读**。"""
    from .. import roundtable

    root_path = _project_root(root)
    if not (panel or "").strip():
        return {"ok": True, "rounds": roundtable.list_rounds(root_path),
                "参会上限": roundtable.MAX_ROLES, "可参会": list(roundtable.PANEL_ROLES)}
    return {"ok": True, "panel": panel, "events": roundtable.read_round(root_path, panel)}

# ── 逐章步骤(侧边栏按章列六棒 + 人工单步启动)────
@router.get("/api/preflight")
def preflight_ep(root: str, chapter: int = 0, role: str = ""):
    """开跑前的风险体检(崩溃风险出现前预警,并说清具体风险)。

    **只读、不花钱**:只打一次 GET /models(不调生成)。返回每条风险的级别/说明/怎么办,
    前端在开写前把它摆给作者看——高危可以拦,其余只提醒。
    """
    from .. import agents as _ag
    root = Path(root)
    risks: list[dict] = []

    def add(level: str, what: str, how: str) -> None:
        risks.append({"级别": level, "风险": what, "怎么办": how})

    # ① 网关/模型可达 + 模型名真在清单里 —— 上次那个 502「目标模型 … 启动失败」就是这一条
    models, err = [], ""
    try:
        r = list_models(cfg_provider(root), base_url=cfg_base_url(root), api_key=cfg_key(root))
        models = r.get("models") or []
        if not r.get("ok"):
            err = r.get("message") or "拉不到模型列表"
    except Exception as e:      # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    cur = cfg_model(root)
    if err:
        add("高", f"连不上模型端点或拉不到模型列表:{err}",
            "先确认模型服务在跑(端口/地址对不对),再开写;否则每一棒都会在第一步失败。")
    elif models and not _model_in(models, cur):
        add("高", f"当前模型「{cur}」不在服务端返回的 {len(models)} 个模型里",
            "点「拉取可用模型」重新选一个真实存在的,再点「保存后端」。这一条正是上次 "
            "「目标模型 … 启动失败」502 的成因。")

    # ② 角色提示词文件缺 —— load_agent 会直接抛,写章在第 1 棒就崩
    cfg = None
    try:
        cfg = load_config(root)
    except Exception:      # noqa: BLE001
        pass
    if cfg is not None:
        want = [role] if role else [(getattr(x, "role", "") or "") for x in _ag.STEPS]
        miss = [r for r in want if r and not (root / "agents" / f"{r}.md").is_file()]
        if miss:
            add("高", f"缺角色提示词文件:{'、'.join(miss)}",
                "从模板补回 agents/<角色>.md,或用「搬进设定集/重新初始化」把书补齐。")

        # ③ 目标字数太大 → 输出容易被截断/超时
        tgt = int(getattr(cfg, "chapter_chars", 0) or 0)
        if tgt > 5000:
            add("中", f"终稿目标字数 {tgt} 偏高",
                "本地模型单次输出有上限,超过约 5000 字容易半截收尾;建议先按 3000~4000 跑通。")

    # ④ 对话历史太长 → 本地模型上下文塞爆
    chat_dir = root / ".伙伴对话"
    if chat_dir.is_dir():
        for f in chat_dir.glob("*.jsonl"):
            try:
                mb = f.stat().st_size / 1024 / 1024
            except OSError:
                continue
            if mb > 1.5:
                add("中", f"对话记录「{f.name}」已经有 {mb:.1f} MB",
                    "对话越长越吃上下文,本地模型会开始丢前面的内容;建议点「另起一段对话」归档旧段再继续。")

    # ④b 上一章偏短(裁定乙:提示不拦)
    if chapter > 1:
        _pv = root / "正文" / f"第{chapter - 1}章.md"
        if _pv.is_file() and cfg is not None:
            try:
                _pc = len(_pv.read_text(encoding="utf-8"))
            except OSError:
                _pc = 0
            _pt = int(getattr(cfg, "chapter_chars", 0) or 0)
            if _pt and _pc < _pt * 0.6:
                add("中", f"上一章(第 {chapter - 1} 章)只有 {_pc} 字,目标是 {_pt} 字",
                    "可能还没写完。写新章前先回去补完上一章,或者确认这就是你要的长度。")

    # ⑤ 写作指纹还是中性默认 → 写出来不会像你
    fp = root / "外置大脑" / "写作指纹.md"
    if fp.is_file():
        try:
            txt = fp.read_text(encoding="utf-8")
        except OSError:
            txt = ""
        if "中性默认" in txt or len(txt.strip()) < 200:
            add("低", "写作指纹还是中性默认", "先从「喂样本」喂几篇你自己的稿子,出来的东西才像你。")

    hi = sum(1 for x in risks if x["级别"] == "高")
    return {"ok": True, "风险": risks, "高危数": hi,
            "模型": cur, "模型数": len(models), "章节": chapter,
            "模型清单": [m if isinstance(m, str) else (m.get("id") or "") for m in models]}

def _model_in(models, cur: str) -> bool:
    """清单里到底有没有这个模型。比字符串相等宽松一点(去空白、忽略大小写、允许前缀差),
    宁可漏报也不误报 —— 误报会让作者把好配置改坏(第一版就误报过一次)。"""
    c = (cur or "").strip().lower()
    if not c:
        return True          # 没配模型时不在这里报,交给别的检查
    for m in models or []:
        mid = (m if isinstance(m, str) else (m.get("id") or "")).strip().lower()
        if not mid:
            continue
        if mid == c or mid.endswith(c) or c.endswith(mid):
            return True
    return False

def cfg_provider(root: Path) -> str:
    try:
        return load_config(root).provider
    except Exception:      # noqa: BLE001
        return ""

def cfg_base_url(root: Path) -> str:
    try:
        return load_config(root).base_url or ""
    except Exception:      # noqa: BLE001
        return ""

def cfg_key(root: Path) -> str:
    """取这本书该用的 key:先让 load_config 把书级/用户级 .env 都装进 env,再读。

    真机踩坑教训:只 load_config(root) 时 openai_compat 的 key 仍读不到(它常常只写在
    用户级 ~/.loom/.env 里),于是 list_models 直接回"先填 API Key"——那样 preflight 会把
    "拉不到模型列表"误报成高危。所以这里再显式装一次用户级 .env,和 /api/backend/ping 一致。
    """
    try:
        c = load_config(root)
        try:
            from dotenv import load_dotenv
            from ..config import user_env_path
            load_dotenv(user_env_path(), override=True)
        except Exception:      # noqa: BLE001
            pass
        spec = PROVIDERS.get((c.provider or "").lower()) or {}
        return os.environ.get(spec.get("key_env", ""), "") if spec.get("key_env") else ""
    except Exception:      # noqa: BLE001
        return ""

def cfg_model(root: Path) -> str:
    try:
        return load_config(root).model or ""
    except Exception:      # noqa: BLE001
        return ""

            # 裁定(乙):**字数低于目标 60% 只提示,不拦**——
            # 「文件存在」太松(200 字半截也算),「六棒全做过」又会把"我就想先写第 3 章"堵死。
    # 上一章还没弄完就别开新的
@router.get("/api/agent/chat/history")
def agent_chat_history_ep(root: str, role: str, tail: int = 50):
    """读某角色的对话记录。纯只读。"""
    from .. import agent_chat
    _known_role(role)
    return {"events": agent_chat.history(_project_root(root), role, tail=tail)}

# ── 任务准备架构（2026-11-08）:快照读取 / 拒绝承接 / 任务列表 ─────────────

@router.get("/api/agent/task")
def agent_task_snapshot_ep(root: str, role: str = "", conversation_id: str = "默认",
                           task_id: str = "", active_only: bool = False):
    """读任务状态快照。**纯只读、零模型调用**（§13.2:恢复读取不触发模型工作）。

    给 task_id → 直接读;不给 → 返回该会话代际的全部任务快照(前端刷新恢复用)。
    夜间补缺第五班(补1):`active_only=true` 只返回活动任务(非终态);role 非空时
    再按 scope.role 过滤——1:1 刷新恢复用它按当前角色筛「当前任务」,旧任务/
    completed 不冒充。不传参数时行为与旧版完全一致(向后兼容)。
    """
    from .. import task_state as _ts_mod
    from ..paths import TASKS_DIR
    # F10（分片5）：root 走 _project_root 口径（与 chat/new 等一致）——
    # 原来直接 Path(root)，指定外部目录+task_id 可读任意已知任务目录。
    root = str(_project_root(root))
    base = Path(root) / TASKS_DIR / _ts_mod.sanitize_conversation_id(conversation_id or "默认")
    if task_id:
        st = _ts_mod.load_or_rebuild(base, task_id)
        return {"ok": st is not None, "task": st.to_dict() if st else None}
    out = []
    if base.is_dir():
        for candidate_id in _ts_mod.task_ids(base):
            st = _ts_mod.load_snapshot(base, candidate_id)
            if st is None:
                continue
            # active_only=「当前可承接的活动任务」:paused(另起对话归档/让位)不算——
            # 复审用例(审查记录):另起后旧 paused 任务不得回显(2026-11-08)
            if active_only and st.status not in ("ready", "running", "waiting_author", "blocked"):
                continue
            if active_only and role and \
                    (st._d.get("scope") or {}).get("role", "") != role:
                continue
            out.append(st.to_dict())
    return {"ok": True, "tasks": out}


class AgentRejectBody(BaseModel):
    root: str
    proposal_id: str
    role: str = ""
    original_text: str = ""     # 作者拒绝原话（§13.3:必须可定位,不接受空拒绝）


@router.post("/api/agent/reject")
def agent_reject_ep(b: AgentRejectBody):
    """作者明确原话拒绝某张候选/建议:记录+跨轮承接进任务状态（§13.3 必做）。

    **不调模型**。original_text 为空时拒绝不承接（如实返回 recorded=False,不谎称已有按钮路径）。
    """
    b.root = str(_project_root(b.root))
    return usecases.task_reject(Path(b.root), b.proposal_id, b.role,
                                original_text=b.original_text)

# ── 候选卡换落点（审查跟进项①）────────────────────────────
class RetargetBody(BaseModel):
    root: str
    role: str
    id: str
    new_slot: str

@router.post("/api/proposal/retarget")
def proposal_retarget_ep(b: RetargetBody):
    """把一张候选卡换到另一个落点：**内容原样保留，不调模型、不花钱**。

    为什么不在前端直接改：proposal 是"已生成的事实"，改它必须留档。
    这里复制原提案、只换 slot、新 id 写回同一个角色的对话记录，原提案不动
    （作者之后还能从历史里翻到它）。
    """
    root = _project_root(b.root)
    _known_role(b.role)
    from .. import agent_chat, partner_store
    prop = partner_store.find_proposal(root, b.id)
    if not prop:
        return {"error": "这张候选卡找不到了（可能已被清理）"}
    new_slot = (b.new_slot or "").strip()
    if not new_slot or "#" not in new_slot:
        return {"error": "新落点不合法（要 容器#键 形式）"}
    new_prop = dict(prop)
    new_prop["slot"] = new_slot
    new_prop["id"] = f'{b.id}-m{int(time.time() * 1000) % 1000000}'
    # before 要按新格子重算，否则落库时的快照比对会误判 stale
    try:
        from .. import usecases
        new_prop["before"] = usecases._slot_preview(root, new_slot) or ""
    except Exception:
        new_prop["before"] = ""
    new_prop["retargeted_from"] = b.id
    agent_chat._append_event(Path(root), b.role, new_prop)
    return {"id": new_prop["id"], "slot": new_slot, "before": new_prop.get("before", "")}

# ── 旧结构 → 设定集（裁定「乙」：作者点了才搬）────────────
    # 立项卡.md → 00-立项.md
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
# ── 联动提示层(丙·固定工作流)──────────────────────────────
# 三个端点,纪律见 src/loom/chains.py 文档头:
#   GET  /api/chains          纯只读:现在该提示哪些链(读盘算指纹,不写盘、不调模型)
#   POST /api/chains/baseline 每本书第一次检查时建基线(缺哪个键补哪个,已有不动)
#   POST /api/chains/ack      作者点「知道了」/ 点了该链动作 → 只记账那一个键
# 三个端点都不发模型请求、都不写稿子;ack/baseline 只写 .loom_state.json 的 chain_seen。
# ── 编辑总监「一次审全书」(不挂链,只有人点才跑)──────────
# 三段:plan(0 调用,报账+体检) → block(1 调用/块,落块记,可中断续跑) → finish(1 调用,写报告)。
# 只写 .审稿留痕/(块记 + 报告),**一个字都不改稿子**;块记存在就跳过,所以续跑不会重复花钱。
class FullReviewBlockBody(BaseModel):
    root: str
    lo: int = 0
    hi: int = 0
    force: bool = False      # 块记已在盘上还硬要重跑（默认跳过，不重复花钱）

@router.get("/api/fullreview/plan")
def api_fullreview_plan(root: str):
    """报账:几块、几次调用、预计几分钟,外加体检问题地图。**0 次模型调用、纯只读。**"""
    r, bad = _checked_root(root)
    if bad:
        return bad
    from .. import fullreview
    try:
        return fullreview.plan(r)
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}

@router.post("/api/fullreview/block")
def api_fullreview_block(b: FullReviewBlockBody):
    """通读一块(1 次调用,落块记)。拿不到写锁 → 409,绝不并发烧钱。"""
    r, bad = _checked_root(b.root)
    if bad:
        return bad
    lo, hi = int(b.lo or 0), int(b.hi or 0)
    if lo < 1 or hi < lo:
        return JSONResponse({"ok": False, "reason": "章号得从 1 起,且「起」不能大于「止」。"},
                            status_code=400)
    hi = min(hi, lo + 119)      # 一块最多 120 章：再大会把窗口撑爆，也不省调用次数
    from .. import fullreview
    lock = usecases.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再审。", "code": "busy"},
                            status_code=409)
    t0 = time.monotonic()
    try:
        from ..config import load_config
        from ..routes import get_backend_for
        backend = get_backend_for(load_config(r), "长上下文检索")
        out = fullreview.run_block(r, lo, hi, backend, force=bool(b.force))
        _log_op(r, 0, "全书审·通读", bool(out.get("ok")), t0,
                f"第{lo}-{hi}章 " + str(out.get("块记") or out.get("reason") or ""))
        return out
    except Exception as e:      # noqa: BLE001 - 付费失败也要记账
        _log_op(r, 0, "全书审·通读", False, t0, f"{type(e).__name__}: {e}"[:200])
        return _fail(e, "全书审·通读")
    finally:
        lock.release()

@router.post("/api/fullreview/finish")
def api_fullreview_finish(b: RootBody):
    """收口(1 次调用):块记+体检地图+大纲 → 《全书审读报告》落 .审稿留痕/。"""
    r, bad = _checked_root(b.root)
    if bad:
        return bad
    from .. import fullreview
    lock = usecases.try_lock(r)
    if lock is None:
        return JSONResponse({"error": "这本书正有任务在写,等它跑完再审。", "code": "busy"},
                            status_code=409)
    t0 = time.monotonic()
    try:
        from ..config import load_config
        from ..routes import get_backend_for
        backend = get_backend_for(load_config(r), "长上下文检索")
        out = fullreview.finish(r, backend)
        _log_op(r, 0, "全书审·收口", bool(out.get("ok")), t0,
                str(out.get("报告") or out.get("reason") or ""))
        return out
    except Exception as e:      # noqa: BLE001
        _log_op(r, 0, "全书审·收口", False, t0, f"{type(e).__name__}: {e}"[:200])
        return _fail(e, "全书审·收口")
    finally:
        lock.release()

