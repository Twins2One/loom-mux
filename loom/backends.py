"""可插拔后端:一个 complete(system, user) -> str 的协议 + 实现 + 供应商路由表。

两类后端:
- OpenAI 兼容(HTTP):DeepSeek 是锁死 base_url 的预设;openai_compat 是用户自填 base_url 的通用口子
  (接智谱GLM / Moonshot / 通义Qwen / 硅基流动 等),两者共用同一个 OpenAICompatBackend。
- CLI:Claude 走 `claude -p`、Codex 走 `codex exec`,shell 到本机客户端、复用其登录态,Loom 不碰 key。

路由唯一真相是 PROVIDERS:前端下拉、后端构造、模型校验全从它派生——避免「模型名写死成白名单、
厂商一改名就过时再炸一遍」(用户实报:DeepSeek 改名 v4-flash/v4-pro 把旧默认 deepseek-chat 顶没了)。

所有 complete 在【正常返回】也会校验非空:空响应一律 raise(带 code),绝不把空串往下传——
这是用户实报「换模型 → learn 学到空 → 指纹被擦」链条上后端这一环的闸(写盘那一环在 guard.py)。
"""

from __future__ import annotations
from .concurrency import configure as _gate_configure, gated

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Protocol

from .config import Config
from .errors import render

# 流式回调:每收到一小段生成文本就调一次(让前端"看着它一句句写出来")。
OnChunk = Callable[[str], None]


# ----------------------------- 供应商路由表(唯一真相) -----------------------------
# kind: "openai"=OpenAI 兼容 HTTP;"cli"=shell 到本机客户端。
# models 只是【预设建议】,前端是可编辑下拉 + 「拉取可用模型」实时拉真实列表,名字怎么变都不过时。
# 行为字段(S7,缺省即默认值,新接供应商只改数据不改代码):
#   thinking_budget: True=思考型,max_tokens 给底线+思考余量(缺省 False,见 _budget_tokens)
#   error_family: "deepseek"=异常映射/空响应走 DeepSeek 专属错误目录(缺省 "generic")
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek", "kind": "openai",
        "base_url": "https://api.deepseek.com", "base_url_locked": True,
        "key_env": "DEEPSEEK_API_KEY", "needs_key": True,
        "default_model": "deepseek-v4-pro",
        "thinking_budget": True, "error_family": "deepseek",
        # 现役 V4 都是【思考型】模型(deepseek-chat 非思考别名已停用);backend 已为思考预留 token 预算
        # (见 OpenAICompatBackend.complete)。默认 pro(更强);更快更省可选 flash。
        "models": [
            {"id": "deepseek-v4-pro", "label": "V4 Pro · 更强(默认)"},
            {"id": "deepseek-v4-flash", "label": "V4 Flash · 更快·更省"},
        ],
        "can_list_models": True,
    },
    # ---- 国产一等预设(全走 OpenAI 兼容形态,base_url 锁死;模型列表刻意留空:----
    # ---- 点「拉取可用模型」拉当前真实型号,模型名绝不硬编码进白名单(会过时)) ----
    "zhipu": {
        "label": "智谱 GLM", "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4", "base_url_locked": True,
        "key_env": "LOOM_ZHIPU_KEY", "needs_key": True,
        "default_model": "", "models": [],
        "hint": "key 在 open.bigmodel.cn 申请;填好后点「拉取可用模型」选一个",
        "can_list_models": True,
    },
    "moonshot": {
        "label": "Moonshot Kimi", "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1", "base_url_locked": True,
        "key_env": "LOOM_MOONSHOT_KEY", "needs_key": True,
        "default_model": "", "models": [],
        "hint": "key 在 platform.moonshot.cn 申请;填好后点「拉取可用模型」选一个",
        "can_list_models": True,
    },
    "qwen": {
        "label": "通义 Qwen", "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "base_url_locked": True,
        "key_env": "LOOM_QWEN_KEY", "needs_key": True,
        "default_model": "", "models": [],
        "hint": "key 在 bailian.console.aliyun.com(百炼)申请;填好后点「拉取可用模型」选一个",
        "can_list_models": True,
    },
    "doubao": {
        "label": "豆包(火山方舟)", "kind": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3", "base_url_locked": True,
        "key_env": "LOOM_DOUBAO_KEY", "needs_key": True,
        "default_model": "", "models": [],
        "hint": "key 在 console.volcengine.com(方舟)申请;模型填接入点或型号名,可先「拉取可用模型」",
        "can_list_models": True,
    },
    "siliconflow": {
        "label": "硅基流动", "kind": "openai",
        "base_url": "https://api.siliconflow.cn/v1", "base_url_locked": True,
        "key_env": "LOOM_SILICONFLOW_KEY", "needs_key": True,
        "default_model": "", "models": [],
        "hint": "key 在 cloud.siliconflow.cn 申请(聚合多家开源模型);填好后点「拉取可用模型」",
        "can_list_models": True,
    },
    "claude": {
        "label": "Claude Code", "kind": "cli",
        "needs_key": False, "default_model": "sonnet",
        "models": [
            {"id": "sonnet", "label": "Sonnet · 均衡(默认,别名自动跟最新)"},
            {"id": "opus", "label": "Opus · 最强"},
            {"id": "haiku", "label": "Haiku · 最快最省"},
        ],
        "can_list_models": False,
    },
    "codex": {
        "label": "Codex(GPT)", "kind": "cli",
        "needs_key": False, "default_model": "",
        "models": [
            {"id": "", "label": "默认(codex 自己选,推荐)"},
            {"id": "gpt-5.3-codex", "label": "GPT-5.3-Codex · 编码最强"},
            {"id": "gpt-5.5", "label": "GPT-5.5 · 通用最强"},
        ],
        "can_list_models": False,
    },
    "openai_compat": {
        "label": "OpenAI 兼容(自定义)", "kind": "openai",
        "base_url": "", "base_url_locked": False,
        "key_env": "LOOM_OPENAI_COMPAT_KEY", "needs_key": True,
        "default_model": "",
        "models": [],   # 用户自填 / 点「拉取可用模型」实时拉
        "hint": "上面没列到的任何 OpenAI 兼容供应商:自填它的 base_url + key",
        "can_list_models": True,
    },
}


def provider_catalog() -> list[dict]:
    """给前端的供应商清单(下拉/默认值/预设模型全从这派生)。"""
    out = []
    for pid, p in PROVIDERS.items():
        out.append({
            "id": pid, "label": p["label"], "kind": p["kind"],
            "needs_key": p["needs_key"], "default_model": p["default_model"],
            "base_url": p.get("base_url", ""), "base_url_locked": p.get("base_url_locked", True),
            "models": p.get("models", []), "hint": p.get("hint", ""),
            "can_list_models": p.get("can_list_models", False),
        })
    return out


def validate_model(provider: str, model: str) -> str | None:
    """启发式软校验(不联网):明显填错就返回一句人话提示,否则 None 放行。只警告、绝不阻断保存。"""
    model = (model or "").strip()
    provider = (provider or "").lower()
    if not model or provider not in PROVIDERS:
        return None
    if provider == "deepseek" and "deepseek" not in model.lower():
        return (f"模型名「{model}」不像 DeepSeek 的模型——DeepSeek 现在是 deepseek-v4-flash / deepseek-v4-pro。"
                f"如果你本来想用别家的「{model}」,把上面的供应商切到「OpenAI 兼容(自定义)」再填它的 base_url。")
    return None


