"""桌面入口:本地起 FastAPI(127.0.0.1),再用 PyWebView 套成原生窗口。

- loom-app   : 原生窗口(Mac WebKit / Windows WebView2)
- loom-serve : 不开窗口,只起服务并打开浏览器(pywebview 出问题时的兜底)
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

import uvicorn

from .server import app


# 固定默认端口:localStorage(记住上次的书 / 最近书 / 主题)按 origin=127.0.0.1:端口 存,
# 端口每次变 origin 就变、存的全丢——这正是「每次进来都要重开书」的真根因。固定端口=稳定 origin,
# 本地记忆才留得住。端口被别的程序占了才回退随机(此时那次记忆丢失可接受,极少发生)。
DEFAULT_PORT = 8473  # 冷门端口,撞车概率低;LOOM_PORT 可覆盖

# 启动落地页( §12:「先渲染书房壳,进入书后再加载原 Loom-mux」)。
# 单独提成一个常量,是因为有三处要开窗:原生窗口 / Edge·Chrome 应用模式兜底 / 纯浏览器兜底。
# 三个地方各写一个字面量,改的时候必漏一个——漏掉的那个就是"某台机器上进不去书房"。
# 进书房后点某本书才走 "/?root=<路径>" 进织布机(对应 app.js 的 autoReopen)。
LANDING = "/study.html"


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    import os
    want = int(os.environ.get("LOOM_PORT", DEFAULT_PORT))
    if _port_free(want):
        return want
    s = socket.socket()          # 固定端口被占 → 回退随机(记忆这次留不住,但至少能起来)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_up(port: int, timeout: float = 8.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)


def _set_macos_app_name(name: str) -> None:
    """dev 模式(loom-app,进程是 python)下,把菜单栏/应用名从 'Python' 改成 name。

    从源码跑没有 .app 身份,macOS 会拿进程名当应用名,顶部菜单栏首项显示 'Python'。
    这里在 NSApplication 菜单构建前,改写 mainBundle 的 CFBundleName 即可纠正。
    打包成 .app 后由 Info.plist 决定,这步是 no-op(也不会出错)。
    (注:Activity Monitor / ps 里进程名仍是 python,那只有真打包 .app 才变。)
    """
    import sys

    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
    except Exception:
        pass  # 拿不到就算了,不影响启动


def _set_dock_icon() -> None:
    """dev 模式(loom-app,进程是 python)下,把 Dock 图标换成 Loom 的。

    打包成 .app 后 Dock 图标走 Info.plist 的 .icns,不需要这步;但从源码跑没有
    app 身份,默认是 Python 火箭,这里运行时补上。
    """
    try:
        from pathlib import Path

        png = Path(__file__).resolve().parent / "webui" / "app-icon.png"
        if not png.exists():
            return
        from AppKit import NSApplication, NSImage

        img = NSImage.alloc().initByReferencingFile_(str(png))
        if img and img.isValid():
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:
        pass  # 锦上添花,失败不影响启动




def _window_log(msg: str) -> None:
    """把开窗过程/异常落到 `loom-window.log`(exe 同目录优先,退 ~/.loom)。

    2026-10-23:打包版是**无终端**构建,`traceback.print_exc()` 会被 devnull 吃掉 ——
    于是「原生窗口起不来、退回了浏览器」这件事在用户机器上**完全无从诊断**,
    只能看到"打开的是 Chrome 窗口"。这里给它一个落点,和兜底开窗共用同一个文件。
    """
    import os
    import sys

    for d in (os.path.dirname(os.path.abspath(sys.executable)),
              os.path.join(os.path.expanduser("~"), ".loom")):
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "loom-window.log"), "a", encoding="utf-8") as f:
                f.write(msg + "\n")
            return
        except Exception:      # noqa: BLE001 - 日志写不进去也不能影响启动
            continue


def run() -> None:
    """原生窗口;窗口起不来(pythonnet/.NET/WebView2 缺失)就自动退回浏览器,不再硬崩。"""
    import os
    import sys

    _set_macos_app_name("Loom")  # 菜单栏首项显示 Loom 而非 Python(须在建菜单前)
    port = _free_port()
    # log_config=None:别让 uvicorn 跑 dictConfig 去配彩色 formatter(内嵌服务器用不上,
    # 且无终端构建里 sys.stdout=None 会让它崩在 formatter 'default')。
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    )
    threading.Thread(target=server.run, daemon=True).start()
    _wait_up(port)

    try:
        import webview

        class _JsApi:
            """暴露给 webui 的原生能力桥(window.pywebview.api.*)。
            C端用户不该手输文件路径——导入书/选文件夹走系统对话框。"""

            def pick_folder(self):
                w = webview.windows[0] if webview.windows else None
                if w is None:
                    return None
                res = w.create_file_dialog(webview.FOLDER_DIALOG)
                return res[0] if res else None   # 取消选择 → None,前端保持原值

            def pick_files(self):
                """多选文件(文风档案的「浏览…」用)。取消 → []。"""
                w = webview.windows[0] if webview.windows else None
                if w is None:
                    return []
                res = w.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=True,
                    file_types=("文本 / Markdown (*.txt;*.md)", "所有文件 (*.*)"))
                if not res:
                    return []
                return list(res) if isinstance(res, (list, tuple)) else [res]

        webview.create_window(
            "Loom · 织布机",
            f"http://127.0.0.1:{port}{LANDING}",
            width=1180, height=780, min_size=(920, 600),
            js_api=_JsApi(),
        )
        _set_dock_icon()
        webview.start()
    except Exception:
        # pythonnet/.NET/WebView2 任何问题都不该让整个 App 崩(用户群里精简版 Windows 很多)。
        # 服务线程已经在跑,开一个窗口指向同一端口,并 hold 住进程别退出,等同 serve() 兜底。
        import traceback

        traceback.print_exc()  # 无终端构建里会被 devnull 吃掉,所以下面再落一份到文件
        _window_log("[Loom] 原生窗口起不来,改用兜底窗口。原因:\n" + traceback.format_exc())
        _open_fallback_window(port)
        while True:
            time.sleep(1)


def _open_fallback_window(port: int) -> None:
    """窗口起不来时的兜底:优先用 Edge/Chrome 的「应用模式」开一个**独立窗口**。

    2026-10-21 真机:打包版里 pythonnet 加载 .NET 失败(缺 System.Windows.Forms 解析),
    pywebview 的 winforms 后端起不来,于是退化成"在浏览器里开一个标签"——
    对作者来说那是"没有窗口"。而 Edge 的 `--app=URL` 本来就给一个无地址栏、无标签页的
    独立窗口,零额外依赖(WebView2 安装包自带 Edge),视觉上与原生窗口几乎一致。
    所以顺序:Edge 应用模式 → Chrome 应用模式 → 实在没有才退回默认浏览器。
    """
    import shutil
    import subprocess
    import tempfile
    import webbrowser

    # 兜底路径没人看得见(冻结后 stdout 指向 devnull),所以写日志留证据:
    # %TEMP%\loom-window.log 里能看到"试了哪些 exe / 最后走了哪条路"。
    def _log(msg: str) -> None:
        # 落点优先级:exe 同目录(打包版最好的位置,用户一定能找到) → 用户 ~/.loom → 系统 temp。
        # 为什么不用 tempfile.gettempdir():这台机器上它对某些解释器不可用(会抛
        # "No usable temporary directory"),日志会静默丢掉 —— 兜底路径就变得无从诊断了。
        for d in (os.path.dirname(os.path.abspath(sys.executable)),
                  os.path.join(os.path.expanduser("~"), ".loom")):
            try:
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "loom-window.log"), "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
                break
            except Exception:      # noqa: BLE001
                continue
        else:
            try:
                with open(os.path.join(tempfile.gettempdir(), "loom-window.log"), "a",
                          encoding="utf-8") as f:
                    f.write(msg + "\n")
            except Exception:      # noqa: BLE001
                pass
        print(msg)

    url = f"http://127.0.0.1:{port}{LANDING}"
    cands: list[str] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        cands += [
            os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
        ]
    for name in ("msedge", "chrome", "brave", "vivaldi"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    _log(f"[Loom] 兜底开窗 url={url} 候选={len(cands)}")
    for exe in cands:
        if not os.path.isfile(exe):
            _log(f"[Loom]   跳过(不存在) {exe}")
            continue
        # 2026-10-21 关键点:必须给一个**独立 user-data-dir**。
        # 否则 Chrome 会挂到已经在跑的那个实例上,把 --app 吞掉,表现就是"点了没反应"。
        profile = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), ".loom-window-profile")
        try:
            os.makedirs(profile, exist_ok=True)
        except Exception:      # noqa: BLE001 - 装到只读目录时退回用户目录
            profile = os.path.join(os.path.expanduser("~"), ".loom", "window-profile")
            try:
                os.makedirs(profile, exist_ok=True)
            except Exception:  # noqa: BLE001
                profile = ""
        args = [exe, f"--app={url}", "--window-size=1280,860", "--no-first-run",
                "--no-default-browser-check"]
        if profile:
            args.append(f"--user-data-dir={profile}")
        try:
            # 2026-10-21 真机:在**冻结进程**里直接 Popen(chrome.exe …) 时,Chrome 会立刻退出
            # 且不出窗(开发态同一条语句却正常)。走 ShellExecute(start)这条和手工双击等价的路径
            # 就正常,所以这里统一用 `cmd /c start` 起浏览器。
            if os.name == "nt":
                subprocess.Popen(  # noqa: S603, S607 - 只起本地浏览器,参数是我们自己拼的 URL
                    ["cmd", "/c", "start", "", *args],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x00000008,      # DETACHED_PROCESS:别挂在我们的控制台上
                )
            else:
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
            # 起完要**验一下**:本机真机出现过"调用返回成功、但 Chrome 静默没出窗"的情形。
            # 6 秒内看不到标题带「织布机」的窗口,就退回默认浏览器 —— 宁可多个标签页,
            # 也不能让作者双击后什么都看不见。
            import time as _t
            _t.sleep(6)
            if _find_loom_window():
                _log(f"[Loom]   已用独立窗口打开:{exe}  profile={profile}")
                if profile:
                    _watch_window_close()
                return
            _log(f"[Loom]   起过 {os.path.basename(exe)} 但 6 秒内没看到窗口,退回默认浏览器")
        except Exception as e:      # noqa: BLE001 - 换下一个浏览器
            _log(f"[Loom]   启动失败 {exe}: {e}")
            continue
    # 连 Edge/Chrome 都没有(极少):只能退回默认浏览器
    _log("[Loom]   没有可用的 Chromium 浏览器,退回默认浏览器")
    webbrowser.open(url)


def _find_loom_window() -> bool:
    """枚举顶层窗口,看有没有一个标题里带 "Loom" 的可见窗口。

    2026-10-21:原先用"进程命令行里有没有 --user-data-dir=..."来判断,不可靠 ——
    Chrome 会把实际承载窗口的进程 fork 出去(命令行里就不带这个参数了),
    于是"有窗口"会被判成"没窗口"。直接问系统要窗口列表才是准的。
    纯 ctypes 枚举,只读;任何异常都返回 False。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = {"hit": False}

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                # 只认**应用页标题里的那三个字**。
                # 为什么不用 "loom":开发者自己的编辑器/终端窗口标题里可能也带 Loom
                # (本机就有一个 "Loom — DeepSeek Harness"),那样会永远判成"窗口还在"。
                if "织布机" in (buf.value or ""):
                    found["hit"] = True
                    return False          # 找到就别再枚举了
            except Exception:      # noqa: BLE001
                pass
            return True

        user32.EnumWindows(_cb, 0)
        return found["hit"]
    except Exception:          # noqa: BLE001
        return False


