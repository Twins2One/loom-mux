# -*- coding: utf-8 -*-
"""书房自带转写（loom/study/asr.py）:路径解析、缺件降级、解码失败、成功链路。

为什么值得单测：这条路**不能用 PyAV 解码**（PyAV 19 与 faster-whisper 1.2.1 撞关键字），
所以它自己拼 ffmpeg 命令、自己转 float32。命令拼错、降级漏一种，作者看到的就是
"取稿失败"四个字——这里是唯一能在没有 GPU/网络/模型时把它钉住的地方。
"""
from __future__ import annotations

import sys
import types

import pytest

from loom.study import asr


class _Arr:
    """假波形：只提供 asr.py 用到的那两个动作（len / 除 32768）。"""

    def __init__(self, samples: int) -> None:
        self.samples = samples

    def __len__(self) -> int:
        return self.samples

    def astype(self, dtype):
        assert dtype == "float32", dtype
        return self

    def __truediv__(self, other):
        return self


def _fake_numpy(samples: int):
    mod = types.ModuleType("numpy")

    def frombuffer(raw, dtype=None):
        assert dtype == "<i2", dtype
        return _Arr(samples)

    mod.frombuffer = frombuffer
    return mod


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


def _fake_whisper(segs):
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, path, device=None, compute_type=None):
            self.path, self.device, self.compute_type = path, device, compute_type

        def transcribe(self, audio, **kw):
            info = types.SimpleNamespace(language="zh", language_probability=1.0)
            return iter([_Seg(*s) for s in segs]), info

    mod.WhisperModel = WhisperModel
    return mod


def _fake_files(tmp_path, *, model=True, ffmpeg=True):
    ff = tmp_path / "ffmpeg" / "ffmpeg.exe"
    if ffmpeg:
        ff.parent.mkdir(parents=True, exist_ok=True)
        ff.write_bytes(b"x")
    md = tmp_path / "whisper"
    md.mkdir()
    if model:
        (md / "model.bin").write_bytes(b"x")
    return str(ff), str(md)


def test_ready_says_what_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOM_ASR_FFMPEG", str(tmp_path / "nope.exe"))  # 指向不存在的文件
    monkeypatch.setenv("LOOM_ASR_MODEL", str(tmp_path / "nope-dir"))
    monkeypatch.setattr(asr, "VENDOR", tmp_path / "vendor")            # 包内也没有
    monkeypatch.setattr(asr, "VENDOR_TOOLS", tmp_path / "fallback")
    monkeypatch.setattr(asr, "HF_SNAP", tmp_path / "hf")
    st = asr.ready()
    assert st["ok"] is True and st["转写"] is False
    assert "ffmpeg" in st["缺"] and "whisper 模型" in st["缺"]
    assert st["hint"]                      # 必须给人话,不能空
    assert asr.ffmpeg() == "" and asr.model_dir() == ""


def test_bundled_wins_over_external(monkeypatch, tmp_path):
    """装好的 Loom 包里那份要优先——作者不用再配环境。"""
    ff, md = _fake_files(tmp_path)
    monkeypatch.delenv("LOOM_ASR_FFMPEG", raising=False)
    monkeypatch.delenv("LOOM_ASR_MODEL", raising=False)
    monkeypatch.setattr(asr, "VENDOR", tmp_path)
    monkeypatch.setattr(asr, "VENDOR_TOOLS", tmp_path / "fallback")
    monkeypatch.setattr(asr, "HF_SNAP", tmp_path / "hf")
    assert asr.ffmpeg() == ff
    assert asr.model_dir() == md


def test_transcribe_degrades_without_swallowing_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(asr, "VENDOR", tmp_path / "vendor")
    monkeypatch.setattr(asr, "VENDOR_TOOLS", tmp_path / "fallback")
    monkeypatch.setattr(asr, "HF_SNAP", tmp_path / "hf")
    monkeypatch.setenv("LOOM_ASR_FFMPEG", "")
    monkeypatch.setenv("LOOM_ASR_MODEL", "")

    assert asr.transcribe("")["code"] == "no_target"
    assert asr.transcribe(str(tmp_path / "不存在.mp4"))["code"] == "no_file"
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    got = asr.transcribe(str(media))
    if not asr.ready()["转写"]:            # 本机缺依赖时:必须给出 hint,不是空话
        assert got["code"] == "not_ready"
        assert got["text"] == asr.ready()["hint"]
    else:
        raise AssertionError("本测试环境不该真好用")


