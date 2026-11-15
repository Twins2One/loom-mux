"""书房伙伴工具注册表 + v1a 三工具(spec §5.3):读文件/看地基/提设定。

设计(docs/superpowers/specs/2026-07-16-navigator-agent-design.md §5):
- **注册表单一真相**:工具在一处声明,同时渲染 prompt 契约段(render_contract)、
  驱动分发(run_tool)——prompt 与执行永不漂移(STAGES 表模式的延伸)。
- **路径守卫两层**:机制守卫(safe_join 锁书根 + 「路径任一段以『.』开头即拒」的通用规则,
  一条规则涵盖 .env/.loom_state.json/.伙伴对话//外置大脑/.拆书/ 等全部内部区)
  + 白名单谓词(外置大脑/skills/正文,只读;前缀从 paths.py 常量派生,不手拼字面量)。
  注意:brainedit.check_rel 是 AI 改写白名单(只放行世界观/人物,窄),职责不同,不复用不改动。
- **提设定不落盘**:mutates=True,产 proposal 事件记入返回 dict;真正落盘要等 P3 的
  拍板确认通道(§6),这里只负责产出候选载荷。
- handler 契约:mutates=False → 返回 str(结果文本,直接进 result 事件的 text 字段);
  mutates=True → 返回 dict(proposal 载荷字段,如 slot/content),run_tool 补 t/id 信封。
  v1a 只有「提设定」一个 mutates 工具,此处按其固定参数名(落点/内容)在 handler 里组装
  proposal 载荷;未来若新增第二个 mutates 工具,这个「handler 自产载荷字段」的形状已经
  是通用的(不用改 run_tool),不需要现在过度设计。
"""
from __future__ import annotations
import os

import re
import secrets

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import journey, paths, slots
from .fsutil import safe_join
from .request_turn import tool_scope, StaleRequest

_READ_MAX_CHARS = 3000   # 单条工具结果预算(spec §4 常量表:单条工具结果 ≤3k 字)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    params: tuple[str, ...]
    desc: str
    handler: Callable
    mutates: bool
    # 工具开始按角色分了。「改指纹」是语料分析师的专属活(改的是写手/润色师
    # 都照着的硬约束),让设定师也能调它等于开了一扇越权门;空元组 = 人人可用(老行为)。
    roles: tuple[str, ...] = ()
    # 内容是不是「设定正文」。False = 不走 `_prepare_content` 那两道机械清理
    # (截对白行 / 剥尾部落话)——那两道是给「写进设定集」的卡设计的;写作指纹里的
    # anchor 例句允许是第一人称对白,拿人名册去截它反而会砍掉作者的原话。
    setting_body: bool = True


# ── 路径守卫:safe_join(机制守卫①)+ 点开头段拒(机制守卫②)+ 白名单谓词 ──────────
# 前缀从 paths.py 常量派生(BRAIN_DIR/BODY_DIR),不手拼「外置大脑」「正文」字面量;
# 「skills」paths.py 没有对应常量(它是模板/题材库的概念,归 scaffold.py 的 GENRE_DIR 派生),
# 沿用仓库既有惯例(agents.py/draft.py/deconstruct.py 均手写该字面量)。
_READ_PREFIXES = (f"{paths.BRAIN_DIR}/", f"{paths.BODY_DIR}/", "skills/")


def _safe_read_path(root: Path | str, rel: str) -> Path:
    """把 rel 锁进读白名单:越界/点开头段/白名单外一律 ValueError。

    点段检查跑两遍,缺一都留后门:
    ①原始请求串——挡字面写 外置大脑/.拆书/x.md 这种直球。
    ②safe_join resolve 后、相对 root 的真实路径——挡「请求串字面干净、但是个符号链接,
      resolve 跟随后落进点目录」的绕过(书内 外置大脑/link.md 指向 外置大脑/.拆书/secret.md,
      字符串本身不含点段、①放行,不补②就能读到 .拆书 里的机密)。
    白名单前缀检查同样基于②算出的真实相对路径,不基于原始请求串——防链接指向白名单外。
    root 自身可能是符号链接(macOS /tmp -> /private/tmp):这里的 base 和 safe_join 内部
    第二次 resolve 出的 base 是同一个值(对已 resolve 的路径再 resolve 是幂等的),
    两边比较口径一致,不会因 root 自身是链接而把合法子路径误判越界或误判点段。
    """
    base = Path(root).resolve()
    r = str(rel or "").replace("\\", "/").strip()
    if not r:
        raise ValueError("路径不能为空。")
    # 点段检查①:查原始请求串。即便某个含 .. 的路径最终 resolve 后仍落在书根内,也照拒——
    # 这条是「点段」黑名单,不只是「越界」检测,两把尺子各管各的。
    if any(seg.startswith(".") for seg in r.split("/") if seg):
        raise ValueError(f"路径不合法(点开头段是引擎自动维护区,拒绝访问):{rel}")
    target = safe_join(base, r)   # 越界(../、绝对路径顶掉 root)抛 ValueError;target 已 resolve,符号链接已跟随
    rel_norm = target.relative_to(base).as_posix()
    # 点段检查②:查 resolve 后的真实路径(挡符号链接 resolve 后落入点目录的绕过)。
    if any(seg.startswith(".") for seg in rel_norm.split("/") if seg):
        raise ValueError(f"路径不合法(解析后落入引擎自动维护区,拒绝访问):{rel}")
    if not rel_norm.startswith(_READ_PREFIXES):
        raise ValueError(f"路径不在只读白名单内(仅 {paths.BRAIN_DIR}/{paths.BODY_DIR}/skills):{rel}")
    return target


def _handle_read(root: Path, 路径: str = "", 起行: str = "", 止行: str = "", **_ignored) -> str:
    """返回文件正文;超 3k 字截断并提示带「起行/止行」参数重取指定行区间。"""
    p = _safe_read_path(root, 路径)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在:{路径}")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = int(起行) if str(起行).strip().isdigit() else None
    end = int(止行) if str(止行).strip().isdigit() else None
    if start or end:
        s = max(1, start or 1)
        e = min(len(lines), end or len(lines))
        text = "\n".join(lines[s - 1:e])
    if len(text) > _READ_MAX_CHARS:
        total_chars = len(text)
        # 提示语本身也要计入预算(之前正文截到 3000 字后再拼提示语,总长会超红线)——
        # 先把提示语拼出来,正文按「预算减提示语长度」截断,最后再钳一次兜底。
        suffix = (f"\n\n…(已截断;此区间共 {len(lines)} 行、{total_chars} 字,超 {_READ_MAX_CHARS} 字预算。"
                  f"带「起行」「止行」参数重取指定行区间。)")
        body_budget = max(0, _READ_MAX_CHARS - len(suffix))
        text = text[:body_budget] + suffix
        text = text[:_READ_MAX_CHARS]
    return text


def _handle_kandiji(root: Path) -> str:
    """槽位扫描器全量明细(spec §4/§5.3:看地基=全量明细,区别于环境快照的缩略只读投影)。

    每段列出全部槽位(不只未填的前几个),每槽一行:容器#键 + 已填/未填 + preview
    (已填值前若干字)。环境快照(T4 会建)才是「每段一行未填N/总数」的缩略版,
    两者刻意不同——伙伴细看某格要看到实际内容,靠的就是这份全量明细。
    """
    lines: list[str] = []
    for spec in journey.STAGES:
        stage_slot_list = slots.stage_slots(root, spec)
        if not stage_slot_list:
            continue
        filled_n = sum(1 for s in stage_slot_list if s.filled)
        lines.append(f"【{spec.key}】共 {len(stage_slot_list)} 槽,已填 {filled_n}/未填 {len(stage_slot_list) - filled_n}")
        for s in stage_slot_list:
            if s.filled:
                mark = f"已填：{s.preview}"
            else:
                # 未填槽附 hint(如「不是平台」),让模型知道这格填什么——已填槽 preview 已够说明,不重复附
                mark = f"未填 — {s.hint}" if s.hint else "未填"
            lines.append(f"  {s.id}：{mark}")
    if lines:
        # 地基卡是有限的。上面列的是「卡里有的」;卡里没有的,作者想记也能记。
        # 这句放在明细末尾,让角色看完已知项就知道还有出口,不用等踩一次错才发现。
        lines.append("")
        lines.append(f"(上面是地基卡里已有的项。想记一件卡里没有的事,直接给一个 "
                     f"`{paths.SET_CUSTOM_DIR_REL}/名字.md#小节名` 当落点,"
                     f"作者拍板后会自动新建;已在的设定文件也能这样加新的一节。)")
    return "\n".join(lines) if lines else "(暂无可扫描的槽位)"


