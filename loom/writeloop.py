"""写作 agent 循环:一个 agent、六个人格、循环到终稿提交。

spec 2026-08-16 §3.2。**形状照搬 `partner.run_turn`**(已在 claude/codex/DeepSeek 三类后端
上跑通,不是新路子):每轮 = 一次 `complete()`;模型按文本协议输出「用:」块;Loom 侧执行工具、
回喂结果、再 complete。真实读写永远在 Loom 服务端(见 `write_tools.run_tool`),模型自己
没有 shell/文件能力,`agent_mode=True` 只是解掉 CLI 后端 prompt 层面的反 agent 护栏。

与流水线时代的两处根本差异:
- **没有固定拓扑**。六个角色降级成人格,agent 自己决定先干什么、要不要回头重来
  ——「可回头」是付这次重构代价的理由:今天写手写崩了只能整章重跑。
- **护栏挂在产物提交上**(`artifacts.ARTIFACTS`),不挂在棒上。agent 绕不过提交这道门。

两条纪律原样继承伙伴通道:
- **流式行缓冲**(spec 2026-07-16 §5.2 critical):协议行绝不许漏到作者屏幕
  (`parse.stream_line_relay`,与伙伴通道共用同一份判据)。
- **每轮重建、零挂起**:本模块不留任何跨调用的进程内状态,轨迹在 `Session` 里,
  杀进程最多丢正在写的一轮。
"""

from __future__ import annotations

from . import artifacts, events, write_tools
from .backends import LoomBackendError
from .errors import render
from .parse import (parse_json_tool_calls, parse_tool_blocks, stream_line_relay,
                    strip_protocol_lines)
from .context_guard import check as _guard_check, build_summary as _guard_summary
from . import scratchpad as _scratchpad


# 人格名单单一真源 = 产物表里各产物挂的 persona。
# 2026-10-21 修:这里原来手写着五棒时代的老名单(设定师/大纲师/写手/编辑/润色师),
# 六棒改造后「编辑」连 agents/编辑.md 都不存在 —— 模型照那句发「取人格 角色:编辑」
# 会吃 FileNotFoundError(被 run_tool 兜住回喂),白烧一轮调用。改成派生,以后加角色不会再漏。
def _persona_names() -> list[str]:
    out: list[str] = []
    for spec in artifacts.ARTIFACTS:
        name = getattr(spec, "persona", "")
        if name and name not in out:
            out.append(name)
    return out


_PERSONAS = _persona_names()

from .config import DEFAULT_CONTEXT_LEN, effective_context_len

_CTX_LIMIT = DEFAULT_CONTEXT_LEN


def _ctx_limit_of(sess) -> int:
    """守卫分母跟随本轮写作后端；老调用方无标签时才回退项目默认窗口。"""
    backend = getattr(sess, "backend", None)
    routed = getattr(backend, "loom_context_len", None)
    if routed is not None:
        return effective_context_len(routed)
    return effective_context_len(getattr(getattr(sess, "config", None), "context_len", 0))

# 一章的调用上界(spec §9 第一风险:自主循环的成本必须定死)。
# 定 24 的依据:六件产物 + 每件前面一次取数 ≈ 12 轮是顺跑的量,留一倍余量给「回头重来」
# (改一次细纲重写一次正文 ≈ +6 轮),再留几轮给提交被打回的自愈。撞到就是真跑飞了。
MAX_ROUNDS = 24


_SYSTEM = """你在为一本网文写一章。你不是助手、不是聊天机器人——你的每一次输出都直接进入生产流程。

你的输出只有一种合法形状:工具调用块。第一行必须是工具调用行,其后每行一个参数,然后空行。

用:取人格
角色:写手

出现下面任何一句,即视为格式错误、本轮作废:
- 我先看一下细纲,然后写初稿。
- 接下来我要调用「取人格」工具。
- 让我想想这一章该怎么写。
- 把工具名和参数写在同一行。
- 一个回复里放两个「用:」块。

你有 __PERSONA_COUNT__ 个可以戴的人格(__PERSONA_LIST__)。动手写某件产物之前,先「取人格」拿到
它的写法要求和它该遵循的设定。你可以按自己的判断决定顺序,**也可以回头重来**——比如写完初稿
发现细纲不好,就重新提交一份细纲再重写。

硬规矩:
1. 只能通过下面的工具做事。想要什么材料就去取,别猜、别编。
2. 写正文前必须「查硬设定」,境界名/专名/金手指代价一字不改照抄,不许自己新造体系。
3. 每件产物都要「提交」。提交会被校验,不合格会被打回并告诉你原因——照原因改好重交即可。
4. 提交「本章终稿」就是收工信号,交完不要再做别的。
5. 一轮只调一个工具。
"""