def test_decode_failure_is_a_sentence(monkeypatch, tmp_path):
    """ffmpeg 打不开文件时,作者要看到原因,不是栈。"""
    ff, md = _fake_files(tmp_path)
    monkeypatch.setenv("LOOM_ASR_FFMPEG", ff)
    monkeypatch.setenv("LOOM_ASR_MODEL", md)
    monkeypatch.setitem(sys.modules, "numpy", _fake_numpy(0))
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_whisper([]))
    media = tmp_path / "bad.mp4"
    media.write_bytes(b"not a video")

    def bad_runner(cmd, capture_output=False, timeout=None):
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"Invalid data found\n")

    got = asr.transcribe(str(media), runner=bad_runner)
    assert got["ok"] is False and got["code"] == "decode_failed"
    assert "Invalid data found" in got["text"]


def test_success_path_uses_ffmpeg_stream_and_local_model(monkeypatch, tmp_path):
    ff, md = _fake_files(tmp_path)
    monkeypatch.setenv("LOOM_ASR_FFMPEG", ff)
    monkeypatch.setenv("LOOM_ASR_MODEL", md)
    monkeypatch.setitem(sys.modules, "numpy", _fake_numpy(16000 * 90))   # 90 秒
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_whisper([(0.0, 2.0, "第一句。"), (2.0, 3.5, "  "), (3.5, 6.0, "第二句。")]))
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    seen = {}

    def runner(cmd, capture_output=False, timeout=None):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout=b"\x00\x01" * 8, stderr=b"")

    got = asr.transcribe(str(media), runner=runner)
    assert got["ok"] is True, got
    assert got["文本"] == "第一句。\n第二句。"      # 空段被丢掉,不乱拼
    assert got["字数"] == len("第一句。第二句。")
    assert abs(got["音频秒数"] - 90.0) < 0.5
    cmd = seen["cmd"]
    assert "-f" in cmd and "s16le" in cmd and "16000" in cmd
    assert cmd[cmd.index("-ac") + 1] == "1"        # 单声道
    assert cmd[-1] == "-"                          # 走管道,不落临时文件


def test_cached_media_prefers_audio(monkeypatch, tmp_path):
    """没字幕时,靠缓存里已经下下来的音轨救——音轨优先于整段视频。"""
    from loom.study import video

    monkeypatch.setattr(video, "cache_dir", lambda: tmp_path)
    d = tmp_path / "v2" / "BV1aaaaaa111" / "p1" / "12345"
    d.mkdir(parents=True)
    (d / "video_720.mp4").write_bytes(b"x" * 4096)
    (d / "audio.m4a").write_bytes(b"y" * 4096)
    got = video._cached_media("https://www.bilibili.com/video/BV1aaaaaa111/")
    assert got.endswith("audio.m4a")
    assert video._cached_media("https://example.com/nothing") == ""


# ── HTTP:本机文件转写（2026-10-28 新增路由）──────────────────────────────────
def _client():
    from fastapi.testclient import TestClient
    from loom import server
    return TestClient(server.app, base_url="http://127.0.0.1:8611")


def test_转写路由报进度并回文本(monkeypatch):
    """事件流要有 status→done;转写失败要变成 error+人话,不能 500。"""
    import json

    def fake_ok(path, *, progress=None, **kw):
        if progress:
            progress("正在转写…")
        return {"ok": True, "文本": "第一句。\n第二句。", "字数": 8,
                "段": [{"start": 0.0, "end": 1.0, "text": "第一句。"}],
                "音频秒数": 12.0, "耗时": 3.2}

    monkeypatch.setattr(asr, "transcribe", fake_ok)
    events = []
    with _client().stream("POST", "/api/study/video/transcribe",
                          json={"path": "D:/某课.mp4"}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    assert [e["t"] for e in events][0] == "status"
    assert events[-1]["t"] == "done"
    assert events[-1]["字数"] == 8 and events[-1]["段"]

    def fake_fail(path, *, progress=None, **kw):
        return {"ok": False, "code": "decode_failed", "text": "这个文件里没有声音轨（或时长为 0）。"}

    monkeypatch.setattr(asr, "transcribe", fake_fail)
    events = []
    with _client().stream("POST", "/api/study/video/transcribe",
                          json={"path": "D:/静音.mp4"}) as r:
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    assert events[-1]["t"] == "error" and events[-1]["code"] == "decode_failed"
    assert "声音轨" in events[-1]["text"]
