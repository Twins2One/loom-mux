"""下载 C-7 的第三方大件（不随仓库分发的 3 个文件）。

为什么单独有这个脚本：仓库只放代码，`model.onnx`（≈266 MB）、`ffmpeg.exe`、
`ffprobe.exe`（各≈130 MB）不进去。缺了它们，语义检索与视频画面提取不可用，
所以要有一条**可验证**的获取路径，而不是一句"自己去下"。

用法（在装好的 Loom 里，或源码根目录）：
    python -m loom.vendor.fetch_vendor            # 下到默认位置并逐个校验
    python -m loom.vendor.fetch_vendor --only ffmpeg
    python -m loom.vendor.fetch_vendor --dest "<某个>/vendor"
    python -m loom.vendor.fetch_vendor --allow-upstream-drift   # 见下

默认落点：`~/.loom/vendor/`（与 `config.vendor_dir()` 一致；运行时会在
包内 `loom/vendor/` 找不到时回退到这里）。
> 想直接塞进安装包目录，用 `--dest <安装目录>/loom/vendor`；安装目录可能只读。

**两级校验**（对 ffmpeg/ffprobe）：
  ① 用上游发布的 `.sha256` 校验下载到的 zip —— 证明"来源可信"；
  ② 从 zip 里解出 exe，再用**我们分发的这份**的 sha256 校验 —— 证明"产物就是
     我们要的那份"，而不只是"某个官方构建"。
  上游构建会轮换，所以 ② 可能因上游更新而失败。失败时脚本**不会**静默接受：
  它打印两边的 sha256 并退出，提示你用 `--allow-upstream-drift` 明确接受差异
  （接受后会记录这一偏差，仍然落位）。

`model.onnx` 没有官方下载地址：它是用 torch 从 bge-large-zh-v1.5 本地导出的
（ONNX 头里 producer=pytorch 2.0.1），上游仓库不含 onnx/。所以 MANIFEST 里它的
`url` 留空 —— 脚本遇到空 URL 会**明确报错并指向 README**，绝不写假地址。
导出办法见 `loom/vendor/README.md`，或运行 `python -m loom.vendor.export_embed_onnx`。

退出码：0 全部就绪；1 有文件缺失/校验失败（已尽量保留可用状态）。
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import os
import re
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

CHUNK = 1 << 20
UA = "loom-vendor-fetch/1.0 (+https://github.com/)"

# ---------------------------------------------------------------- 清单
# `kind="direct"`：一条 URL 就是一个文件。
# `kind="zip"`   ：上游只发 zip；下 zip → 校验 zip → 取出 member → 再校验 exe。
MANIFEST: tuple[dict, ...] = (
    {
        "rel": "embed/model.onnx",
        "kind": "direct",
        # 留空是有意的：这个文件由我们自己导出，上游没有官方 ONNX 发布物。
        "url": "",
        "size": 279_247_489,
        "sha256": "470B1A4F914B2F40C72B7A495240A75FC8858F52F7B6ED8FB13185E571F98F71",
        "what": "语义检索用的向量模型（bge-large-zh-v1.5，本地导出 ONNX）",
    },
    {
        "rel": "ffmpeg/ffmpeg.exe",
        "kind": "zip",
        # **必须钉带版本号的包**（2026-11-12 实测踩到）：
        # 原来钉的是 `ffmpeg-release-essentials.zip` —— 那是 gyan 的**滚动别名**，恒指最新构建。
        # 上游换一版，下面的 sha256 立刻过期：实测 9.0.2 的 ffmpeg.exe 是 105,423,872 B，
        # 而我们钉的是上一版 136,121,856 B ⇒ 默认路径直接 `FAIL:exe-sha256`，用户被卡死。
        # 换成 `/builds/packages/ffmpeg-9.0.2-essentials_build.zip` 后，URL 与内容一起固定，
        # 钉子不再随上游轮换而腐坏（该包旁的 `.sha256` 同样存在且数值一致）。
        "zip_url": "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip",
        "zip_sha256_url": "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip.sha256",
        "member_re": r"(^|/)bin/ffmpeg\.exe$",
        "size": 105_423_872,
        "sha256": "3256173F3F8BFFD7DF12227C68ADF68025EDB1832273A9530688A7BB1ED8EDEC",
        "what": "视频/音频解码（画面提取、转写前处理）",
    },
    {
        "rel": "ffmpeg/ffprobe.exe",
        "kind": "zip",
        "zip_url": "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip",
        "zip_sha256_url": "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip.sha256",
        "member_re": r"(^|/)bin/ffprobe\.exe$",
        "size": 105_221_120,
        "sha256": "F0D36ECBBDD3BCFAC3EFA078C96C7271C2E68B3810595552AC3B7F17E9A65C52",
        "what": "读媒体信息（时长/码率/流），配合 ffmpeg 用",
    },
)

README_HINT = "缺什么、去哪拿 → 见 loom/vendor/README.md（含导出/校验步骤）"


def default_dest() -> Path:
    """`~/.loom/vendor`：与 config.vendor_dir() 同一口径，运行时会回退到这里。"""
    from ..config import vendor_dir
    return vendor_dir()


# ---------------------------------------------------------------- 工具
def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest().upper()


# 单次下载的**总时间预算**（秒）。`urlopen(timeout=…)` 只管"单次读阻塞"，
# 不是总时长：实测 2026-11-11 一次慢速下载跑了 **825 秒**才被网络层放弃 ——
# 用户看到的是"卡十几分钟"，而不是"网络不行"。所以这里加硬预算，超了就明确放弃。
DOWNLOAD_BUDGET_SEC = int((os.environ.get("LOOM_VENDOR_TIMEOUT") or "600").strip() or "600")
# 单次 socket 读的最长阻塞与每次读的切片大小。
# 为什么不用 1MB 切片 + 60s 超时：预算检查是"每读完一片才查一次"，
# 切片越大/阻塞越久，预算的**实际粒度**就越粗（实测 5s 预算仍跑了 152s）。
_READ_TIMEOUT = 30
_SLICE = 64 * 1024


def _sock_of(resp) -> object | None:
    """尽量挖出底层 socket（拿不到就返回 None，不抛）。"""
    for path in (("fp", "raw", "_sock"), ("fp", "fp", "raw", "_sock"), ("_sock",)):
        obj = resp
        try:
            for attr in path:
                obj = getattr(obj, attr)
            if obj is not None and hasattr(obj, "settimeout"):
                return obj
        except Exception:            # noqa: BLE001
            continue
    return None


def _open(url: str, deadline: float, held: dict):
    """自己建连（http.client），**从建连那一刻起就握着 socket**。

    为什么不用 `urllib.request.urlopen`（独立复核第三次打回）：
    `urlopen` 读完响应头才返回；读头期间我们**手里什么都没有**，
    于是超时后 `release` 无处可关，工作线程会一直挂在读头上（实测：返回后 0.6s、再加 0.7s 仍活着）。
    改成自己建连后，`held["conn"].sock` 在 `getresponse()` 阻塞期间就已存在，
    `release` 关掉它，读头立刻抛错、线程当场退出 —— 这才是**真取消**而不是"不再等"。

    代价：要自己处理重定向（最多 6 跳）与 TLS 上下文。
    """
    for _ in range(6):
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "http").lower()
        host = parts.hostname or ""
        port = parts.port
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if not host:
            raise ValueError(f"URL 缺主机名：{url}")
        left = max(0.001, min(float(_READ_TIMEOUT), deadline - time.monotonic()))
        if scheme == "https":
            conn = http.client.HTTPSConnection(
                host, port, timeout=left, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=left)
        held["conn"] = conn
        conn.request("GET", path, headers={"User-Agent": UA})
        resp = conn.getresponse()
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location") or ""
            try:
                resp.read()
            except Exception:        # noqa: BLE001
                pass
            resp.close()
            conn.close()
            if not loc:
                raise IOError(f"重定向缺 Location：{url}")
            url = urllib.parse.urljoin(url, loc)
            continue
        if not (200 <= resp.status < 300):
            # **非 2xx 必须抛**：`urlopen` 原本对 4xx/5xx 抛 HTTPError。
            # 我换成自建连接后曾"一律 return"，结果 404 页面被当成正文解析 ——
            # 一个错误页里若恰好有 64 位十六进制串，就会被当成 sha256 用（危险）。
            code = resp.status
            try:
                resp.read()
            except Exception:        # noqa: BLE001
                pass
            resp.close()
            conn.close()
            raise IOError(f"HTTP {code}：{url}")
        return resp
    raise IOError(f"重定向次数过多：{url}")


def _pump(resp, fh, deadline: float, *, what: str) -> int:
    """把 resp 读进 fh（fh=None 时只计数），**截止时刻受控**。

    关键点（独立复核两次打回才补上的）：
    `urlopen(timeout=…)` 只影响**建连**那一次的 socket 超时；之后 `resp.read()` 里
    每一次 `recv` 仍用那个超时。慢速小包（比如每 0.1s 一个字节）会让 `read()` 一直有数据、
    永远不触发超时 —— 于是"预算 0.3s 实测 2.4s 才报错"：最终报错了，但**截止时刻不受控**。
    所以这里在**每次读之前**把 socket 超时重设成"剩余预算"，剩余不足 1ms 就立刻判超时。
    """
    total = 0
    # **必须优先用 read1**：`resp.read(n)` 会**阻塞到凑满 n 字节或 EOF 才返回**，
    # 慢速小包（1 字节/0.1s）时它会在内部一直循环 recv —— 设 socket 超时也没用，
    # 因为每次 recv 都有数据、永不超时。实测预算 0.3s 仍要 2.4s 才回来。
    # `read1(n)` 一有数据就返回，循环因此能按预算及时判停。
    read = getattr(resp, "read1", None) or resp.read
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{what}超过时间预算，已收 {total} 字节")
        sock = _sock_of(resp)
        if sock is not None:
            try:
                sock.settimeout(max(0.001, min(float(_READ_TIMEOUT), remaining)))
            except Exception:        # noqa: BLE001 - 设不了就退回循环前检查
                pass
        chunk = read(_SLICE)
        if not chunk:
            break
        total += len(chunk)
        if fh is not None:
            fh.write(chunk)
    if deadline - time.monotonic() <= 0:
        raise TimeoutError(f"{what}超过时间预算，已收 {total} 字节")
    return total


def _with_deadline(fn, deadline: float, what: str, *, release=None):
    """在**工作线程**里跑 fn，主线程按 deadline 硬等；超时就抛 TimeoutError。

    为什么必须有这一层（独立复核第三次打回）：
    `_pump` 只能管**正文读取**。而 `urlopen(...)` 自己要先建连、再读**响应头**——
    如果对方把响应头一个字节一个字节地挤（状态行 + 若干 header，每 0.1s 一个字节），
    `urlopen` 会一直有数据、永不超时，**根本走不到 `_pump`**。
    实测：预算 0.3s，响应头慢挤时 download 2.513s / text 2.514s 才回来。

    **超时后必须收尾**（独立复核第四次打回，Windows 实测）：
    只 `join` 等于"不再等"，工作线程仍活着、还握着输出文件句柄；
    主线程随后退出 `TemporaryDirectory` 清理时，Windows 会抛
    `PermissionError [WinError 32]`，于是 `fetch_one` **根本没返回 `FAIL:network`**，
    把已验过的错误契约弄坏了。
    所以：超时时先调 `release()`（关 socket / 关文件），再给一小段宽限让线程自己退。
    """
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:      # noqa: BLE001 - 原样转抛给调用方
            box["e"] = exc
        finally:
            box["done"] = True

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(max(0.0, deadline - time.monotonic()))
    if t.is_alive():
        if release is not None:
            try:
                release()
            except Exception:            # noqa: BLE001 - 收尾失败不许盖住超时
                pass
        t.join(0.5)                      # 宽限：让线程看到句柄已关自己退出
        raise TimeoutError(
            f"{what}超过时间预算 {DOWNLOAD_BUDGET_SEC}s（连接/响应头阶段）")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _download(url: str, dst: Path) -> None:
    """流式下到 dst（调用方给的是 .part，成功后再 rename）。

    时间约束分两层，缺一层都会被不同的慢法绕过：
    1. **整请求硬 deadline**（`_with_deadline`）：覆盖建连 + 响应头 + 正文，线程 join 兜底；
       超时后由 `release` 关掉 socket 与输出文件，保证调用方拿得到 `FAIL:network`
       而不是被 Windows 的文件占用错误顶掉。
    2. **正文内可控截止**（`_pump`）：`read1` + 每次读前重设 socket 超时。
    """
    deadline = time.monotonic() + DOWNLOAD_BUDGET_SEC
    held: dict = {}

    def _close_conn():
        conn = held.get("conn")
        if conn is not None:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                # 关 socket 对象不一定唤醒另一个线程里阻塞的 `recv`；
                # `shutdown` 才会让对方立刻收到断开（Windows 上尤其如此）。
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:        # noqa: BLE001
                    pass
                try:
                    sock.close()
                except Exception:        # noqa: BLE001
                    pass
            try:
                conn.close()
            except Exception:            # noqa: BLE001
                pass

    def release():
        """超时收尾：先让连接**断开**（shutdown），再放文件。"""
        _close_conn()
        resp = held.get("resp")
        if resp is not None:
            try:
                resp.close()
            except Exception:            # noqa: BLE001
                pass
        fh = held.get("fh")
        if fh is not None:
            try:
                fh.close()
            except Exception:            # noqa: BLE001
                pass

    def body():
        resp = _open(url, deadline, held)
        held["resp"] = resp
        try:
            with open(dst, "wb") as fh:
                held["fh"] = fh
                _pump(resp, fh, deadline, what="下载")
        finally:
            # 正常结束也要放资源（复查指出原来只在超时路径收，成功路径靠 GC）
            try:
                resp.close()
            except Exception:            # noqa: BLE001
                pass
            _close_conn()

    _with_deadline(body, deadline, "下载", release=release)


def _fetch_text(url: str) -> str:
    """抓一小段文本（上游 `.sha256`）。同款两层时间约束，见 `_download`。"""
    deadline = time.monotonic() + DOWNLOAD_BUDGET_SEC
    held: dict = {}

    def release():
        conn = held.get("conn")
        if conn is not None:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:        # noqa: BLE001
                    pass
                try:
                    sock.close()
                except Exception:        # noqa: BLE001
                    pass
            try:
                conn.close()
            except Exception:            # noqa: BLE001
                pass
        resp = held.get("resp")
        if resp is not None:
            try:
                resp.close()
            except Exception:            # noqa: BLE001
                pass

    def body() -> str:
        resp = _open(url, deadline, held)
        held["resp"] = resp
        try:
            buf = io.BytesIO()
            _pump(resp, buf, deadline, what="抓取校验值")
            return buf.getvalue().decode("utf-8", "replace")
        finally:
            try:
                resp.close()
            except Exception:            # noqa: BLE001
                pass
            conn = held.get("conn")
            if conn is not None:
                try:
                    conn.close()
                except Exception:        # noqa: BLE001
                    pass

    return _with_deadline(body, deadline, "抓取校验值", release=release)


def _upstream_zip_sha(url: str) -> str:
    """抓上游 .sha256 文件，取出其中的 64 位十六进制（兼容"hash 文件名"两种排版）。"""
    text = _fetch_text(url)
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    return m.group(1).upper() if m else ""


def _atomic_place(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


# ---------------------------------------------------------------- 主流程
def fetch_one(entry: dict, dest: Path, *, allow_drift: bool, only_zip_cache: dict) -> str:
    """返回状态串：ok / skipped / missing-url / drift-accepted / FAIL:<原因>。

    zip 缓存保存为 bytes，而不是 TemporaryDirectory 内的路径；同一轮处理
    ffmpeg 与 ffprobe 时，前一个条目结束后临时目录已清理，路径缓存会失效。
    """
    rel = entry["rel"]
    want = entry["sha256"].upper()
    target = dest / rel

    if target.is_file():
        if sha256_file(target) == want:
            return f"skipped（已存在且 sha256 相符）"
        print(f"  ! 已存在但 sha256 不符，重新获取：{rel}")

    kind = entry["kind"]
    with tempfile.TemporaryDirectory(prefix="loom-vendor-",
                                     ignore_cleanup_errors=True) as td:
        td_path = Path(td)

        if kind == "direct":
            url = (entry.get("url") or "").strip()
            if not url:
                print(f"  ✗ {rel}：清单里没有 URL —— 这个文件需要我们自己发布或本地导出。")
                print(f"    {README_HINT}")
                return "missing-url"
            part = td_path / "payload.part"
            print(f"  ↓ {rel}  （{entry['size'] / 1e6:.0f} MB）")
            try:
                _download(url, part)
            except Exception as exc:            # noqa: BLE001 - 网络不可用不吐裸栈
                print(f"  ✗ 下载失败：{type(exc).__name__}: {exc}")
                print(f"    {README_HINT}")
                return "FAIL:network"
            got = sha256_file(part)
            if got != want:
                print(f"  ✗ sha256 不符\n      期望 {want}\n      实际 {got}")
                return "FAIL:sha256"
            _atomic_place(part, target)
            return "ok"

        # kind == "zip"：同一个 zip 下多个 member，复用缓存避免重复下载
        zip_url = entry["zip_url"]
        # 负缓存：同一轮里同一个 zip 已经下载失败过，就别为下一个目标再等一次超时。
        # 实测（2026-11-11 真实网络）：断网时 ffmpeg 与 ffprobe 各等满一次 timeout=60，
        # 用户看到的是"卡两分钟"，而不是"网络不行"。
        prev_fail = only_zip_cache.get(("failed", zip_url))
        if prev_fail:
            print(f"  ✗ 同一轮里这个上游 zip 已经下载失败过（{prev_fail}），跳过重试")
            print(f"    {README_HINT}")
            return "FAIL:network"
        cache = only_zip_cache.get(zip_url)
        if cache is None:
            part = td_path / "upstream.zip"
            print(f"  ↓ 上游 zip（{rel} 用）")
            try:
                _download(zip_url, part)
            except Exception as exc:            # noqa: BLE001 - 同上
                print(f"  ✗ 下载失败：{type(exc).__name__}: {exc}")
                print(f"    {README_HINT}")
                only_zip_cache[("failed", zip_url)] = type(exc).__name__
                return "FAIL:network"
            # 官方校验值也要走网络：它的失败同样不能吐裸栈（与 payload 下载同等待遇）。
            try:
                official = (_upstream_zip_sha(entry["zip_sha256_url"])
                            if entry.get("zip_sha256_url") else "")
            except Exception as exc:            # noqa: BLE001
                print(f"  ✗ 取上游 sha256 失败：{type(exc).__name__}: {exc}")
                print(f"    {README_HINT}")
                only_zip_cache[("failed", zip_url)] = type(exc).__name__
                return "FAIL:network"
            got_zip = sha256_file(part)
            if not official:
                print("  ✗ 上游 zip 没有可解析的官方 sha256，停止（不跳过来源校验）")
                return "FAIL:zip-sha256-missing"
            if got_zip != official:
                print(f"  ✗ 上游 zip 校验失败（来源不可信，停止）\n"
                      f"      发布方 {official}\n      实际   {got_zip}")
                return "FAIL:zip-sha256"
            cache = part.read_bytes()
            only_zip_cache[zip_url] = cache
        else:
            print("  · 复用本次已下载的上游 zip")

        pattern = re.compile(entry["member_re"])
        # 上游给的可能根本不是 zip（CDN 错误页 / 半截下载 / 被劫持）。
        # BadZipFile 必须被当成"来源不可用"如实上报，而不是吐一个裸 traceback。
        try:
            with zipfile.ZipFile(io.BytesIO(cache)) as zf:
                names = [n for n in zf.namelist() if pattern.search(n)]
                if not names:
                    print(f"  ✗ 上游 zip 里找不到匹配 {entry['member_re']} 的成员")
                    return "FAIL:member-missing"
                data = zf.read(names[0])
        except zipfile.BadZipFile:
            print("  ✗ 上游内容不是合法 zip（可能是错误页或半截下载），停止")
            print(f"    {README_HINT}")
            return "FAIL:not-a-zip"
        except Exception as exc:            # noqa: BLE001 - 解压异常同样不许裸栈
            print(f"  ✗ 解压失败：{type(exc).__name__}: {exc}")
            print(f"    {README_HINT}")
            return "FAIL:unzip"
        got = hashlib.sha256(data).hexdigest().upper()
        if got != want:
            msg = (f"  ! 上游构建与我们要分发的这份不同（上游会轮换）\n"
                   f"      我们分发 {want}\n"
                   f"      当前上游 {got}")
            if not allow_drift:
                print(msg)
                print("      未落位。若确认接受上游当前构建，请加 --allow-upstream-drift 重跑。")
                return "FAIL:exe-sha256"
            print(msg)
            print("      --allow-upstream-drift 已开启：按上游当前构建落位（已在报告中记录该偏差）。")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return "ok" if got == want else "drift-accepted"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="loom.vendor.fetch_vendor", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default="", help="落点（默认 ~/.loom/vendor）")
    ap.add_argument("--only", default="", help="只处理名字里含该子串的项（如 ffmpeg / model）")
    ap.add_argument("--allow-upstream-drift", action="store_true",
                    help="上游构建已轮换时，接受其当前产物（会记录偏差）")
    args = ap.parse_args(argv)

    dest = Path(args.dest).expanduser() if args.dest else default_dest()
    print(f"落点：{dest}")
    print(f"清单：{len(MANIFEST)} 项\n")

    zip_cache: dict = {}
    bad = 0
    for entry in MANIFEST:
        if args.only and args.only not in entry["rel"]:
            continue
        print(f"[{entry['rel']}] {entry['what']}")
        status = fetch_one(entry, dest, allow_drift=args.allow_upstream_drift,
                           only_zip_cache=zip_cache)
        if status == "missing-url":
            bad += 1
            print("  状态：需要发布/导出后才有下载地址（不影响其它文件）")
        elif status.startswith("FAIL"):
            bad += 1
            print(f"  状态：失败（{status}）")
        else:
            print(f"  状态：{status}")
        print()

    print("完成。")
    if bad:
        print(f"{bad} 项未就绪；按上面的提示处理（{README_HINT}）。")
        return 1
    print("三件齐备即可用；语义检索与画面提取会自动发现它们。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