# 人格名单不写字面量:从产物表派生(见上方 _personas 的说明与理由)。
_SYSTEM = _SYSTEM.replace("__PERSONA_COUNT__", str(len(_PERSONAS))).replace(
    "__PERSONA_LIST__", "/".join(_PERSONAS))

# 严格模式(同一类格式错误连续 2 次后切):把 system 砍到只剩形状约定,给弱模型减负。
# 真机教训:格式规则混在长 prompt 中段时,弱模型会照着历史叙述学形状,越写越长。
_STRICT_SYSTEM = """你的输出只有一种合法形状:工具调用块。

用:工具名
参数名:值

第一行必须是「用:」,一个参数一行,然后空行。不要写任何旁白。一轮只调一个工具。
"""

# 格式错误止损:连续这么多次没解析出工具块就报错,不许烧满 MAX_ROUNDS。
# 24 轮是给「回头重来」留的余量,不是给反复喷叙述留的。
MAX_FORMAT_FAILS = 3
# 连续这么多次就切严格模式(给一次自我修正的机会,再不改就减负)。
_STRICT_AFTER = 2


def _catalog() -> str:
    """可用工具清单,**带参数签名**。

    只给名字时弱模型只能猜参数名,错一次就是一轮——清单里把键摆出来,直接消掉这类失败。
    """
    rows = []
    for name, spec in write_tools.REGISTRY.items():
        keys = "/".join(spec.params) if spec.params else "无参数"
        rows.append(f"{name}({keys})")
    return "、".join(rows)



# 回喂窗口(字符)。按【总量】裁,不按条数裁,更不截单条——
# 真机实测 2026-08-16:把单条工具结果截到 400 字,agent 连取了 4 次「设定师」人格,
# 因为它压根没看见人格内容。取数结果被截断 = 这个工具等于不存在。
_TRAIL_BUDGET = 16000


def _trim_trail(trail: list[str]) -> list[str]:
    """从尾往前收,收到预算为止。**单条永不截断**:宁可少给几条,也不给半条。"""
    kept: list[str] = []
    total = 0
    for line in reversed(trail):
        if kept and total + len(line) > _TRAIL_BUDGET:
            break
        kept.append(line)
        total += len(line)
    return list(reversed(kept))


def _existing_outline(sess: write_tools.Session) -> str:
    """作者手上那份细纲(`正文/.细纲/第N章.md`)。没有/读不了就当没有——绝不因此拖累出稿。"""
    from . import paths
    try:
        p = paths.outline_path(sess.root, sess.chapter_n)
        return p.read_text(encoding="utf-8").strip() if p.is_file() else ""
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError 继承自 ValueError 而非 OSError,裸 `except OSError` 抓不到,
        # 编码损坏的细纲(如作者用 GBK 存过)会直接抛穿、agent 化每一轮 _assemble 都踩到,
        # 整章跑不了(同 mirror.py 已踩过的坑,这里显式并列,不用裸 except Exception)。
        return ""


_JSON_SYSTEM = """你在为一本网文写一章。你的每一次输出都必须是一个工具调用,形状只有一个:

<tool_call>{"name":"工具名","args":{"参数名":"值"}}</tool_call>

不要写旁白、不要解释、不要给第二种形状。工具名与参数名照抄清单。
"""


_JSON_CONTRACT = """
本轮请用 JSON 形状调工具(你这台后端用文本协议容易把对白写成参数):
<tool_call>{"name":"取人格","args":{"角色":"写手"}}</tool_call>
没有第三种形状;不要写旁白,不要解释。
"""