class LoomBackendError(RuntimeError):
    """后端层的友好错误(CLI 会把它打成多段提示,而不是抛栈)。

    可选 code 指向 errors.py 的错误目录;不传时退化成普通字符串错误,向后兼容。
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _deepseek_error(e: Exception) -> LoomBackendError:
    """把 DeepSeek/OpenAI SDK 的异常映射成可操作的友好错误(鉴权/余额/限流/通用)。"""
    msg = str(e)
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "response", None), "status_code", None)
    low = msg.lower()
    if status == 401 or "authentication" in low or "invalid api key" in low:
        code = "deepseek_auth_failed"
    elif status == 402 or "insufficient balance" in low or "余额" in msg:
        code = "deepseek_insufficient_balance"
    elif status == 429 or "rate limit" in low or "too many requests" in low:
        code = "deepseek_rate_limited"
    elif isinstance(e, TimeoutError) or "timed out" in low or "timeout" in low:
        code = "deepseek_timeout"
    else:
        return LoomBackendError(render("deepseek_call_failed", detail=msg), code="deepseek_call_failed")
    return LoomBackendError(render(code), code=code)


def _openai_compat_error(e: Exception) -> LoomBackendError:
    """通用 OpenAI 兼容供应商的异常映射(没有 DeepSeek 那么细,够用就好)。"""
    msg = str(e)
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "response", None), "status_code", None)
    low = msg.lower()
    # 能走到这里说明 key 就在 env 里、请求真发出去了,所以「被供应商拒」≠ 「没填 key」——
    # 两者下一步动作完全不同(去后台查 token / 去顶栏补填),不能共用一个码。
    if status in (401, 403) or "authentication" in low or "invalid api key" in low or "invalid token" in low:
        return LoomBackendError(render("openai_compat_key_rejected", detail=msg),
                                code="openai_compat_key_rejected")
    if isinstance(e, TimeoutError) or "timed out" in low or "timeout" in low:
        return LoomBackendError(render("model_timeout"), code="model_timeout")
    return LoomBackendError(render("model_call_failed", detail=msg), code="model_call_failed")


def accepts_kwarg(backend, name: str) -> bool:
    """`backend.complete` 是否声明了 `name` 形参(或用 `**kwargs` 兜底吃掉任意关键字)。

    两条 agent 通道(伙伴对话 / 写章循环)都要按需传后加的关键字参数(`agent_mode` 解 CLI
    反 agent 护栏、`on_reasoning` 收思考型后端的思维链)——但这些参数都是 Backend Protocol
    定稿之后才补的,测试里的极简假后端先于它们存在、没声明,硬传会直接 `TypeError` 炸掉一批
    既有测试。探测后按需传:声明了的(真实后端 + 新写的假后端)吃得到;没声明的旧假后端 /
    无此能力的后端优雅退化成不传,行为与改动前完全一致。

    这是「不进 Protocol、只对声明者传」的通用套路(刻意不动 Backend Protocol)。
    """
    import inspect
    try:
        params = inspect.signature(backend.complete).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return name in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


class Backend(Protocol):
    def complete(self, system: str, user: str, *, max_chars: int | None = None,
                 on_chunk: OnChunk | None = None, agent_mode: bool = False) -> str:
        """生成。给了 on_chunk 的后端会边生成边回调(流式);不支持流式的后端忽略它、结尾一次性返回。

        agent_mode(书房伙伴对话通道专用,spec 2026-07-16-navigator-agent-design.md §3):
        True=解除 CLI 后端(claude/codex)的反 agent 护栏——允许按文本协议输出至多一个「用:」
        工具块、允许反问;五工序(设定/大纲/写手/编辑/润色/领航员出题)仍传默认 False,
        护栏原样生效。HTTP 类后端(OpenAI 兼容)和 DemoBackend 没有「护栏」这个概念,
        接受这个参数但忽略——纯粹是为了让 Backend Protocol 的签名在所有实现间保持一致。
        """
        ...


# 思考型后端的输出预算:一个常数,不再按 max_chars 现算(依据见 _budget_tokens 的 docstring)。
_THINK_BUDGET = 65536


def _budget_tokens(provider: str, max_chars: int | None) -> int:
    """max_tokens 预算——只防截断,【不是】篇幅刹车(篇幅靠 prompt 字数指令,见 agents._length_hint)。

    换算:DeepSeek 中文实测约 0.6 token/字(旧注释的 ~1.6 是按老式 tokenizer 高估的近 3 倍);
    base = max_chars*2.2 是按旧换算定的,如今相当于 ~3.5 倍余量——故意不缩,截断永远比宽裕更贵。
    DeepSeek V4(v4-flash/v4-pro)是【思考型】:reasoning 也吃 max_tokens,预算不够就在思考中途
    被 `finish_reason=length` 腰斩、content 空着回来(deepseek_empty_response 的真因)。

    **思考型不再按 max_chars 现算,直接给常数 65536**。三条真机证据(v4-flash,
    样例书《重生记忆》第 3 章的真实 prompt,非流式重放):

        写手棒(prompt 5964 字):  6736→length 思考9652字 正文0 | 12288→length 思考17289字 正文0
                                16384→stop 思考10057字 正文1102 | 32768→stop 思考19489字 正文1055
        润色师棒(prompt 5308 字):16384→length 思考【43433】字 正文0 | 32768→stop 思考13084字 正文1332
        上限探测:               max_tokens=65536 与 131072 都正常受理

    三条结论:
    ① **思考长度方差极大**——同一本书不同棒,实测跨度 9.6k~43.4k 字。任何「刚好够」的算式
       都会在某一棒上翻车,所以不算了,直接给一个装得下最坏情况的常数。
    ② 旧注释「封顶 8192(DeepSeek 接受的上限)」**对 V4 是错的**。
    ③ **抬上限零成本**:按实际产出的 token 计费——上面 65536 那次只出了 31 个 token。
       max_tokens 是纯上限,不是预付额度。

    「章节超长」绝不能靠调小这里治——会拦腰截断 + 复发思考型空响应(2.0.1 的老坑);篇幅靠
    prompt 字数指令(见 artifacts 的提交契约 / agents._length_hint)。其它 OpenAI 兼容供应商
    维持原样——它们各家模型输出上限不同,贸然抬高 max_tokens 可能被拒。

    按 PROVIDERS 的 thinking_budget 字段分发(S7):openai_compat 接新思考型时只改数据。"""
    thinking = PROVIDERS.get(provider, {}).get("thinking_budget", False)
    if not max_chars:
        return _THINK_BUDGET if thinking else 2048
    if thinking:
        return _THINK_BUDGET   # 常数:思考长度方差太大,任何按 max_chars 现算的公式都会翻车
    return int(max_chars * 2.2)



def _http_transport():
    """返回 openai 用的传输层模块:1.x/2.x 是 httpx,3.0 起换成 httpx2。

    两者的 Timeout(总秒数, connect=, read=) 构造签名一致(实测 httpx2 2.10.0),
    我们只用这一个 API,所以取到哪个都能用。
    单独成函数、单独 except:缺的是【传输层】不是 openai 本身——把它混进 openai 的
    except 会报出「缺少 openai」这种假话(openai 明明装着),就这么
    在 CI 上误导了半天。
    """
    for name in ("httpx", "httpx2"):
        try:
            return __import__(name)
        except ModuleNotFoundError:
            continue
    raise LoomBackendError(render("httpx_not_installed"), code="httpx_not_installed")


# ── 纯标准库的 openai 替身 ──────────────────────────────────────
# 为什么需要它：本地/自建环境常常只有 fastapi/uvicorn，没装 openai 库，于是整套写章起不来。
# Loom 对 SDK 的依赖其实只有 `client.chat.completions.create(...)`、流式 delta 和 usage，
# 这三样用 urllib + json 就能做。装上 SDK 时仍走 SDK（行为一字不变），没装才落到这里。
class _LiteHTTPError(RuntimeError):
    """带 status_code 的 HTTP 错误，让 `_openai_compat_error` 能认出 401/超时。"""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"HTTP {status_code}: {body[:600]}")
        self.status_code = status_code


class _NS:
    """极简命名空间：让替身返回的对象能按 SDK 的属性链访问（resp.choices[0].message.content）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _usage_obj(u):
    if not u:
        return None
    return _NS(prompt_tokens=u.get("prompt_tokens"),
               completion_tokens=u.get("completion_tokens"),
               total_tokens=u.get("total_tokens"))


def _resp_obj(obj):
    choices = []
    for c in obj.get("choices") or []:
        msg = c.get("message") or {}
        raw_tcs = msg.get("tool_calls") or []
        tool_calls = None
        if raw_tcs:
            tool_calls = []
            for t in raw_tcs:
                fn = t.get("function") or {}
                tool_calls.append(_NS(
                    id=t.get("id"), type=t.get("type") or "function",
                    function=_NS(name=fn.get("name"), arguments=fn.get("arguments")),
                ))
        choices.append(_NS(
            message=_NS(content=msg.get("content"),
                        reasoning_content=msg.get("reasoning_content"),
                        tool_calls=tool_calls),
            finish_reason=c.get("finish_reason"),
        ))
    return _NS(choices=choices, usage=_usage_obj(obj.get("usage")))


def _chunk_obj(obj):
    choices = []
    for c in obj.get("choices") or []:
        d = c.get("delta") or {}
        # finish_reason 必须带出来(交叉审查):旧版把它丢了,于是流式路径上
        # 「这一棒是不是被 max_tokens 腰斩」这个信号在解析层就消失了——本地 Qwen 走的正是
        # 这条 lite 路径,所以真机上永远看不到 length。
        choices.append(_NS(delta=_NS(content=d.get("content"),
                                     reasoning_content=d.get("reasoning_content")),
                           finish_reason=c.get("finish_reason")))
    return _NS(choices=choices, usage=_usage_obj(obj.get("usage")))


