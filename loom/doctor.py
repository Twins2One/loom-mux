"""极简启动自检:检查 key/后端命令/agent 文件/外置大脑四件套齐不齐。

只读、无副作用:纯 stat + find_spec + which,绝不写文件、不调后端 LLM、不碰 learn/diff。
只有 ok|缺失 二态 + 一行修复;不引入 SQLite/RAG/projection/dashboard/phase/severity。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import key_is_set, load_config, openai_compat_key_is_set
from .paths import NAV_TRACE_REL, brain_rel

# 单一真相:server.py 也从这里导入,避免清单各存一份漂移
BRAIN_FILES = ["世界观", "人物卡", "卡章纲", "写作指纹"]   # 外置大脑【四件套强制】
OPTIONAL_BRAIN = ["立项卡", "文风参考", "违禁词", "状态账本"]  # 【可选】:有无都 ok,不阻断出稿
AGENT_FILES = ["章节细纲师", "章节设定师", "章节分镜师", "写手", "责任编辑", "润色师"]  # 6 棒流水线


@dataclass
class Check:
    name: str
    ok: bool
    missing: str = ""   # 缺什么(仅 ok=False)
    fix: str = ""       # 怎么补(仅 ok=False)


def _c(name: str, ok: bool, missing: str = "", fix: str = "") -> Check:
    return Check(name, ok, "" if ok else missing, "" if ok else fix)


def run_checks(root: Path) -> list[Check]:
    checks: list[Check] = []
    # a. loom.toml 可解析
    try:
        cfg = load_config(root)
        checks.append(_c("loom.toml 可解析", True))
    except Exception as e:
        checks.append(_c("loom.toml 可解析", False,
                         f"无法读取/解析:{e}", "确认 loom.toml 存在且为合法 TOML,或重新 loom init"))
        return checks  # 没配置,后面的检查无意义,提前返回
    prov = (cfg.provider or "deepseek").lower()
    # b. provider 凭据(deepseek / openai_compat 要 key;自定义还要 base_url)
    if prov == "deepseek":
        checks.append(_c("DEEPSEEK_API_KEY 已配", key_is_set(root),
                         ".env 里没读到 DEEPSEEK_API_KEY",
                         ".env 加一行 DEEPSEEK_API_KEY=sk-你的key(platform.deepseek.com 申请)"))
    elif prov == "openai_compat":
        checks.append(_c("自定义供应商 base_url 已填", bool((cfg.base_url or "").strip()),
                         "loom.toml 里没填 backend.base_url",
                         "顶栏填这家供应商的接口地址 base_url(如智谱 https://open.bigmodel.cn/api/paas/v4)再保存后端"))
        checks.append(_c("LOOM_OPENAI_COMPAT_KEY 已配", openai_compat_key_is_set(root),
                         ".env 里没读到 LOOM_OPENAI_COMPAT_KEY",
                         "顶栏 API Key 框填这家供应商的 key,点保存后端(写进项目 .env)"))
    # c. provider 对应命令/库
    # 2026-10-21 删掉「openai 库已装」这条:它是个**假要求**。
    # backends.py 走的是自家 `_http_get_json`(标准库 urllib),**从不 import openai**,
    # 那条检查只会逼人去装一个用不上的包,然后还是报别的错。留着就是拿一条假红盖住真问题。
    if prov in ("deepseek", "openai_compat"):
        pass      # 这两家都不需要额外库,只要 base_url + key
    elif prov == "claude":
        checks.append(_c("claude 命令可用", shutil.which("claude") is not None,
                         "PATH 里没有 claude", "装 Claude Code 并确保 claude 在 PATH"))
    elif prov == "codex":
        checks.append(_c("codex 命令可用", shutil.which("codex") is not None,
                         "PATH 里没有 codex",
                         "装 Codex CLI(npm i -g @openai/codex)并 codex login(复用 ChatGPT 订阅,不用填 key)"))
    else:
        checks.append(_c("provider 受支持", False,
                         f"未知 provider={prov!r}", "loom.toml 里改成 deepseek/claude/codex/openai_compat"))
    # d. 5 个 agent 文件齐
    for n in AGENT_FILES:
        p = root / "agents" / f"{n}.md"
        checks.append(_c(f"agent · {n}", p.is_file(),
                         f"缺 agents/{n}.md", f"补回 agents/{n}.md(可对照 loom init 模板)"))
    # e. 设定集四件套在(世界观/人物卡认双形态:单文件或目录皆可,路径都指向设定集)
    from .paths import CHARS_DIR_REL, CHARS_REL, WORLD_DIR_REL, WORLD_REL, brain_form
    _dual = {"世界观": (WORLD_REL, WORLD_DIR_REL), "人物卡": (CHARS_REL, CHARS_DIR_REL)}
    for n in BRAIN_FILES:
        if n in _dual:
            file_rel, dir_rel = _dual[n]
            ok = brain_form(root, file_rel, dir_rel) != "none"
            if not ok:
                # 旧结构书(2026-10-21):改造前建的书,世界观/人物放在 `外置大脑/世界观/`、
                # `外置大脑/人物/` 这些**旧目录**里(或旧的单文件形态)。那种书**能正常写**,
                # 所以不该在这儿亮红叉——亮红只会让人以为坏了。只有两种形态都没有才报红。
                _legacy = {"世界观": [root / "外置大脑" / "世界观.md", root / "外置大脑" / "世界观"],
                           "人物卡": [root / "外置大脑" / "人物卡.md", root / "外置大脑" / "人物"]}.get(n, [])
                if any(x.exists() for x in _legacy):
                    checks.append(_c(f"设定集 · {n}(这本用的旧结构,能正常写)", True))
                    continue
            checks.append(_c(f"设定集 · {n}", ok,
                             f"缺 {file_rel}(或对应目录 {dir_rel}/)",
                             f"手动补回 {file_rel},或建 {dir_rel}/ 目录(一节/一人一文件)"))
            continue
        p = root / brain_rel(n)
        fix = ("loom seed 重新生成,或从备份恢复" if n == "写作指纹"
               else f"外置大脑是人维护文件,手动补回 外置大脑/{n}.md")
        checks.append(_c(f"外置大脑 · {n}", p.is_file(), f"缺 外置大脑/{n}.md", fix))
    # f. 可选卡(立项卡/文风参考/违禁词):有无都 ok——纯信息、绝不 ok=False、绝不阻断(同违禁词待遇)
    for n in OPTIONAL_BRAIN:
        checks.append(_c(f"外置大脑 · {n}(可选)", True))
    # g. 领航员近况(读留痕,纯只读;没有留痕文件 = 没降级过,不出这条,零噪音)
    trace = root / NAV_TRACE_REL
    if trace.is_file():
        n = len(re.findall(r"^## ", trace.read_text(encoding="utf-8", errors="replace"), re.M))
        checks.append(_c("领航员出题", n == 0,
                         f"最近留痕里有 {n} 次降级(只保最近20条)",
                         f"看 {NAV_TRACE_REL} 的「结果」列:backend_error→查key/网络;"
                         "unparsed/few_options/false_exhausted→升级loom或换模型。修好后删掉这个文件即清零"))
    return checks


def report(checks: list[Check]) -> dict:
    return {"ok": all(c.ok for c in checks), "checks": [asdict(c) for c in checks]}
