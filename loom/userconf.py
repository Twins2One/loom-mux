"""用户级默认后端:连一次(接入模型),全局记住;新建的每本书自动继承。

存在用户级本地目录(~/.loom,LOOM_HOME 可覆盖)——纯本地文件、非服务器,守 Loom
「无服务器、稿子不离机」。两份:
- backend.json:provider / model / base_url(非密文,给新书铺 loom.toml)。
- .env(0600):各供应商的 key,与项目 .env 同格式;load_config 已把它当回退加载,
  所以新书不填 key 也能用默认 key(继承),某本单独填了就覆盖。key 不拷进书 .env,
  于是 zip 分享某本书永远不含 key。
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from . import config as _cfg
from .backends import PROVIDERS
from .config import load_config, save_config, set_provider_key, user_config_dir
from .fsutil import atomic_write_text

_BACKEND_JSON = "backend.json"


def _json_path() -> Path:
    return user_config_dir() / _BACKEND_JSON


def load_default_backend() -> dict:
    """读用户级默认后端(provider/model/base_url);没设过返回 {}。"""
    p = _json_path()
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:   # 坏 JSON 当没设(极简:不做迁移)
        return {}


def has_default() -> bool:
    return bool(load_default_backend().get("provider"))


def save_default_backend(provider: str, model: str, base_url: str = "",
                         api_key: str | None = None, context_len: int = 0) -> None:
    """保存用户级默认后端及手填窗口上限。

    context_len=0 表示未声明，运行时使用 128k 默认；已有 routes 必须原样保留。
    """
    old = load_default_backend()
    limit = _cfg.parse_context_len(context_len)
    d = dict(old)
    d.update({"provider": provider, "model": model, "base_url": base_url or "",
              "context_len": limit})
    atomic_write_text(_json_path(), json.dumps(d, ensure_ascii=False, indent=2))
    if api_key:
        key_env = (PROVIDERS.get(provider) or {}).get("key_env")
        if key_env:
            set_provider_key(user_config_dir(), key_env, api_key.strip())


def load_user_routes() -> dict:
    """读用户级槽表（书房没有书，槽表得存在用户目录）。

    ：书房伙伴要能单独选模型。但书房没有 `loom.toml`，
    所以槽表存在 `~/.loom/backend.json` 的 `routes` 字段里。
    老文件没这个字段 = 空槽表（全部跟随默认），不需要迁移。。
    """
    d = load_default_backend()
    r = d.get("routes")
    return {k: dict(v) for k, v in r.items() if isinstance(v, dict)} if isinstance(r, dict) else {}


def save_user_route(purpose: str, *, provider: str = "", model: str = "",
        base_url: str = "", tool_protocol: str = "", context_len: int = 0) -> dict:
    """改一个用户级槽。空 provider + 空 model = 恢复「跟随默认」。

    为什么单开一个而不复用 `routes.set_route`：那个是纯函数（返回新 routes、不落盘），
    而这里要落盘到用户目录。校验规则与它一致（不认识的供应商/协议拒掉）。
    """
    from .backends import PROVIDERS as _P
    from .routes import PROTOCOLS as _PROT, PURPOSES as _PUR

    if purpose not in _PUR:
        return {"ok": False, "reason": f"不认识的用途：{purpose}"}
    provider = (provider or "").strip()
    model = (model or "").strip()
    base_url = (base_url or "").strip()
    tool_protocol = (tool_protocol or "").strip()
    try:
        context_len = _cfg.parse_context_len(context_len)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    if provider and provider not in _P:
        return {"ok": False, "reason": f"不认识的供应商：{provider}"}
    if base_url and provider not in ("", "openai_compat"):
        return {"ok": False, "reason": "自定义 base_url 只对 openai_compat 生效。"}
    if tool_protocol and tool_protocol not in _PROT:
        return {"ok": False, "reason": f"不认识的工具协议：{tool_protocol}"}

    routes = load_user_routes()
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

    d = load_default_backend()
    d["routes"] = routes
    atomic_write_text(_json_path(), json.dumps(d, ensure_ascii=False, indent=2))
    return {"ok": True, "routes": routes}


def default_key_is_set(provider: str) -> bool:
    key_env = (PROVIDERS.get(provider) or {}).get("key_env")
    return bool(key_env) and _cfg._env_var_set(user_config_dir(), key_env)


# ---------- 项目书架(用户级注册表:新建/导入/打开过的书都记着,欢迎页点选,不再输路径) ----------

_PROJECTS_JSON = "projects.json"


def _projects_path() -> Path:
    return user_config_dir() / _PROJECTS_JSON


def list_projects() -> list[dict]:
    """书架清单(最近打开在前):[{root, title, last_opened}]。没有/坏 JSON → 空。"""
    p = _projects_path()
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def record_project(root: Path | str, title: str) -> None:
    """把这本书记上书架(upsert + 提到最前),容量 50。任何异常吞掉——书架是便利,绝不拖累开书。"""
    try:
        import time
        root = str(Path(root).resolve())
        rest = [x for x in list_projects() if x.get("root") != root]
        rest.insert(0, {"root": root, "title": title or Path(root).name,
                        "last_opened": int(time.time())})
        atomic_write_text(_projects_path(), json.dumps(rest[:50], ensure_ascii=False, indent=1))
    except Exception:
        pass


def forget_project(root: str) -> None:
    """Explicit shelf actions report storage errors; a failed action is not success."""
    p = _projects_path()
    rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    if not isinstance(rows, list):
        raise ValueError("书架登记格式异常，暂时不能移出书架。")
    rest = [x for x in rows if x.get("root") != str(Path(root).resolve())]
    atomic_write_text(p, json.dumps(rest, ensure_ascii=False, indent=1))


def apply_default_to_new_book(root: Path) -> None:
    """新书创建后铺上用户级默认后端:把 provider/model/base_url 写进该书 loom.toml。
    key 不拷(靠 load_config 的用户级回退继承),privacy 不变。没设默认则什么都不做。"""
    d = load_default_backend()
    provider = d.get("provider")
    if not provider:
        return
    cfg = load_config(root)   # 保留书自己的 title/字数/idea 等,只覆盖后端四项
    save_config(root, replace(
        cfg, provider=provider, model=d.get("model", ""),
        cheap_model=cfg.cheap_model, base_url=d.get("base_url", ""),
        context_len=_cfg.parse_context_len(d.get("context_len", 0)),
    ))

#2026-10-31：行动模式三档 + 记忆库长程维护开关。
#存 prefs.json（用户目录，与 backend.json 同层）——跟着人走，不进包。

def load_prefs() -> dict:
    """读用户偏好。没设过返回缺省：行动模式=默认，长程维护=关。"""
    p = user_config_dir() / "prefs.json"
    if not p.is_file():
        return {"mode": "默认", "memory_startup": False}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {"mode": "默认", "memory_startup": False}
    except Exception:
        return {"mode": "默认", "memory_startup": False}


def save_prefs(mode: str, memory_startup: bool) -> None:
    from .fsutil import atomic_write_text
    if mode not in ("主动思考", "默认", "安静"):
        mode = "默认"
    d = {"mode": mode, "memory_startup": bool(memory_startup)}
    atomic_write_text(user_config_dir() / "prefs.json", json.dumps(d, ensure_ascii=False, indent=2))