def _iter_chunks(resp):
    """逐行解析 SSE：`data: {...}` / `data: [DONE]`。"""
    import json as _json
    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                yield _chunk_obj(_json.loads(body))
            except ValueError:
                continue          # 不完整帧/心跳（有的端点会发 `: ping`）直接跳过
    finally:
        try:
            resp.close()
        except Exception:
            pass


class _LiteCompletions:
    """`client.chat.completions` 的标准库实现：只做 Loom 用到的那一件事。"""

    def __init__(self, api_key: str, base_url: str, timeout: float):
        self._key = api_key
        self._base = (base_url or "").rstrip("/")
        self._timeout = timeout

    def create(self, *, model, messages, max_tokens=None, temperature=0.9,
               stream=False, timeout=None, tools=None, tool_choice=None, **_ignored):
        import json as _json
        import urllib.error
        import urllib.request

        payload = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._base + "/chat/completions", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._key}"},
        )
        to = timeout if isinstance(timeout, (int, float)) else self._timeout
        try:
            resp = urllib.request.urlopen(req, timeout=to)
        except urllib.error.HTTPError as e:
            raise _LiteHTTPError(e.code, e.read().decode("utf-8", "replace")) from e
        except urllib.error.URLError as e:
            raise _LiteHTTPError(0, f"连接失败：{e.reason}") from e
        except TimeoutError as e:
            raise _LiteHTTPError(0, f"超时：{e}") from e
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            # 审查要求:连到一半被掐断（网络抖动 / 服务端重启）要变成明确错误，
            # 不能让它冒成裸 OSError 把上层写章流程带崩。
            raise _LiteHTTPError(0, f"连接中断：{type(e).__name__}: {e}") from e
        if not stream:
            return _resp_obj(_json.loads(resp.read().decode("utf-8", "replace")))
        return _iter_chunks(resp)


class _LiteOpenAI:
    """`OpenAI(...)` 的最小替身：只暴露 `.chat.completions`。

    ⚠️ **与官方 SDK 不等价**，已知差异（审查要求写明，防后人误以为行为一致）：
    1. **超时是单值**，不支持 connect / read 分离（SDK 用 httpx.Timeout 分两段）；
       流式思考期统一按 300s 算，不区分「连接慢」和「模型思考久」。
    2. **没有重试**：SDK 会重试部分 5xx / 连接错误，这里一次失败就直接抛。
       本地开发可接受；将来若用于远程推理，必须补重试。
    3. **错误对象是 `_LiteHTTPError`**（带 `status_code`），不是 SDK 的 APIError 家族；
       现有 `_openai_compat_error` 靠 `status_code` + 错误文本认 401/超时，够用。
    4. SSE 解析用 `line.strip()`，因此 `\\n` 与 `\\r\\n` 两种行尾都能吃。
    5. 连接中途断开（ConnectionResetError / BrokenPipeError / 其它 OSError）统一包成
       `_LiteHTTPError(0, "连接中断…")`，不让裸 OSError 冒到写章流程里。
    """

    def __init__(self, api_key: str, base_url: str, timeout: float = 300.0):
        self.chat = _NS(completions=_LiteCompletions(api_key, base_url, timeout))


