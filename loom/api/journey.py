"""loom.api.journey —— /api/journey/*(从 server.py 原样搬家)。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import usecases
from ..backends import LoomBackendError
from . import _common
from ._common import _err_json, _project_root
from .projects import RootBody

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
class JourneyAnswerBody(BaseModel):
    root: str
    answer: str

class JourneyGotoBody(BaseModel):
    root: str
    stage: str
    skip: bool = False

    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
@router.get("/api/journey/state")
def journey_state_ep(root: str):
    try:
        return usecases.journey_state(Path(root))
    except (ValueError, FileNotFoundError) as e:
        return _err_json(e)

@router.post("/api/journey/card")
def journey_card_ep(b: RootBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        return usecases.journey_card(Path(b.root))
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)

@router.post("/api/journey/answer")
def journey_answer_ep(b: JourneyAnswerBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        return usecases.journey_answer(Path(b.root), b.answer)
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)

@router.post("/api/journey/goto")
def journey_goto_ep(b: JourneyGotoBody):
    # 越界拦截:root 必须是真书(有 loom.toml),否则 400
    b.root = str(_project_root(b.root))
    try:
        return usecases.journey_goto(Path(b.root), b.stage, b.skip)
    except (LoomBackendError, ValueError, FileNotFoundError) as e:
        return _err_json(e)

