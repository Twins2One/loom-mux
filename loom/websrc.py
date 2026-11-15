# -*- coding: utf-8 -*-
r"""联网取资料:搜索服务 + 抓网页正文( 2)。

需求背景:「增加知识顾问搜索服务配置项,同时内置知识顾问 AI 可用的浏览器插件,到时我单独配置」。

所以这里做两件事,**都不内置任何 key、不默认联网**:
1. `search(root, q)`:按 `loom.toml` 的 `[search]` 去问一个搜索服务。没配 / key 环境变量没值 ->
   明确回 `not_configured` 并说清怎么配 —— **绝不假装搜过**;
2. `fetch_page(url)`:抓一个网页正文(内置的「浏览器」),把 HTML 削成纯文本。只许 http/https;
   内网/本机地址一律拒(这是给"作者查资料"用的,不是内网扫描器)。

资料一律标 `[联网]`,并且**当作可能不可靠**:知识顾问那边已有这条规矩,这里只负责把原始文本取回来。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import threading
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
import ipaddress

MAX_PAGE = 400_000          # 最多读这么多字节的响应(防巨型页面拖死)
DEFAULT_CHARS = 8000        # 削成文本后最多回这么多字
TIMEOUT = 20
_UA = "Mozilla/5.0 (compatible; Loom/0.5; knowledge-advisor)"

# 只有这几家的形状认得(其余按通用形状试)
_ENGINES = ("tavily", "bing", "generic")
_REFUSE_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0")


# ---------------------------------------------------------------- 配置
def config_for(root: Path | str) -> dict:
    """读 `loom.toml` 的 `[search]`。**只读配置,不发请求。**

    形状:
        [search]
        engine = "tavily"          # tavily | bing | generic
        base_url = "https://api.tavily.com/search"
        key_env = "LOOM_SEARCH_KEY" # key 放环境变量里,不写进文件
        limit = 8
    """
    import tomllib

    p = Path(root) / "loom.toml"
    if not p.is_file():
        return {"配置了": False, "为什么": "这本书没有 loom.toml。"}
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception as e:      # noqa: BLE001 - 坏了要说清,不要静默
        return {"配置了": False, "为什么": f"loom.toml 读不了({type(e).__name__}: {e})"}
    sec = data.get("search") or {}
    if not isinstance(sec, dict):      # 写成 `search = "x"` 时以前会 AttributeError → 500
        return {"配置了": False, "为什么": "[search] 应该是一张表([search] 段),现在不是。"}
    engine = str(sec.get("engine") or "").strip().lower()
    base = str(sec.get("base_url") or "").strip()
    key_env = str(sec.get("key_env") or "").strip()
    try:
        limit = int(sec.get("limit") or 8)
    except (TypeError, ValueError):      # 手写坏了不许 500(交叉审查 P2-6)
        limit = 8
    why = ""
    if not base:
        why = '没配搜索服务:在 loom.toml 里加 [search] 一节(engine / base_url / key_env)。'
    elif engine and engine not in _ENGINES:
        why = f"不认识的搜索服务形状「{engine}」(只认 {' / '.join(_ENGINES)})。"
    key_ok = bool(os.environ.get(key_env)) if key_env else False
    if not why and key_env and not key_ok:
        why = f"环境变量 {key_env} 里没有值 —— key 只从环境变量读,不写进配置文件。"
    return {"配置了": not why, "为什么": why, "engine": engine or "generic", "base_url": base,
            "key_env": key_env, "key有值": key_ok, "limit": max(1, min(limit, 20))}


# ---------------------------------------------------------------- 抓网页
class _Text(HTMLParser):
    """HTML → 纯文本。丢 script/style/noscript,块级标签之间留空行。"""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "blockquote", "pre", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self.skip:
            self.skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", raw)
        return "\n".join(line.strip() for line in raw.splitlines()).strip()


# 真落到的那一页(重定向之后)。用线程局部:模块全局在多线程下会互相覆盖,闸就看不准了(复审 N)。
_LOCAL = threading.local()


def _set_last(url: str) -> None:
    _LOCAL.last_url = url


def _get_last() -> str:
    return getattr(_LOCAL, "last_url", "")


class _RefusedRedirect(Exception):
    """对方把我转去了不能碰的地址 —— 在**读正文之前**就断掉。"""


def _http_get(url: str, *, data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    """一个薄封装:测试要能替换它,所以单独成函数(不要在别处直接 urlopen)。"""
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"User-Agent": _UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:     # noqa: S310 - 只允许 http/https,下面有闸
        landed = r.geturl() or url
        _set_last(landed)
        if landed.strip() != url.strip():
            why = _check_url(landed)      # 先拦,再读:不许把内网内容读进内存(复审 P2-5)
            if why:
                raise _RefusedRedirect(why)
        return r.status, r.read(MAX_PAGE)


def _check_url(url: str) -> str:
    """只许 http/https,内网/本机一律拒。返回空串表示通过,否则返回中文原因。"""
    u = (url or "").strip()
    if not u:
        return "没给网址。"
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return "这个网址我看不懂。"
    if parts.scheme not in ("http", "https"):
        return f"只抓 http/https 的网页,不碰「{parts.scheme or '空'}」。"
    host = (parts.hostname or "").lower().rstrip(".")     # `localhost.` 是同一个地方
    if not host:
        return "网址里没有主机名。"
    # IPv6(含 [::ffff:127.0.0.1] 这种映射、fd00::/8 私网、[::] 未指定)—— 以前整条漏了
    if ":" in host:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return "这个 IPv6 地址看不懂,不抓。"
        mapped = getattr(ip, "ipv4_mapped", None)
        if (mapped is not None and (mapped.is_private or mapped.is_loopback or mapped.is_unspecified)) \
                or ip.is_private or ip.is_loopback or ip.is_link_local \
                or ip.is_unspecified or ip.is_reserved or ip.is_multicast:
            return "本机/内网地址不抓(IPv6 换个写法也不行)。"
    # 只由数字点组成的「像 IP」的东西:要求是规范点分十进制,别的一律当可疑(0177.0.0.1 / 1.2.3.04)
    if re.fullmatch(r"[0-9.]+", host):
        try:
            if str(ipaddress.IPv4Address(host)) != host:
                return "这个地址写法可疑(前导零之类),不抓。"
        except ValueError:
            return "这个地址写法可疑,不抓。"
    if host in _REFUSE_HOSTS or host.endswith(".local") or host.endswith(".localhost"):
        return "本机/内网地址不抓(这是查资料用的,不是内网扫描器)。"
    if re.match(r"^(10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|127\.)", host):
        return "内网地址不抓。"
    # 换个写法绕:http://2130706433/ = 127.0.0.1,http://0x7f000001/ 同理
    canon = _canon_host(host)
    if canon != host:
        if canon in _REFUSE_HOSTS or re.match(r"^(10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|127\.)",
                                              canon):
            return "这个地址换个写法就是本机/内网,不抓。"
    return ""


def _canon_host(host: str) -> str:
    """把 2130706433 / 0x7f000001 这类写法还原成点分十进制;认不出就原样返回。"""
    h = (host or "").strip("[]").lower()
    if re.fullmatch(r"\d{1,10}", h) or re.fullmatch(r"0x[0-9a-f]{1,8}", h):
        try:
            return str(ipaddress.IPv4Address(int(h, 0)))
        except (ValueError, TypeError):
            return h
    return h


def fetch_page(url: str, *, max_chars: int = DEFAULT_CHARS) -> dict:
    """抓一页正文。返回 {ok, 网址, 标题, 正文, 字符数, 截断}。"""
    why = _check_url(url)
    if why:
        return {"ok": False, "code": "bad_url", "reason": why, "网址": url}
    _set_last("")
    try:
        status, body = _http_get(url.strip())
    except _RefusedRedirect as e:
        return {"ok": False, "code": "bad_url", "网址": _get_last() or url,
                "reason": "对方把这次抓取转去了不能碰的地址,不跟。" + str(e)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": "http_error", "reason": f"对方回了 HTTP {e.code}。", "网址": url}
    except Exception as e:      # noqa: BLE001 - 网络失败要说清是哪一类
        return {"ok": False, "code": "fetch_failed",
                "reason": f"抓不下来({type(e).__name__}: {e})", "网址": url}
    _last = _get_last()
    if _last and _last != url.strip():
        # 302 到内网/本机:闸不能只管用户填的那一跳(交叉审查 P2-绕过)
        why = _check_url(_last)
        if why:
            return {"ok": False, "code": "bad_url", "网址": _last,
                    "reason": "对方把这次抓取转去了不能碰的地址,不跟。" + why}
    if status >= 400:
        return {"ok": False, "code": "http_error", "reason": f"对方回了 HTTP {status}。", "网址": url}
    charset = "utf-8"
    m = re.search(rb'charset=["\']?([\w-]+)', body[:4000], re.I)
    if m:
        charset = m.group(1).decode("ascii", "ignore")
    try:
        html = body.decode(charset, "replace")
    except LookupError:
        html = body.decode("utf-8", "replace")
    title = ""
    mt = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if mt:
        title = re.sub(r"\s+", " ", mt.group(1)).strip()[:120]
    parser = _Text()
    try:
        parser.feed(html)
    except Exception:      # noqa: BLE001 - 畸形 HTML 不该炸
        pass
    text = parser.text()
    cut = text[:max_chars]
    return {"ok": True, "网址": url, "标题": title, "正文": cut, "字符数": len(cut),
            "原文长度": len(text), "截断": len(text) > max_chars}


# ---------------------------------------------------------------- 搜索
def _parse_hits(engine: str, raw: bytes) -> list[dict]:
    """把各家回包削成统一形状:[{标题, 网址, 摘要}]。认不出就回空表(不编)。"""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:      # noqa: BLE001 - 不是 JSON 就当没结果
        return []
    items: list = []
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):                 # tavily / generic
            items = data["results"]
        elif isinstance(data.get("webPages"), dict):              # bing
            items = data["webPages"].get("value") or []
        elif isinstance(data.get("items"), list):                 # 还一种常见形状
            items = data["items"]
    out = []
    for it in items[:20]:
        if not isinstance(it, dict):
            continue
        out.append({"标题": str(it.get("title") or it.get("name") or "").strip(),
                    "网址": str(it.get("url") or it.get("link") or "").strip(),
                    "摘要": str(it.get("content") or it.get("snippet") or it.get("description") or "").strip()})
    return [x for x in out if x["网址"]]


def search(root: Path | str, query: str, *, limit: int = 0) -> dict:
    """问搜索服务。**没配就明说没配。**"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "code": "no_query", "reason": "先写要搜什么。"}
    cfg = config_for(root)
    if not cfg.get("配置了"):
        return {"ok": False, "code": "not_configured", "reason": cfg.get("为什么") or "搜索服务没配置。",
                "怎么配": '在 loom.toml 加 [search]\nengine = "tavily"\n'
                          'base_url = "https://api.tavily.com/search"\nkey_env = "LOOM_SEARCH_KEY"',
                "落盘": False}
    n = max(1, min(int(limit or cfg["limit"]), 20))
    url = cfg["base_url"]
    engine = cfg["engine"]
    key = os.environ.get(cfg["key_env"], "") if cfg["key_env"] else ""
    headers = {"Content-Type": "application/json"}
    payload: dict = {"query": q, "max_results": n}
    if engine == "bing":
        # bing 那套走 query string + 头里带 key
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"q": q, "count": n})
        body = None
        if key:
            headers["Ocp-Apim-Subscription-Key"] = key
    else:
        if key:
            payload["api_key"] = key
        body = json.dumps(payload).encode("utf-8")
    try:
        status, raw = _http_get(url, data=body, headers=headers)
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": "http_error", "reason": f"搜索服务回了 HTTP {e.code}。", "落盘": False}
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "code": "search_failed",
                "reason": f"搜索失败({type(e).__name__}: {e})", "落盘": False}
    hits = _parse_hits(engine, raw)[:n]
    return {"ok": True, "结果": hits, "条数": len(hits), "engine": engine,
            "说明": "这些是**未经核实**的网上内容;引用时带网址,知识顾问会标 [联网]。", "落盘": False}


def status(root: Path | str) -> dict:
    """界面用:配没配、配了什么形状。**不显示 key 的值。**"""
    cfg = config_for(root)
    return {"ok": True, "配置了": cfg.get("配置了"), "为什么": cfg.get("为什么", ""),
            "engine": cfg.get("engine", ""), "base_url": cfg.get("base_url", ""),
            "key_env": cfg.get("key_env", ""), "key有值": cfg.get("key有值", False),
            "limit": cfg.get("limit") or 8,
            "怎么配": '在 loom.toml 加 [search]\nengine = "tavily"\n'
                      'base_url = "https://api.tavily.com/search"\nkey_env = "LOOM_SEARCH_KEY"',
            "落盘": False}