class OpenAICompatBackend:
    """OpenAI 兼容 HTTP 后端:DeepSeek(锁死 base_url)和 openai_compat(自填 base_url)共用。"""

    def __init__(self, config: Config, provider: str) -> None:
        spec = PROVIDERS[provider]
        self.provider = provider
        self.model = (config.model or "").strip() or spec["default_model"]
        # base_url:锁死的取注册表,自定义的取 config.base_url
        base_url = spec["base_url"] if spec.get("base_url_locked") else ((config.base_url or "").strip() or spec.get("base_url", ""))
        family = spec.get("error_family", "generic")   # 异常映射按注册表字段分发(S7)
        self._empty_code = "deepseek_empty_response" if family == "deepseek" else "model_empty_response"
        self._map = _deepseek_error if family == "deepseek" else _openai_compat_error
        self.last_usage: dict | None = None   # 最近一次调用的 usage:{prompt_tokens, completion_tokens, total_tokens}
        # 最近一次调用的 finish_reason("stop"/"length"/...)。存在的理由:长度只是"撞没撞上限"
        # 的间接指标,finish_reason 才是权威信号——审计与自动补预算都要靠它(见 _complete_nonstream)。
        self.last_finish: str = ""

        if provider != "deepseek" and not self.model:
            raise LoomBackendError(render("model_name_missing"), code="model_name_missing")
        if not base_url:
            raise LoomBackendError(render("openai_compat_base_url_missing"), code="openai_compat_base_url_missing")
        api_key = os.environ.get(spec["key_env"])
        if not api_key:
            code = "deepseek_key_missing" if provider == "deepseek" else "openai_compat_key_missing"
            raise LoomBackendError(render(code), code=code)
        # 延迟 import,免得没装 openai 时连 --help 都跑不起来。
        # 真机实测:本机 Python 只有 fastapi/uvicorn、**没装 openai**,
        # 于是整套写章起不来。这里改成:SDK 在就照旧用 SDK(行为一字不变);
        # 不在就落到 `_LiteOpenAI`——纯 urllib 实现,形状与 SDK 对齐,不再要求装包。
        try:
            from openai import OpenAI
        except ModuleNotFoundError:
            OpenAI = None  # noqa: N806 - 有意复用同名变量表示"不可用"
        if OpenAI is not None:
            httpx = _http_transport()
            # 显式超时(SDK 默认 600s,挂了要等 10 分钟才有反馈):连接 10s / 其余 120s;
            # 流式的 read 按 chunk 间隔计,思考型模型思考期可能久无正文,放宽到 300s。
            self._client = OpenAI(api_key=api_key, base_url=base_url,
                                  timeout=httpx.Timeout(120.0, connect=10.0))
            self._stream_timeout = httpx.Timeout(120.0, connect=10.0, read=300.0)
        else:
            # 标准库替身:本地模型思考期同样可能久无正文,超时按 300s 给。
            self._client = _LiteOpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
            self._stream_timeout = 300.0

    def _empty(self) -> LoomBackendError:
        return LoomBackendError(render(self._empty_code, detail=f"model={self.model}"), code=self._empty_code)

    def _call_once(self, messages: list[dict], max_tokens: int) -> tuple[str, str, str]:
        """一次非流式调用,回 (正文, finish_reason, 思考文本)。同时记录 usage。"""
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages, max_tokens=max_tokens, temperature=0.9,
        )
        ch = resp.choices[0]
        # usage 记录(优先调真实数据)
        if hasattr(resp, "usage") and resp.usage is not None:
            u = resp.usage
            self.last_usage = {
                "prompt_tokens": getattr(u, "prompt_tokens", None),
                "completion_tokens": getattr(u, "completion_tokens", None),
                "total_tokens": getattr(u, "total_tokens", None),
            }
        finish = getattr(ch, "finish_reason", None) or ""
        self.last_finish = finish          # 账本/守卫要读,别只回给调用方
        return ((ch.message.content or "").strip(), finish,
                (getattr(ch.message, "reasoning_content", None) or "").strip())

    def _complete_nonstream(self, messages: list[dict], max_tokens: int) -> str:
        """非流式取全文,带两道空响应兜底——只在【正文为空】时才动,正常路径零额外开销。

        用户实报(自建 OpenAI 兼容端点接本地思考型模型):200 回来 content 是空的。
        两种成因必须分开处理,合并会把思维链当正文交上去:

        ① `finish_reason == "length"`:reasoning 也吃 max_tokens,思考没说完就被腰斩,正文一个字
           还没开始写。这时 reasoning_content 里躺的是【半截思维链】——拿它当正文入库,等于把
           「嗯,这一章应该先写验伤……」写进作者的书里,比报错更糟。正确动作是【补一次满预算】。
           不给 openai_compat 直接挂 thinking_budget,是因为那会让所有自定义端点都恒发 65536,
           上限低的模型(不少是 4096/8192)会当场 400——按需重试只在真被腰斩时多花这一次。
        ② `finish_reason == "stop"` 且正文空、思考非空:模型把整段回答放错字段了(部分本地部署
           的 GLM/Qwen 是这个行为)。这时 reasoning_content 就是正文,采信它。
        """
        text, finish, think = self._call_once(messages, max_tokens)
        # 交叉审查纠正:旧版**只在正文为空时**才补预算重跑。正文非空但被腰斩
        # (写到一半撞 max_tokens)会被当成"完整产出"往下传。真机代价见
        # docs/流水线诊断-v2-审查.md §5.5:责任编辑交了 2424 字自述分析、末句断在"林砚",
        # 下游照收。截断信号(finish_reason)比"空不空"可靠得多。
        if text and finish != "length":
            return text
        if finish == "length" and max_tokens < _THINK_BUDGET:
            try:
                text2, finish2, think2 = self._call_once(messages, _THINK_BUDGET)   # ① 补满预算重来
            except Exception as e:      # noqa: BLE001 - 抬预算被端点拒 ≠ 这一棒完蛋
                # 有的 OpenAI 兼容端点输出上限低(Qwen/GLM 常见 4096/8192),给 65536 会 400。
                # 这时保留半截正文往下走:截断有损,崩掉更贵;交付契约检查(agents._missing_edit_note)
                # 会在该拦的地方拦住。
                print(f"[截断补预算] 抬高 max_tokens 被端点拒({type(e).__name__}: {e}),保留半截正文",
                      file=__import__("sys").stderr)
                text2, finish2, think2 = "", finish, think
            if text2:
                return text2
            # 补预算后仍空:退回半截,绝不留空(空串会被上层当成模型故障)
            text, finish, think = text2 or text, finish2, think2 or think
        if text:
            return text
        if think and finish != "length":       # ② 正文放错字段
            return think
        return ""

    @gated
    def complete(self, system: str, user: str, *, max_chars: int | None = None,
                 on_chunk: OnChunk | None = None, on_reasoning: OnChunk | None = None,
                 agent_mode: bool = False) -> str:
        # agent_mode 对 HTTP 后端无意义(没有 CLI 反 agent 护栏这回事)——接受、忽略,只为对齐协议签名。
        # on_reasoning(v2 思考层):思考型模型(DeepSeek)的思维链走独立字段 reasoning_content,
        # 与正文 content 分开流式;给了就 relay 思考流(不进正文 parts、不落盘,纯 UI 灰字)。
        max_tokens = _budget_tokens(self.provider, max_chars)
        # 2026-11-08（图片识别修复）：user 允许是 list（多模态 content，text + image_url 部分）
        # ——书房取稿把采样帧图直接附进 user 消息。str 仍原样（零行为变化）。
        user_content: object = user if isinstance(user, list) else user
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
        finish_stream = None          # 流式路径下本条调用的 finish_reason(None = 没走流式)
        try:
            if on_chunk is not None:  # 流式:边写边回调
                stream = self._client.chat.completions.create(
                    model=self.model, messages=messages, max_tokens=max_tokens,
                    temperature=0.9, stream=True, timeout=self._stream_timeout,
                )
                parts, buf, rbuf = [], "", ""
                finish_stream = ""
                ev = None                 # 空流时下面取 usage 会用到它,先置空(旧版这里会 UnboundLocalError)
                for ev in stream:
                    if not ev.choices:
                        continue
                    # finish_reason 只在末帧给;中途帧是 None,取到就留着(别用 or 覆盖成空)
                    finish_stream = getattr(ev.choices[0], "finish_reason", None) or finish_stream
                    d = ev.choices[0].delta
                    # 思考流:必须在 content 空守卫【之前】读——思考帧 content=None,否则整帧被下面
                    # continue 丢掉。getattr 兜底:非思考型供应商的 delta 没这属性(直接取会 AttributeError)。
                    if on_reasoning is not None:
                        rc = getattr(d, "reasoning_content", None)
                        if rc:
                            rbuf += rc
                            if len(rbuf) >= 12 or rbuf[-1] in "。\n!?!?…,，、":  # 攒几字再发,别一 token 一次(免前端抖)
                                on_reasoning(rbuf)
                                rbuf = ""
                    delta = d.content or ""
                    if not delta:
                        continue
                    parts.append(delta)
                    buf += delta
                    if len(buf) >= 8 or buf[-1] in "。\n!?!?…":  # 攒几字/到句末再发,别一 token 一行
                        on_chunk(buf)
                        buf = ""
                if rbuf and on_reasoning is not None:
                    on_reasoning(rbuf)
                if buf:
                    on_chunk(buf)
                # 流式结束:从最后帧取 usage(优先调真实数据)
                if ev is not None and hasattr(ev, "usage") and ev.usage is not None:
                    u = ev.usage
                    self.last_usage = {
                        "prompt_tokens": getattr(u, "prompt_tokens", None),
                        "completion_tokens": getattr(u, "completion_tokens", None),
                        "total_tokens": getattr(u, "total_tokens", None),
                    }
                text = "".join(parts).strip()
                if not text:
                    # 流式空响应:有的端点流里只发 reasoning、正文帧一条不发。回退非流式重取一次
                    # (走上面那两道兜底);拿到了就补发给前端,别让作者停在一片空白上。
                    text = self._complete_nonstream(messages, max_tokens)
                    if text:
                        on_chunk(text)
                    finish_stream = self.last_finish
                elif finish_stream == "length" and max_tokens < _THINK_BUDGET:
                    # 半截正文(交叉审查):正文非空不等于写完了。旧的流式路径从不看
                    # finish_reason,于是"写了 2424 字断在句中"会被当作完整产出交付。
                    # 这里补一次满预算重来;走非流式,免得把同一段正文往前端再刷一遍。
                    try:
                        fuller = self._complete_nonstream(messages, _THINK_BUDGET)
                    except Exception as e:      # noqa: BLE001 - 抬预算被拒不该把这一棒搞崩
                        print(f"[截断补预算] 流式半截后补预算失败({type(e).__name__}: {e}),保留半截正文",
                              file=__import__("sys").stderr)
                        fuller = ""
                    if fuller:
                        text = fuller
                    finish_stream = self.last_finish
            else:
                text = self._complete_nonstream(messages, max_tokens)
                finish_stream = self.last_finish
        except Exception as e:  # 网络/限流/鉴权/余额 —— 映射成可操作的友好错误
            raise self._map(e) from e
        if finish_stream is not None:
            self.last_finish = finish_stream or self.last_finish
        if not text:  # 200 空响应(多半模型名不对)→ 报错,绝不把空串往下传去覆盖用户数据
            raise self._empty()
        return text


    @gated
    def complete_with_tools(self, messages: list[dict], tools: list[dict], *,
                            max_chars: int | None = None) -> dict:
        """function calling 版。

        返回 {"content": str, "tool_calls": [{"id","name","arguments"}]}。
        调用方用 `hasattr(backend, "complete_with_tools")` 探测：有就走结构化工具调用，
        没有就退回文本协议（`用:`/`TOOL:` 那套）。

        **为什么值得单独开一条路**：文本协议三次真机实测——模型要么把调用写进正文、
        要么参数只留第一行。function calling 下这些都不存在：模型不需要"写格式"，
        参数由服务端按 JSON Schema 约束，实测 content 也完整。
        """
        max_tokens = _budget_tokens(self.provider, max_chars)
        try:
            resp = self._client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=max_tokens,
                temperature=0.9, tools=tools, tool_choice="auto",
            )
        except Exception as e:      # noqa: BLE001 - 与 complete() 同一套友好错误映射
            raise self._map(e) from e
        ch = resp.choices[0]
        msg = ch.message
        self.last_finish = getattr(ch, "finish_reason", None) or ""
        if hasattr(resp, "usage") and resp.usage is not None:
            u = resp.usage
            self.last_usage = {
                "prompt_tokens": getattr(u, "prompt_tokens", None),
                "completion_tokens": getattr(u, "completion_tokens", None),
                "total_tokens": getattr(u, "total_tokens", None),
            }
        calls = []
        for t in (getattr(msg, "tool_calls", None) or []):
            fn = getattr(t, "function", None)
            calls.append({
                "id": getattr(t, "id", "") or "",
                "name": getattr(fn, "name", "") or "",
                "arguments": getattr(fn, "arguments", "") or "",
            })
        return {"content": (getattr(msg, "content", None) or "").strip(),
                "tool_calls": calls}