def _protocol_of(sess: write_tools.Session) -> str:
    """这条会话该用哪种工具协议(读 R21 的「写作」槽;读不到就文本协议,绝不让写章挂掉)。"""
    try:
        from . import routes as _routes

        return str(_routes.resolve(sess.config, "写作").get("工具协议") or "text")
    except Exception:
        return "text"


def _assemble(sess: write_tools.Session, trail: list[str],
              *, strict: bool = False, protocol: str = "text") -> tuple[str, str]:
    """拼这一轮的 (system, user)。

    与今天 `_build_user_prompt` 的根本差异:设定/硬设定/上一章/工作区**不再全量拼进来**,
    改由 agent 按需取——于是每轮 prompt 比流水线时代更小,不是更大。

    `strict=True`(连续格式错误后):system 换成只剩形状约定的短版 + 工具签名清单,
    给弱模型减负——长 prompt 里格式规则容易被叙述冲掉。
    """
    # 上游稿实测字数随轮次变 → 篇幅契约也随之变(压缩授权只在真超标时才给)
    actual = write_tools.draft_chars(sess.workspace)
    system = (_STRICT_SYSTEM if strict else _SYSTEM) + "\n" + write_tools.render_contract(sess.config.chapter_chars, actual)
    if strict:
        system += "\n\n可用工具(照抄名字,参数一行一个):\n" + _catalog()
    if protocol == "json":
        # JSON 协议**换掉**文本形状,不是叠加上去:两套形状同时出现,弱模型必然混着学。
        system = (_JSON_SYSTEM + "\n\n"
                  + write_tools.render_contract(sess.config.chapter_chars, actual, protocol="json")
                  + _JSON_CONTRACT + "\n可用工具:\n" + _catalog())
    head = (f"# 你要写的是《{sess.config.title}》第 {sess.chapter_n} 章。"
            if (sess.config.title or "").strip()
            else f"# 你要写的是第 {sess.chapter_n} 章。")
    parts = [head, f"本章正文目标约 {sess.config.chapter_chars} 字。"]
    done = [label for label, _ in sess.workspace]
    parts.append("已提交的产物:" + ("、".join(done) if done else "(还没有)"))
    existing = _existing_outline(sess)
    if existing and not any("细纲" in label for label in done):
        # ADR 0008 的 WYSIWYG 读侧:细纲文件已在(多半是作者手改过)→ 按他的来,别自顾自重拆。
        parts.append("## 作者已经给了本章细纲(照它写,不要重拆)\n" + existing)
    if trail:
        parts.append("## 刚才发生了什么\n" + "\n".join(_trim_trail(trail)))
    parts.append("## 现在\n继续推进,直到提交「本章终稿」。")
    return system, "\n\n".join(parts)


def _tool_params(tool: dict) -> dict:
    """工具参数:声明了 body_param 的工具(提交),「内容」缺省时取块正文体。

    整章正文是多行散文,塞不进一行 `键:值`——故走 `parse_tool_blocks` 给的 body。
    两者都给时以显式参数优先(短产物模型爱写成一行,别让 body 顶掉它)。
    """
    spec = write_tools.REGISTRY.get(tool["name"])
    params = dict(tool.get("params") or {})
    bp = getattr(spec, "body_param", "") if spec else ""
    if bp and not params.get(bp) and tool.get("body"):
        params[bp] = tool["body"]
    return params


def _replay(sess: write_tools.Session) -> str | None:
    """产物级续跑:按提交序重放【已提交且上游未变】的产物,撞到第一个签名失配即停。

    流水线那套是「每棒 sha + 上游签名」;agent 化后没有棒,但提交序是有的,于是判据平移成
    「重放轨迹的合法前缀」。上游变了(改了人格提示词/设定/上一章/章字数)必须重算——
    这是续跑的正确性底线,不然作者的改动被静默吃掉。

    返回终稿文本 = 整章都重放完了、连收工产物都在(此时一次调用都不必发);否则 None。
    """
    from . import trail as trail_store
    for c in trail_store.read_commits(sess.root, sess.chapter_n):
        try:
            spec = artifacts.spec_for(c.get("产物", ""))
        except KeyError:
            break                     # 轨迹里有本版本不认识的产物名 → 保守起见就此打住
        if not c.get("sig") or c["sig"] != write_tools.artifact_sig(sess, spec):
            break                     # 上游变了 → 从这件产物起重算
        if spec.into_workspace:
            sess.workspace.append((spec.name, c.get("text", "")))
        sess.progress(events.agent_skip(spec.persona or spec.name, "已提交且上游未变"))
        if spec.is_final:
            return c.get("text", "")
    return None