def _handle_tishe(root: Path, 落点: str = "", 内容: str = "") -> dict:
    """产候选卡载荷(不写盘):校验落点/内容非空+落点存在,返回 {"slot", "content", "before"} 供
    run_tool 组装 proposal。落点不存在则抛 ValueError(LLM 自纠,不等 confirm 才炸)。
    before=落点当前 preview(现扫 stage_slots 取),confirm 落盘前会用快照守卫比对,
    防止「提案挂起期间作者手改了这一格」被覆盖(P1 快照守卫,见 partner_confirm)。"""
    slot = str(落点 or "").strip()
    content = str(内容 or "").strip()
    if not slot:
        raise ValueError("提设定缺少「落点」参数(格式:容器#键,如 外置大脑/设定集/00-立项.md#题材)。")
    if not content:
        raise ValueError("提设定缺少「内容」参数。")
    before = ""
    found_slot = None
    for spec in journey.STAGES:
        found = next((s for s in slots.stage_slots(root, spec) if s.id == slot), None)
        if found is not None:
            # bug4:占位模板不算「已有值」——只有实质内容(filled)才算 before。占位当 before
            # 会让候选卡显「现在是:(占位示例…)」+ 按钮变「替换」(误导)。必须与 usecases.
            # _slot_preview 的 filled 门同口径,否则 confirm 时 current!=before 误判 stale。
            before = found.preview if found.filled else ""
            found_slot = found
            break
    if found_slot is None:
        # 地基卡是有限的,角色想记卡里没有的事时以前无处可落。
        # 先看它能不能算「按作者想法长出来的新项」(设定集内新小节 / 50-自定义 新文件);
        # 能,就照样出候选卡(标 new),作者拍板才落盘。
        try:
            plan = slots.plan_new_slot(root, slot)
        except ValueError as new_err:
            plan = None
            new_why = str(new_err)
        else:
            new_why = ""
        if plan is not None:
            return {"slot": slot, "content": content, "before": "", "new": plan}
        # 真跑实测:模型会自己编键名(如 `#金手指·三秒逆时`),只说"去看地基"它还会接着猜。
        # 这里把它写的那份容器文件里**真实存在的键**直接摆出来,一次纠正到位。
        hint = ""
        if "#" in slot:
            cont = slot.split("#", 1)[0]
            keys: list[str] = []
            for spec in journey.STAGES:
                for s in slots.stage_slots(root, spec):
                    if s.container == cont and s.key and s.key not in keys:
                        keys.append(s.key)
            if keys:
                hint = ("该文件真实可用的键是：" + "、".join(f"{cont}#{k}" for k in keys)
                        + "。**原样复制其中一个**，不要自己造键名。")
            else:
                hint = f"没有找到容器「{cont}」的任何槽位——先用「看地基」列全部有效落点。"
        raise ValueError(
            f"落点不存在:「{slot}」。{new_why} {hint}"
            f" 想做一件地基里没有的新项,就写 "
            f"`{paths.SET_CUSTOM_DIR_REL}/名字.md#小节名`:作者拍板后会自动新建。")
    return {"slot": slot, "content": content, "before": before}


def _handle_xuegaifa(root: Path, 角色: str = "", *, backend=None) -> dict:
    """把「作者反复改成什么样」蒸成这个人格的个人增补候选(spec 2026-08-16 §5.4)。

    与「提设定」同一条红线:**只出候选卡,不落盘**——作者在卡上拍板才算数。
    区别只在落点:提设定落外置大脑(书的内容),这条落 `agents/<角色>.md` 的个人增补区
    (工具配置),基座永不碰。
    """
    from . import evolve
    role = str(角色 or "").strip()
    learnable = sorted(evolve.learnable_personas())
    if not role:
        raise ValueError(f"学改法缺少「角色」参数(今天能学的:{'/'.join(learnable)})。")
    # 能学的人格是【结构性】的:判据只能是「作者实际改成了什么」,所以那个人格必须有一件
    # 盘上可比对的产物(见 evolve._COMPARABLE,现由 artifacts.ARTIFACTS 派生 = 细纲三棒)。
    # 真机实测:对没有可比对产物的人格,ripe 恒 False、collect 恒 0——
    # 旧文案却回「再写几章再说」,那是一句作者照做一百章也不会兑现的话。
    # **说不出口的能力就别把它列成合法参数。**
    if role not in learnable:
        raise ValueError(
            f"「{role}」今天学不了:学改法比对的是「AI 交的那份」和「你改成的那份」,"
            f"而这个人格没有你能在盘上直接改的产物。今天能学的只有:{'/'.join(learnable)}。")
    if backend is None:
        raise ValueError("这一步要发一次模型调用,但当前通道没有可用后端。")
    if not evolve.ripe(root, role):
        n = len(evolve.collect(root, persona=role))
        raise ValueError(
            f"「{role}」目前只攒到 {n} 章证据,还不够归纳出稳定的改法。"
            f"再写一两章、并且动手改改它交的细纲(照单全收的那章没有可比对的差异),之后再来。")
    prop = evolve.propose(root, role, backend)
    if prop is None:
        raise ValueError(f"这次没从「{role}」的证据里归纳出反复出现的改法。")
    out = {k: v for k, v in prop.items() if k != "t"}
    # 终审③critical:webui 的 pcProposal 统一读 slot/content 渲染候选卡(与「提设定」出的卡
    # 同一套渲染逻辑)——不带这两个键,候选卡会渲染成空白正文(humanizeSlot(undefined) 填
    # 落点行、content.textContent = ev.content || "" 填空正文),且两张「人格增补」卡的 slot
    # 都是 undefined,slotTakenBySibling(prop.slot === ev.slot)会把第二张误灰成
    # 「这一格已选了其他方向」。落点固定是 agents/<角色>.md(人格增补的落点,不是外置大脑),
    # content 就是蒸出的增补正文;角色/内容 两个键仍保留(partner_confirm 的「人格增补」
    # 分支在用,见 usecases.partner_confirm)。
    out["slot"] = f"agents/{role}.md"
    out["content"] = out["内容"]
    return out


def _learnable_hint() -> str:
    """「学改法」能学谁 —— 从 evolve 派生,不手抄。

    「学改法」desc 曾经硬写「今天只有大纲师有盘上可比对的产物」;
    但 `大纲师` 早已不在 `artifacts.ARTIFACTS` 里(它是遗留模板),真正能学的是细纲三棒
    对应的人格;这段文字又会被 `render_contract()` 渲进领航员的 system prompt,
    等于每轮都在教模型去点一个必然失败的角色。改成派生,名单随产物表变。
    """
    try:
        from . import evolve

        return "/".join(sorted(evolve.learnable_personas()))
    except Exception:      # noqa: BLE001 - 文案不能因为派生失败把整个注册表拖崩
        return "细纲相关人格"


_LEARNABLE_HINT = _learnable_hint()

def _handle_wen_tongshi(root: Path, 角色: str = "", 问题: str = "", *, backend=None) -> str:
    """向另一位角色问一句专业意见。一次询问 = 一次模型调用,每轮有上限。"""
    from . import collab
    asker = collab.current_asker()
    if not asker:
        raise ValueError("现在没有进行中的对话,没法替你问同事。")
    if backend is None:
        raise ValueError("这一步要发一次模型调用,但当前通道没有可用后端。")
    out = collab.consult(asker, str(角色).strip(), 问题, root=root, backend=backend)
    return out or "(对方这次没有给出答复)"


