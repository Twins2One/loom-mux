"""读项目根的 loom.toml + .env。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .fsutil import atomic_write_text


def _toml_str(s: str) -> str:
    """TOML 基本字符串字面量:反斜杠/双引号正经转义,别再用「引号换单引号」的糊弄净化。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


CONCURRENCY_MIN, CONCURRENCY_MAX = 2, 8
DEFAULT_CONTEXT_LEN = 128_000
# 与浏览器 Number.MAX_SAFE_INTEGER 对齐，保证 JSON 往返不发生整数精度损失。
MAX_CONTEXT_LEN = 9_007_199_254_740_991


def parse_context_len(value, *, allow_default: bool = True) -> int:
    """解析上下文窗口；只接受整数或十进制整数字符串，0/空表示跟随默认。"""
    if value is None or value == "":
        return 0 if allow_default else DEFAULT_CONTEXT_LEN
    if isinstance(value, bool):
        raise ValueError("上下文长度必须是非负整数，不能是布尔值。")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return 0 if allow_default else DEFAULT_CONTEXT_LEN
        if text.startswith("-") and text[1:].isdecimal():
            raise ValueError("上下文长度不能是负数。")
        if not text.isdecimal():
            raise ValueError("上下文长度必须是非负整数。")
        number = int(text)
    else:
        raise ValueError("上下文长度必须是非负整数。")
    if number < 0:
        raise ValueError("上下文长度不能是负数。")
    if number > MAX_CONTEXT_LEN:
        raise ValueError(f"上下文长度不能超过 {MAX_CONTEXT_LEN}。")
    return number if number > 0 else (0 if allow_default else DEFAULT_CONTEXT_LEN)


def effective_context_len(value) -> int:
    """返回实际生效窗口；未声明统一回退 128k。"""
    return parse_context_len(value, allow_default=False)


def context_len_info(value, *, provider: str = "", model: str = "", purpose: str = "") -> dict:
    declared = parse_context_len(value)
    return {"context_len": declared, "context_limit": declared or DEFAULT_CONTEXT_LEN,
            "context_len_source": "手填" if declared else "默认",
            "provider": provider, "model": model, "purpose": purpose}