def _next_hint(sess: write_tools.Session) -> str:
    """还没提交的产物里排第一的那件——止损报错时告诉人卡在哪一步,别只说「格式不对」。"""
    done = {label for label, _ in sess.workspace}
    for spec in artifacts.ARTIFACTS:
        if spec.name not in done:
            return f"提交「{spec.name}」"
    return "本章产物已齐,应提交「本章终稿」"


# 格式失败分型:三种失败的修法完全不同,回喂同一句话会让弱模型重复同一种错。
_FORMAT_TIPS = {
    "叙述": "你在用文字叙述,没有调工具。第一行必须直接是「用:工具名」。",
    "名称": "工具名不在可用清单里(或工具名与参数写在了同一行)。照抄清单里的名字,参数一行一个。",
}


def classify_failure(botched: bool) -> str:
    """格式失败分型(纯函数,自检可直接调):`botched`=想调但名字/形状不对,否则=纯叙述。"""
    return "名称" if botched else "叙述"


def run_chapter(sess: write_tools.Session, *, max_rounds: int = MAX_ROUNDS,
                should_cancel=None) -> str:
    """跑一章,返回终稿文本。撞轮数上界仍没有终稿 → 报错(绝不静默交一章空的)。

    `should_cancel`(可选):无参可调,返回 True 则在**轮边界**提前抛停止——同伙伴通道的
    「停」落点,正跑的 `complete()` 不在此处中断(不碰 Backend Protocol)。
    """
    from .backends import accepts_kwarg
    final = _replay(sess)
    if final is not None:
        return final          # 上游一点没变 → 整章重放完毕,一次调用都不发
    trail: list[str] = []
    kwargs: dict = {}
    if accepts_kwarg(sess.backend, "agent_mode"):
        # 解掉 CLI 后端(claude/codex)prompt 层面的反 agent 护栏:允许输出工具块。
        # 真实文件读写始终锁在 loom 服务端,这里传的只是护栏开关。
        kwargs["agent_mode"] = True

    fmt_fails = 0          # 连续格式错误计数:解析出工具块即清零(见 MAX_FORMAT_FAILS)
    for _round in range(max_rounds):
        if should_cancel is not None and should_cancel():
            raise LoomBackendError("这一章已停下(作者取消)。", code="cancelled")
        strict = fmt_fails >= _STRICT_AFTER
        system, user = _assemble(sess, trail, strict=strict, protocol=_protocol_of(sess))
        persona = sess.persona or "写手"
        on_chunk, flush = stream_line_relay(
            lambda line, r=persona: sess.progress(events.agent_chunk(r, line + "\n")))
        raw = sess.backend.complete(system, user, max_chars=sess.config.chapter_chars,
                                    on_chunk=on_chunk, **kwargs)
        flush()

        # 新架构:上下文守卫(作者 2026-10-20 裁定:优先取真实 usage,无则估算)
        # context_used = (prompt_tokens + completion_tokens) 的实际值;备选估算(prompt 字符÷2)
        ctx_used = None
        bu = getattr(sess.backend, 'last_usage', None)
        if bu and bu.get('total_tokens') is not None:
            ctx_used = bu['total_tokens']
        elif bu and bu.get('prompt_tokens') is not None:
            ctx_used = bu['prompt_tokens']
        if ctx_used is None:
            ctx_used = (len(system) + len(user)) // 2   # 备选:估算
        gr = _guard_check(ctx_used, _ctx_limit_of(sess))
        if gr.level == 'high':
            raise LoomBackendError('【上下文守卫·高阈值】上下文接近上限,无法继续写这一章。'
                                   '请检查后重试。', code='context_guard_high')
        elif gr.level == 'low':
            # 低阈值:把警告追加进 trail,让 AI 知道要收尾
            trail.append(gr.message)

        # known_params(终审①critical):白名单外的键立刻停手转 body,防中文对白行(合法的
        # 「键:值」形状,如「林三：「你来晚了。」」)被 params 扫描误吞、吃掉正文首行。
        say, tools = parse_tool_blocks(raw, valid_names=set(write_tools.REGISTRY),
                                       known_params={n: s.params for n, s in write_tools.REGISTRY.items()})
        # critical:say 里可能混入没被选中的「用:」协议行(工具名瞎编、或误触发块排在真工具前)
        # ——绝不许漏到作者屏幕。
        say, botched = strip_protocol_lines(say)

        if not tools:
            # JSON 旁路:弱模型写 <tool_call>{…}</tool_call> 比「键:值」稳(没有中文对白同形
            # 的歧义)。只在 `用:` 解析不出工具时兜底,不改既有解析行为。
            j_say, j_tools = parse_json_tool_calls(
                raw, valid_names=set(write_tools.REGISTRY),
                known_params={n: s.params for n, s in write_tools.REGISTRY.items()})
            if j_tools:
                say, tools, botched = j_say, j_tools, False

        if not tools:
            # 光说话不动手:分型回喂(叙述 / 名字错),连续错就升级→严格模式,再错就止损。
            fmt_fails += 1
            kind = classify_failure(botched)
            trail.append(f"你说:{say[:200]}" if say else "你什么也没做。")
            trail.append(f"系统:[格式:{kind}]这一轮作废。{_FORMAT_TIPS[kind]}")
            # 光说「格式不对」不够——真机证明模型会去抄它看得见的形状,所以每次都把形状原样摆出来。
            trail.append("照这个形状来(第一行就是工具行,参数一行一个):\n"
                         "用:取人格\n角色:写手")
            trail.append(f"可用工具:{_catalog()}")
            if fmt_fails >= MAX_FORMAT_FAILS:
                done = "、".join(l for l, _ in sess.workspace) or "无"
                raise LoomBackendError(
                    render("model_output_invalid",
                           detail=(f"连续 {fmt_fails} 轮没有输出可执行的工具块"
                                   f"(最后一次:{_FORMAT_TIPS[kind]})。已提交:{done};"
                                   f"下一步该做的是{_next_hint(sess)}。")),
                    code="model_output_invalid")
            continue

        fmt_fails = 0
        if say:
            trail.append(f"你说:{say[:200]}")
        if len(tools) > 1:
            # 不丢执行(信息已经在手上),只提醒规范做法:多块要求模型同时守多个格式,
            # 弱模型容易在第二个块上崩。
            trail.append(f"系统:本轮你给了 {len(tools)} 个工具块,已按顺序执行;规范做法是一轮一个。")
        for tool in tools:
            ev = write_tools.run_tool(sess, tool["name"], _tool_params(tool))
            if ev.get("error"):
                # 分型标注:参数/校验错与格式错是两回事,别让弱模型以为又是形状问题。
                trail.append(f"系统:[参数/校验]「{tool['name']}」没成——{ev['error']}")
                continue
            if ev.get("t") == "committed":
                name = ev["产物"]
                sess.progress(events.agent_done(sess.persona or name, name))
                # 夜间补缺最终班(§2a):agentic 循环内计划更新——每件产物 committed
                # 投影一步 completed + plan_updated(零额外模型调用,失败只吞)。
                from .agents import _agentic_step_done
                _agentic_step_done(sess.root, sess.chapter_n, name, sess.progress)
                trail.append(f"系统:「{name}」已提交。")
                if artifacts.spec_for(name).is_final:
                    return sess.workspace[-1][1]
            else:
                # 完整回喂,不截断(见 _TRAIL_BUDGET 的真机教训);总量控制交给 _trim_trail。
                trail.append(f"系统:「{tool['name']}」返回:\n{ev.get('text', '')}")

    raise LoomBackendError(
        render("model_output_invalid",
               detail=f"跑满 {max_rounds} 轮仍没有提交「本章终稿」(已提交:"
                      f"{'、'.join(l for l, _ in sess.workspace) or '无'})"),
        code="model_output_invalid")
