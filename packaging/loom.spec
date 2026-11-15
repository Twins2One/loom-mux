# -*- mode: python ; coding: utf-8 -*-
"""跨平台 PyInstaller 配置:Mac 出 Loom.app,Windows 出 dist/Loom/Loom.exe。

关键点(踩过的坑都在这):
- templates/ 和 webui/ 是【数据文件】,引擎用 Path(__file__).parent 找它们,
  必须按 `loom/...` 的包内路径打进去,否则冻结后找不到界面和模板。
- uvicorn / webview 有大量动态 import 和平台后端,用 collect_all 兜全。
"""

import importlib.util
import os
import re
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT_ROOT = os.path.dirname(SPECPATH)  # spec 在 packaging/ 下,上一级就是项目根


def _loom_version():
    """版本号单一真源 = loom/__init__.py 的 __version__;正则读取,不 import(避免冻结期副作用)。"""
    init = os.path.join(PROJECT_ROOT, "loom", "__init__.py")
    with open(init, encoding="utf-8") as f:
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', f.read())
    return m.group(1) if m else "0.0.0"


LOOM_VERSION = _loom_version()

# --- 数据文件:保持 loom/ 包内结构 ---
# 2026-10-20 需求确认删掉「看看样例」⇒ loom/sample 整目录已删除,这里同步去掉那一行
# (留着会让 PyInstaller 直接报源目录不存在,打包当场失败)。
datas = [
    (os.path.join(PROJECT_ROOT, "loom", "templates"), "loom/templates"),
    (os.path.join(PROJECT_ROOT, "loom", "webui"), "loom/webui"),
]
binaries = []
hiddenimports = collect_submodules("loom")

# --- 动态 import 大户:整包收 ---
for pkg in ("uvicorn", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# --- 书房取稿自带的「听写」组件(2026-10-28 作者裁定 A 案:包内自带,全离线) ---------
# 为什么必须自带:本机那套 faster-whisper 的解码走 PyAV,而 PyAV 19.x 与 faster-whisper
# 1.2.1 撞关键字(metadata_errors),任何没字幕的视频都转不出字。Loom 改成自己用 ffmpeg
# 解成 16k 裸流 + numpy 喂模型(见 loom/study/asr.py),所以 ffmpeg/ffprobe 与模型要进包。
VENDOR = os.path.join(os.path.dirname(PROJECT_ROOT), "vendor")
#2026-10-30（0.8.3）：把 embed 也带上——语义检索的 ONNX 向量模型（约266 MB）。
#不带它的话，安装版里语义检索会报“缺向量模型”然后静默退回纯字面。
#2026-10-31（作者裁定「B站下载层必须纳入包内」）：bv.py 也收进包。
#它只用标准库，进程内就能跑，所以不必为它打一整套 Python 环境。
for _sub in ("ffmpeg", "whisper", "embed", "bili-vision"):
    _p = os.path.join(VENDOR, _sub)
    if os.path.isdir(_p):
        datas.append((_p, "loom/vendor/" + _sub))
if not os.path.isdir(os.path.join(VENDOR, "whisper")):
    print("!! 警告:没有 vendor/whisper,打出来的包转写能力为空(只有字幕能取稿)")
if not os.path.isdir(os.path.join(VENDOR, "embed")):
    print("!! 警告:没有 vendor/embed,打出来的包没有语义检索(退回纯字面)")

for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "tokenizers",
            "numpy", "av", "huggingface_hub",
            # 2026-11-01：bv.py 进程内跑（视觉/OCR 链）需要的三件——bv.py 是
            # 数据文件，PyInstaller 不会扫它的 import，必须显式收。
            # 2026-11-03（批A 开机维护）：维护进程要降优先级，需要 psutil。
            "PIL", "rapidocr_onnxruntime", "psutil"):
    try:
        d, b, h = collect_all(pkg)
    except Exception as _e:  # 缺哪个就少哪块能力,别让打包当场失败
        print("!! 听写依赖缺失:%s(%s)" % (pkg, _e))
        continue
    datas += d
    binaries += b
    hiddenimports += h

# 2026-10-21 结论:**底座版本**决定原生窗口能不能开(见 docs/原生窗口-底座版本对比与三路线.md):
# - 打包用 Python 3.12 + pythonnet 3.1.0 + clr-loader 0.2.6(与原版同构),走 .NET Framework 4.8,
#   WinForms 完整 ⇒ 原生窗口可用。Python 3.14 + pythonnet 3.2.x 会被迫走 .NET 8,WinForms 缺
#   System.Windows.Forms.ContextMenu ⇒ 架构级死路。
# - **另一条硬约束:pythonnet / clr_loader 绝对不能被冻进 PYZ**。冻进去以后 `import clr` 会在
#   pyclr_get_function 上拿回 NULL(`Failed to resolve Python.Runtime.Loader.Initialize`);
#   同样的文件放在包外的普通目录、只用 sys.path 挂进来,同一个冻结进程就能起 .NET 并出窗口。
#   所以这两个包按「外置目录」打:_internal/_pylibs/{pythonnet,clr_loader,clr.py},
#   由 packaging/loom_app.py 启动时插到 sys.path(见那里的 _extend_path_with_extlib)。
hookspath_list = []