def clamp_concurrency(v) -> int:
    """并发上限收敛到 **2–8**：原 1–4 → 2–8，默认仍是 2）。

    下限抬到 2：并发 1 等于"任何时刻只许一个模型调用"，连议事厅都开不起来，没有意义。
    上限放到 8：托管 API 才真吃得下；本地单卡开到 5 以上通常是抢显存、反而更慢。

    坏值不报错、不崩：手改 toml 写成 "abc" / 99 / 0 都落回默认 2 ——
    这是一格省事的偏好设置，不值得为它让整本书打不开。
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 2
    if n < CONCURRENCY_MIN:
        return CONCURRENCY_MIN
    if n > CONCURRENCY_MAX:
        return CONCURRENCY_MAX
    return n


@dataclass
class Config:
    provider: str = "deepseek"        # deepseek | claude | codex | openai_compat
    model: str = "deepseek-v4-pro"    # DeepSeek V4 默认 pro(更强);更快更省可填 v4-flash。都是思考型,见 backends 预算
    cheap_model: str = ""              # 可选:复审/写后摘要这类「评估」调用走的便宜模型(同供应商换 model);空=全用主模型
    base_url: str = ""                 # 仅 openai_compat 有意义:第三方 OpenAI 兼容供应商的接口地址
    # 2026-11-05（作者裁定「审核上下文上限真实性」）：模型上下文窗口（token）。
    # 老 toml 没这行 → 0 = 未声明,守卫退回 128k 兜底。上限判定从「写死」变「按配置」。
    context_len: int = 0
    title: str = "我的第一本书"
    idea: str = ""                     # 建书时的一句话设定:AI 铺设定底稿(draft)的种子;空=没填
    chapter_chars: int = 800           # 终稿目标字数;中间工序自然更短
    narrative_voice: str = ""         # 叙事人称:第一人称 / 第二人称 / 第三人称(全知/有限);打地基时填
    core_theme: str = ""             # 核心主题;打地基时填
    volume_structure: str = ""        # 卷/部划分,如"3卷·共30章"或"序章+5部+尾声";打地基时填
    core_characters: str = ""         # 核心人物列表(逗号分隔),人物设计师细化;打地基时填
    gate_rounds: int = 1               # 质检/去AI味 复审轮数:1=只诊断列留痕(默认,不替作者改稿)、≥2 才自动回炉重写、0=关闭(见 ADR-0006)
    foreshadow_distance: int = 8       # 伏笔悬空提醒章距:埋设超过这么多章仍无推进/回收 → 写第N章时进留痕提醒(纯提示、不回炉、不阻断);0=关
    continuity_scan: bool = True       # 每章终稿后自动除虫(跨章连续性,非阻断附赠);False=只手动
    agentic: bool = False              # 【实验·灰度】写章走 agent 循环(五角色降级为人格、可回头重来)
    stop_after: str = ""               # 一章跑到哪一站就停:空=跑满五步出终稿;大纲师=只到细纲;写手=只到初稿
    # 2026-11-02：AI 介入程度三档（写作模式）。writing_mode 是给人看/设置页用的，
    # 落到流水线的仍是 stop_after（映射见 writing_mode_to_stop_after）；
    # 教学模式=完全不代笔（前端隐藏编织按钮，后端拒绝编织调用）。
    writing_mode: str = "FullAuto"     # FullAuto / 辅助 / 教学 / 剧作（2026-11-03 四档）
    # 并发上限(「议事例会」):同时最多几个角色在跑。
    # 只影响「议事」这种一对多的对话;写章流水线本来就是串行,不受它影响。
    # 1-4,默认 2 —— 本地单卡开 4 通常是抢显存、反而更慢;托管 API 才值得往上开。
    max_concurrency: int = 2
    # R21 按用途的模型路由表:{用途: {"provider": ..., "model": ...}}。
    # 空 model = 「跟随默认」;写了 model 就固定用它(换默认模型时这类槽不跟)。
    routes: dict[str, dict] = field(default_factory=dict)
    # 联网查资料(2026-10-19 裁定 2):{engine, base_url, key_env, limit}。
    # 必须进 Config —— 否则 save_config 整文件重写时会把作者手写的 [search] 冲掉。
    search: dict = field(default_factory=dict)
    #   默认关:新路子在真机跑通一章之前不顶掉老流水线(spec 2026-08-16 §6 P1 验收)。
    #   两条路的护栏完全一致——只是挂点从「棒」换到了「产物提交」(§4)。


def find_project_root(start: Path | None = None) -> Path:
    """从当前目录向上找含 loom.toml 的项目根。"""
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        if (d / "loom.toml").exists():
            return d
    from .errors import render
    raise FileNotFoundError(render("project_root_not_found"))


def user_config_dir() -> Path:
    """用户级配置目录(默认后端 backend.json + 默认 key 的 .env)。本地文件、非服务器,守「稿子不离机」。
    LOOM_HOME 可覆盖(测试/自定义)。"""
    return Path(os.environ.get("LOOM_HOME") or (Path.home() / ".loom"))




def vendor_dir() -> Path:
    """Loom自己下载的第三方件（B站的 bili-vision venv / ffmpeg / 取稿缓存、whisper模型等）。

    Loom 的运行数据与桌面宿主的运行数据彼此独立：
    Loom 统一使用自己的用户级数据目录，不把取稿缓存或模型缓存放入宿主应用目录。

    现在的落点：`LOOM_HOME/vendor/`（默认 `~/.loom/vendor/`），跟 Loom的配置住在一起。
    可用环境变量 `LOOM_VENDOR` 单独指到别处（比如 D盘）。
    """
    raw = (os.environ.get("LOOM_VENDOR") or "").strip()
    return Path(raw).expanduser() if raw else (user_config_dir() / "vendor")
def user_env_path() -> Path:
    return user_config_dir() / ".env"


def load_config(project_root: Path) -> Config:
    # 先加载用户级默认 key(~/.loom/.env)作回退,再加载项目 .env——两次 override=True,项目在后=项目赢。
    # 于是:新书没填 key 也能用用户级默认 key(继承);某本单独填了就覆盖(该本 .env 里那行赢)。
    load_dotenv(user_env_path(), override=True)
    load_dotenv(project_root / ".env", override=True)
    try:
        data = tomllib.loads((project_root / "loom.toml").read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"loom.toml 格式有误:{e}") from e
    backend = data.get("backend", {})
    novel = data.get("novel", {})
    gate = data.get("gate", {})
    routes = data.get("routes", {})
    return Config(
        provider=backend.get("provider", "deepseek"),
        model=backend.get("model", "deepseek-v4-pro"),
        cheap_model=backend.get("cheap_model", ""),    # 老 toml 没这行 → 空串=不开,行为不变
        base_url=backend.get("base_url", ""),          # 老 toml 没这行 → 兜底空串,无需迁移
        context_len=parse_context_len(backend.get("上下文长度", backend.get("context_len", 0))),
        title=novel.get("title", "我的第一本书"),
        idea=novel.get("idea", ""),
        chapter_chars=int(novel.get("章节字数", novel.get("chapter_chars", 800))),
        narrative_voice=str(novel.get("narrative_voice",
                                     novel.get("narrativeVoice", "")) or "").strip(),
        core_theme=str(novel.get("core_theme",
                                 novel.get("coreTheme", "")) or "").strip(),
        volume_structure=str(novel.get("volume_structure",
                                       novel.get("volumeStructure", "")) or "").strip(),
        core_characters=str(novel.get("core_characters",
                                       novel.get("coreCharacters", "")) or "").strip(),
        gate_rounds=int(gate.get("轮数", gate.get("rounds", 1))),
        foreshadow_distance=int(gate.get("伏笔提醒章距", gate.get("foreshadow_distance", 8))),
        continuity_scan=bool(gate.get("除虫", gate.get("continuity_scan", True))),
        agentic=bool(novel.get("agent模式", novel.get("agentic", False))),  # 老 toml 没这行 → False,行为不变
        stop_after=str(novel.get("停在哪一站", novel.get("stop_after", "")) or "").strip(),  # 老 toml 没这行 → 跑满五步
        writing_mode=str(data.get("writing_mode", novel.get("写作模式", "FullAuto")) or "FullAuto").strip(),  # 2026-11-02：AI 介入程度三档（顶层键）
        max_concurrency=clamp_concurrency(backend.get("并发上限", backend.get("max_concurrency", 2))),
        # 老 toml 没 [routes] → 空表 = 所有用途都跟随默认,行为与以前完全一致
        # [search]:原样收下(不在这里校验,校验在 websrc.config_for 里做,报错更具体)
        search={str(k): v for k, v in (data.get("search") or {}).items() if str(k).strip()},
        routes={str(k): {kk: (parse_context_len(vv) if kk == "context_len" else str(vv)) for kk, vv in v.items()
                         if kk in ("provider", "model", "base_url", "tool_protocol", "context_len")
                          and (kk == "context_len" or str(vv or "").strip())}
                for k, v in routes.items() if isinstance(v, dict)},
    )


def _set_env_var(project_root: Path, name: str, value: str) -> None:
    """把 name=value 写进项目根的 .env,只替换 name 那一行——多个 key(DeepSeek / 自定义供应商)各占一行,互不覆盖。"""
    env = project_root / ".env"
    lines = []
    if env.exists():
        lines = [l for l in env.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith(f"{name}=") and l.strip() != name]
    lines.append(f"{name}={value}")
    atomic_write_text(env, "\n".join(lines) + "\n")
    if os.name == "posix":
        env.chmod(0o600)  # .env 存 API key:只留属主读写(Windows 无此权限位,跳过)


def _env_var_set(project_root: Path, name: str) -> bool:
    """这个 key 配了没有——**与 `load_config` 认的范围一致:项目 .env 或用户级 ~/.loom/.env**。

    只看项目 .env 是个真机实测到的假警报(2026-08-26):用户级默认 key 与它的自检漏洞是
    同一个 commit(b3adc02「接入优先 onboarding」)一起发的。作者按那个功能的设计
    「连一次全局记住,不用每本重填」配好 key、写章也确实跑得通,但每开一本书点「自检」
    都被告知「DEEPSEEK_API_KEY ✗ .env 里没读到」,修复提示还让他去项目 .env 加一行
    ——**正是那个功能要免掉的动作**。自检的职责是照出真问题,不是造一个。
    """
    for env in (project_root / ".env", user_env_path()):
        try:
            lines = env.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue          # 不存在/读不了/编码坏了:当这一处没有,继续看下一处
        for line in lines:
            if line.strip().startswith(f"{name}="):
                if line.split("=", 1)[1].strip():
                    return True
                break         # 这一处显式写了空值 → 这一处不算配,但用户级还可能有
    return False


def set_provider_key(project_root: Path, key_env: str, key: str) -> None:
    """按供应商的 key_env(PROVIDERS 注册表声明)把 key 写进项目 .env——各供应商各占一行,互不覆盖。"""
    _set_env_var(project_root, key_env, key)


def provider_key_is_set(project_root: Path, key_env: str) -> bool:
    return _env_var_set(project_root, key_env)


def key_available(project_root: Path, key_env: str) -> bool:
    """这本书能不能拿到这个 key:本书 .env 有 → 有;否则看用户级默认 .env(继承)。"""
    return _env_var_set(project_root, key_env) or _env_var_set(user_config_dir(), key_env)

def set_env_key(project_root: Path, key: str) -> None:
    """把 DEEPSEEK_API_KEY 写进项目根的 .env(替换已有行)。"""
    _set_env_var(project_root, "DEEPSEEK_API_KEY", key)


def key_is_set(project_root: Path) -> bool:
    return _env_var_set(project_root, "DEEPSEEK_API_KEY")


def set_openai_compat_key(project_root: Path, key: str) -> None:
    """第三方 OpenAI 兼容供应商的 key 写进 .env 的 LOOM_OPENAI_COMPAT_KEY(与 DeepSeek key 各占一行)。"""
    _set_env_var(project_root, "LOOM_OPENAI_COMPAT_KEY", key)


def openai_compat_key_is_set(project_root: Path) -> bool:
    return _env_var_set(project_root, "LOOM_OPENAI_COMPAT_KEY")


_MANAGED_TABLES = {"backend", "novel", "gate", "search"}


def _extra_tables(path: Path) -> str:
    """把 loom.toml 里**不归 Loom 管**的表原样留下来。

    `save_config` 是按 Config 整文件重写的(第 96 轮的 P0-1 就是这么吃掉八槽路由的)。
    Config 里没有的字段现在能活了,但**作者自己加的表**(例如 `[我的笔记]`)仍会被吞掉。
    这里把那些块原样抄回来追加到末尾 —— 宁可多留一段,也不静默吃掉人家的手写配置。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    blocks, cur = [], None
    for line in text.splitlines(keepends=True):
        st = line.lstrip()
        if st.startswith("["):
            name = st[1:].split("]", 1)[0].strip().strip('"').strip("'")
            keep = name not in _MANAGED_TABLES and not name.startswith("routes.")
            cur = [] if keep else None
            if keep:
                blocks.append(cur)
        if cur is not None:
            cur.append(line)
    out = "".join("".join(b) for b in blocks).strip("\n")
    return out