def _handle_cha_zhishi(root: Path, 问题: str = "", 范围: str = "", *, backend=None) -> str:
    """「查知识」：把原「知识顾问」的取证能力做成**任何智库角色都能叫的工具**
    （帮设定师/大纲师/情绪节奏设计师/战斗设计师等拓展思路）。

    三条边界（都是为了「不把别人的活揉进自己的活」）：
    1. **只出提案、不落盘**：底层用 `knowledge.ask()`，它本身就不写盘；
    真要入设定集，还得走原来的「提设定」候选卡。两件事不合并。
    2. **一次 = 一次模型调用**：跟「问同事」同一个代价级，所以要在 desc 里写明“真卡住才用”。
    3. **没后端就明说**：不回一个假的“查过了”。
    """
    from . import knowledge as _kn

    q = str(问题 or "").strip()
    if not q:
        raise ValueError("「查知识」要一个具体问题，比如“明代秀才的束发在哪一年改的”。")
    if backend is None:
        raise ValueError("这一步要发一次模型调用，但当前通道没有可用后端。")

    p = _kn.ask(root, q, backend, inside=str(范围 or "").strip())
    # 把提案压成可读文本：只给这几项，别把整个 dict 吐给模型。
    # W1 修复 #6：摘取键对齐 knowledge.parse_proposal 真实产出的键
    # （主题/结论/依据/不确定/关联）——原来摘 结论/要点/可复用/存疑/引用，
    # 交集只剩「结论」，依据与不确定永远到不了父角色；那五个键是模型不会按
    # 该协议输出的假键，不补模型侧，只对齐消费侧。
    parts = []
    if p.get("主题"):
        parts.append("主题：" + str(p["主题"]))
    for k in ("结论", "依据", "不确定", "关联"):
        v = p.get(k)
        if v:
            parts.append(("## " + k + "\n" + str(v)) if isinstance(v, str) else ("## " + k + "\n" + "\n".join(str(x) for x in v)))
    if not parts:
        # 解析不出结构时，把模型原文截一段给回去（不装作成功）
        raw = str(p.get("模型原文") or "")
        return ("提案没解析出标准结构，下面是模型原文的前一段（未落盘）\n" + raw[:1500])
    return "\n\n".join(parts)


def _note_stats_report(text: str, label: str) -> str:
    """便条/任意文本的轻量统计（纯本地、零依赖语料库）。W1 修复 #11 配套。"""
    from .corpus import stats, _fmt
    st = stats(text)
    head = f"【语料数据】范围:{label}（便条文本） · {_fmt(st['字数'])} 字"
    lines = [head]
    lines.append(f"句:{_fmt(st['句数'])} 句 / 平均 {st['平均句长']} 字 / 短句(≤10字) "
                 f"{st['短句比']:.0f}% / 长句(>30字) {st['长句比']:.0f}% / 最长 {st['最长句']} 字")
    lines.append(f"段:{_fmt(st['段数'])} 段 / 平均 {st['平均段长']} 字 / 最长 {st['最长段']} 字")
    if st["标点"]:
        lines.append("标点:" + " ".join(f"{ch}{pct:.0f}%" for ch, pct in st["标点"]))
    if st["高频片段"]:
        lines.append("高频片段:" + "、".join(f"『{w}』×{n}" for w, n in st["高频片段"][:8]))
    return chr(10).join(lines)


