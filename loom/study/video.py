"""书房 · 知识顾问的「取稿」：本机 `bv.py` 的薄封装要视觉理解）。

三条设计：

1. **只在作者点「取稿」时才跑**：`import` 不发请求、不探测网络、不起子进程。
2. **缺什么说什么**：bv.py / venv python / ffmpeg / ffprobe 各有明确缺失码与人话，绝不静默失败。
3. **能退化**：纯文字路径（`--subtitle-only --no-video`）**不需要 ffmpeg**。
   先把这条打通，抽帧那套装不上也不至于全废——能退化的功能才敢当主路径。

为什么走子进程而不是 import：`bv.py` 是 1082 行的独立 CLI，依赖（faster-whisper / onnxruntime /
PyAV）与 Loom 自己的运行时是两套；子进程是唯一不把两边依赖搅在一起的接法，也天然做到了
"它能跑就说明它自己那套是完整的"。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import sys
from pathlib import Path

# ===== 深度用户：想换 B 站取稿链路上的工具/模型，看这里 =====
#
# 这个文件不自己跑模型，它**调本机现成的 bili-vision**（一个 Python CLI）。
# 所以“换模型”在这里分两层：
#
# ① **换路径**（CLI / 解释器 / ffmpeg）——用环境变量，不用改代码：
# `BILI_VISION_BVPY` bv.py 的位置
# `BILI_VISION_PYTHON` 跑它的解释器
# `BILI_VISION_FFMPEG` / `BILI_VISION_FFPROBE`
# `BILI_VISION_CACHE` 取稿缓存目录
# `BILI_VISION_ASR_MODEL` 转写模型目录（传给下层，与 asr.py 的 `LOOM_ASR_MODEL` 同样格式）
#
# ② **换下层的视觉/语音模型**——那不在本仓库里，在 bili-vision 自己的配置里
# （通常是 bili-vision 工具包自身目录下的配置文件）。
# 本文件只负责“找到它、把参数传过去、把结果读回来”，不插手它内部。
#
# 换错了会怎样：`ready()` 把缺的那项报出来，界面显示“这一块不可用”，
# **不会崩也不会影响其他功能**。注意 `_pick()` 只认真实存在的文件——
# 路径打错一个字不会被当成“配好了”，而是当成没找到。

# ── 默认落点（都能被环境变量覆盖）────────────────────────────────────────────
# 默认落点为 Loom 自己的用户级 vendor 目录；可用 LOOM_VENDOR 覆盖。
# bv.py 已随包提供，并允许用户在 Loom vendor 中放置覆盖版本。
from ..config import vendor_dir as _vendor_dir

LOOM_VENDOR = _vendor_dir()
# bv.py 随包提供并优先使用。
PKG_VENDOR = Path(__file__).resolve().parent.parent / "vendor"   # loom/vendor（打进包的那份）
DEFAULT_BVPY = PKG_VENDOR / "bili-vision" / "bv.py"
USER_BVPY = LOOM_VENDOR / "bili-vision" / "bv.py"                # 作者自己放的（可覆盖）
DSH_BVPY = USER_BVPY
DEFAULT_PYTHON = LOOM_VENDOR / "bili-vision-venv" / "Scripts" / "python.exe"
DEFAULT_FFMPEG_DIR = LOOM_VENDOR / "bili-vision-tools" / "bin"


def _pick(env_key, *candidates):
    """先看环境变量，再按候选顺序找，**只认真实存在的文件**。

    2026-10-30修一处真实碰到的坑：环境变量指向**一个已被删掉的路径**时，
    原来会直接返回空串、把后面正确的候选全都遮住了。
    现在改成：环境变量那个不存在就**继续往下找**，都没有才返回空串。
    """
    if env_key:
        from_env = os.environ.get(env_key, "").strip()
        if from_env and Path(from_env).is_file():
            return from_env
    for c in candidates:
        if c.is_file():
            return str(c)
    return ""


def cache_dir() -> Path:
    """证据/视频缓存，放在 Loom 自己的用户级 vendor 目录，可通过环境变量改位置。"""
    raw = os.environ.get("BILI_VISION_CACHE", "").strip()
    return Path(raw) if raw else LOOM_VENDOR / "bili-vision-cache"


def tools() -> dict:
    """定位四件套（只读文件系统，不联网、不起进程）。"""
    # 候选顺序：包内随附版本优先，其次是用户 vendor 目录中的覆盖版本。
    bvpy = _pick("LOOM_BVPY", DEFAULT_BVPY, USER_BVPY)
    python = _pick("LOOM_BV_PYTHON", DEFAULT_PYTHON, Path(sys.executable))
    # ffmpeg/ffprobe：先认**打进包里的那份**（装好的 Loom 不用作者再配环境）
    from .asr import VENDOR
    # ffmpeg/ffprobe：环境变量明确指定时优先，否则依次查包内与 Loom vendor。
    ffmpeg = _pick("BILI_VISION_FFMPEG", VENDOR / "ffmpeg" / "ffmpeg.exe", LOOM_VENDOR / "ffmpeg" / "ffmpeg.exe", DEFAULT_FFMPEG_DIR / "ffmpeg.exe")
    ffprobe = _pick("BILI_VISION_FFPROBE", VENDOR / "ffmpeg" / "ffprobe.exe", LOOM_VENDOR / "ffmpeg" / "ffprobe.exe", DEFAULT_FFMPEG_DIR / "ffprobe.exe")
    missing = []
    if not bvpy:
        missing.append("bv.py")
    if not python:
        missing.append("python")
    if not ffmpeg:
        missing.append("ffmpeg")
    if not ffprobe:
        missing.append("ffprobe")
    return {"bvpy": bvpy, "python": python, "ffmpeg": ffmpeg, "ffprobe": ffprobe,
            "cache": str(cache_dir()), "缺失": missing}


def text_ready() -> bool:
    """纯文字取稿够不够。

    2026-10-31：**不再要求 python** —— bv.py 只用标准库，改成进程内跑（见 _bv_inproc）。
    以前必须带一个能跑的 python.exe，换台没装过环境的机器就取不了稿。
    """
    t = tools()
    return bool(t["bvpy"])


def frames_ready() -> bool:
    """要画面还得多两件：ffmpeg + ffprobe（不再要 python）。"""
    t = tools()
    return bool(t["bvpy"] and t["ffmpeg"] and t["ffprobe"])


def probe() -> dict:
    """给界面用的就绪信息（**零花费、零网络**）。"""
    from . import asr

    t = tools()
    text_ok, frames_ok = text_ready(), frames_ready()
    asr_state = asr.ready()
    if text_ok and frames_ok:
        hint = ""
    elif text_ok:
        hint = "能取文字（字幕/转写），但缺 ffmpeg，暂时看不了画面。"
    else:
        hint = ("这台机器还没配取稿能力（缺 %s）。请把字幕/转写文件直接拖进来，"
                "或者在设置里指定 bv.py 的位置。" % "、".join(x for x in t["缺失"] if x in ("bv.py", "python")))
    if hint and asr_state["转写"]:
        hint += "（没字幕的视频可以用自带转写救回来。）"
    return {"ok": True, "取稿": text_ok, "画面": frames_ok,
            "转写": asr_state["转写"], "转写缺": asr_state["缺"], "转写hint": asr_state["hint"],
            "缺失": t["缺失"], "缓存目录": t["cache"], "hint": hint}


def _env() -> dict:
    """给子进程的环境：把我们找到的路径与缓存目录显式塞进去，不靠它自己 `which`。"""
    env = dict(os.environ)
    t = tools()
    env["BILI_VISION_CACHE"] = t["cache"]
    if t["ffmpeg"]:
        env["BILI_VISION_FFMPEG"] = t["ffmpeg"]
    if t["ffprobe"]:
        env["BILI_VISION_FFPROBE"] = t["ffprobe"]
    if os.environ.get("BILI_VISION_ASR_MODEL"):
        env["BILI_VISION_ASR_MODEL"] = os.environ["BILI_VISION_ASR_MODEL"]
    #2026-10-31：作者的 B站凭据跟着传下去（每人在设置里自己初始化）。
    if os.environ.get("BILI_SESSDATA"):
        env["BILI_SESSDATA"] = os.environ["BILI_SESSDATA"]
    return env


def _last_json(text: str) -> dict | None:
    """bv.py 会往 stdout 打日志行 + 最后一行是证据 JSON。从后往前找第一个能解析的。"""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


BV_RE = r"(BV[0-9A-Za-z]{10})"
_AUDIO_EXT = (".m4a", ".mp3", ".aac", ".wav", ".opus", ".flac")
_VIDEO_EXT = (".mp4", ".flv", ".mkv", ".webm")


def _cached_media(target: str) -> str:
    """在取稿缓存里找这个视频已经下下来的音轨（没有音轨就找视频文件）。

    为什么要找缓存而不是重新下载：没字幕的视频，`bv.py` 已经把它下到缓存里了，
    只是它自己解不开（PyAV 那个 bug）。直接拿现成的音轨转写，既省一次外发，
    也不要求作者再点一次。**只读缓存目录，不联网。**
    """
    import re

    m = re.search(BV_RE, str(target or ""))
    if not m:
        return ""
    bvid = m.group(1)
    root = cache_dir()
    if not root.is_dir():
        return ""
    files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if bvid.lower() not in str(dirpath).lower():
            continue
        for name in filenames:
            low = name.lower()
            if low.endswith(_AUDIO_EXT) or low.endswith(_VIDEO_EXT):
                files.append(Path(dirpath) / name)
    if not files:
        return ""
    # 音轨优先，其次才是视频；同类取最新的
    files.sort(key=lambda p: (0 if p.suffix.lower() in _AUDIO_EXT else 1, -p.stat().st_mtime))
    for p in files:
        if p.is_file() and p.stat().st_size > 1024:
            return str(p)
    return ""


def _rescue_via_text(bvpy: str, python: str, target: str, *, start, end, runner, timeout: int) -> dict | None:
    """字幕取不到时，补一次 bv.py text（子进程）——它会下载音轨并转写，写进缓存。

    2026-11-01 作者的真实视频踩到的：无字幕视频在 subtitle-only 模式下不下载音轨，
    _with_asr 找不到缓存媒体 → 证据永远为空。这里用 bv.py 的完整 text 模式救一次
    （它的转写跑在自己的 venv python 里，依赖齐全）。救不成返回 None（原错误照返）。
    """
    if not python:
        return None
    argv = ["text", target]
    if start is not None or end is not None:
        if start is not None:
            argv += ["--start", str(start)]
        if end is not None:
            argv += ["--end", str(end)]
    cmd = [python, bvpy] + argv
    try:
        r = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=timeout, env=_env())
    except Exception:
        # 2026-11-11：原先这里（以及下面）有一段「夜间调试」把异常与退出码写到
        # 开发机上某个**写死的绝对路径**（含用户名）——属隐私暴露，已整体移除。
        # 诊断信息改用调用方的返回值/日志（本函数不承担写调试文件的职责）。
        return None
    out = r.stdout or ""
    # bv.py text 的 stdout 就是「日志行 + 带 [起-止 s asr] 前缀的转写正文」。
    # 提取 asr 正文段（[数字-数字s asr] 开头的行，拼接到下一个非 asr 段）。
    lines = out.split("\n")
    body, cur = [], None
    for l in lines:
        if l.startswith("[") and "s asr]" in l:
            cur = l
            body.append(l)
        elif cur is not None and l.strip() and not l.startswith(("语义单元", "关键帧", "《", "转写", "音轨", "本地")):
            body.append(l)
        elif l.strip() and cur is None:
            continue
    text = "\n".join(body).strip()
    if not text:
        return None
    import re as _re
    m = _re.search(BV_RE, target)
    bvid = m.group(1) if m else target
    evidence = {"text": text, "images": [],
                "coverage": {"range": [0.0, 0.0], "note": "bv.py text 模式，转写来源"},
                "asr": {"来源": "bv.py text 兜底", "字数": len(text.replace("\n", ""))}}
    return {"ok": True, "证据": evidence,
            "原文": json.dumps(evidence, ensure_ascii=False)}


def _with_asr(result: dict, target: str, *, start, end, runner) -> dict:
    """取稿失败/没字时，用**自带转写**救一次。救不成把原错误原样返回（不掩盖）。"""
    from . import asr

    if result.get("ok") and (result.get("证据") or {}).get("text"):
        return result
    if not asr.ready()["转写"]:
        return result
    media = _cached_media(target)
    if not media:
        return result
    got = asr.transcribe(media, runner=runner)
    if not got.get("ok"):
        return result
    segs = got["段"]
    if start is not None or end is not None:
        lo = float(start or 0.0)
        hi = float(end) if end is not None else 10 ** 9
        segs = [s for s in segs if s["end"] >= lo and s["start"] <= hi]
    text = "\n".join(s["text"] for s in segs)
    if not text.strip():
        return result
    evidence = dict(result.get("证据") or {})
    evidence["text"] = text
    evidence["asr"] = {"来源": "loom 自带转写", "媒体": Path(media).name,
                       "字数": len(text.replace("\n", "")), "段数": len(segs)}
    return {"ok": True, "证据": evidence, "原文": result.get("原文") or "",
            "转写凑的": True}


# =====进程内跑 bv.py（2026-10-31）=====
#
#为什么能这么做：bv.py **只用标准库**（实测 import全是 stdlib，没有 requests/bs4这类三方依赖）。
#既然如此，就没必要为它打包一套430 MB的 Python环境，也不需要机器上装过 Python。
#
#一个实测踩到的坑：用 importlib加载含 @dataclass的文件时，
#必须先把模块登记进 sys.modules，否则 dataclass取 __module__.__dict__会报 None。

_BV_MOD = None
_BV_LOCK = threading.Lock()


def _load_bv(bvpy: str):
    """把 bv.py当模块加载进来（只加载一次）。失败返回 None。"""
    global _BV_MOD
    with _BV_LOCK:
        if _BV_MOD is not None:
            return _BV_MOD
        try:
            import importlib.util
            import sys as _sys

            spec = importlib.util.spec_from_file_location("loom_bv_inproc", bvpy)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            _sys.modules["loom_bv_inproc"] = mod # dataclass要它先在 sys.modules里
            spec.loader.exec_module(mod)
            _BV_MOD = mod
            return mod
        except Exception:
            return None #加载不了就退回子进程路


def _bv_inproc(bvpy: str, argv: list, timeout: int):
    """在当前进程里跑 bv.py，拿它 stdout里的证据 JSON。跑不成返回 None。

    为什么拦 stdout：bv.py是 CLI，结果是「日志行 +最后一行证据 JSON」。
    启动器看不到控制台，所以临时把 sys.stdout换成 StringIO，结束必复原。

    超时：同进程里做不到强制中断，只能在跑完后看耗时。仍然值得，
    因为它比"机器上没有 python就取不了稿"强得多。
    """
    mod = _load_bv(bvpy)
    if mod is None:
        return None
    import io as _io
    import sys as _sys

    buf = _io.StringIO()
    old_out, old_err, old_argv = _sys.stdout, _sys.stderr, _sys.argv
    #2026-10-31：bv.py 从 os.environ 读 BILI_SESSDATA（第631行），同进程里必须临时设上
    old_sess = os.environ.get("BILI_SESSDATA")
    sess = os.environ.get("BILI_SESSDATA") or ""
    t0 = time.time()
    try:
        _sys.stdout = buf
        _sys.stderr = _io.StringIO()
        _sys.argv = ["bv.py"] + list(argv)
        if sess:
            os.environ["BILI_SESSDATA"] = sess
        mod.main()
    except SystemExit:
        pass # CLI正常结束会抛 SystemExit
    except Exception:
        return None
    finally:
        _sys.stdout, _sys.stderr, _sys.argv = old_out, old_err, old_argv
        if old_sess is None:
            os.environ.pop("BILI_SESSDATA", None)
        else:
            os.environ["BILI_SESSDATA"] = old_sess

    if time.time() - t0 > max(1, int(timeout or 0)):
        return {"__slow__": True, "秒": round(time.time() - t0,1)}
    return _last_json(buf.getvalue())

def _merge_frames_pass(ev: dict, bvpy: str, target: str, *, start, end, timeout: int) -> None:
    """第二趟：带视觉采样重跑一次 bv.py inspect（不加 --subtitle-only/--no-video），
    把帧图（images）与画面 OCR 文字合并回第一趟的证据 dict（就地修改）。

    为什么两趟：第一趟纯文字路线快且不需要 ffmpeg；帧采样要下视频、可能几分钟，
    失败不应拖垮已经到手的文字稿。任何异常都静默吞掉——帧是锦上添花，不是命脉。

    bv.py 同命令同范围会命中自己的缓存（meta/subtitle/asr/帧网格全部落盘），
    第二趟的真实新增成本主要是「下载视频 + 抽帧」这一段。

    2026-11-08 审查修正(#6)：合并不覆盖第一趟已有的 images/coverage——rescue/ASR
    路径可能已经带图；再补合并时取并集并显式记录覆盖状态（审查 #7：partial 要可见）。
    """
    try:
        argv2 = ["inspect", target]
        if start is not None:
            argv2 += ["--start", str(start)]
        if end is not None:
            argv2 += ["--end", str(end)]
        got2 = _bv_inproc(bvpy, argv2, timeout)
        if not isinstance(got2, dict) or got2.get("__slow__"):
            return
        imgs2 = got2.get("images") or []
        cov2 = got2.get("coverage") or {}
        ev2 = got2.get("text") or ""
        # 画面文字：第二趟证据 text 里 [x.xs OCR] 行是画面独有的；原 text 是口播。
        ocr_rows = [ln.strip() for ln in ev2.splitlines() if ln.strip().startswith("[") and "s OCR]" in ln]
        if imgs2:
            old = ev.get("images") or []
            old_paths = {str(x.get("path") or "") for x in old if isinstance(x, dict)}
            merged = old + [x for x in imgs2 if isinstance(x, dict) and str(x.get("path") or "") not in old_paths]
            ev["images"] = merged
        if cov2 and not ev.get("coverage"):
            ev["coverage"] = cov2
        if ocr_rows:
            prev = str(ev.get("画面文字") or "")
            ev["画面文字"] = (prev + chr(10) if prev else "") + chr(10).join(ocr_rows)
        if imgs2 or ocr_rows:
            extra = "（帧采样补充：" + str(len(imgs2)) + " 张图"
            if ocr_rows:
                extra += "、" + str(len(ocr_rows)) + " 行画面文字"
            ev["帧采样说明"] = extra + "）"
        ev.setdefault("画面覆盖", "部分" if not imgs2 else "已采样")
    except Exception:      # noqa: BLE001 - 帧是锦上添花
        ev.setdefault("画面覆盖", "帧采样失败(仅文字)")
        return


def fetch_text(target: str, *, start: float | None = None, end: float | None = None,
               ocr: bool = True, timeout: int = 1800,
               runner=subprocess.run) -> dict:
    """**文字取稿**：先走 `bv.py inspect <target> --subtitle-only --no-video`；
    失败或没字幕时，用 Loom 自带的 ffmpeg + 本地模型转写缓存里已经下下来的音轨。

    返回 `{ok:True, 证据, 原文}` 或 `{ok:False, code, text}`，**永不抛异常**。
    一次调用 = 一次对 B 站的外发（由作者点「取稿」触发），函数本身不做任何预检请求。
    """
    target = (target or "").strip()
    if not target:
        return {"ok": False, "code": "no_target", "text": "先给个视频地址。"}
    t = tools()
    if not t["bvpy"]:
        return {"ok": False, "code": "no_bvpy",
                "text": "这台机器上没找到 bv.py，取不了字幕。可以把字幕文件直接拖进来。"}
    #2026-10-31（作者裁定「B站下载层必须纳入包内」）：
    #**先在进程内跑** —— bv.py 只用标准库，不需要 python.exe。
    #跑不成（导入失败/没输出）才退回子进程那条老路（那时才需要 python）。
    argv = ["inspect", target, "--subtitle-only", "--no-video"]
    if ocr:
        argv.append("--ocr")
    if start is not None:
        argv += ["--start", str(start)]
    if end is not None:
        argv += ["--end", str(end)]
    #2026-11-02（视觉链路定稿）：ocr=True 时补一次帧采样调用（bv.py 自动拉视频抽帧）。
    #帧图进证据 images，由调用方决定喂给视觉模型（支持视觉）还是只跑 OCR 文字（不支持）。
    #探测在 agent 侧做（backends.supports_vision），fetch_text 只管采样。
    #2026-11-08（作者报告「图片识别怎么还是没起效」）：原实现里 want_frames 算完从没用过
    #——argv 永远带 --subtitle-only --no-video，bv.py 侧 text_only=True，一帧都不采。
    #现改为：第一趟仍走纯文字路线（快、不需要 ffmpeg），拿到文字后补一趟帧采样（_merge_frames_pass）。
    want_frames = bool(ocr)

    got_inproc = _bv_inproc(t["bvpy"], argv, timeout)
    if isinstance(got_inproc, dict) and got_inproc.get("__slow__"):
        return _with_asr({"ok": False, "code": "timeout",
                          "text": "取稿超时了（跑了 %s 秒还没完）。可以只用字幕、或缩小片段时间。"
                                  % got_inproc.get("秒")},
                         target, start=start, end=end, runner=runner)
    if isinstance(got_inproc, dict):
        if want_frames:
            _merge_frames_pass(got_inproc, t["bvpy"], target, start=start, end=end, timeout=timeout)
        base = {"ok": True, "证据": got_inproc, "原文": json.dumps(got_inproc, ensure_ascii=False)}
        # 2026-11-01：subtitle-only 模式对无字幕视频返回的"证据"只有标题和限制说明
        #（正文 <200 字）。这时补一次 bv.py text（会下载音轨+转写），救回真实内容。
        _ev_text = got_inproc.get("text") or ""
        # 2026-11-01：无字幕视频的证据是 408 字的框架说明（含「(无文本证据)」），
        # 字数阈值 200 拦不住它——用内容特征判断：出现「(无文本证据)」= 无真实内容。
        if len(_ev_text) < 200 or "(无文本证据)" in _ev_text:
            rescued = _rescue_via_text(t["bvpy"], t["python"], target, start=start, end=end,
                                       runner=runner, timeout=timeout)
            if rescued is not None:
                # 2026-11-08 审查修正(#6)：rescue 直接返回会丢掉已合并的帧图——
                # 把第一趟+第二趟攒下的 images/画面文字/覆盖状态合并进 rescue 结果。
                try:
                    _ev_old = rescued.get("证据") or {}
                    _imgs_old = got_inproc.get("images") or []
                    if _imgs_old and not _ev_old.get("images"):
                        _ev_old["images"] = _imgs_old
                    _ocr_old = str(got_inproc.get("画面文字") or "")
                    if _ocr_old:
                        _ev_old["画面文字"] = _ocr_old
                    if got_inproc.get("画面覆盖"):
                        _ev_old.setdefault("画面覆盖", got_inproc["画面覆盖"])
                except Exception:      # noqa: BLE001
                    pass
                return rescued
        return _with_asr(base, target, start=start, end=end, runner=runner)

    if not t["python"]:
        return {"ok": False, "code": "no_python",
                "text": "进程内跑取稿没成，机器上又没有能跑的 python（备用路线）。可以把字幕文件直接拖进来。"}

    cmd = [t["python"], t["bvpy"]] + argv

    try:
        r = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=timeout, env=_env())
    except FileNotFoundError:
        return {"ok": False, "code": "no_python", "text": "叫不起 bv.py（找不到解释器）。"}
    except subprocess.TimeoutExpired:
        return _with_asr({"ok": False, "code": "timeout",
                          "text": f"取稿超过 {timeout // 60} 分钟还没完，先停下。可以只用字幕、或缩小片段时间。"},
                         target, start=start, end=end, runner=runner)
    except OSError as e:
        return _with_asr({"ok": False, "code": "spawn_failed", "text": f"起不来：{e}"},
                         target, start=start, end=end, runner=runner)

    out, err = (r.stdout or ""), (r.stderr or "")
    if getattr(r, "returncode", 0) != 0:
        tail = (err or out).strip().splitlines()[-3:]
        return _with_asr({"ok": False, "code": "fetch_failed",
                          "text": "取稿失败：" + ("；".join(tail) if tail else f"退出码 {r.returncode}")},
                         target, start=start, end=end, runner=runner)
    evidence = _last_json(out)
    if evidence is None:
        return _with_asr({"ok": False, "code": "bad_output",
                          "text": "bv.py 跑完了，但没看懂它给的证据（输出不是 JSON）。"},
                         target, start=start, end=end, runner=runner)
    # 2026-11-08 审查修正(#6)：子进程成功分支同样补帧（原来只有进程内分支有）。
    if want_frames:
        _merge_frames_pass(evidence, t["bvpy"], target, start=start, end=end, timeout=timeout)
    got = _with_asr({"ok": True, "证据": evidence, "原文": out},
                    target, start=start, end=end, runner=runner)
    if got.get("转写凑的"):
        got["原文"] = out      # 救回来的是转写，"原文"仍是 bv.py 的原始输出
    return got


def evidence_text(evidence: dict) -> str:
    """把证据包压成一段可读文本，喂给「整理成笔记」那一步。

    只取人类可用的三样：转写正文、画面描述（带时间点）、画面文字（OCR）。
    **不编**：哪一样没有就哪一段不出现。
    """
    parts: list[str] = []
    meta = evidence.get("meta") or {}
    if isinstance(meta, dict):
        title = meta.get("title") or meta.get("name")
        if title:
            parts.append(f"【片名】{title}")
    text = evidence.get("text")
    if isinstance(text, str) and text.strip():
        parts.append("【转写/字幕】\n" + text.strip())
    images = evidence.get("images") or evidence.get("frames") or []
    lines = []
    for item in images if isinstance(images, list) else []:
        if not isinstance(item, dict):
            continue
        at = item.get("at") or item.get("time") or item.get("t")
        desc = item.get("desc") or item.get("vision") or item.get("summary") or ""
        ocr = item.get("ocr") or ""
        row = " ".join(x for x in (f"[{at}s]" if at is not None else "", str(desc).strip()) if x)
        if ocr:
            row += f"（画面文字：{str(ocr).strip()[:200]}）"
        if row.strip():
            lines.append(row)
    if lines:
        parts.append("【画面】\n" + "\n".join(lines))
    return "\n\n".join(parts).strip()