def _watch_window_close() -> None:
    """用户关掉那个应用窗口后,把进程也收掉()。

    判据 = 标题带 "Loom" 的可见窗口还在不在。先等它出现(最多 40 秒),
    出现之后连续两次数不到(约 8 秒)就退出 —— 既不会因为浏览器慢启动而误杀,
    也不会在用户关窗后留下一个看不见的后台进程占着端口。
    """
    import threading
    import time

    def _log_exit(msg: str) -> None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                   "loom-window.log"), "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:      # noqa: BLE001
            pass

    def _loop() -> None:
        for _ in range(20):                 # 最多等 40 秒让它起来
            if _find_loom_window():
                break
            time.sleep(2)
        else:
            _log_exit("[Loom] 40 秒内没检测到窗口(可能浏览器没起来),不做自动退出")
            return
        miss = 0
        while True:
            time.sleep(4)
            if _find_loom_window():
                miss = 0
            else:
                miss += 1
                if miss >= 2:
                    _log_exit("[Loom] 窗口已关闭,服务进程随之退出")
                    os._exit(0)         # uvicorn 在别的线程,直接收进程最干净

    threading.Thread(target=_loop, daemon=True).start()


def serve_only() -> None:
    """只起服务不开窗口（部署更新用：杀服务→换文件→重启服务→窗口自动恢复）。

    端口固定用 DEFAULT_PORT（不用 _free_port 随机）——窗口进程要连**确定的端口**。
    端口被占 = 服务已在跑 = 直接返回（幂等）。
    """
    port = int(os.environ.get("LOOM_PORT", DEFAULT_PORT))
    if not _port_free(port):
        print(f"[Loom] 端口 {port} 已被占用（服务已在跑），不重复启动。")
        return
    import uvicorn as _uv
    server = _uv.Server(_uv.Config(app, host="127.0.0.1", port=port,
                                   log_level="warning", log_config=None))
    print(f"[Loom] 服务进程已启动（端口 {port}）。")
    server.run()