class ClaudeCodeBackend:
    """接 Claude Code:把 system+user 拼成 prompt 走 `claude -p` headless。"""

    def __init__(self, config: Config) -> None:
        if shutil.which("claude") is None:
            raise LoomBackendError(render("claude_not_found"), code="claude_not_found")
        # 让 loom.toml 的 model 生效;没填/填了别家(deepseek/gpt)默认值时退回 sonnet(写文质量好,禁工具后仍是一次性补全)
        m = (config.model or "").strip()
        bad = (not m) or ("deepseek" in m) or m.startswith("gpt")
        self.model = "sonnet" if bad else m

    # 护栏(五工序默认,agent_mode=False):逼 `claude -p` 当"纯文本补全",别当 Claude Code agent
    # (去找文件/反问/解释)
    _GUARD = (
        "[严格指令] 你是一个纯文本生成函数,不是助手、不是 agent。只输出要求的成品中文文本本身。"
        "禁止:调用或提及任何工具、查找或读取任何文件、反问、说明你在做什么、"
        "输出「我检查了目录」「请提供」之类的话。你需要的全部材料都在下面,直接用。"
    )
    # 护栏(书房伙伴对话变体,agent_mode=True;spec §3):伙伴协议本身要求它能输出一行
    # 「用:工具名」文本块(loom 解析执行,CLI 自己不碰真工具——真实读写仍锁死在
    # `--allowed-tools ""` 之后),也要能对着作者提一句反问(资料不够时问清楚,别替作者瞎编)。
    # 解除上面 _GUARD 里「禁止提及工具/禁止反问」这两条,其余「别自称助手、别写与协议无关的旁白」
    # 的克制仍然保留。
    _GUARD_AGENT = (
        "[严格指令] 你在扮演一位写作伙伴,通过纯文本对话交流——你说的每个字都是最终输出,"
        "不会再有人替你加工。资料不够时,可以向作者提一句反问,而不是替他瞎编。"
        "需要用工具时,按下面的协议格式输出一行「用:工具名」及参数;这只是文本协议,"
        "真正的文件读写由 loom 服务端执行,你自己没有、也不要尝试真的调用任何 shell/文件/网络能力。"
        "除此之外禁止输出「我是 AI」「我检查了目录」这类与协议无关的旁白。"
    )

    @gated
    def complete(self, system: str, user: str, *, max_chars: int | None = None,
                 on_chunk: OnChunk | None = None, agent_mode: bool = False) -> str:
        guard = self._GUARD_AGENT if agent_mode else self._GUARD
        # claude 子进程不做 token 级流式(返回即全量);on_chunk 接受但忽略,前端靠 agent_done 展示。
        prompt = f"{guard}\n\n{system}\n\n---\n\n{user}"
        # 关键:用快模型 + 禁工具,把 `claude -p` 压成一次性文本补全。
        # 否则它会以完整 agent 形态运行(调工具、反复探索),大 prompt 直接跑到超时。
        cmd = ["claude", "-p", prompt, "--model", self.model, "--allowed-tools", ""]
        timeout = int(os.environ.get("LOOM_CLAUDE_TIMEOUT", "600"))  # sonnet 写长章较慢,放宽;可环境覆盖
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise LoomBackendError(render("claude_timeout", detail=f"timeout={timeout}s"), code="claude_timeout") from e
        except Exception as e:
            raise LoomBackendError(render("claude_call_failed", detail=str(e)),
                                   code="claude_call_failed") from e
        if out.returncode != 0:
            raise LoomBackendError(render("claude_call_failed", detail=out.stderr.strip()[:500]),
                                   code="claude_call_failed")
        text = out.stdout.strip()
        if not text:  # 跑完却没吐正文 → 报错,别把空往下传
            raise LoomBackendError(render("backend_empty_response", detail=f"model={self.model}"),
                                   code="backend_empty_response")
        return text


class CodexBackend:
    """接 Codex CLI:把 system+user 拼成 prompt 走 `codex exec` 非交互(headless)。

    复用 codex 客户端自己的登录态(ChatGPT 订阅 / OPENAI_API_KEY),Loom 不碰 key——
    和 Claude 后端同一套思路:shell 到装好的客户端,鉴权交给客户端。
    """

    def __init__(self, config: Config) -> None:
        if shutil.which("codex") is None:
            raise LoomBackendError(render("codex_not_found"), code="codex_not_found")
        # 只在用户显式配了 codex 适用的模型时才传 --model;否则让 codex 用它自己的默认模型
        # (订阅登录下默认模型即可,硬塞一个模型名反而可能"未知模型")。
        m = (config.model or "").strip()
        self.model = m if m and "deepseek" not in m else ""

    # 护栏(五工序默认,agent_mode=False):逼 `codex exec` 当"纯文本补全",别当 Codex agent
    # (改文件/跑命令/反问)
    _GUARD = (
        "[严格指令] 你是一个纯文本生成函数,不是助手、不是 agent。只输出要求的成品中文文本本身。"
        "禁止:调用或提及任何工具、读写或查找任何文件、执行命令、反问、说明你在做什么、"
        "输出「我检查了目录」「请提供」之类的话。你需要的全部材料都在下面,直接用。"
    )
    # 护栏(书房伙伴对话变体,agent_mode=True;spec §3):同 ClaudeCodeBackend._GUARD_AGENT——
    # 允许工具协议行 + 反问,真实执行仍锁死在 `--sandbox read-only`(下方 complete 无条件保留)之后。
    _GUARD_AGENT = (
        "[严格指令] 你在扮演一位写作伙伴,通过纯文本对话交流——你说的每个字都是最终输出,"
        "不会再有人替你加工。资料不够时,可以向作者提一句反问,而不是替他瞎编。"
        "需要用工具时,按下面的协议格式输出一行「用:工具名」及参数;这只是文本协议,"
        "真正的文件读写由 loom 服务端执行,你自己没有、也不要尝试真的读写文件或执行命令。"
        "除此之外禁止输出「我是 AI」「我检查了目录」这类与协议无关的旁白。"
    )

    @gated
    def complete(self, system: str, user: str, *, max_chars: int | None = None,
                 on_chunk: OnChunk | None = None, agent_mode: bool = False) -> str:
        guard = self._GUARD_AGENT if agent_mode else self._GUARD
        # codex 子进程不做 token 级流式(返回即全量);on_chunk 接受但忽略,前端靠 agent_done 展示。
        prompt = f"{guard}\n\n{system}\n\n---\n\n{user}"
        timeout = int(os.environ.get("LOOM_CODEX_TIMEOUT", "600"))  # 与 claude 一致放宽,可环境覆盖
        # 关键:read-only 沙箱 + 在临时空目录里跑,codex 既改不了你的稿子、也读不到项目文件;
        # --skip-git-repo-check 避免它因"不在 git 仓库"而中断;最终回复写临时文件以拿到纯文本。
        with tempfile.TemporaryDirectory() as td:
            last = Path(td) / "last.txt"
            cmd = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--output-last-message", str(last)]
            if self.model:
                cmd += ["--model", self.model]
            cmd.append(prompt)
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=td)
            except subprocess.TimeoutExpired as e:
                raise LoomBackendError(render("codex_timeout", detail=f"timeout={timeout}s"),
                                       code="codex_timeout") from e
            except Exception as e:
                raise LoomBackendError(render("codex_call_failed", detail=str(e)),
                                       code="codex_call_failed") from e
            if out.returncode != 0:
                raise LoomBackendError(render("codex_call_failed", detail=out.stderr.strip()[:500]),
                                       code="codex_call_failed")
            text = ""
            if last.exists():
                text = last.read_text(encoding="utf-8").strip()
            if not text:  # 兜底:--output-last-message 没写出来时退回 stdout
                text = out.stdout.strip()
        if not text:  # 跑完却没拿到正文 → 报错,别把空往下传
            raise LoomBackendError(render("backend_empty_response", detail=f"model={self.model or '默认'}"),
                                   code="backend_empty_response")
        return text


class DemoBackend:
    """录屏/试玩用的离线桩后端:不联网、不花钱,产出占位中文,让流水线真实点亮。

    用 LOOM_DEMO=1 开启。内容是占位,不代表真实生成质量。
    """

    def __init__(self, config: Config) -> None:
        self._chars = config.chapter_chars

    def _pick(self, system: str, user: str) -> str:
        head = system.strip().splitlines()[0]
        if "文风分析器" in system:
            return _DEMO["seed"]
        if "维护一个作者" in system:
            return _DEMO["learn"]
        if "剧情脊柱记录员" in system:
            return "摘要:(demo 占位摘要)。\n伏笔:\n- 无"
        if "起一个" in system or "章节标题" in system:  # 标题生成(demo 占位)
            return "废矿里的火光"
        if "领航员" in system:
            # 「领航员」人设(agents/领航员.md)现在被两条截然不同的调用点复用:
            # ①journey.next_card 的老式单轮出题(system=人设原文,格式必须能被
            #   parse_journey_card 解析成「问: + 2-4 候选」的卡);
            # ②partner.run_turn 的对话循环(system=人设 + partner_tools.render_contract()
            #   的工具契约段 + skills 索引——同一份人设文件,多拼了「可用工具」那一段)。
            # 修:判据原来用「可用工具」,但 partner_tools.render_contract()
            # 改成 ```loom 围栏协议后,那段开头已变成「## Tools（要动手就必须写
            # tool block…）」,全库再无「可用工具」这个子串 —— 于是伙伴通道每轮都掉进
            # 上面那条访谈出题卡,demo 伙伴对话退化成永远同一张卡(而且测试只断言 "(demo)"
            # 在场,一直是绿着错)。改用契约段独有的稳定串,与下面「用:提交」同一套路子。
            if "## Tools" in system:
                return _demo_nav_partner_reply(user)
            return _DEMO["nav"]   # 访谈出题:格式必须合规能解析成卡(问: + 2-4 个候选)
        # 写章 agent 循环(agent 模式):system 是 writeloop._SYSTEM + 工具契约,
        # 既有的「按角色名分流」一条都不命中。用契约段独有的「用:提交」当判据,
        # 与上面领航员那条用「可用工具」分流是同一套路子。
        if "用:提交" in system:
            return _demo_write_reply(user)
        for role, key in (("章节细纲师", "outline"), ("章节设定师", "outline_setting"),
                          ("章节分镜师", "outline_storyboard"), ("写手", "draft"),
                          ("责任编辑", "edit"), ("润色师", "final")):
            if role in head:
                return _DEMO[key]
        return "（demo 占位）"

    @gated
    def complete(self, system: str, user: str, *, max_chars: int | None = None,
                 on_chunk: OnChunk | None = None, agent_mode: bool = False) -> str:
        # agent_mode 不影响 demo 罐头内容——demo 靠 system/user 关键词分流(_pick),
        # 伙伴对话分支已经用「可用工具」判据单独识别,接受这个参数只为对齐协议签名。
        import time
        text = self._pick(system, user)
        if on_chunk is not None:  # 模拟流式:按小块吐,离线也能看见"一句句写出来"
            buf = ""
            for ch in text:
                buf += ch
                if len(buf) >= 6 or ch in "。\n!?！?":
                    on_chunk(buf)
                    buf = ""
                    time.sleep(0.02)
            if buf:
                on_chunk(buf)
            return text.strip()
        time.sleep(0.7)  # 让录屏里五个 agent 一个一个亮起来
        return text


