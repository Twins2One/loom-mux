"""R21 按用途的模型路由表:每个流程可以独立选模型(可跨 provider),也可以「跟随默认」。

**为什么不是「一个默认模型 + 一个便宜模型」**:那套两档(Loom 原有的 `model` + `cheap_model`)
把「用途」和「价格」绑在一起——可审稿需要的是**判别力**,文风学习需要的是**长文归纳**,
长上下文检索需要的是**窗口**,三者未必是同一个模型,也未必比默认便宜。

**三种槽**:
- **跟随默认**(`model` 留空):默认模型一换,它自动跟着换。这是绝大多数槽的常态。
- **显式指定**:写死了 provider/model,换默认时它**不动**。
- **三个必须独立成槽**:`长上下文检索` / `文风学习` / `深度逻辑推理`——项目定义里点名的三个。
  它们**必须**出现在槽表里(哪怕当前是跟随默认),否则界面会漏掉它们、以后想换时找不到入口。

**记账**:经过 `get_backend_for()` 拿到的后端会在自己身上记一笔 `loom_purpose`,
`agents._log_call` 把它写进运行账本(见 `runlog.py`)。任一用途的调用都能查到用了谁。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from .backends import PROVIDERS
from .config import Config, load_config, save_config

# 用途 → 一句话说明。顺序即界面顺序。
PURPOSES: dict[str, str] = {
    "写手": "写手:真正落笔出正文的那一棒(2026-10-19 裁定 1A,与改稿分开配)",
    "写作": "编辑 / 润色师:改稿、去 AI 味那一棒(老配置里这个槽就是改稿)",
    "大纲": "设定师 / 大纲师:铺设定、拆卷拆章",
    "审稿": "质检 / 去AI味 / 除虫 / 建议:只诊断、要判别力",
    "长上下文检索": "跨很多章找线索、做长程对照(吃窗口)",
    "文风学习": "从「人手改 vs AI 原稿」里归纳改法(吃归纳,不吃速度)",
    "深度逻辑推理": "时间线/因果链/设定冲突这类硬推理",
    "摘要": "写后摘要、章节回顾、状态入账",
    # 2026-10-21 裁定(丙):智库对话此前不属于任何一格 ⇒「协议按格配、对话却没格」是语义漏洞。
    "对话": "领航员 / 作者智库 8 人的一对一聊天(工具协议也在这里设)",
    #：书房伙伴用「知识顾问」这个槽。
    # 为什么单开一槽而不让它跟「对话」：「对话」是书内一对一聊天（要工具、要动设定集），
    # 而书房伙伴只是“答疑启发、不代笔”。两件事的选型要求不同（后者小模型就够）。
    "知识顾问": "书房伙伴：在书房里答疑启发、不代笔（原书内知识顾问的取证能力也走这一槽）",
}

# 这三个必须独立成槽(作者 2026-10-18 点名),不能只靠「跟随默认」蒙过去。
REQUIRED = ("长上下文检索", "文风学习", "深度逻辑推理")


# R25:工具协议。弱模型用「键:值」容易把中文对白写成参数(同形歧义),
# 换成 JSON 就少一整类失败;强模型用文本协议省 token。
# 每个用途**实际被谁调用**。这张表不是装饰:槽能配但没人用,人配了不生效还以为是生效的
# (交叉审查 B-P1-2:长上下文检索 / 深度逻辑推理 此前就是这样两个死槽)。
# 自检会核对:表里写有消费方的用途,源码里必须真有 `get_backend_for(..., "用途")` 调用。
CONSUMERS = {
    "写手": ["写手(写第 N 章正文)", "续写"],
    # 2026-10-21 校准:细纲三棒已挪到「大纲」格。这一格只剩正文两棒。
    "写作": ["责任编辑", "润色师"],
    "大纲": ["章节细纲师", "章节设定师", "章节分镜师", "大纲师", "设定师", "重新生成细纲"],
    "审稿": ["质检", "去AI味", "除虫", "建议"],
    "摘要": ["写后摘要", "章节回顾", "状态入账"],
    "文风学习": ["文风档案蒸馏"],
    # 2026-10-19 裁定 3:这一槽接给「知识顾问(跨章取证)」与「领航员(大纲符合度复核)」。
    # 之前它是空着的(检索走确定性索引),现在真的有两处需要「吃大窗口」的活儿。
    # 2026-11-05（作者裁定「开始下线后端的知识顾问」）：知识顾问从两槽摘除——
    # 槽位保留（将来别的角色可用），「长上下文检索」只剩领航员。
    "长上下文检索": ["领航员(大纲符合度复核)"],
    "深度逻辑推理": [],   # R27 消费者知识顾问已下线（2026-11-05）；槽位保留备用
    "对话": ["领航员", "作者智库 8 人"],
    "知识顾问": ["书房伙伴", "知识顾问（取稿理解）"],
}


def consumers_of(purpose: str) -> list[str]:
    """这个槽现在有没有人用。**只读。**"""
    return list(CONSUMERS.get(purpose) or [])


PROTOCOLS = {"text": "文本协议(用:工具名 + 每行一个参数)",
              "json": "JSON 协议(<tool_call>{...}</tool_call>,结构明确)",
              # 2026-10-21 开放:后端真有 tools 能力就直连,结构化 tool_calls 不经文本解析,
              # 弱模型最容易出错的那一步(把中文对白当参数)被 schema 挡住了。
              # 后端不支持时**自动降级**回文本协议并明确告知,不会整条链崩。
              "native": "原生函数调用(后端支持 tools 时直连;不支持会自动降级为文本协议)"}

def resolve(config: Config, purpose: str) -> dict:
    """一个用途最终用哪个 provider/model。`跟随默认` = 用 config 的默认值。"""
    if purpose not in PURPOSES:
        raise KeyError(f"不认识的用途:{purpose}(只认 {' / '.join(PURPOSES)})")
    spec = (getattr(config, "routes", {}) or {}).get(purpose) or {}
    provider = str(spec.get("provider") or "").strip()
    model = str(spec.get("model") or "").strip()
    base_url = str(spec.get("base_url") or "").strip()
    slot_context_len = spec.get("context_len", 0)
    # 2026-10-21 热修复:两个值要分开 ——
    #   `protocol_raw` = 作者**钉住**的那个(没钉就是空,= 自动);
    #   `protocol`     = 实际生效的那个(没钉时是文本协议)。
    # 以前只给一个「没钉就填 text」的值,于是「跟随默认」的格子在界面上永远显示成 text,
    # 作者切成 native 保存后一看又回到 text —— 误以为没存进去。
    protocol_raw = str(spec.get("tool_protocol") or "").strip()
    protocol = protocol_raw or "text"
    follow = not model and not provider and not base_url and not int(slot_context_len or 0)
    return {"用途": purpose,
            "跟随默认": follow,
            # 「这一格到底谁在用」——直接摆到界面上(:标题不够明了)
            "消费方": consumers_of(purpose),
            "provider": (config.provider if follow or not provider else provider),
            "model": (config.model if not model else model),
            # 第三方/本地模型系统的入口:写 base_url 就只对这一个槽生效(不动全局配置)
            "base_url": (base_url or getattr(config, "base_url", "")),
            "context_len": int(slot_context_len or getattr(config, "context_len", 0) or 0),
            "context_limit": int(slot_context_len or getattr(config, "context_len", 0) or 128000),
            "context_len_source": ("用途手填" if int(slot_context_len or 0) > 0 else
                                   ("默认后端手填" if int(getattr(config, "context_len", 0) or 0) > 0 else "默认")),
            "工具协议": protocol,
            # 工具协议是否被作者钉住(空 = 自动:后端支持 tools 就走原生,否则文本)。
            # 界面用它决定下拉框显示「默认(自动)」还是具体那一项。
            "工具协议已钉": bool(protocol_raw),
   "写明": {"provider": provider, "model": model, "base_url": base_url,
            "tool_protocol": protocol_raw,
            "context_len": int(slot_context_len or 0)}}


def slots(config: Config) -> list[dict]:
    """整张槽表(每个用途一行)。REQUIRED 的三个即使没配也会出现。"""
    out = []
    for purpose in PURPOSES:
        out.append({**resolve(config, purpose), "说明": PURPOSES[purpose],
                    "必须独立": purpose in REQUIRED,
                    "消费方": consumers_of(purpose),
                    "本版无消费方": not consumers_of(purpose)})
    return out


def set_route(config: Config, purpose: str, *, provider: str = "", model: str = "",
              base_url: str = "", tool_protocol: str = "", context_len: int = 0) -> dict:
    """改一个槽(纯函数:返回新的 routes,不落盘)。空 provider+空 model = 恢复「跟随默认」。"""
    if purpose not in PURPOSES:
        return {"ok": False, "reason": f"不认识的用途:{purpose}"}
    provider = (provider or "").strip()
    model = (model or "").strip()
    base_url = (base_url or "").strip()
    tool_protocol = (tool_protocol or "").strip()
    from .config import parse_context_len
    try:
        context_len = parse_context_len(context_len)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    if provider and provider not in PROVIDERS:
        return {"ok": False, "reason": f"不认识的供应商:{provider}"}
    if base_url and provider not in ("", "openai_compat"):
        return {"ok": False,
                "reason": "自定义 base_url 只对 openai_compat 生效(第三方/本地模型走这条);"
                          f"「{provider}」有自己的地址,不要混用。"}
    if tool_protocol and tool_protocol not in PROTOCOLS:
        return {"ok": False,
                "reason": f"不认识的工具协议:{tool_protocol}(只认 {' / '.join(PROTOCOLS)})"}
    routes = {k: dict(v) for k, v in (getattr(config, "routes", {}) or {}).items()}
    if not provider and not model and not base_url and not tool_protocol and not context_len:
        routes.pop(purpose, None)
    else:
        spec = {"provider": provider, "model": model}
        if base_url:
            spec["base_url"] = base_url
        if tool_protocol:
            spec["tool_protocol"] = tool_protocol
        if context_len:
            spec["context_len"] = context_len
        routes[purpose] = spec
    return {"ok": True, "routes": routes, "用途": purpose,
            "跟随默认": not provider and not model and not base_url and not tool_protocol and not context_len}


def save_routes(project_root: Path, config: Config, routes: dict) -> Config:
    """把新槽表写进 loom.toml(其余字段照旧)。返回新的 Config。"""
    new = dataclasses.replace(config, routes={k: dict(v) for k, v in routes.items()})
    save_config(Path(project_root), new)
    return new


def get_backend_for(config: Config, purpose: str):
    """按用途造后端。**只有这一条路**能拿到「按用途选好模型」的后端。"""
    from .backends import get_backend

    r = resolve(config, purpose)
    cfg = config
    if not r["跟随默认"]:
        fields = {"provider": r["provider"], "model": r["model"],
                  "context_len": r["context_len"]}
        if r.get("base_url"):
            fields["base_url"] = r["base_url"]      # 第三方 / 本地模型:只动这个槽
        cfg = dataclasses.replace(config, **fields)
    backend = get_backend(cfg)
    try:
        object.__setattr__(backend, "loom_purpose", purpose)     # 供运行账本记「用途」
        object.__setattr__(backend, "loom_context_len", r["context_limit"])
        object.__setattr__(backend, "loom_context_len_source", r["context_len_source"])
    except Exception:      # noqa: BLE001 - 记不上用途不影响出稿
        pass
    return backend


def reload(project_root: Path | str) -> Config:
    return load_config(Path(project_root))

def preferred_tool_protocol(config) -> str:
    """对话该用哪种工具协议(2026-10-21)。

    「工具协议」是按**用途格**设的,而智库对话不属于任何一格。这里的取舍:
    把所有格上显式设过的值收起来——**只有一个不同取值时就用它**,冲突或没设就返回空
    (空 = 让后端能力自己决定,等于原来的行为)。宁可回到自动,也不替作者瞎猜。
    """
    slot = ((getattr(config, "routes", {}) or {}).get("对话") or {})
    direct = str(slot.get("tool_protocol") or "").strip().lower()
    if direct:
        return direct
    vals = set()
    for spec in (getattr(config, "routes", {}) or {}).values():
        v = str((spec or {}).get("tool_protocol") or "").strip().lower()
        if v:
            vals.add(v)
    return vals.pop() if len(vals) == 1 else ""