EXT_LIBS_PKGS = ("pythonnet", "clr_loader")  # 整棵树外置
EXT_LIBS_MODULES = ("clr",)                  # 单独的外置模块
EXT_LIBS_DEST = "_pylibs"


def _ext_libs(packages, modules):
    """把窗口依赖原样拷成数据文件(不进 PYZ)。找不到就当场失败,别静默出一个开不了窗的包。"""
    out = []
    for pkg in packages:
        found = importlib.util.find_spec(pkg)
        if found is None or not found.origin:
            raise SystemExit(
                "打包环境里找不到 %s —— 请用装了窗口依赖的解释器跑 PyInstaller"
                "(py312site: pythonnet==3.1.0, clr-loader==0.2.6, pywebview==6.2.1)" % pkg
            )
        pkg_dir = os.path.dirname(found.origin)      # .../site/pythonnet
        parent = os.path.dirname(pkg_dir)            # .../site
        for root, dirs, files in os.walk(pkg_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, parent)
                out.append((full, os.path.join(EXT_LIBS_DEST, os.path.dirname(rel))))
    for mod in modules:
        found = importlib.util.find_spec(mod)
        if found is None or not found.origin:
            raise SystemExit("打包环境里找不到外置模块 %s" % mod)
        out.append((found.origin, EXT_LIBS_DEST))
    return out


datas += _ext_libs(EXT_LIBS_PKGS, EXT_LIBS_MODULES)
hiddenimports += ["cffi"]  # clr_loader 靠它调 ClrLoader.dll;外置包要用到,必须进包

a = Analysis(
    [os.path.join(PROJECT_ROOT, "packaging", "loom_app.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=hookspath_list,
    runtime_hooks=[],
    excludes=["pythonnet", "clr_loader", "clr"],  # 外置,不许冻进 PYZ(见上面那段)
    noarchive=False,
)
pyz = PYZ(a.pure)


def _vtuple(ver):
    """把 "0.7.2" 变成 Windows 版本资源的四元组 (0, 7, 2, 0)。"""
    parts = []
    for chunk in str(ver).split(".")[:4]:
        digits = re.sub(r"\D", "", chunk)
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def _version_resource():
    """生成 Windows 版本资源文件:属性面板里"它是什么、谁的、什么版本"那几行。

    2026-10-25:一份陌生视角的审查指出 —— 这六个字段原来是**全空**的,
    陌生人拿到 exe 连"这是什么程序、哪个版本"都要靠猜。填这些不花钱,也不改行为。
    (代码签名是另一回事:那要买证书,这里只解决"看得出它是什么"。)
    """
    path = os.path.join(PROJECT_ROOT, "packaging", "_version_info.txt")
    v = _vtuple(LOOM_VERSION)
    body = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%s, prodvers=%s, mask=0x3f, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', 'Loom-mux'),
      StringStruct('FileDescription', 'Loom \\u00b7 \\u672c\\u5730\\u5c0f\\u8bf4\\u5199\\u4f5c\\u5de5\\u5177'),
      StringStruct('FileVersion', '%s'),
      StringStruct('InternalName', 'Loom'),
      StringStruct('LegalCopyright', '\\u00a9 2026 Loom-mux'),
      StringStruct('OriginalFilename', 'Loom.exe'),
      StringStruct('ProductName', 'Loom'),
      StringStruct('ProductVersion', '%s')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""" % (v, v, LOOM_VERSION, LOOM_VERSION)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return path


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Loom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 产品形态:无终端窗口。首次调试可临时改 True 看报错
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_resource(),   # 只在 Windows 生效;Mac 侧忽略这个参数
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Loom",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Loom.app",
        icon=os.path.join(PROJECT_ROOT, "packaging", "loom.icns"),  # 织标墨绿图标(make_icon.py 生成)
        bundle_identifier="com.two2one.loom",
        info_plist={
            "CFBundleName": "Loom",
            "CFBundleDisplayName": "Loom · 织布机",
            "CFBundleShortVersionString": LOOM_VERSION,  # 跟 loom/__init__.py 同步,别再写死
            "CFBundleVersion": LOOM_VERSION,
            "NSHighResolutionCapable": True,
        },
    )