_DEMO = {
    "seed": (
        "# 写作指纹\n\n## 句式偏好\n- 爱用短句,单句成段;很少用关联词焊接\n"
        "- 动作收尾,不解释情绪\n\n## 口头禅 / 高频表达\n- “他没说话。”\n- “就这样。”\n\n"
        "## 禁用词 / 绝不这么写\n- 殊不知、仿佛、宛如\n- 直接点名情绪\n\n## 节奏\n- 紧处短句快切,缓处一句带过\n\n"
        "## anchor 例句\n> 风停了。他把刀收回鞘里,没回头。\n"
    ),
    "learn": (
        "# 写作指纹\n\n## 句式偏好\n- 短句为主,单句成段;偏爱动作收尾\n- 对白只留半句,潜台词留给读者\n\n"
        "## 口头禅 / 高频表达\n- “他没说话。”\n- “就这样。”\n\n## 禁用词\n- 殊不知、仿佛、宛如;不直接点名情绪\n\n"
        "## 节奏\n- 紧处短句快切\n\n## anchor 例句\n> 风停了。他把刀收回鞘里,没回头。\n> 她笑了一下。那笑没到眼睛里。\n"
    ),
    "nav": (
        "问:(demo)这本书的核心冲突先走哪一路?\n"
        "- (demo 候选)旧秩序崩塌,主角靠金手指逆流翻盘\n"
        "- (demo 候选)双雄相争,主角在夹缝里闷声发育\n"
        "- (demo 候选)悬案倒查,每章揭开一层旧真相\n"
    ),
    # ---- 领航员·伙伴对话分支(脚本化多轮,见 _demo_nav_partner_reply)----
    # 四段按对话轮数推进:开场问候→提问→提设定候选(工具协议块)→收尾;免 key 也能把
    # 「聊天→拍板→落盘」这条链路端到端点一遍。内容全部自报「(demo)」,不冒充真实生成。
    "nav_partner_greet": (
        "(demo)你好呀,我是这本书的领航员。准备好就说一声,我们从最要紧的空格开始聊。"
    ),
    "nav_partner_ask": (
        "(demo)先问个直接的:这本书最想让读者记住的一个反差点是什么?"
    ),
    "nav_partner_propose": (
        "(demo)我先把这条记成一张候选卡,你看看对不对——\n"
        "用:提设定\n"
        "落点:外置大脑/设定集/00-立项.md#题材\n"
        "内容:(demo 候选)废土复仇流,金手指是「借命」"
    ),
    "nav_partner_wrap": (
        "(demo)这张候选卡先记这儿,你确认后就正式落盘;demo 试玩到这里先告一段落。"
    ),
    "outline": (
        "【本章细纲】\n1. 废矿·入口:火把逼近,主角屏息,伤口渗血。(蓄压) 约800字\n"
        "2. 矿道深处:追兵搜索,主角被逼到绝路。(更大压迫) 约900字\n"
        "3. 绝境:濒死触发逆息,一招逼退三人。(爆发·爽点) 约1000字\n"
        "4. 章末:火光里走出一个不该出现的人——师姐。(悬念钩) 约600字"
    ),
    "outline_setting": (
        "【本章细纲·含设定】\n1. 废矿·入口:火把逼近,主角屏息,伤口渗血。(蓄压) 约800字\n"
        "   设定对照:逆息体质此章首次触发,前文已埋「胸口那点几乎熄灭的气」。(已核对)·\n"
        "2. 矿道深处:追兵搜索,主角被逼到绝路。(更大压迫) 约900字\n"
        "   设定对照:宗门追兵只用制式钢刀,不用术法。(已核对)·\n"
        "3. 绝境:濒死触发逆息,一招逼退三人。(爆发·爽点) 约1000字\n"
        "   设定对照:逆息代价=寿元折损,须在文中点出。(已补)·\n"
        "4. 章末:火光里走出一个不该出现的人——师姐。(悬念钩) 约600字\n"
        "   设定对照:师姐当前立场为中立,不得写成敌方。(已核对)"
    ),
    "outline_storyboard": (
        "【本章细纲·含分镜】\n1. 废矿·入口:火把逼近,主角屏息,伤口渗血。(蓄压) 约800字\n"
        "   <!-- 分镜: 洞口逆光剪影 / 中景固定 / 限制性第三人称 -->\n"
        "2. 矿道深处:追兵搜索,主角被逼到绝路。(更大压迫) 约900字\n"
        "   <!-- 分镜: 手持跟拍 / 越肩视角 / 快切 -->\n"
        "3. 绝境:濒死触发逆息,一招逼退三人。(爆发·爽点) 约1000字\n"
        "   <!-- 分镜: 主观镜头闯入 / 慢动作 / 特写丹田 -->\n"
        "4. 章末:火光里走出一个不该出现的人——师姐。(悬念钩) 约600字\n"
        "   <!-- 分镜: 背光轮廓 / 拉远收束 / 主角视角 -->"
    ),
    "draft": (
        "火把的光爬上矿壁。\n他屏住呼吸,后背贴着冰冷的石头。伤口又开了,血顺着指缝往下滴,一滴,一滴。\n"
        "“搜。”外面有人说。\n脚步声近了。他退,再退,退到矿道尽头,没路了。\n"
        "刀光劈下来的那一刻,胸口那点几乎熄灭的气,忽然烧了起来。\n"
        "逆息。\n他睁开眼。三个人影,一招之内,全倒了。\n矿道很静。他喘着气,握刀的手在抖。\n"
        "火光里,有人慢慢走进来。是个女人。\n“好久不见。”她说。\n他认得这个声音。"
    ),
    "edit": (
        "火把的光爬上矿壁。\n他屏住呼吸,后背贴着冰冷的石头。伤口又开了,血顺着指缝往下滴。\n"
        "“搜。”外面有人说。\n脚步声近了。他退到矿道尽头,没路了。\n"
        "刀光劈下来的那一刻,胸口那点快熄的气,忽然烧了起来。\n逆息。\n"
        "三个人影,一招之内全倒了。\n他喘着气,握刀的手在抖——这一下,又折了几年寿。\n"
        "火光里,一个女人慢慢走进来。\n“好久不见。”\n他认得这个声音。是师姐。"
    ),
    "final": (
        "火把的光爬上矿壁。\n他屏住呼吸,背贴着冷石。伤口又开了,血顺指缝往下滴。\n"
        "“搜。”\n脚步近了。他退到尽头,没路了。\n刀光下来那一刻,胸口快熄的那点气,烧了起来。\n"
        "逆息。\n三个人影,一招全倒。\n他喘着气,手在抖。这一下,又折了几年寿。\n"
        "火光里,一个女人走进来。\n“好久不见。”\n他认得这声音。\n是师姐。\n他没说话。"
    ),
}


