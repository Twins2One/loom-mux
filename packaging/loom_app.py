"""PyInstaller 的入口脚本。

打包出来的就是这个:起本地 FastAPI + PyWebView 原生窗口。
设 LOOM_NO_WINDOW=1 时退回浏览器兜底(调试用)。
pyproject 里的 `loom-app` 仍是开发期 `pip install -e .` 的入口,二者并存。
"""

from __future__ import annotations

import os
import sys


_EXT_LIBS_TAG = "py312-pn310-cl026"  # pythonnet 3.1.0 + clr-loader 0.2.6,换版本一定要改这个

# 外置依赖"完整"的判据:这几项缺一个,`import clr` 就会 ImportError
# (clr.py 是 pythonnet 的入口壳;pythonnet/ 里是 Python.Runtime.dll + runtime/;
#  clr_loader/ 里是 ClrLoader.dll)。**只判 pythonnet/ 在不在是不够的** —— 见过缓存里
# 只剩 pythonnet/ 而 clr.py 丢了的情形,那会让 App 静默退回浏览器。
_EXT_LIBS_REQUIRED = (
    "clr.py",
    os.path.join("pythonnet", "runtime", "Python.Runtime.dll"),
    os.path.join("clr_loader", "ffi", "dlls"),
)


def _extlib_ok(d: str) -> bool:
    return all(os.path.exists(os.path.join(d, p)) for p in _EXT_LIBS_REQUIRED)


def _extlib_missing(d: str) -> str:
    return "、".join(p for p in _EXT_LIBS_REQUIRED if not os.path.exists(os.path.join(d, p)))


