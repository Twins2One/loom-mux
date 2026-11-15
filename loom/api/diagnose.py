"""loom.api.diagnose —— /api/diagnose/* /sensitive/scan /studio /mirror /repair/* /report /run/card /advise /guardrails(从 server.py 原样搬家)。"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import usecases
from ..backends import LoomBackendError, list_models
from ..config import load_config
from . import _common
from ._common import (_bad_chapter, _chapter_nums, _checked_root, _err_json, _fail,
                      _log_op, _project_root, _state)
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
class DiagnoseCommitBody(BaseModel):
    root: str
    picks: dict = {}

@router.post("/api/diagnose/scan")
def diagnose_scan_ep(b: RootBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        return {"ok": True, "candidates": usecases.diagnose_scan(Path(b.root))}
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)

@router.post("/api/diagnose/commit")
def diagnose_commit_ep(b: DiagnoseCommitBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        usecases.diagnose_commit(Path(b.root), b.picks)
        return usecases.project_state(Path(b.root))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)

class ScanBody(BaseModel):
    root: str
    text: str

@router.post("/api/sensitive/scan")
def sensitive_scan(b: ScanBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    from ..sensitive import scan
    return {"hits": scan(b.text, b.root)}

@router.get("/api/studio")
def studio_view(root: str):
    """书房三视图(时间轴/伏笔账本/专名册):纯只读投影,不调模型、不写盘。"""
    from ..studio import studio
    try:
        return studio(Path(root))
    except Exception as e:
        return JSONResponse({"error": f"书房读取失败:{e}"}, status_code=400)

@router.get("/api/mirror")
def mirror_view(root: str):
    """镜台(它有多懂你):手改量双曲线 + 写作指纹 + 人格增补。纯只读投影,不调模型、不写盘。"""
    from ..mirror import mirror
    try:
        return mirror(Path(root))
    except Exception as e:
        return JSONResponse({"error": f"镜台读取失败:{e}"}, status_code=400)

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
@router.get("/api/run/card")
def api_run_card(root: str, chapter: int = 0):
    """本章运行卡(R12):逐棒耗时、失败原因、重试次数。**只读**。

    tokens 一栏是 None 并写明原因——Loom 的后端不回报 usage,宁可不给,也不按字数估一个假数。
    """
    from .. import chapterstate as cs
    from .. import runlog

    p, bad = _checked_root(root)
    if bad:
        return bad
    if not chapter:
        nums = _chapter_nums(p)
        chapter = max(nums) if nums else 1
    return {"ok": True, **runlog.card(p, chapter),
            "状态": (cs.load(p).get(chapter).status if cs.load(p).get(chapter) else "未写")}

class RepairApplyBody(BaseModel):
    root: str
    chapter: int
    picks: list = []      # 序号列表,或 [{序号, 新句}]

@router.get("/api/report")
def api_report(root: str, chapter: int = 0, note: int = 1):
    """一份报告,两个维度:严重度 × 能不能自动修。**不调模型、不改正文**。

    `note=0` 可以不落留痕;默认落(R16:报告不阻断,但不等于不留痕)。
    """
    from .. import repair

    p, bad = _checked_root(root)
    if bad:
        return bad
    if not chapter:
        nums = _chapter_nums(p)
        chapter = max(nums) if nums else 1
    return repair.report(p, chapter, note=bool(note))

@router.get("/api/repair/plan")
def api_repair_plan(root: str, chapter: int = 0):
    """AI 修的方案:每条给「旧句 → 新句」。**还没改一个字。**"""
    from .. import repair

    if not chapter:
        nums = _chapter_nums(Path(root))
        chapter = max(nums) if nums else 1
    return repair.plan(root, chapter)

@router.post("/api/repair/apply")
def api_repair_apply(b: RepairApplyBody):
    """落地 AI 修:一次写正文 + 一次外科式同步快照(守 learn 不被 AI 重写污染)。

    只改「正文里能唯一定位的那一句」;同一句上两条 → 整批拒绝;旧句已被改 → 拒绝并提示 rebase。
    """
    from .. import repair

    return usecases.repair_apply(_project_root(b.root), b.chapter, b.picks)

class AdviseBody(BaseModel):
    root: str
    chapter: int = 0

@router.post("/api/advise")
def api_advise(b: AdviseBody):
    """建议(R14):调一次复审员,**只诊断、绝不落盘**。每条带 (章, 版本, 选区hash)。

    这里是 POST 不是需求文档写的 GET:**它会花一次模型调用**,而本站所有 GET 都是纯只读的
    (这条纪律在 R7 就立下了)。要钱的动作不挂 GET,见待裁定清单 Q8。
    """
    from .. import advise as advise_mod

    try:
        root = Path(b.root)
        nums = _chapter_nums(root)
        chapter = b.chapter or (max(nums) if nums else 1)
        out = usecases.advise_chapter(_project_root(b.root), chapter)
    except (LoomBackendError, ValueError, FileNotFoundError, OSError) as e:
        # 缺 key 时后端写好的中文指引原来被裸 500 吃掉(本轮审计按调用图扫出来的最后一处)
        return _err_json(e)
    if out.get("ok"):
        out["条目"] = advise_mod.annotate(out["条目"], advise_mod.version_of(root, chapter))
    return out

            # 并发上限(2–8,全局闸):
            # 不再只管"议事时几个角色在跑",而是所有会走模型的调用共用一个水龙头
            # (见 loom/concurrency.py,闸装在 backends 层)。
@router.get("/api/guardrails")
def api_guardrails():
    """三级护栏清单(R19):哪个动作可撤销、哪个要打字、哪个根本不给入口。**纯只读。**"""
    from .. import guardrail

    return {"ok": True, "级别": list(guardrail.LEVELS), "动作": guardrail.ACTIONS, "落盘": False}