def _handle_kandata(root: Path, 范围: str = "") -> str:
    """「看数据」:纯本地语料统计。

    之前语料分析师只能靠「读文件」把正文塞进上下文,用眼睛估词频/句长——又贵又不准,
    还得一章一章读。这里把能算的都在本地算准(见 corpus.py),模型只负责解读。
    **不发模型、不计费、不写盘。**

    W1 修复 #11：区分两个输入域——
    - 书内语境（root 是一本书，有 loom.toml）：走 corpus.report 按章正文统计（原行为）；
    - 书房语境（root 是知识库根，无 loom.toml）：便条/任意文本走轻量文本统计
      （corpus.stats 纯本地计算，不依赖 正文/第N章.md）。「范围」此时是知识库根下的
      相对路径或文件名；不填就把便条目录里最近改动的几份 .md 拼起来统计。
    """
    from . import corpus
    if (Path(root) / "loom.toml").is_file():
        return corpus.report(root, str(范围 or "").strip())
    # 书房语境：任意文本统计
    if str(范围 or "").strip():
        p = Path(root) / str(范围).strip()
        p = p if p.is_file() else Path(root) / "便条" / str(范围).strip()
        if not p.is_file():
            return f"没找到叫「{范围}」的文本。书房语境下「范围」给知识库根下的文件名。"
        text = p.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return "这份文本是空的，没有可统计的。"
        return _note_stats_report(text, p.stem)
    # 范围空：把知识库根下的便条 .md（浅层，跳过 _ 开头/目录元数据）拼起来统计
    notes = []
    for p in sorted(Path(root).glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            notes.append((p.stem, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    if not notes:
        return "书房语境下没有可统计的便条文本（知识库根下没有 .md）。统计书内章正文请对着一本书调。"
    text = "\n".join(t for _, t in notes)
    return _note_stats_report(text, "、".join(n for n, _ in notes[:5]) + ("…" if len(notes) > 5 else ""))


def _handle_gai_zhiwen(root: Path, 段: str = "", 内容: str = "") -> dict:
    """「改指纹」:给写作指纹加一条(候选卡,作者拍板才落盘)。

    为什么单开一个工具而不是走「提设定」:写作指纹不在设定集里(`提设定` 的落点白名单只放
    设定集与分析目录),而且它的小节是固定的五节 —— 得有个地方把「段」卡死,
    不然模型会自己长出一节「## 我的独特风格」,写手读了也不知道照哪条。
    落盘只增不删(`journey._land_fingerprint`),删/改既有规则那是作者自己的事。
    """
    from . import paths as _paths
    from .fingerprint import resolve_section

    section = resolve_section(段)          # 认不出 → ValueError(带可选值)
    content = (内容 or "").strip()
    if not content:
        raise ValueError("改指纹缺少「内容」参数。把要加的那一条写进去。")
    return {
        "kind": "写作指纹",
        "段": section,
        "slot": f"{_paths.FINGERPRINT_REL}#{section}",
        "content": content,
        "before": _fingerprint_preview(root, section),
    }


def _fingerprint_preview(root: Path, title: str) -> str:
    """该小节现在写了什么(卡片上「现在是:…」用)。空 = 还没写。"""
    from . import journey as _journey
    from .paths import FINGERPRINT_REL

    p = Path(root) / FINGERPRINT_REL
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        span = _journey._find_section_span(lines, title)
    except Exception:       # noqa: BLE001 - 预览读不出来不该把工具堵死
        return ""
    if span is None:
        return ""
    start, end = span
    body = [l.strip() for l in lines[start + 1:end] if l.strip()]
    if not body:
        return ""
    txt = " / ".join(body)
    return txt[:60] + ("…" if len(txt) > 60 else "")




# ── 书房「知识顾问 / 语料分析师」两个角色的能力工具化 ──
# 他们的页面（study.html 的 roles.js / archive.js）背后的接口本来就是现成的，
# 这里包成工具给所有智能体用。口径与那两个页面完全一致：
#   取稿会访问 B 站（作者点「取稿」才连）；档案只读 / 删除要走确认。

def _handle_qu_gao(root: Path, 地址: str = "", *, backend=None) -> dict:
    """取稿：把一个 B 站视频取成完整证据。
    ① 字幕/音轨转写（口播内容）——永远做。
    ② 画面：帧采样交给 fetch_text（纯文字一趟 + 帧采样一趟）；
    探测当前模型是否支持视觉——支持则把帧图一起装进返回值（图片直接进模型消息），
    不支持则只给帧文字 OCR（画面里的字幕/PPT 文字提取）。
    **会访问 B 站**：一次调用 = 一次外发，作者明确给链接才调。取回后先总结要点，
    再问作者要不要沉淀成笔记。

    2026-11-08（作者报告「图片识别怎么还是没起效」）：
    - 返回形状 str → dict：视觉可用时附 images:[{mime, b64, times}]，
      对话层认到这个键就把图装进模型消息（文本协议/native 都能吃到）；
      纯 str 仍在（意外路径兜底），对话层只把 dict 当文本铺平。
    - 视觉探测的 key：原写死 LOOM_OPENAI_COMPAT_KEY，而默认供应商是 deepseek
      （key 在 DEEPSEEK_API_KEY）→ 永远「无 key」→ 永远退化 OCR。现按供应商
      注册表的 key_env 取，再兜环境直读 ~/.loom/.env。
    """
    addr = str(地址 or "").strip()
    if not addr:
        return {"t": "result", "text": "取稿要给个视频地址或 BV 号。"}
    from .study import video as _v
    from .backends import supports_vision as _sv, PROVIDERS as _PROV
    # ① 探测当前模型视觉能力（探测失败=保守走 OCR 路径）
    # 2026-11-08 审查修正（多模态增量审查 #1）：原 `from ..config`/`from .. import userconf`
    # 在 loom.partner_tools 内是越包导入（.. 指向 loom 之外）→ ImportError 被下方 except
    # 整块吞掉 → base_url/model 恒空 → 视觉探测从未执行。改为包内正确导入；
    # 并优先用本轮实际 backend 的 model/端点做能力真源（书级路由可能选了别的后端，
    # 探测 A 却把图送给 B 是审查点名的错配）。
    vision = False
    base_url = model = api_key = ""
    try:
        import os as _os
        from .config import load_config as _lc
        from . import userconf as _uc
        cfg = _lc(root)
        d = _uc.load_default_backend()
        provider = (d.get("provider") or "").strip()
        base_url = (d.get("base_url") or cfg.base_url or "").strip()
        # model：本轮 backend 的真实模型优先（OpenAICompatBackend.model / CLI 的 .model），
        # 取不到再退用户级默认/书级配置。
        _bm = (getattr(backend, "model", "") or "").strip()
        model = _bm or (d.get("model") or cfg.model or "").strip()
        # base_url：backend 客户端里存的真实端点优先（SDK client.base_url / Lite._base），
        # 取不到再退配置链。
        _bb = ""
        _client = getattr(backend, "_client", None)
        if _client is not None:
            _bu = getattr(_client, "base_url", None)
            _bb = str(_bu) if _bu else (getattr(_client, "_base", "") or "")
        base_url = (_bb or base_url).strip().rstrip("/")
        # key：按供应商注册表的 key_env 找（deepseek→DEEPSEEK_API_KEY，…），
        # 环境没有再试 ~/.loom/.env（ready() 同款兜底）。
        key_env = (_PROV.get(provider) or {}).get("key_env") or ""
        api_key = _os.environ.get(key_env, "") if key_env else ""
        if not api_key:
            try:
                from dotenv import load_dotenv
                from .config import user_env_path as _uep
                load_dotenv(_uep(), override=True)
                api_key = _os.environ.get(key_env, "") if key_env else ""
            except Exception:      # noqa: BLE001
                pass
        # 兜底：LOOM_OPENAI_COMPAT_KEY（原行为，自定义网关仍用它）
        if not api_key:
            api_key = _os.environ.get("LOOM_OPENAI_COMPAT_KEY", "")
    except Exception:
        pass
    if base_url and model:
        vision = _sv(base_url, model, api_key)
    # ② 取稿：纯文字一趟 + 帧采样一趟（帧图/OCR 已在证据里）
    got = _v.fetch_text(addr, ocr=True, timeout=900)
    if not got.get("ok"):
        return {"t": "result", "text": "取稿没成：" + str(got.get("text") or got.get("code") or "")}
    ev = got.get("证据") or {}
    body = str(ev.get("text") or "")
    imgs = ev.get("images") or []
    head = "取到稿了（" + str(len(body)) + " 字）。"
    payload_imgs: list[dict] = []
    if vision and imgs:
        # 帧图装进返回值 → 对话层把它喂进模型消息（图片真进上下文）
        import base64 as _b64
        import os as _os2
        for it in imgs:
            try:
                p = str(it.get("path") or "")
                if p and _os2.path.isfile(p):
                    with open(p, "rb") as fh:
                        payload_imgs.append({
                            "mime": "image/jpeg",
                            "b64": _b64.b64encode(fh.read()).decode("ascii"),
                            "times": it.get("times") or [],
                        })
            except Exception:      # noqa: BLE001 - 单张失败不拦整份
                continue
        if payload_imgs:
            head += "当前模型支持视觉：" + str(len(payload_imgs)) + " 张采样帧图已附上，请直接结合画面分析（带时间标注）。"
        else:
            head += "帧图读不出来（文件缺失），只能按画面 OCR 文字理解。"
    elif imgs:
        ocr_note = ev.get("画面文字") or ""
        head += "当前模型不支持视觉：已按帧文字 OCR 理解画面" + (
            ("（" + str(len(ocr_rows)) + " 行画面文字已并入正文）。") if (ocr_rows := (ocr_note or "").splitlines()) else "（画面文字为空——视频可能没挂字幕）。")
    if not body or "(无文本证据)" in body:
        return {"t": "result", "text": head + " 但证据为空，无法总结。",
                **({"images": payload_imgs} if payload_imgs else {})}
    text = head + chr(10) + body[:4000]
    ocr_extra = str(ev.get("画面文字") or "")
    if ocr_extra:
        text += chr(10) + "【画面文字】" + chr(10) + ocr_extra[:2000]
    out = {"t": "result", "text": text}
    if payload_imgs:
        out["images"] = payload_imgs
    return out



def _handle_kan_dangan(root: Path, 档案: str = "") -> str:
    """看文风档案：列出有哪些档案，或看一份档案的三层内容。

    书房语料分析师页背后的就是这一条（GET/POST /style/*）。**只读**——
    删除和重新蒸馏不做成工具：那是作者在页面上带确认才能动的。
    """
    from pathlib import Path as _P
    name = str(档案 or "").strip()
    if not name:
        return "要看重哪些档案？给档案名；不知道有哪些就传「?」让我列清单。"
    try:
        from .study.library import library_root as _lr
        base = _lr() / "语料库"
        if not base.is_dir():
            return "还没有语料库目录（蒸馏过才会有）。"
        if name == "?" or name == "？":
            dirs = sorted(d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith("_"))
            return "语料库现有档案：" + ("、".join(dirs) if dirs else "（空）")
        safe = _P(name).name   # 防路径穿越：只认目录名本身
        d = base / safe
        if not d.is_dir():
            return "没有叫「%s」的档案。传「?」让我列清单。" % safe
        out = []
        for layer in ("原文", "蒸馏原稿", "写手参考"):
            f = d / (layer + ".md")
            # W1 修复 #10：前两层在真实库结构里是**目录**（stylepack.DOCS_SUB/DISTILLED_SUB），
            # 只有「写手参考」是单文件——原来逐层读单文件，前两层恒显"未生成"。
            # 目录层：读该目录下的 .md（按名字排序、每篇截 1200 字预算内拼接）；单文件层照旧。
            if f.is_file():
                body = f.read_text(encoding="utf-8", errors="replace")
                out.append("【" + layer + "】(" + str(len(body)) + " 字)")
                out.append(body[:1200])
                continue
            sub = d / layer
            if sub.is_dir():
                files = sorted(p for p in sub.iterdir() if p.is_file() and p.suffix.lower() == ".md")
                if files:
                    total = 0
                    pieces = ["【" + layer + "】（目录，" + str(len(files)) + " 篇）"]
                    for p in files:
                        body = p.read_text(encoding="utf-8", errors="replace")
                        pieces.append("— " + p.stem + "（" + str(len(body)) + " 字）")
                        pieces.append(body[:1200])
                        total += len(body)
                    pieces.append("（本层共 " + str(total) + " 字，每篇截前 1200 字展示。）")
                    out.extend(pieces)
                else:
                    out.append("【" + layer + "】目录是空的")
            else:
                out.append("【" + layer + "】未生成")
        return chr(10).join(out)
    except Exception as e:
        return "读不了：" + str(e)


# ── 向量记忆库的两把入口 ──
# 速记本是唯一事实源，这里是索引层的读写口。随想也可以记，
# 但 author_note 必须如实标来源（作者原话/已拍板/随想/联想）——
# 检索时标签跟着给模型，让它自己判断可信度。进正文仍必须走候选卡。

_MEM_MODE_KEY = "LOOM_MEMORY_MODE"   # 主动思考/默认/安静
_MEM_LIMITS = {"主动思考": 4, "默认": 2, "安静": 1}

def _mem_limit() -> int:
    mode = os.environ.get(_MEM_MODE_KEY, "").strip()
    return _MEM_LIMITS.get(mode, 2)   # 没设就是默认档

def _handle_ji_yaodian(root: Path, 内容: str = "", 来源: str = "") -> str:
    """「记要点」：把一条信息沉淀进记忆库（速记本落原文 + 向量入库）。

    随想也可以记——**但来源必须如实标**（作者原话/已拍板/随想/联想），
    检索时标签跟着走，模型自己判断可信度。进正文永远要走「提设定」。"
    """
    from . import memory as _m
    from . import collab as _c
    body = str(内容 or "").strip()
    if not body:
        return "要记的内容是空的。"
    src = str(来源 or "").strip() or "随想"
    note = src if src in ("作者原话", "已拍板", "随想", "联想") else ("随想/" + src)
    # 限流按行动模式（三档）
    used = getattr(_handle_ji_yaodian, "_used", 0)
    if used >= _mem_limit():
        return "这一轮已经记了 %d 条（行动模式上限）。真重要的等下一轮再记。" % used
    book = str(os.environ.get("LOOM_PROJECT_ROOT", "")).strip()
    role = _c.current_asker().strip()
    # collab 的 asker 可能带显示后缀（如"领航员(无会话)"），剥掉——
    # 速记本与记忆库都以**纯角色名**为键
    role = role.split("(")[0].strip() or "领航员"
    got = _m.remember(root, role, book, body, author_note=note)
    if not got.get("ok"):
        return "没记上：" + str(got.get("reason"))
    _handle_ji_yaodian._used = used + 1
    return "记下了（%s，第 %d 条/本轮上限 %d）。原文在速记本-%s。" % (note, used + 1, _mem_limit(), role)


def _handle_xiang_qi(root: Path, 问题: str = "", 书: str = "") -> str:
    """「想起」：按问题向量检索记忆库，命中的回速记本取**原文段落**。

    检索结果永远带来源标签——随想不是事实，进正文要走「提设定」。"""
    from . import memory as _m
    q = str(问题 or "").strip()
    if not q:
        return "想什么？给一个具体问题。"
    book = str(书 or "").strip() or str(os.environ.get("LOOM_PROJECT_ROOT", ""))
    # 书房角色跨书可读——scope=all；
    # 织布机六智库调这里时（agent_chat 共用 REGISTRY）保持 book 域，防书间污染。
    scope = "all" if str(os.environ.get("LOOM_ROLE", "")) in ("书房伙伴", "知识顾问", "语料分析师") else "book"
    got = _m.recall(root, q, book=book, scope=scope)
    if not got.get("ok"):
        return "记忆库暂时用不了：" + str(got.get("reason"))
    rows = got.get("条") or []
    notice = got.get("notice", "")
    if not rows:
        return (notice + "\n" if notice else "") + "没想起相关的。可以换个说法再试。"
    from . import scratchpad as _sp
    out = ([notice] if notice else []) + ["想起 %d 条（越靠前越相关）：" % len(rows)]
    for i, r in enumerate(rows, 1):
        out.append(chr(10) + "[%d] (%s · %s · 分 %.2f)" % (i, r["role"], r["book"] or "全局", r["score"]))
        # 原文按 anchor 回速记本取（事实源在那里，摘要只是排序用的）
        hit = r.get("原文") or _sp.find_by_anchor(Path(r["book"]) if r.get("book") else root,
                                                r["role"], r["anchor"])
        out.append(hit if hit else r["anchor"])
    out.append(chr(10) + "（记忆是工作草稿，要进正文必须走「提设定」/「改指纹」，作者拍板才算。）")
    return chr(10).join(out)




# ── 语料分析师独立 agent 的蒸馏工具 ──
# 原语料分析师页的蒸馏接口（/style/*）收进工具面。批量=agent 循环调本工具，
# 每篇一次模型调用；进度逐篇回报（agent 汇总给作者）。

def _handle_zhengliu(root: Path, 标题: str = "", 文本: str = "", 来源: str = "", *, backend=None) -> str:
    '蒸馏：把一份文本蒸成三层档案（原文/蒸馏原稿/写手参考），落进语料库。'
    '**会调模型**（=花一次钱）：作者在对话里明确要求蒸馏时才调。重名不覆盖。'
    title = str(标题 or "").strip()
    text = str(文本 or "").strip()
    if not title or not text:
        return "蒸馏要给标题和文本。多份文本请一份一调，标题区分。"
    # W1 修复 #8：三处都坏过——①首次 import 在 try 外（ImportError 直接冒出 handler）；
    # ②入口名 `distill_sync` 不存在（stylepack 只有 distill_docs）；
    # ③签名也对不上（distill_docs(root, name, titles, backend) 四参）。
    # 现在整体纳入 try；入口对齐 distill_docs：先 add_doc 把文本落进该档案的 原文/ 层，
    # 再调 distill_docs(root, name, [标题], backend)——参数按真实签名适配；
    # backend 由 run_tool 按声明注入（同「查知识」口径），缺了明说，不悄悄走错通道。
    try:
        from . import stylepack as _sp
        if backend is None:
            return "这一步要发模型调用，但当前通道没有可用后端。"
        got = _sp.add_doc(root, title, title, text)
        if not got.get("ok"):
            return "蒸馏没成（原文入库失败）：" + str(got.get("reason") or got.get("code"))
        out = _sp.distill_docs(root, title, [title], backend)
        if not out.get("ok"):
            reasons = "；".join(str(f.get("原因") or "") for f in out.get("失败") or [])
            return "蒸馏没成：" + (reasons or str(out.get("reason") or out.get("code") or "未知原因"))
        n = int(out.get("成功数") or 0)
        return "蒸馏完成：《%s》已蒸出三层档案并落进语料库（成功 %d 篇）。" % (title, n)
    except ImportError as e:
        return "蒸馏同步入口没接上（%s）——请从文风档案页手动蒸，或检查 stylepack。" % e
    except Exception as e:      # noqa: BLE001 - 蒸馏失败要给人话，不许崩出 handler
        return "蒸馏没成：%s: %s" % (type(e).__name__, e)

def _handle_sou_shufang(root: Path, 查询: str = "") -> str:
    """「搜书房」：在全部教材/素材里搜一次（字面+语义），返回节选。

    原是知识顾问旧工具面（study/tools.search_books），现在任何智库角色都能叫：写作时查教材
    本来就是设定师/大纲师/战斗设计师等的职责。一次调用只搜一次。"""
    from .study import ask as _ask
    q = str(查询 or "").strip()
    if not q:
        return "搜书房要一个查询词。"
    try:
        got = _ask.search(q, top_k=6)
    except Exception as e:
        return f"搜书房失败:{e}"
    # W1 修复 #7：底层 search 的返回形状是 {节选,统计}（无 ok 键）——判 `ok`
    # 恒 False，任何查询都回"书房检索暂不可用"。改为判**存在性**（节选键在不在），
    # 不在才报不可用；片段取键同步从「文」改回真实键「正文」。
    hits = got.get("节选")
    if hits is None:
        return "书房检索暂不可用。"
    if not hits:
        return "书房里没搜到相关内容。可以换个说法。"
    how = "字面+语义" if (got.get("统计") or {}).get("语义") else "字面"
    out = [f"搜到 {len(hits)} 段（{how}）："]
    for i, e in enumerate(hits, 1):
        out.append(f"[{i}] 《{e.get('卷', '')}》{e.get('节', '')}")
        out.append(str(e.get("正文", ""))[:300])
    return chr(10).join(out)


def _handle_luo_biji(root: Path, 标题: str = "", 稿子: str = "") -> str:
    """「落笔记」：把一份稿子整理成笔记落进个人图书馆（素材层）。

    知识顾问提示词里的第三职责（他说"这条留下"才调）。真实实现一直走
    /api/study/material 端点（作者在界面上点），Agent 侧一直缺这把工具——
    _ROLE_TOOLS 列了名字、REGISTRY 却没有，模型被静默吞掉调用。这里补上：
    同一次模型调用（material.ingest ③理解→④沉淀），落点 知识库/素材/<科目>/。
    只写素材层，不碰教材/语料库/书的目录。
    W1 修复 #9：注册从 mutates=True 改为 **False**。理由（以 run_tool 的消费
    逻辑为准）：mutates=True 的分支组装的是 **proposal 候选卡事件**（t=proposal，
    等作者拍板才落盘），而落笔记在 handler 内部**已经落盘**、没有拍板环节——
    把它注册成 mutates 属语义错位，还要求 handler 返回 dict 载荷（原返 str
    在 `**result` 处 TypeError，造成"笔记已落盘、事件组装却崩"的成功后崩溃）。
    改 False 后返回 str → {"t":"result","text":…}，与事件组装契约对齐。
    """
    title = str(标题 or "").strip()
    text = str(稿子 or "").strip()
    if not title or not text:
        return "落笔记要给标题和稿子。稿子就是取回来的字幕/转写全文。"
    from .study.material import MAX_TEXT_CHARS, ingest, save_note
    if len(text) > MAX_TEXT_CHARS:
        return "稿子太长（>%d 字），请先分段落笔记。" % MAX_TEXT_CHARS
    # 同步调用 material 的两步：此处 backend 缺省时 ingest 自己取书房配置。
    # ingest 是事件驱动的（emit 回调），这里收完 done/error 转成工具结果文本。
    state: dict = {}
    ingest(title, text, lambda ev: state.update(ev, _final=ev.get("t") in ("done", "error")))
    if state.get("t") == "error":
        raise ValueError("落笔记没成：" + str(state.get("text") or state.get("code")))
    if state.get("t") == "done":
        return "落好了：《%s》(%s 字) → %s" % (state.get("标题") or title,
                                          state.get("字数"), state.get("路径"))
    raise ValueError("落笔记流程意外结束（没收到结果事件），请重试或从界面上落。")

REGISTRY: dict[str, ToolSpec] = {
    "读文件": ToolSpec(
        name="读文件", params=("路径",),
        desc="返回文件正文(只读白名单:外置大脑/skills/正文);超 3000 字截断,可带「起行」「止行」重取。",
        handler=_handle_read, mutates=False,
    ),
    "看地基": ToolSpec(
        name="看地基", params=(),
        desc="槽位扫描器全量明细:各段(立项/世界观/人物/卡章纲)每个槽位的完整ID(容器#键)、已填/未填、preview。"
              "落点必须从本工具的返回值中完整复制,不要自己构造 ID 或路径。",
        handler=_handle_kandiji, mutates=False,
    ),
    "问同事": ToolSpec(
        name="问同事", params=("角色", "问题"),
        desc="向另一位角色问一句专业意见(如向情绪节奏设计师问节奏、向战斗设计师问打斗)。"
             "一次询问 = 一次模型调用,每轮最多 2 次;只在真卡住时用,拿到意见自己判断要不要采纳。",
        handler=_handle_wen_tongshi, mutates=False,
    ),
    "学改法": ToolSpec(
        name="学改法", params=("角色",),
        desc="把作者反复改成什么样,蒸成这个人格的写法增补(候选卡,作者拍板才落)。"
             "只在环境快照提示某个人格「攒够证据」时才用;别对快照没提到的人格试——"
             "今天能学的只有" + _LEARNABLE_HINT + ",别的人格调了必然失败。",
        handler=_handle_xuegaifa, mutates=True,
    ),
    "提设定": ToolSpec(
        name="提设定", params=("落点", "内容"),
        desc="产出候选卡(proposal),不写盘;作者拍板确认才落盘。"
             "落点有两种写法:①地基里现成的槽(用「看地基」拿到的完整 ID);"
             "②**新项**——地基卡里没有的东西也能记:"
             f"`{paths.SET_CUSTOM_DIR_REL}/名字.md#小节名` 会新建一份(可省掉 #小节名);"
             "已存在的设定文件想加一节,写 `该文件路径#新小节名`。"
             "新项同样只出候选卡,作者拍板后才落盘。"
             "**「内容」里只放要进设定集的原文**;要说给作者听的话(建议、要不要、下一步)"
             "请放在最后另起一段——我会把它当附言显示,不会写进设定。",
        handler=_handle_tishe, mutates=True,
    ),
    "查知识": ToolSpec(
        name="查知识", params=("问题", "范围"),
        desc=("查一个具体事实（年代/器物/术语/地理/真实案例），拿回一份**未落盘的提案**。"
            "一次调用 = 一次模型调用，**真卡住才用**（你自己已经知道的别查）。"
            "它不会把东西写进设定集：要落盘另用「提设定」。范围可不填。"),
        handler=_handle_cha_zhishi, mutates=False,
        #查知识改成**所有智能体都可调**（知识顾问已工具化）
        setting_body=False,
    ),
    "看数据": ToolSpec(
        name="看数据", params=("范围",),
        desc="纯本地语料统计(**不发模型、不计费**):字数/句数/平均句长/短句长句比例/段落/标点/句首/"
             "高频片段/章长分布,再算「你自己的绝不这么写」在正文里命中几处、AI腔判了几处。"
             "「范围」留空=全书,也可写 第12章 / 第5-9章 / 最近5章。"
             "要给建议就先看数据,别凭印象说文风。",
        handler=_handle_kandata, mutates=False,
    ),
    "改指纹": ToolSpec(
        name="改指纹", params=("段", "内容"),
        desc="给写作指纹加一条(候选卡,作者拍板才落盘)。「段」只能是这五个之一:"
             "句式偏好 / 口头禅 / 禁用词 / 节奏 / anchor 例句。"
             "「内容」写要加的那条本身(anchor 填作者的原句,别的填规则,一行一条)。"
             "只增不删:它删不掉既有规则——那是作者的嗓音。",
        handler=_handle_gai_zhiwen, mutates=True,
        #改指纹改成**所有智能体都可调**（语料分析师已工具化）
    ), "取稿": ToolSpec(
    name="取稿", params=("地址",),
    desc="把一个 B 站视频取成文字稿（字幕优先，缺字幕用自带转写救）。**会访问 B 站**：一次调用 = 一次外发，只在作者明确要取稿时用。拿回来的是证据与原文，要不要沉淀进个人图书馆等作者拍板。",
    handler=_handle_qu_gao, mutates=False,
    ),
 "看档案": ToolSpec(
    name="看档案", params=("档案",),
    desc="看文风档案：传「?」列出现有档案；传档案名看它的三层（原文/蒸馏原稿/写手参考）。**只读**，删除与重新蒸馏不在此列（作者在页面上带确认才能动）。",
    handler=_handle_kan_dangan, mutates=False,
    ), "记要点": ToolSpec(
    name="记要点", params=("内容", "来源"),
    desc="把一条信息沉淀进记忆库：速记本落原文，向量入库供日后「想起」。随想也可以记，但「来源」必须如实标（作者原话/已拍板/随想/联想）。记忆是工作草稿：进正文永远走「提设定」。",
    handler=_handle_ji_yaodian, mutates=False,
    ),
 "想起": ToolSpec(
    name="想起", params=("问题", "书"),
    desc="按问题检索记忆库（agent 们沉淀的要点），命中回速记本原文。检索永远带来源标签——随想不是事实。书参数留空=全书+全局。",
    handler=_handle_xiang_qi, mutates=False,
    ),
 "搜书房": ToolSpec(
    name="搜书房", params=("查询",),
    desc="在全部教材/素材里搜一次（字面+语义），返回几段节选。写作时查教材先例、查世界观依据都用这把。一次调用只搜一次。",
    handler=_handle_sou_shufang, mutates=False,
    ), "落笔记": ToolSpec(
    name="落笔记", params=("标题", "稿子"),
    desc="把一份稿子（字幕/转写全文）整理成笔记落进个人图书馆。**会调模型（花一次钱）**：作者明确说「这条留下」「落成笔记」时才调。只写素材层，重名不覆盖。落盘即时生效（无拍板环节）。",
    handler=_handle_luo_biji, mutates=False,
    ), "蒸馏": ToolSpec(
    name="蒸馏", params=("标题", "文本", "来源"),
    desc="把一份文本蒸成三层语料档案（原文/蒸馏原稿/写手参考），落进语料库。**会调模型（花一次钱）**：作者明确要求蒸馏时才调。批量任务=逐份调用，每份一调。",
    handler=_handle_zhengliu, mutates=False,
    ),
}


def visible_tools(role: str = "") -> dict[str, ToolSpec]:
    """该角色能看到的工具(按 `roles` 过滤;空 roles = 人人可用)。"""
    who = (role or "").strip()
    if not who:
        return dict(REGISTRY)
    return {n: s for n, s in REGISTRY.items() if not s.roles or who in s.roles}


# ── 0.9.0 工具合一(4.2):schema/contract 的单一实现 ─────────────────────
# 之前三处各写一份同构的 OpenAI tools schema 生成(agent_chat._tools_schema /
# study.tools.schema / study.ask._RoleToolset.schema),契约文本四种口径。
# 这里收口成两个纯函数:输入 [(中文名, 参数元组, 描述, ASCII名)],输出统一形状。
# 两个域的 spec 形状不同(ToolSpec dataclass vs dict),由调用方先归一成四元组。

def schema_of(specs: list[tuple[str, tuple, str, str]]) -> list[dict]:
    """四元组列表 → OpenAI tools schema。specs 元素 = (中文名, params, desc, ascii名)。"""
    out = []
    for name, params, desc, ascii_name in specs:
        ps = list(params or ())
        out.append({"type": "function", "function": {
            "name": ascii_name or name,
            "description": desc,
            "parameters": {"type": "object",
                           "properties": {p: {"type": "string"} for p in ps},
                           "required": ps}}})
    return out


def contract_of(specs: list[tuple[str, tuple, str, str]], *, header: str,
                footer: str = "") -> str:
    """四元组列表 → 契约文本。header/footer 由调用方定(协议外壳不同:英文 TOOL 块 vs 中文「用:」行)。"""
    lines = [header]
    for name, params, desc, _ascii in specs:
        tail = ("(" + "、".join(params) + ")") if params else ""
        lines.append("- %s%s：%s" % (name, tail, desc))
    if footer:
        lines.append(footer)
    return chr(10).join(lines)


def registry_specs(role: str = "", *, locked_slot: str = "") -> list[tuple[str, tuple, str, str]]:
    """REGISTRY(按角色过滤) → 四元组列表。locked_slot 非空时「提设定」收成单参(丁方案:落点不进模型)。"""
    from .agent_chat import _TOOL_ASCII as _ASCII   # ASCII 映射单一真相在 agent_chat
    out = []
    for spec in visible_tools(role).values():
        params = list(spec.params or ())
        desc = spec.desc
        if spec.name == "提设定" and locked_slot:
            params = ["内容"]
            desc = "提交一条设定候选。**写入位置已由会话锁定，你不需要也不能指定落点**，只给内容。"
        out.append((spec.name, tuple(params), desc, _ASCII.get(spec.name, spec.name)))
    return out


def render_contract(role: str = "") -> str:
    """渲染进 prompt 的工具契约段(稳定前缀②):注册表单一真相,不手写第二份协议文案。

    两次真机实测后定稿（工具协议外壳改英文，与中文对话区分）：
    - 第 1 次：本地 35B 把 `用:提设定` 加粗写进 Markdown 正文，参数只留第一行；
    - 第 2 次：它**完全没调工具**，只在正文里"描述"候选卡 —— 纯中文前缀挡不住。
    于是协议外壳换成英文 + ```loom 代码块围栏：结构标记走英文（与中文叙述视觉上彻底分开），
    参数名和内容仍是中文（模型理解成本不变）。解析侧另有 `_unwrap_tool_fences` 兜底。
    """
    lines = [
        "## Tools（要动手就必须写 tool block；只在正文里描述不算调用）",
        "",
        "调用格式：把调用包进 ```loom 代码块，块内第一行 `TOOL: 工具名`，之后每行一个参数。",
        "",
        "正确示范：",
        "```loom",
        "TOOL: 提设定",
        "落点: 外置大脑/设定集/10-世界观/金手指.md#核心功能",
        "内容: 逆息每用一次折损三年寿元",
        "```",
        "",
        "错误示范（以下一律不生效，等于没做）：",
        "- 只在正文里写「我会用提设定把这条写进设定集」← 没有 tool block",
        "- 写成函数调用样子 `提设定(落点=…, 内容=…)`",
        "- 用了别的语言标记（```markdown / ```json）",
        "",
        "工具清单（一次最多 5 个工具块；要一次给作者几个方向挑，就连写几个 ```loom 块）：",
    ]
    for spec in visible_tools(role).values():
        params_txt = "、".join(spec.params) if spec.params else "无"
        lines.append(f"- `{spec.name}` | 参数：{params_txt} | {spec.desc}")
    return "\n".join(lines)


def _roster_names(project_root) -> list[str]:
    """人物专名册 → 名字列表(长的排前面,避免「林」抢先匹配掉「林三」)。

    判据不新造词表:直接用写正文时就在用的 `agents._name_roster_for`(老书读人物卡 H2,
    目录形态读 人物/ 下一人一文件,文件名即「类型·名字」)。
    """
    try:
        from .agents import _name_roster_for

        raw = _name_roster_for(project_root) or ""
    except Exception:      # noqa: BLE001 - 名册读不出来不该把提设定整条堵死
        return []
    names: list[str] = []
    for line in raw.splitlines():
        s = line.strip().lstrip("-").strip()
        if not s:
            continue
        if "·" in s:
            s = s.split("·", 1)[1].strip()
        if s:
            names.append(s)
    names.sort(key=len, reverse=True)
    return names


def _cut_at_dialogue(project_root, text: str) -> tuple[str, int, str]:
    """把「对白行」从内容里截掉,返回 (截断后文本, 行号, 那一行原文)。

    B:body 回填机制(见 run_tool)是为了**多行设定内容**,
    但它同时会把作者/领航员对话里的「林三：这样如何？」这类**对白行**一起带进「内容」。
    两套护栏正面冲突,这里给边界:**从第一行"人名 + 冒号"开头的行起,后面全部不收**。
    判据用人名册(现成),不新造词表 —— 设定内容里几乎不会出现「张三：」这种行;
    真出现了,截断 + 明说,比静默吞掉安全(作者能手动补回去)。
    """
    names = _roster_names(project_root)
    if not names:
        return text, 0, ""
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        s = line.strip()
        for nm in names:
            if s.startswith(nm + "：") or s.startswith(nm + ":"):
                return "\n".join(lines[: i - 1]).rstrip(), i, s
    return text, 0, ""


# 作者反馈:角色常把「后半截要跟作者说的话」一起塞进候选卡(如
# 「…设定内容…\n\n另外我建议把这条也写进人物卡,要不要?」——后一句会进「内容」里落盘)。
# 这些词开头的行、以及「不像结构行的短问句」「以你/您开头」几乎都是**说给作者听的话**,
# 不是设定原文。判据偏保守:只有第 2 行起才截,首行不动(首行是承接着「内容:」的正文)。
_TALK_PREFIX = re.compile(
    r"^(附言|说明|备注|补充一句|另外|对了|顺便|此外|要不要|需要我|我可以|我建议|建议你|"
    r"如果你|你可以|你也可以|下一步|接下来|稍后|等你|你看|我先|这样吧|那我|我这边|"
    r"以上|就这样|请确认|确认后|要是你觉得|你觉得|需要的话)")
_STRUCT_HEAD = re.compile(r"^([|#>\-*+]|\d+[.、)])")


def _looks_like_talk(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _TALK_PREFIX.match(s):
        return True
    if s[0] in "你您":
        return True
    # 短问句 + 不像结构行 → 多半是在问作者要不要这么办
    return bool(s[-1] in "？?" and len(s) <= 60 and not _STRUCT_HEAD.match(s))


def _split_trailing_talk(text: str) -> tuple[str, str]:
    """把「要对作者说的话」从内容里剥出来,返回 (设定正文, 附言)。

    附言**永远不落盘**,只作为候选卡上的提示给作者看(见 run_tool 的 note)。
    宁可漏判(把话当正文)也不能错杀正文:所以首行永不截,且要求命中明确的说话标志。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if i == 0:
            continue
        if _looks_like_talk(line):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
    return text, ""


def _prepare_content(root, text: str) -> tuple[str, list[str]]:
    """落地前的两道机械清理:先截对白行,再剥附言。返回 (正文, 说明列表)。

    修:这一套**原来只在「多行内容落进 body」时**才跑 —— 而原生函数调用
    (native)那条路把 `内容` 参数直接送进卡里,一行都没过滤,于是作者仍能看到杂话。
    现在两条路共用这一个入口,口径只有一份。
    """
    if not (text or "").strip():
        return text, []
    body, cut_line, cut_text = _cut_at_dialogue(root, text)
    body, aside = _split_trailing_talk(body)
    notes: list[str] = []
    if cut_line:
        notes.append(f"第 {cut_line} 行「{cut_text[:40]}」看着像对白,已截断,没写进候选卡。")
    if aside:
        notes.append(f"后面这段看着是你要对作者说的话,没写进设定:「{aside[:60]}」"
                     f"(要落盘的话,重发一次只带设定正文。)")
    return body, notes


# ── 候选卡自检(「让 agent 看一眼候选卡内容,决定要不要再改一次」)──
# 设计取舍:**让模型当"指路牌",让代码动刀**。
# 不采用「让模型重写一遍正文」——那会引入两件事:①它顺手改掉/润色掉你的原文(没人批准);
# ②改写后长度对不上,谁也不知道丢了什么。所以模型只回答两件事之一:
#   CLEAN          = 这张卡是干净的
#   CUT: <那句话开头的原文>  = 从这句起不是正文
# 代码再拿那句原文去正文里**找位置**,找到就从那儿截断(只删,不改一个字)。
#
# **每张卡都看**(选定,不做"可疑才看"):宁可每张卡多一次很短的调用,
# 也不要漏。代价是每次出卡多一次调用(max_chars=160,很短),作者知情。
_REVIEW_SYSTEM = """你是一道**只做删除**的质检岗,检查一张「候选卡」的内容。

候选卡里的内容会被**原样写进作者的设定集**,所以里面只该有设定正文:
条款、结论、规则、条目。**不该有两种东西**:
1. 对作者说的话:寒暄、表态、解释「我为什么这么写」、追问「要不要我再…」;
2. 与作者的对白行(人名 + 冒号那种)。

## 只允许两种回答
- 这张卡是干净的 → 只输出:CLEAN
- 有不该进卡的内容 → 只输出一行,格式严格是:CUT: <那句话开头的原文,原样抄 8~20 个字>

## 硬规矩
- 只指位置,**不要重写、不要润色、不要补内容**。
- 粘在设定正文里、拆不开的句子,不要指(拆开就变味的东西留着)。
- 抄错一个字就等于没对上,所以照着原文抄。
"""


def _agent_review(content: str, backend) -> tuple[str, str]:
    """请模型看一眼,返回 (处理后正文, 给作者的一句说明)。说明为空 = 什么都没做。

    失败一律"保持原样"——自检是加分项,不许因为它把一张本来能用的卡搞坏。

    **选定:每张卡都看**,不再"可疑才看"。所以这里不设本地预筛门:
    宁可每张卡多一次 160 字以内的小调用,也不让杂话漏进设定集。
    """
    if backend is None or not (content or "").strip():
        return content, ""
    try:
        raw = backend.complete(
            _REVIEW_SYSTEM,
            "## 候选卡内容(将被原样写进设定集)\n" + content[:2000] + "\n\n## 你的判断",
            max_chars=160,
        ) or ""
    except Exception:       # noqa: BLE001 - 自检失败不该拦住这张卡
        return content, ""
    head = (raw or "").strip().lstrip("*`\"'“” 「").strip()
    up = head.upper()
    if up.startswith("CLEAN") or "干净" in head[:12]:
        return content, ""
    m = re.search(r"CUT\s*[:：]\s*(.+)", head, re.I)
    if not m:
        return content, ""
    snippet = m.group(1).strip().strip("「」『』\"'“”`*").strip()
    if len(snippet) < 4:
        return content, ""
    # 原文里找得到才动刀;找不到就明说,不猜位置
    pos = content.find(snippet)
    if pos < 0 and len(snippet) > 20:
        pos = content.find(snippet[:20])
    if pos < 0:
        return content, "自检:模型说卡里还有杂话,但没在原文里找到那句话,所以保持原样(可以点「改一改」手动删)。"
    kept = content[:pos].rstrip()
    # 只保留「确实什么都没剩下」这一种否决。别用比例门槛:短卡(六个字一条设定)
    # 截掉尾巴必然掉三成以上,按比例判会永远不生效 —— 而短卡恰恰最容易混进一句客套。
    if pos == 0 or len(kept) < 4:
        return content, "自检:疑似杂话的位置太靠前,删掉会把正文砍掉太多,所以保持原样。"
    return kept, f"自检:从「{snippet[:20]}」起那段不是设定正文,已截掉(其余一个字没动)。"


@tool_scope
def run_tool(root: Path | str, name: str, params: dict | None, *, ts: str,
             backend=None, body: str = "") -> dict:
    """执行一次工具调用 → 结果事件 dict(不落盘;提设定产 proposal,真正落盘走 P3 拍板通道)。

    id 由传入的 ts 派生(无 Date.now 依赖);同一轮内多次 mutates 调用的 ts 唯一性由调用方
    (对话循环)保证——本函数只做纯派生,不生成随机数、不读挂钟。

    body(真跑实测后补):`键:值` 参数只吃得到**那一行**。模型写多行内容(表格、
    列表、整段设定)时,第二行起会落进 `parse_tool_blocks` 的 body。此前 run_tool 不接 body,
    于是「内容」只剩表格表头 `| 字段 | 内容 |`,实质内容全丢——真机一次就是这么丢的。
    现在:body 非空且该工具声明了「内容」参数时,把它接回「内容」。
    """
    root = Path(root)
    params = dict(params or {})
    spec = REGISTRY.get(name)
    if spec is None:
        return {"t": "result", "error": f"未知工具:{name}"}
    if spec.roles:
        # 越权门:同一条检查在「模型看得见吗」(agent_chat._tools_schema)之外再做一次,
        # 因为文本协议下模型可以手打任何工具名,光靠「不给看」挡不住。
        from . import collab
        asker = collab.current_asker().strip()
        if asker not in spec.roles:
            return {"t": "result",
                    "error": f"「{name}」是{'、'.join(spec.roles)}专属的工具,你这轮用不了。"}
    note = ""
    if "内容" in spec.params and spec.setting_body:
        prev = str(params.get("内容", "") or "").strip()
        add = body.strip() if body else ""
        merged = (prev + "\n" + add).strip() if (prev and add) else (prev or add)
        if merged:
            merged, notes = _prepare_content(root, merged)
            params["内容"] = merged
            note = "".join(notes)
    try:
        # backend 只传给声明了它的 handler(「学改法」要发一次调用;读类工具用不着)——
        # 同 backends.accepts_kwarg 的老套路,不为一个工具改全体 handler 的签名。
        from .backends import accepts_kwarg as _ak
        import inspect
        wants = "backend" in inspect.signature(spec.handler).parameters
        result = spec.handler(root, **params, **({"backend": backend} if wants else {}))
    except StaleRequest:
        raise
    except TypeError as e:
        return {"t": "result", "error": f"参数不对(「{name}」需要 {spec.params}):{e}"}
    except (ValueError, FileNotFoundError, OSError) as e:
        return {"t": "result", "error": str(e)}
    if spec.mutates:
        # 出卡前让 agent 自己看一眼「这张卡干净吗」。
        # 先在本地预筛(可疑才花钱),再让模型**指位置**,由代码截断 —— 只删不改(见 _agent_review)。
        if isinstance(result, dict) and "content" in result:
            fixed, why = _agent_review(str(result.get("content") or ""), backend)
            if why:
                result = dict(result)
                result["content"] = fixed
                result["reviewed"] = True
                note = (note + why).strip()
        # 并发真 bug 修复：id 原来只有 `p-{ts}`，而 ts 是**秒级**
        # （`time.strftime("%Y%m%d-%H%M%S")`）⇒ 同一秒里开两个会话（两个角色、
        # 或同一角色开两个窗口）会出两张 **同 id** 的卡；拍板时
        # `partner_store.find_proposal` 是"全库最新优先"扫，拿到哪张不确定 ⇒
        # 你在 A 的卡上点落库，可能把 B 的内容写进 B 的落点。
        # 现在加 6 位随机段，并把「这张卡是谁出的」一起记进载荷（给确认侧留痕）。
        role = ""
        try:
            from . import collab as _collab
            role = _collab.current_asker().strip()
        except Exception:      # noqa: BLE001 - 拿不到角色不该让出卡失败
            role = ""
        out = {"t": "proposal", "id": f"p-{ts}-{secrets.token_hex(3)}", **result}
        if role:
            out.setdefault("角色", role)
        if note:
            out["note"] = note
        return out
    # 2026-11-08：handler 已经按 t 信封自打包的（如「取稿」带 images）直接透传——
    # 不再二次包 {"t":"result","text":result} 把图片剥掉。仅认 {t:"result"}，
    # 其他形状仍走老包装（保底）。
    if isinstance(result, dict) and result.get("t") == "result":
        if note:
            out = dict(result)
            out["note"] = note
            return out
        return result
    if note:
        return {"t": "result", "text": result, "note": note}
    return {"t": "result", "text": result}