def _extlib_note(msg: str) -> None:
    """把外置依赖的处理过程写进 %TEMP%\\loom-extlib.log(无终端构建里唯一的排查线索)。"""
    try:
        import time

        root = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
        path = os.path.join(root, "loom-extlib.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def _extend_path_with_extlib() -> None:
    """把窗口依赖(pythonnet / clr_loader / clr.py)挂到 sys.path 最前面。

    2026-10-21 实测两条硬结论(都在 docs/原生窗口-底座版本对比与三路线.md 里有现场记录):

    1. 这两个包 **不能**被 PyInstaller 冻进 PYZ。冻进去以后 `import clr` 会在
       `pyclr_get_function` 上拿回 NULL(`Failed to resolve Python.Runtime.Loader.Initialize`);
       改成普通目录 + sys.path 就通了。
    2. 光「不冻进 PYZ、按数据文件塞进包目录」**仍然不够** —— 放在安装目录里(哪怕 `_internal/_pylibs`)
       依旧失败,而把同一份文件挪到用户目录/临时目录就成功。机制没完全查清,
       但现象在同机反复复现(安装目录内必失败、包外必成功),所以这里不赌路径:
       **启动时把 `_pylibs` 复制到用户本地目录,再把那份挂到 sys.path**。
       代价是首次启动多一次约 10MB 的复制,换来「装在哪个目录都能开窗」。

    复制失败就退回包内目录,至少不会比原来更差。
    """
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    src = os.path.join(base, "_pylibs")
    _extlib_note("enter base=%s src_exists=%r frozen=%r" % (base, os.path.isdir(src), True))
    if not os.path.isdir(src):
        return

    ext = src
    try:
        import shutil

        # 候选落点逐个试:本机实测 LOCALAPPDATA 下建目录会被拒(权限错误),
        # TEMP 下正常;所以不赌单一位置,谁能写就用谁。都写不了才退回包内那份。
        _tag = _EXT_LIBS_TAG
        candidates = []
        for root in (
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("TEMP"),
            os.environ.get("TMP"),
            os.environ.get("APPDATA"),
        ):
            if root:
                candidates.append(os.path.join(root, "loom-runtime-deps", _tag))

        for dest in candidates:
            try:
                # 2026-10-23 真机事故:这里原来只判 `dest/pythonnet/` 在不在就当成"已经拷好了"。
                # 结果缓存里 `clr.py` 丢了(半删/半拷都能造成),判据照样说"OK",
                # 于是 `import clr` 抛 ImportError → pywebview 报
                # 「You must have pythonnet installed」→ 原生窗口起不来、静默退回 Chrome。
                # 现在按**完整性**判:缺任何一个必需项就整份重建,重建完还不够就退回包内那份。
                if not _extlib_ok(dest):
                    if os.path.isdir(dest):
                        shutil.rmtree(dest, ignore_errors=True)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    tmp = dest + ".tmp"
                    if os.path.isdir(tmp):
                        shutil.rmtree(tmp, ignore_errors=True)
                    shutil.copytree(src, tmp)
                    try:
                        os.replace(tmp, dest)
                    except OSError:
                        # dest 被**另一个 Loom 实例**占着(它的 CLR 已经把 DLL 映射进去了,
                        # Windows 不允许替换目录)→ 改成原地合并覆盖,再把临时目录收掉。
                        shutil.copytree(src, dest, dirs_exist_ok=True)
                        shutil.rmtree(tmp, ignore_errors=True)
                if not _extlib_ok(dest):
                    raise RuntimeError("复制后仍不完整(缺 %s)" % _extlib_missing(dest))
                ext = dest
                _extlib_note("OK %s" % dest)
                break
            except Exception as exc:
                _extlib_note("跳过 %s: %s: %s" % (dest, type(exc).__name__, exc))
                ext = src
                continue
    except Exception:
        import traceback

        traceback.print_exc()  # 控制台构建里能看见;无终端构建里会被 devnull 吃掉
        ext = src  # 复制不了就用包内那份,行为退回「可能开不了窗」而不是直接崩

    if not _extlib_ok(ext):
        # 连包内那份都不完整时,别再假装能开窗 —— 让 pywebview 自己报错,别静默退浏览器。
        _extlib_note("!! 外置依赖不完整:%s(缺 %s)" % (ext, _extlib_missing(ext)))
    if os.path.isdir(ext) and ext not in sys.path:
        sys.path.insert(0, ext)


def main() -> None:
    # 2026-11-03（随开机自启的后台维护）——
    # `Loom.exe --maintain-only [--full]` 只跑记忆库维护，**不进窗口、不起 HTTP 服务**。
    # 这一步刻意放在所有重依赖 import 之前：maintenance 属于 core 层，不碰 pywebview /
    # uvicorn / fastapi，所以 WebView2 缺失或损坏都不会让维护跑不起来（外部 AI 审阅 Q3）。
    if any(a == "--maintain-only" for a in sys.argv[1:]):
        try:
            from loom.maintenance import _log as _mlog, main as _maintain

            # 入口自证：打包版是无终端构建，出问题时这行是唯一能证明"参数确实传进来了"的线索
            _mlog("入口：--maintain-only，argv=%s" % (list(sys.argv[1:]),))
            raise SystemExit(_maintain())
        except SystemExit:
            raise
        except Exception:
            # 无终端构建里没有 stderr 可看；写日志后静默退出（退出码 1 让任务计划能看见）
            try:
                import traceback

                root = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
                with open(os.path.join(root, "loom-maintain.log"), "a", encoding="utf-8") as f:
                    f.write("维护入口异常:\n" + traceback.format_exc())
            except Exception:
                pass
            raise SystemExit(1)

    # 2026-11-03（服务/窗口分离）：部署更新时只重启服务进程，窗口不受影响。
    # Loom.exe --serve-only  = 只起服务（部署后重启用）
    # Loom.exe --window-only = 只开窗口（连已有服务端口）
    if "--serve-only" in sys.argv[1:]:
        from loom.desktop import serve_only

        serve_only()
        return
    if "--window-only" in sys.argv[1:]:
        from loom.desktop import window_only

        window_only()
        return

    _extend_path_with_extlib()

    # 冻结后没有终端;把未捕获异常写到用户能找到的日志里,别静默退出
    if getattr(sys, "frozen", False):
        import tempfile
        from pathlib import Path

        # Windows 无终端(--noconsole)构建里 sys.stdout/stderr 是 None;
        # 任何 .isatty()/.write() 都会炸 —— uvicorn 配置日志的 formatter 'default'
        # 首当其冲(Windows 打不开就是这个)。指到 devnull,给所有库一个能安全写入的空流。
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")

        # tempfile 在某些受限环境里会直接抛 FileNotFoundError(本机实测过:
        # "No usable temporary directory found")—— 那会让 App 起都起不来。
        # 崩日志是最后一道诊断线索,不能因为它自己挂了就没了。
        try:
            _log_dir = tempfile.gettempdir()
        except Exception:
            _log_dir = os.path.dirname(os.path.abspath(sys.executable))
        log = Path(_log_dir) / "loom-crash.log"

        def _hook(exc_type, exc, tb):
            import traceback

            try:
                with log.open("a", encoding="utf-8") as f:
                    traceback.print_exception(exc_type, exc, tb, file=f)
            except Exception:
                pass
            sys.__excepthook__(exc_type, exc, tb)

        sys.excepthook = _hook

    if os.environ.get("LOOM_NO_WINDOW") == "1":
        from loom.desktop import serve

        serve()
    else:
        from loom.desktop import run

        run()


if __name__ == "__main__":
    main()
