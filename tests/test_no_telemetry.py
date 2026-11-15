# -*- coding: utf-8 -*-
"""「零遥测」自证(2026-10-25,来自一份陌生视角审查的建议)。

Loom 最值钱的信用是:**它不往任何第三方上报你的东西**。口头保证会随时间失效,
所以这里把它做成一条**会红的测试**:

- 扫 `loom/**` 里所有 `.py` 与 `webui/**` 里的 `.js/.html`,抽出全部 http(s) URL;
- 每个域名必须出现在白名单里,而白名单里每一条都写明"为什么它该在";
- 想偷偷加一个上报域名 → 这条测试立刻失败,而且失败信息会告诉后来人该怎么做。

**这不是安全扫描**。它只回答一个问题:代码里还连着哪些外面的地方。
"""
from __future__ import annotations

import pathlib
import re

import pytest

LOOM_DIR = pathlib.Path(__file__).resolve().parents[1] / "loom"

# 白名单:域名 → 为什么它该在这份代码里
ALLOWED_HOSTS: dict[str, str] = {
    # ── 模型供应商的接口(作者自己配 key 才会通;代码里只是地址) ──────────────
    "api.deepseek.com": "DeepSeek 官方接口",
    "api.moonshot.cn": "月之暗面 Kimi 接口",
    "api.siliconflow.cn": "硅基流动接口",
    "ark.cn-beijing.volces.com": "火山方舟接口",
    "dashscope.aliyuncs.com": "阿里百炼接口",
    "open.bigmodel.cn": "智谱接口",
    # ── 联网查资料(知识顾问;作者自己在设置里配了才用) ────────────────────────
    "api.tavily.com": "Tavily 联网搜索(可选,作者自配)",
    # ── 本机 ──────────────────────────────────────────────────────────────────
    "127.0.0.1": "本机服务(桌面窗口连自己的 HTTP 面)",
    # 这两个不是地址,是「本机」的另外两种写法 —— 出现处是 SSRF 检查的黑名单,
    # 用来挡住「把自己的地址写成十进制/十六进制」这种绕过手法的。
    "2130706433": "127.0.0.1 的十进制写法(SSRF 检查用)",
    "0x7f000001": "127.0.0.1 的十六进制写法(SSRF 检查用)",
    # ── 帮助链接(错误提示里让作者去查额度/文档) ──────────────────────────────
    "platform.deepseek.com": "错误提示里的 DeepSeek 控制台链接",
    # ── B站取稿(「B站下载层必须纳入包内」) ──────────────────
    # bv.py 收进包内后,这些地址出现在 loom/vendor/bili-vision/bv.py 里。
    # 都不是上报:只有作者点「取稿」才会连,取的是字幕/元数据/转写模型。
    "api.bilibili.com": "取字幕与视频元数据(作者点「取稿」才连)",
    "www.bilibili.com": "短链与页面地址解析(同上)",
    "hf-mirror.com": "本地转写模型下载(国内镜像,可用 HF_ENDPOINT 改)",
    "huggingface.co": "同上,海外备选",
    # ── C-7 大件下载(2026-11-11):与上面 hf-mirror 同类,都不是上报 ────────────
    # 三个大件(model.onnx / ffmpeg.exe / ffprobe.exe)不随仓库分发,
    # 只有作者主动跑 `python -m loom.vendor.fetch_vendor` 才会连这里。
    "www.gyan.dev": "ffmpeg/ffprobe 官方 Windows 构建下载(作者主动跑 fetch_vendor 才连)",
    # 不是请求:fetch_vendor 的 User-Agent 里写了项目主页(与 www.w3.org 那条同类)
    "github.com": "fetch_vendor 的 UA 字符串里指项目主页,代码不会去连它",
    # ── 不是网络请求 ──────────────────────────────────────────────────────────
    # SVG 的 xmlns 命名空间字符串。浏览器不会去取它,它只是格式标识。
    "www.w3.org": "SVG 命名空间标识(iconfont 里),不是网络请求",
}

_URL_RE = re.compile(r"https?://[A-Za-z0-9_.:/\-{}$%]+")
_SCAN_SUFFIX = {".py", ".js", ".html"}
_SKIP_DIRS = {"__pycache__", "fonts", "agents", "_bak-diag-20261021-040245"}

# 出现这些词就值得人看一眼(不是自动判红:注释里提到也算,交给下面那条断言去核)
_SMELLS = ("telemetry", "analytics", "sentry", "posthog", "gtag(", "mixpanel",
           "amplitude", "umami", "baidu.com/hm", "google-analytics")


def _files():
    for p in sorted(LOOM_DIR.rglob("*")):
        if not p.is_file() or p.suffix not in _SCAN_SUFFIX:
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


def _hosts() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in _files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for url in _URL_RE.findall(text):
            host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
            #2026-10-31：模板残片 `{path}` 不是域名的一部分（如 `https://api.bilibili.com{path}`），
            #不剥掉它白名单里就永远对不上，只能被迫把残片也写进去 —— 那是自欺。
            host = re.sub(r"\{.*?\}", "", host)
            if host:
                out.setdefault(host, set()).add(p.name)
    return out


def test_只连白名单里的地方():
    """多出任何一个域 → 这条红。要加新域名,必须先把它写进 ALLOWED_HOSTS 并说明理由。"""
    found = _hosts()
    extra = sorted(set(found) - set(ALLOWED_HOSTS))
    assert not extra, (
        "代码里出现了白名单之外的网络地址:" + ", ".join(f"{h}(在 {', '.join(sorted(found[h]))})" for h in extra)
        + "\n如果这是必需的(比如新增一家模型供应商),把它加进 ALLOWED_HOSTS 并写清为什么;"
          "如果它是上报/统计/埋点,那就不该存在 —— 这个产品不打点。"
    )


def test_没有埋点库的痕迹():
    """扫一遍埋点常见名字。出现即红 —— 不是"提醒",是零遥测的底线。"""
    hits = []
    for p in _files():
        text = p.read_text(encoding="utf-8", errors="replace").lower()
        for word in _SMELLS:
            if word in text:
                hits.append(f"{p.name}: {word}")
    assert not hits, "发现疑似上报/埋点的字样:" + ";".join(hits)


def test_白名单每一条都写清了理由():
    """白名单不能变成"随手加的名单":每一条都必须有非空理由。"""
    for host, why in ALLOWED_HOSTS.items():
        assert host.strip() == host and host, host
        assert len(why.strip()) >= 4, f"{host} 没写清为什么该在"

    # 反过来:白名单里不该有早就删掉、代码里已经不用的域名(留着会掩盖新加的)
    unused = []
    found = _hosts()
    for host in ALLOWED_HOSTS:
        if host not in found:
            unused.append(host)
    assert not unused, f"白名单里有代码已经不用的域名,删掉:{unused}"