def window_only() -> None:
    """只开窗口不开服务（连已有服务的端口）——服务/窗口分离的窗口半。

    端口从 LOOM_PORT 读（默认 DEFAULT_PORT）。连不上 = 服务没起 = 提示后退出。
    """
    import sys
    port = int(os.environ.get("LOOM_PORT", DEFAULT_PORT))
    if _port_free(port):
        print(f"[Loom] 端口 {port} 没有服务在跑——先起服务（Loom.exe --serve-only）。")
        sys.exit(1)
    try:
        import webview

        class _JsApi:
            def pick_folder(self):
                w = webview.windows[0] if webview.windows else None
                if w is None:
                    return None
                res = w.create_file_dialog(webview.FOLDER_DIALOG)
                return res[0] if res else None

            def pick_files(self):
                w = webview.windows[0] if webview.windows else None
                if w is None:
                    return []
                res = w.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=True,
                    file_types=("文本 / Markdown (*.txt;*.md)", "所有文件 (*.*)"))
                if not res:
                    return []
                return list(res) if isinstance(res, (list, tuple)) else [res]

        webview.create_window(
            "Loom · 织布机",
            f"http://127.0.0.1:{port}{LANDING}",
            width=1180, height=780, min_size=(920, 600),
            js_api=_JsApi(),
        )
        _set_dock_icon()
        webview.start()
    except Exception:
        import traceback

        traceback.print_exc()
        _window_log("[Loom] 原生窗口起不来，改用兜底窗口。\n" + traceback.format_exc())
        _open_fallback_window(port)
        while True:
            time.sleep(1)

def serve() -> None:
    """无窗口兜底:起服务 + 自动开浏览器。"""
    import webbrowser

    port = _free_port()
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}{LANDING}")).start()
    print(f"Loom 跑在 http://127.0.0.1:{port}{LANDING}  (Ctrl-C 退出)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)


if __name__ == "__main__":
    run()