def _demo_nav_partner_reply(user: str) -> str:
    """领航员·伙伴对话分支的脚本化多轮罐头(spec §8):按对话轮数推进
    开场问候→提问→提设定候选→收尾,四段之后原地停在「收尾」(demo 不是无限对话)。

    DemoBackend 每请求新建、无状态(§8「实例计数器活不过一轮」),轮数只能从
    `partner_context.assemble` 拼进 `user` 的对话尾部现推——尾部里每条 assistant 事件
    渲染成一行「你:...」(见 partner_context._render_event),数这个前缀出现的行数
    就是「demo 已经说过几轮」,不依赖任何进程内计数器。
    """
    turns_so_far = sum(1 for line in user.splitlines() if line.startswith("你:"))
    if turns_so_far <= 0:
        return _DEMO["nav_partner_greet"]
    if turns_so_far == 1:
        return _DEMO["nav_partner_ask"]
    if turns_so_far == 2:
        return _DEMO["nav_partner_propose"]
    return _DEMO["nav_partner_wrap"]


# 人格 → demo 罐头产物。**顺序与产物名都从 artifacts.ARTIFACTS 派生**(见 _demo_write_reply),
# 这里只映射「哪个人格产哪份罐头」——产物改名不会让 demo 悄悄失灵。
_DEMO_BY_PERSONA = {"章节细纲师": "outline", "章节设定师": "outline_setting",
                    "章节分镜师": "outline_storyboard", "写手": "draft",
                    "责任编辑": "edit", "润色师": "final"}


def _demo_write_reply(user: str) -> str:
    """写章 agent 循环的 demo 罐头:按【已提交的产物】推进度,每轮交下一件。

    **无状态**(同 `_demo_nav_partner_reply`):DemoBackend 每请求新建,活不过一轮,
    所以进度只能从 `writeloop._assemble` 拼进 user 的那行「已提交的产物:…」现推,
    不依赖任何进程内计数器。

    产物顺序与名字从 `artifacts.ARTIFACTS` 取——产物表改了,demo 跟着改,不会悄悄失灵。
    正文体走「参数行后空一行再写」的块形态(见 parse.parse_tool_blocks 的 body)。
    """
    from . import artifacts   # 函数级 import:artifacts → guard → backends,模块级会成环
    done = next((l for l in user.splitlines() if l.startswith("已提交的产物:")), "")
    for spec in artifacts.ARTIFACTS:
        key = _DEMO_BY_PERSONA.get(spec.persona)
        if key is None or spec.name in done:
            continue
        return (f"(demo)接着交{spec.name}。\n"
                f"用:提交\n产物:{spec.name}\n\n{_DEMO[key]}")
    return "(demo)本章已经交完了。"


def _models_url(base: str) -> str:
    """`…/v1` → `…/v1/models`；已经带 `/models` 的不重复拼。

    为什么字符串拼而不用 `urljoin`：base_url 允许带路径前缀（自建网关常见 `/llm/v1`），
    `urljoin` 会把最后一段当文件名吃掉，拼出 `…/llm/models`。
    """
    b = (base or "").strip().rstrip("/")
    return b if b.endswith("/models") else b + "/models"


def _parse_model_ids(payload) -> list[str]:
    """从 `/models` 的响应里抠模型 id。**三种形状都认**，认不出就回空表（不猜）。

    - OpenAI 官方：`{"data": [{"id": "..."}]}`
    - Ollama / 部分自建网关：`{"models": [{"name": "..."}]}`
    - 少数网关直接给字符串数组：`["a", "b"]`
    """
    items = None
    if isinstance(payload, dict):
        for k in ("data", "models", "items"):
            if isinstance(payload.get(k), list):
                items = payload[k]
                break
        if items is None and isinstance(payload.get("model"), str):
            return [payload["model"].strip()] if payload["model"].strip() else []
    elif isinstance(payload, list):
        items = payload
    out: list[str] = []
    for it in items or []:
        if isinstance(it, str):
            v = it
        elif isinstance(it, dict):
            v = it.get("id") or it.get("name") or it.get("model") or it.get("model_name") or ""
        else:
            v = ""
        v = str(v).strip()
        if v:
            out.append(v)
    return sorted(set(out))


def _http_get_json(url: str, key: str, timeout: float = 20.0):
    """GET 一个 JSON。异常都换成**人话**：401/403、其它 HTTP、连不上、超时、返回不是 JSON。

    为什么不用 openai SDK：本机真机实测 Python 3.14 只装了 fastapi/uvicorn，
    **没有 openai 包** —— 聊天链路早就有 `_LiteOpenAI` 兜底，但「拉取可用模型」这里漏了，
    于是点一下按钮就是一句 `No module named 'openai'`。同一条纪律：能不用外部包就不用。
    """
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        if e.code in (401, 403):
            raise ValueError(f"服务器说没权限(HTTP {e.code}):key 可能不对,或这个 key 不被允许列模型。{detail}") from e
        raise ValueError(f"服务器返回 HTTP {e.code}。{detail}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"连不上 {url}:{e.reason}(本地服务没开?端口变了?)") from e
    except TimeoutError as e:
        raise ValueError(f"连 {url} 超过 {int(timeout)} 秒没回应。") from e
    try:
        return _json.loads(body)
    except ValueError as e:
        raise ValueError(f"这个地址返回的不是 JSON(前 120 字:{body[:120]!r})"
                         f"—— base_url 是不是少了 /v1,或者指到了网页?") from e


def list_models(provider: str, *, base_url: str = "", api_key: str = "") -> dict:
    """列出某供应商可选模型。OpenAI 兼容的实时打 GET /models;CLI 类返回预设(别名自动跟最新)。

    只读、不发任何生成请求、不消耗 token。给「拉取可用模型」按钮用。
    **不依赖 openai 包**（本机就没装，见 `_http_get_json` 的注释）。
    """
    provider = (provider or "").lower()
    spec = PROVIDERS.get(provider)
    if not spec:
        return {"ok": False, "message": f"未知供应商 {provider}"}
    if spec["kind"] == "cli":
        return {"ok": True, "models": spec["models"], "source": "preset",
                "message": f"{spec['label']} 的可选模型(别名自动跟最新版本)"}
    burl = (base_url or "").strip() or (spec["base_url"] if spec.get("base_url_locked") else "") or spec.get("base_url", "")
    if not burl:
        return {"ok": False, "message": "先填 base_url 再拉取可用模型"}
    key = (api_key or "").strip() or os.environ.get(spec.get("key_env", ""), "")
    if not key:
        return {"ok": False, "message": "先填 API Key 再拉取可用模型"}
    try:
        payload = _http_get_json(_models_url(burl), key)
        ids = _parse_model_ids(payload)
        if not ids:
            return {"ok": False,
                    "message": f"{burl} 有回应,但没从中认出模型 id"
                               f"(返回形状:{type(payload).__name__});这个网关可能不提供 /models,"
                               f"直接把模型名手填进去。"}
        return {"ok": True, "models": [{"id": i, "label": i} for i in ids], "source": "live",
                "message": f"拉到 {len(ids)} 个可用模型"}
    except ValueError as e:
        return {"ok": False, "message": f"拉取失败:{e}"}
    except Exception as e:      # noqa: BLE001 - 任何意外都不许把端点打成 500
        return {"ok": False, "message": f"拉取失败:{type(e).__name__}: {e}"}


def probe(provider: str) -> dict:
    """轻量探活:命令在不在 PATH、能不能跑 --version、(codex)登没登录、(openai_compat)base_url+key 齐不齐。
    只跑本地命令 / 查本地配置,不发任何 LLM 请求、不消耗 token。给「检测连接」按钮用。"""
    provider = (provider or "deepseek").lower()
    _spec = PROVIDERS.get(provider)
    if _spec and _spec["kind"] == "openai":
        msg = ("OpenAI 兼容供应商:填好 base_url + 模型名 + key,点「保存」;不确定模型名就点「拉取可用模型」。"
               if not _spec.get("base_url_locked")
               else f"{_spec['label']} 用 API Key 鉴权:填好 key、点「保存」;模型可点「拉取可用模型」现选。")
        return {"provider": provider, "ok": True, "kind": "key", "message": msg}
    cmd = {"claude": "claude", "codex": "codex"}.get(provider)
    if not cmd:
        return {"provider": provider, "ok": False, "message": f"未知后端 {provider}"}
    if shutil.which(cmd) is None:
        hint = ("安装 Claude Code,确保 `claude` 在 PATH,并登录过一次"
                if provider == "claude"
                else "安装 Codex CLI:`npm i -g @openai/codex`,然后运行 `codex login`")
        return {"provider": provider, "ok": False, "installed": False,
                "message": f"没找到 `{cmd}` 命令", "hint": hint}
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=8)
        blob = (out.stdout or out.stderr).strip()
        ver = blob.splitlines()[0] if blob else cmd
    except Exception as e:
        return {"provider": provider, "ok": False, "installed": True,
                "message": f"`{cmd}` 跑不起来:{e}", "hint": "确认命令可用或重装"}
    if provider == "codex":  # codex 能查登录态,顺手确认一下
        try:
            st = subprocess.run([cmd, "login", "status"], capture_output=True, text=True, timeout=8)
            low = (st.stdout + st.stderr).lower()
            if "not logged in" in low or ("not" in low and "log" in low and "in" in low):
                return {"provider": provider, "ok": False, "installed": True, "version": ver,
                        "message": "Codex 已安装,但还没登录", "hint": "运行一次 `codex login`(复用 ChatGPT 订阅,不用填 key)"}
        except Exception:
            pass
        return {"provider": provider, "ok": True, "installed": True, "version": ver,
                "message": f"已就绪 · {ver}(复用 codex 客户端登录,不用填 key)"}
    return {"provider": provider, "ok": True, "installed": True, "version": ver,
            "message": f"已就绪 · {ver}(复用 claude 客户端登录,不用填 key)",
            "hint": "若从没登录过,终端先跑一次 `claude` 登录"}