def save_config(project_root: Path, cfg: Config) -> None:
    # base_url 只对 openai_compat 有意义,只在该供应商下写出这一行——deepseek/claude/codex 重写后照旧无该字段
    base_url_line = (f'base_url = "{cfg.base_url}"\n'
                     if cfg.provider == "openai_compat" and cfg.base_url else "")
    cheap_line = f'cheap_model = "{cfg.cheap_model}"\n' if cfg.cheap_model else ""  # 没设就不写,保持 toml 干净
    idea_line = f'idea  = {_toml_str(cfg.idea)}\n' if cfg.idea else ""
    # 停在哪一站:空=跑满五步(默认),不写这一行,老项目文件保持原样
    stop_line = (f'"停在哪一站" = {_toml_str(cfg.stop_after)}\n' if getattr(cfg, "stop_after", "") else "")
    content = (
        "# Loom 项目配置\n\n"
        "[backend]\n"
        f'provider = "{cfg.provider}"\n'
        f'model    = "{cfg.model}"\n'
        f'{cheap_line}'
        f'{base_url_line}'
        # context_len=0 表示未声明，运行时用 128k 默认；手填后才写，避免默认值伪装成作者声明。
        + (f'"上下文长度" = {int(cfg.context_len)}\n'
           if int(getattr(cfg, "context_len", 0) or 0) > 0 else "")
        # 并发上限:只有不是默认 2 时才写这一行,老项目文件保持原样(不打扰手写配置)
        + (f'"并发上限" = {int(cfg.max_concurrency)}\n'
           if int(getattr(cfg, "max_concurrency", 2)) != 2 else "")
        + "\n"
        "[novel]\n"
        f'title = {_toml_str(cfg.title)}\n'
        f'{idea_line}'
        f'"章节字数" = {int(cfg.chapter_chars)}\n'
        # 叙事人称 / 核心主题 / 卷划分 / 核心人物:打地基时填,老书没有就保持空行
        f'{"narrative_voice" if cfg.narrative_voice else "# narrative_voice ="} = '
        f'{_toml_str(cfg.narrative_voice) if cfg.narrative_voice else ""}\n'
        f'{"core_theme" if cfg.core_theme else "# core_theme ="} = '
        f'{_toml_str(cfg.core_theme) if cfg.core_theme else ""}\n'
        f'{"volume_structure" if cfg.volume_structure else "# volume_structure ="} = '
        f'{_toml_str(cfg.volume_structure) if cfg.volume_structure else ""}\n'
        f'{"core_characters" if cfg.core_characters else "# core_characters ="} = '
        f'{_toml_str(cfg.core_characters) if cfg.core_characters else ""}\n'
        "# 【实验】写章走 agent 循环:五个角色从固定工序降级为可反复戴的人格,写崩了能自己回头重来。\n"
        "# 护栏与流水线完全一致(质检/去AI味/篇幅/留痕/伏笔),只是挂在「产物提交」上而不是「棒」上。\n"
        f'"agent模式" = {"true" if cfg.agentic else "false"}\n'
        f'{stop_line}\n'
        "[gate]\n"
        "# 质检/去AI味 复审轮数:1=只挑硬伤写进 .审稿留痕/(默认,不自动改稿);≥2 才回炉重写;0=关闭。不打分、不硬阻断(见 ADR-0006)\n"
        f'"轮数" = {int(cfg.gate_rounds)}\n'
        "# 伏笔悬空提醒章距:埋设超过这么多章仍无推进/回收 → 写新章时进 .审稿留痕/ 提醒(纯提示,不回炉、不阻断);0=关\n"
        f'"伏笔提醒章距" = {int(cfg.foreshadow_distance)}\n'
        "# 每章写完自动「除虫」(跨章连续性检查,走便宜模型,非阻断只出报告);false=只在编辑器手动点\n"
        f'"除虫" = {"true" if cfg.continuity_scan else "false"}\n'
    )
    if getattr(cfg, "search", None):
        content += ("\n# 联网查资料(知识顾问的「搜一下 / 自己上网抓页」用);key 只从环境变量读。\n"
                    "[search]\n")
        for _k in ("engine", "base_url", "key_env", "limit"):
            if cfg.search.get(_k) not in (None, ""):
                _v = cfg.search[_k]
                # 值里带引号/反斜杠必须转义:否则下一次保存就写出坏 toml,整本书打不开(N-1)
                content += (f"{_k} = {int(_v)}\n" if isinstance(_v, int)
                            else f"{_k} = {_toml_str(str(_v))}\n")
    if getattr(cfg, "routes", None):
        content += ("\n# 按用途的模型路由:不写 model 的槽 = 跟随上面的默认模型(换默认就自动跟)。\n"
                    "# 三个必须独立成槽的用途:长上下文检索 / 文风学习 / 深度逻辑推理。\n")
        for purpose, spec in cfg.routes.items():
            if not isinstance(spec, dict):
                continue
            content += f'\n[routes."{purpose}"]\n'
            if spec.get("provider"):
                content += f"provider = {_toml_str(str(spec['provider']))}\n"
            if spec.get("model"):
                content += f"model    = {_toml_str(str(spec['model']))}\n"
            if spec.get("base_url"):
                content += f"base_url = {_toml_str(str(spec['base_url']))}\n"
            if spec.get("tool_protocol"):
                content += f"tool_protocol = {_toml_str(str(spec['tool_protocol']))}\n"
            if int(spec.get("context_len", 0) or 0) > 0:
                content += f'context_len = {int(spec["context_len"])}\n'
    _keep = _extra_tables(project_root / "loom.toml")
    if _keep:
        content = content.rstrip("\n") + "\n\n# ↓ 下面这些是 Loom 不认识的表,保存时原样留下(不吃掉手写配置)\n" + _keep + "\n"
    atomic_write_text(project_root / "loom.toml", content)
    # 配置变了 → 按槽造过的后端实例作废(否则改完槽要重启才生效,P1-①)
    try:
        from . import usecases
        usecases.drop_backend_cache()
    except Exception:      # noqa: BLE001 - 保存配置不该因为缓存清理失败而失败
        pass


# ── 2026-11-02：写作模式 → 流水线停止点 ──

WRITING_MODES = ("FullAuto", "辅助", "教学", "剧作")   # 2026-11-03：四档（剧作=生成到分镜剧本）


def writing_mode_to_stop_after(mode: str) -> str:
    """写作模式 → stop_after 值。

    FullAuto = 跑满六棒（空串）；辅助 = 跑到章节设定师（细纲+设定校验，无分镜）；
    教学 = "__teaching__"（特殊哨兵：后端拒绝编织，前端隐藏按钮）；
    剧作 = "__drama__"（特殊哨兵：跑到写手出剧本体，跳过责任编辑/润色师——
    由 run_pipeline 侧识别哨兵做跳棒，因为 stop_after 只能"停在某棒之后"，
    表达不了"跑完写手但跳过后两棒"。未知值保守当 FullAuto。
    """
    return {"FullAuto": "", "辅助": "章节设定师", "教学": "__teaching__",
            "剧作": "__drama__"}.get(
        (mode or "").strip(), "")
