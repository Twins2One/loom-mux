# -*- coding: utf-8 -*-
"""取稿封装（bv.py 子进程）的测试：**全用假 bv.py，不联网、不起真进程**。

守四件：
1. 缺件要说清缺什么（不静默失败、不抛栈）；
2. 纯文字路径不需要 ffmpeg（能退化）；
3. bv.py 打日志 + 最后一行 JSON 时，仍能解析到证据；
4. import 这个模块**不发请求、不起子进程**。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from loom.study import video as V


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_bv(tmp_path, monkeypatch):
    """造一个假 bv.py + 假 python 路径，把环境指针都指过去。"""
    bvpy = tmp_path / "bv.py"
    bvpy.write_text("# 假 bv.py\n", encoding="utf-8")
    py = tmp_path / "python.exe"
    py.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOOM_BVPY", str(bvpy))
    monkeypatch.setenv("LOOM_BV_PYTHON", str(py))
    monkeypatch.setenv("BILI_VISION_FFMPEG", str(tmp_path / "ffmpeg.exe"))
    monkeypatch.setenv("BILI_VISION_FFPROBE", str(tmp_path / "ffprobe.exe"))
    return {"bvpy": bvpy, "python": py, "dir": tmp_path}


# ── 就绪探测 ───────────────────────────────────────────────────────────────
def test_缺bvpy时探测说清楚(fake_bv, monkeypatch):
    monkeypatch.setenv("LOOM_BVPY", str(fake_bv["dir"] / "没有这个.py"))
    #2026-10-30：现在 `_pick` 遇到不存在的路径会**继续往下找**，
    #而 Loom已把 bv.py副本到 `~/.loom/vendor/bili-vision/`，所以光改环境变量造不出“缺 bv.py”。
    #这里把两个真实候选也一并遮掉，自己造前提。
    monkeypatch.setattr(V, "DEFAULT_BVPY", fake_bv["dir"] / "不存在.py", raising=False)
    monkeypatch.setattr(V, "DSH_BVPY", fake_bv["dir"] / "也不存在.py", raising=False)
    #2026-10-31：包内那份也补进候选了，遮的时候要一并遮住
    monkeypatch.setattr(V, "USER_BVPY", fake_bv["dir"] / "也不在.py", raising=False)
    monkeypatch.setenv("LOOM_BVPY", str(fake_bv["dir"] / "没有这个.py"))
    p = V.probe()
    assert p["ok"] is True and p["取稿"] is False
    assert "bv.py" in p["缺失"]
    assert "拖" in p["hint"] or "拖进来" in p["hint"] or "指定" in p["hint"]


def test_有bvpy但没ffmpeg时能取文字看不了画面(fake_bv, monkeypatch):
    #2026-10-30：这条以前靠“机器上恰好没 ffmpeg”当前提，很脆；
    #现在把所有能找到它的地方都指到空目录，自己造出这个前提。
    import loom.study.asr as _asr
    monkeypatch.setattr(V, "DEFAULT_FFMPEG_DIR", fake_bv["dir"] / "不存在的目录", raising=False)
    monkeypatch.setattr(V, "VENDOR", fake_bv["dir"], raising=False)
    #2026-11-01：历史上曾改用 LGPL 构建，那时候选链变长——tools() 的候选第二层是
    #`from .asr import VENDOR`（函数内现取 asr 模块的值），第三层 DEFAULT_FFMPEG_DIR
    #真实指向 ~/.loom/vendor/bili-vision-tools/bin（那里现在有真 ffmpeg）。
    #所以 asr.VENDOR 和 DEFAULT_FFMPEG_DIR 都要遮，前提才成立。
    monkeypatch.setattr(_asr, "VENDOR", fake_bv["dir"], raising=False)
    monkeypatch.setattr(V, "PKG_VENDOR", fake_bv["dir"] / "也没有", raising=False)
    p = V.probe()
    assert p["取稿"] is True and p["画面"] is False
    assert "ffmpeg" in p["hint"]


def test_四件齐时画面可用(fake_bv, monkeypatch):
    (fake_bv["dir"] / "ffmpeg.exe").write_text("", encoding="utf-8")
    (fake_bv["dir"] / "ffprobe.exe").write_text("", encoding="utf-8")
    p = V.probe()
    assert p["取稿"] is True and p["画面"] is True and p["hint"] == ""


def test_缓存目录可改位置(monkeypatch, tmp_path):
    monkeypatch.setenv("BILI_VISION_CACHE", str(tmp_path / "我的缓存"))
    assert str(V.cache_dir()) == str(tmp_path / "我的缓存")


# ── 取稿 ──────────────────────────────────────────────────────────────────
def test_取稿命令带纯文字参数(fake_bv):
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env") or {}
        return Result(stdout='日志行\n{"text": "他说：先慢一点。"}\n')

    out = V.fetch_text("https://www.bilibili.com/video/BV1xx", runner=runner)
    assert out["ok"] is True
    assert "--subtitle-only" in seen["cmd"] and "--no-video" in seen["cmd"]
    assert "inspect" in seen["cmd"]
    assert seen["env"]["BILI_VISION_CACHE"] == V.tools()["cache"]
    assert out["证据"]["text"].startswith("他说")


def test_日志混在输出里也能解析(fake_bv):
    stdout = ('[1/3] 下载…\n{"阶段": "1"}\n[2/3] 转写…\n'
              '{"text": "正文", "images": []}\n')
    out = V.fetch_text("BV1", runner=lambda *a, **k: Result(stdout=stdout))
    assert out["ok"] is True and out["证据"]["text"] == "正文"


def test_没有bvpy时不抛异常(fake_bv, monkeypatch):
    monkeypatch.setenv("LOOM_BVPY", "")
    monkeypatch.setattr(V, "DEFAULT_BVPY", fake_bv["dir"] / "缺.py")
    # 测试同时遮住包内与用户 vendor 候选，构造缺件场景
    monkeypatch.setattr(V, "DSH_BVPY", fake_bv["dir"] / "也缺.py")
    monkeypatch.setattr(V, "USER_BVPY", fake_bv["dir"] / "还缺.py", raising=False)
    out = V.fetch_text("BV1", runner=lambda *a, **k: Result())
    assert out["ok"] is False and out["code"] == "no_bvpy"


def test_空地址直接拒(fake_bv):
    out = V.fetch_text("   ")
    assert out["ok"] is False and out["code"] == "no_target"


def test_非零退出给可读原因(fake_bv):
    out = V.fetch_text("BV1", runner=lambda *a, **k: Result(returncode=1, stderr="失败：需要 SESSDATA"))
    assert out["ok"] is False and out["code"] == "fetch_failed"
    assert "SESSDATA" in out["text"]


def test_超时给可读原因(fake_bv):
    def runner(*a, **k):
        raise subprocess.TimeoutExpired(cmd="bv", timeout=1)
    out = V.fetch_text("BV1", timeout=120, runner=runner)
    assert out["ok"] is False and out["code"] == "timeout"
    assert "分钟" in out["text"]


def test_输出不是JSON时说人话(fake_bv):
    out = V.fetch_text("BV1", runner=lambda *a, **k: Result(stdout="一堆普通文字\n没有 JSON"))
    assert out["ok"] is False and out["code"] == "bad_output"


# ── 证据压成文本 ───────────────────────────────────────────────────────────
def test_证据压成文本只有三样且不编():
    ev = {"meta": {"title": "某集"}, "text": "转写正文",
          "images": [{"at": 12.5, "desc": "画面：两人对坐", "ocr": "字幕：走了"},
                     {"at": 30, "desc": ""}]}
    s = V.evidence_text(ev)
    assert "【片名】某集" in s and "【转写/字幕】" in s and "【画面】" in s
    assert "12.5" in s and "画面：两人对坐" in s and "字幕：走了" in s
    assert V.evidence_text({}) == ""          # 什么都没有就返回空，不编


# ── 边界：import 不发请求、不起进程 ────────────────────────────────────────
def test_import模块本身不联网不起进程(monkeypatch):
    """tools()/probe() 只读文件系统；这条测试把 subprocess 换成会爆炸的东西。"""
    def boom(*a, **k):
        raise AssertionError("不该起子进程")
    monkeypatch.setattr(subprocess, "run", boom)
    assert "ok" in V.probe()