def get_backend(config: Config) -> Backend:
    _gate_configure(getattr(config, "max_concurrency", 2))
    if os.environ.get("LOOM_DEMO"):
        return DemoBackend(config)
    provider = (config.provider or "deepseek").lower()
    spec = PROVIDERS.get(provider)
    if spec and spec["kind"] == "openai":   # deepseek / 国产预设 / 自定义,全走 OpenAI 兼容 HTTP
        return OpenAICompatBackend(config, provider)
    if provider == "claude":
        return ClaudeCodeBackend(config)
    if provider == "codex":
        return CodexBackend(config)
    raise LoomBackendError(f"未知后端 provider={provider!r}(支持:{'/'.join(PROVIDERS)})。")


def cheap_backend(config: Config) -> Backend | None:
    """便宜模型后端:仅当 cheap_model 设了且不同于主模型时返回(同 provider/base_url/key,只换 model);
    否则 None,调用方回退主后端。只给复审/写后摘要这类「评估/管 what」调用用,写作/学指纹始终留给主模型。"""
    import dataclasses
    cheap = (getattr(config, "cheap_model", "") or "").strip()
    if not cheap or cheap == (config.model or "").strip():
        return None
    return get_backend(dataclasses.replace(config, model=cheap))

# ── 视觉能力探测（取稿链路用）──────────────────────────────────────

# 已知视觉模型名前缀（声明值;按外部审查报告扩全表）。名单仅作「跳过一次
# 探测」的参考,实测永远压过声明（作者裁定「以实测为准,名单仅作参考」）。
# 三条防误命中的窄前缀：llama-3.2 只加 -vision 变体（3b 纯文本不能命中）；
# 不加 "deepseek" 宽前缀（deepseek-chat/reasoner 纯文本高频,误命中代价大）；
# o1 不支持视觉,不加。
_VISION_PREFIXES = (
    # OpenAI
    "gpt-4o", "gpt-4.1", "gpt-4v", "gpt-4-turbo", "gpt-5", "o3", "o4",
    # Anthropic（claude-opus/claude-sonnet 冗余已删,被 claude-3/claude-4 覆盖）
    "claude-3", "claude-4",
    # Google
    "gemini",
    # Qwen
    "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen2.5-omni",
    # Zhipu GLM（glm-5 恰好命中已实测的 glm-5.3-flash;若智谱出纯文本 glm-5-base 再收紧）
    "glm-4v", "glm-5",
    # Mistral / Meta / DeepSeek
    "pixtral", "llama-3.2-11b-vision", "llama-3.2-90b-vision",
    "deepseek-vl", "deepseek-v4",   # v4.1-flash 实测支持;若 v4 分出纯文本变体再收紧
    # 其他开源系（很可能）
    "internvl", "minicpm-v", "llava", "phi-3-vision", "phi-3.5-vision",
    "phi-4-multimodal", "yi-vl", "cogvlm", "idefics", "moondream", "nvlm",
)

# (base_url, model) → bool。探测结果按会话缓存（同模型只探一次）。
_VISION_CACHE: dict[tuple, bool] = {}


def supports_vision(base_url: str, model: str, api_key: str | None = None, *, probe: bool = True) -> bool:
    """当前模型能不能看图。声明值为主，实测压过声明，都没就保守 False。

    ① 名单前缀命中 → True（声明值）；
    ② 名单没有 → 发一次 1x1 图探测请求（200=支持，4xx=不支持），结果缓存；
    ③ 探测也炸（网络等）→ False（保守退化到 OCR）。
    """
    model = (model or "").strip()
    low = model.lower()
    if any(low.startswith(p) for p in _VISION_PREFIXES):
        return True
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False
    key = (base, model)
    if key in _VISION_CACHE:
        return _VISION_CACHE[key]
    if not probe:
        return False
    # 外部审查 P1:无 key 的探测请求 100% 是 401,发出去纯属浪费——
    # 跳过探测直接「未判定」（不缓存,下次配好 key 再试）。
    if not (api_key or "").strip():
        print(f"[视觉探测] 未配置 API key,跳过视觉探测(model={model}),本轮保守走 OCR。")
        return False
    # 实测（按外部审查方案 A 升级）：4×4 红蓝棋盘格替代 1×1 白点,
    # 让模型描述看到的颜色——200 但忽略图片的网关会答非所问,关键词校验拦住它。
    # 成本增量:图片 ~200 字节 + max_tokens 32,各家均可忽略。
    # 2026-11-08 审查修正(#2 附带):原 PNG IHDR 写的 1×1,与「4×4 棋盘格」注释不符;
    # 1×1 单像素答不出"棋盘/格子"类关键词。现生成真 4×4 红蓝棋盘格 PNG（运行时编码,
    # 不再依赖手写十六进制头——尺寸/像素一次说清,注释与实物一致）。
    import base64 as _b64
    import struct as _struct
    import zlib as _zlib

    def _checker_png() -> str:
        raw = b""
        for y in range(4):
            raw += b"\x00"          # filter: none
            for x in range(4):
                raw += b"\xff\x00\x00" if (x + y) % 2 == 0 else b"\x00\x00\xff"
        def _chunk(typ: bytes, data: bytes) -> bytes:
            c = typ + data
            return _struct.pack(">I", len(data)) + c + _struct.pack(">I", _zlib.crc32(c) & 0xFFFFFFFF)
        ihdr = _struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
        png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
               + _chunk(b"IDAT", _zlib.compress(raw, 9)) + _chunk(b"IEND", b""))
        return _b64.b64encode(png).decode()

    px = _checker_png()
    content = [
        {"type": "text", "text": "Describe the colors you see in this image. Reply in one short sentence."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + px}},
    ]
    body = {"model": model, "max_tokens": 32,
            "messages": [{"role": "user", "content": content}]}
    # 颜色关键词:模型真看到图才可能答出这些词;答非所问 = 图被忽略 = 未判定
    _COLOR_KW = ("red", "blue", "红", "蓝", "color", "colour", "checker", "棋盘", "grid", "square", "square")
    try:
        import json as _json
        import urllib.request as _u
        req = _u.Request(base + "/chat/completions",
                         data=_json.dumps(body).encode(), method="POST",
                         headers={"Content-Type": "application/json",
                                  "Authorization": "Bearer " + (api_key or "")})
        r = _u.urlopen(req, timeout=30)
        if r.status != 200:
            print(f"[视觉探测] 未判定(model={model}, HTTP {r.status}),本轮保守走 OCR,下次再试。")
            return False
        # 200 也要看内容:读出回答里的颜色词才算真支持。
        # 2026-11-08 审查修正(#2):标准 Chat Completions 回答字段是 message.content,
        # 原读 message.text 恒空 → 非前缀模型探测 200 也判「未描述出颜色」恒退化。
        try:
            _msg0 = (_json.loads(r.read().decode()) or {}).get("choices", [{}])[0].get("message", {})
            ans = str(_msg0.get("content") or _msg0.get("text") or "").lower()
        except Exception:
            ans = ""
        if any(kw in ans for kw in _COLOR_KW):
            _VISION_CACHE[key] = True
            return True
        print(f"[视觉探测] 模型返回 200 但未描述出颜色(model={model}),"
              f"疑似忽略图片,本轮保守走 OCR,下次再试。")
        return False
    except Exception as e:
        code = getattr(e, "code", 0)
        # 400/404/422 = 模型明确拒 image_url → 可缓存为不支持;
        # 401/403 = **鉴权失败,请求根本没到模型**——不能据此判「模型不支持」
        # (修:旧代码把 key 没配的 401 也当成「不支持视觉」,多模态模型被误退化到 OCR)。
        # 5xx/网络 = 无法判定。这几种都不缓存、返回 False(本轮保守走 OCR),下次再试。
        if code in (400, 404, 422):
            _VISION_CACHE[key] = False
        else:
            print(f"[视觉探测] 未判定(model={model}, HTTP {code or type(e).__name__}),"
                  f"本轮保守走 OCR,下次再试。")
        return False
