"""本地 FastAPI 服务:把 loom 引擎暴露成 JSON / 流式接口给 Web 界面。

只监听 127.0.0.1,单用户本地桌面应用,不做鉴权。
(2026-11-07 拆分:端点按域搬进 loom/api/ 各模块,本文件收缩为组装层——
app 创建、启动钩子、中间件、静态挂载与各域 router 的 include,行为零变化。)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .backends import get_backend, cheap_backend  # noqa: F401 - 各域端点经 loom.server 动态取这两个名字(测试 monkeypatch 兼容)
from .usecases import ProjectBusyError

WEBUI_DIR = Path(__file__).parent / "webui"

app = FastAPI(title="Loom")


# 启动时在后台预热语义索引。
#为什么：全库编码一遍要 250 秒（实测）。放在用户第一次搜索时就是当场卡住；
#放在启动时，用户还在加载页/登录页，时间就被吃掉了。
#已建好时这个动作只是读一个文件，几毫秒，不占什么。
@app.on_event("startup")
def _warm_embed_index() -> None:
    import threading

    def _run() -> None:
        try:
            from .study import ask as _ask
            _ask.prewarm_index()
        except Exception:
            pass  # 预热失败不能影响启动，搜索侧会自己降级

    threading.Thread(target=_run, name="loom-warm-embed", daemon=True).start()

# 启动时装载行动模式（限流热生效）；
#长程维护**只在作者同意后**（prefs.memory_startup，默认关）后台安静做——
#重建索引、检测速记本漂移。逐条事务、库不脏，中断最多停在中途。
@app.on_event("startup")
def _load_prefs_and_maintain() -> None:
    import os
    import threading

    def _run() -> None:
        try:
            from . import userconf
            prefs = userconf.load_prefs()
            os.environ["LOOM_MEMORY_MODE"] = prefs.get("mode", "默认")
            if not prefs.get("memory_startup"):
                return   # 作者没同意就不做任何后台维护
            from . import memory as _m
            for p in userconf.list_projects():
                root = p.get("root", "")
                if not root or not p.get("exists"):
                    continue
                try:
                    if _m.stale(root):
                        _m.rebuild(root)   # 内部逐条事务，中断安全
                except Exception:
                    continue
        except Exception:
            pass   # 维护失败不能影响启动

    threading.Thread(target=_run, name="loom-memory-maintain", daemon=True).start()


# 本地服务的两道安全闸(本地无鉴权模型成立的前提):
# 1) 只认 Host=127.0.0.1/localhost → 挡 DNS rebinding(恶意网站把域名重绑到本机再调本地端点)
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


# 2) 跨站写请求一律拒(挡 CSRF):非 GET 且 Origin/Referer 非本机 → 403。
#    本机 webui(pywebview/浏览器从 127.0.0.1 发)Origin 即 127.0.0.1,放行;无 Origin 的本地工具(curl)放行。
@app.middleware("http")
async def _block_cross_site_writes(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        from urllib.parse import urlparse
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and (urlparse(origin).hostname or "") not in ("127.0.0.1", "localhost"):
            return JSONResponse({"error": "跨站写请求被拒"}, status_code=403)
    return await call_next(request)

# ---- 写锁:下沉到 usecases(单一宿主),这里只把「锁被占」统一映射成 409 project_busy ----
# 【红线】PUT /api/file(外置大脑保存)豁免不锁:整章生成几分钟,期间必须能存世界观。
@app.exception_handler(ProjectBusyError)
async def _project_busy(request, exc: ProjectBusyError):
    return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409)


# ---- 各域路由注册(保持 server.py 里端点原出现的先后顺序)----
from .api import projects as projects_router, chapters as chapters_router, journey as journey_router
from .api import diagnose as diagnose_router, writing as writing_router, routes_cfg as routes_cfg_router
from .api import importing as importing_router, style as style_router, partner as partner_router
from .api import study as study_router, trash as trash_router, export as export_router
from .api import agents as agents_router
app.include_router(projects_router.router)
app.include_router(chapters_router.router)
app.include_router(journey_router.router)
app.include_router(diagnose_router.router)
app.include_router(writing_router.router)
app.include_router(routes_cfg_router.router)
app.include_router(importing_router.router)
app.include_router(style_router.router)
app.include_router(partner_router.router)
app.include_router(study_router.router)
app.include_router(trash_router.router)
app.include_router(export_router.router)
app.include_router(agents_router.router)

# ── 书房（0.8.0）───────────────────────────────────────────────────────────
# `loom/study/` 是 0.8.0 新增的只读模块（知识库扫描 + 按册读取）。**必须挂在下面 `app.mount("/")` 之前**——
# StaticFiles 挂在 "/" 会把之后注册的 `/api/study/*` 全吃掉（FastAPI 按注册顺序匹配）。
from .study import router as study_router, account as study_account  # noqa: E402 - 刻意放在文件末尾、静态挂载之前

app.include_router(study_router)
app.include_router(study_account.router)   # 账户壳：/api/account/*（平级挂，不嵌 study 前缀）



# 静态界面挂在最后(/api/* 已先匹配)
# ──书房角色头像 ──────────────────
# 书房侧栏要显示与书内「作者智库」同风格的角色头像。图放 `loom/webui/study-avatars/<role>.png`，
# 由下面这条**在 StaticFiles("/") 之前**注册的路由供出去；没生成图就404，前端退回首字圆块。
# 为什么单开一条而不塞进 study 路由：它是静态图，跟知识库/取稿那些接口不是一类东西。
STUDY_AVATARS = WEBUI_DIR / "study-avatars"


#静态挂载（不是路由）：StaticFiles自带目录包含保护，`..`之类规范化后若越界会被它挡住。
#实测教训：写成 @app.get的话，`/study-avatars/..%2Findex.html`会被框架
#在**进入函数之前**规范化成 `/index.html`、交给下面的根 StaticFiles ——判断代码根本没被调用。
STUDY_AVATARS.mkdir(parents=True, exist_ok=True)
app.mount("/study-avatars", StaticFiles(directory=str(STUDY_AVATARS)), name="study-avatars")



app.mount("/", StaticFiles(directory=str(WEBUI_DIR), html=True), name="ui")
