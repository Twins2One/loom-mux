"""书房 · 自带的语音转写 A 案后新增）。

为什么不复用本机那套 faster-whisper 环境：那里的解码走 PyAV，而 **PyAV 19.x 与
faster-whisper 1.2.1 的 `av.open(..., metadata_errors=...)` 不兼容**——机器上一切正常，
一跑转写就 `TypeError: open() got an unexpected keyword argument 'metadata_errors'`。
所以这里**不用 PyAV 解码**：先用自带的 ffmpeg 把任意音视频解成 16k 单声道裸流，再交给
numpy 变 float32 喂模型。这条路已实测（2711.9 秒音频 → 1214 段 / 14387 字）。

三条设计（与 video.py 同口径）：

1. **不 import 就不干活**：`faster_whisper` / `numpy` 都在函数里延迟 import，模块本身只读路径。
2. **缺什么说什么**：ffmpeg / 模型 / 依赖 各自报名字，界面能直接显示，不静默失败。
3. **包内优先、包外兜底**：先找打包进 Loom 的 `loom/vendor/`，找不到再用环境变量与本机既有目录。
   这样装好的 Loom 在哪台机器上都能转写，开发和临时环境也照旧能跑。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor"
# Loom 统一从自己的用户级 vendor 目录发现下载的工具（默认 `~/.loom/vendor`）。
from ..config import vendor_dir as _vendor_dir
VENDOR_HOME = _vendor_dir()
VENDOR_TOOLS = VENDOR_HOME
HF_SNAP = (Path(os.environ.get("USERPROFILE") or Path.home()) / ".cache" / "huggingface" / "hub"
           / "models--Systran--faster-whisper-small" / "snapshots")

_MODEL_CACHE: dict = {}


def _bundled_ffmpeg() -> Path:
    return VENDOR / "ffmpeg" / "ffmpeg.exe"


def _bundled_model() -> Path:
    return VENDOR / "whisper"


def ffmpeg() -> str:
    """ffmpeg 可执行文件。环境变量给了但指向不存在的文件 → 当作没找到。"""
    from_env = os.environ.get("LOOM_ASR_FFMPEG", "").strip()
    if from_env:
        return from_env if Path(from_env).is_file() else ""
    for c in (_bundled_ffmpeg(), VENDOR_HOME / "ffmpeg" / "ffmpeg.exe",
              VENDOR_HOME / "bili-vision-tools" / "bin" / "ffmpeg.exe"): 
        if c.is_file():
            return str(c)
    return ""


# ===== 深度用户：想换自己的语音转写模型，只看这里 =====
#
# 换法：设 `LOOM_ASR_MODEL=<目录>`，该目录里必须有 `model.bin`
# （faster-whisper 要的是**目录**，不是模型名也不是单文件）；
# 不设就依次找：包内 `loom/vendor/whisper/` → HF 缓存里的
# `models--Systran--faster-whisper-small` → 系统缓存。
#
# 新模型要满足：① **CTranslate2 格式**（不是原版 Whisper 的 .pt）；
# ② 目录里必须有 `model.bin`（本文件就靠它判“这个目录能不能用”）；
# ③ 当前固定跑 CPU + int8（`device='cpu', compute_type='int8'`，见 `_model()`）——
# 想用显卡就改那两个参数为 `device='cuda', compute_type='float16'`。
#
# 换错了会怎样：`ready()` 把“whisper 模型”列进 `缺`，取稿功能自己报“没装”，
# **不会崩、不会影响书房其他功能**（知识顾问的取稿那一块才会不可用）。


def model_dir() -> str:
    """本地模型目录（faster-whisper 要的是含 model.bin 的目录，不是模型名）。"""
    from_env = os.environ.get("LOOM_ASR_MODEL", "").strip()
    if from_env:
        return from_env if (Path(from_env) / "model.bin").is_file() else ""
    bundled = _bundled_model()
    if (bundled / "model.bin").is_file():
        return str(bundled)
    if HF_SNAP.is_dir():
        for p in sorted(HF_SNAP.iterdir()):
            if (p / "model.bin").is_file():
                return str(p)
    return ""


def deps() -> list:
    """缺哪几个 python 依赖（延迟 import，缺了不炸）。"""
    missing = []
    for mod in ("numpy", "faster_whisper"):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001 - 任何 import 期异常都算"用不了"
            missing.append(mod)
    return missing


def ready() -> dict:
    """给界面看的就绪信息：**零网络、零花费、不起进程**。"""
    ff, md, miss = ffmpeg(), model_dir(), deps()
    gaps = []
    if not ff:
        gaps.append("ffmpeg")
    if not md:
        gaps.append("whisper 模型")
    gaps += miss
    ok = not gaps
    if ok:
        hint = ""
    elif miss:
        hint = "这台 Loom 里没装语音转写组件（缺 %s），只有字幕的视频仍能取稿。" % "、".join(miss)
    else:
        hint = "缺 %s，没字幕的视频转不出文字；有字幕的仍能取稿。" % "、".join(gaps)
    return {"ok": True, "转写": ok, "缺": gaps, "模型": md, "ffmpeg": ff, "hint": hint}


def _decode(path: str, *, ff: str, timeout: int, runner=subprocess.run):
    """ffmpeg → 16k 单声道 s16le 裸流 → float32 波形。不落临时文件。"""
    import numpy as np

    cmd = [ff, "-v", "error", "-i", str(path),
           "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
    r = runner(cmd, capture_output=True, timeout=timeout)
    if getattr(r, "returncode", 0) != 0:
        tail = (getattr(r, "stderr", b"") or b"").decode("utf-8", "replace").strip().splitlines()[-2:]
        raise RuntimeError("ffmpeg 解不开这个文件：" + ("；".join(tail) if tail else f"退出码 {r.returncode}"))
    raw = r.stdout or b""
    if not raw:
        raise RuntimeError("这个文件里没有声音轨（或时长为 0）。")
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0


def _model(md: str):
    """模型加载一次就留着（1.5 秒 / 461 MB，别每次都读盘）。"""
    m = _MODEL_CACHE.get(md)
    if m is None:
        from faster_whisper import WhisperModel
        m = WhisperModel(md, device="cpu", compute_type="int8")
        _MODEL_CACHE[md] = m
    return m


def transcribe(path: str, *, timeout: int = 1800, runner=subprocess.run,
               progress=None) -> dict:
    """把一个音视频文件转成带时间点的文字。

    返回 `{ok:True, 段:[{start,end,text}], 文本, 字数, 音频秒数, 耗时}` 或
    `{ok:False, code, text}`，**永不抛异常**（调用方是 HTTP 路由，抛了就白屏）。
    """
    p = Path(str(path or "")).expanduser()
    if not str(path or "").strip():
        return {"ok": False, "code": "no_target", "text": "先给个文件。"}
    if not p.is_file():
        return {"ok": False, "code": "no_file", "text": f"找不到这个文件：{p}"}
    st = ready()
    if not st["转写"]:
        return {"ok": False, "code": "not_ready", "text": st["hint"]}
    if progress:
        progress("正在读音频…")
    t0 = time.time()
    try:
        audio = _decode(str(p), ff=st["ffmpeg"], timeout=timeout, runner=runner)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "timeout",
                "text": f"解音频超过 {timeout // 60} 分钟，先停下；可以截短一点再试。"}
    except RuntimeError as e:
        return {"ok": False, "code": "decode_failed", "text": str(e)}
    except Exception as e:  # noqa: BLE001 - 缺依赖/fileNotFound 等一律给一句话
        return {"ok": False, "code": "decode_failed", "text": f"解音频没成功：{e}"}

    seconds = len(audio) / 16000.0
    if progress:
        progress("正在转写…（%d 分钟音频，本机大约要 %d 分钟）"
                 % (max(1, round(seconds / 60)), max(1, round(seconds / 60 * 0.2))))
    try:
        segs, info = _model(st["模型"]).transcribe(
            audio, language=os.environ.get("LOOM_ASR_LANG", "zh"), beam_size=1,
            vad_filter=False, condition_on_previous_text=False)
        rows = [{"start": round(float(s.start), 2), "end": round(float(s.end), 2),
                 "text": (s.text or "").strip()} for s in segs]
    except Exception as e:  # noqa: BLE001 - 模型层面的任何异常都要变成人话
        return {"ok": False, "code": "asr_failed", "text": f"转写没成功：{e}"}
    rows = [r for r in rows if r["text"]]
    text = "\n".join(r["text"] for r in rows)
    return {"ok": True, "段": rows, "文本": text, "字数": len(text.replace("\n", "")),
            "音频秒数": round(seconds, 1), "耗时": round(time.time() - t0, 1),
            "语种": getattr(info, "language", "")}
