# -*- coding: utf-8 -*-
"""R27 知识顾问(作者 2026-10-19 裁定 4 条):
   ①资料 = 联网搜索 + 自附资料 + AI 分析;②产出只给**提案**,勾选才入账;
   ③关联做单向锚点 + **反向索引表**;④独立面板 + agents 角色,且**大纲师/设定师要能检索到**。

这一版是后端内核:提问 → 提案(走「深度逻辑推理」槽,全仓第一个消费者) → 勾选入账
→ 写 `外置大脑/知识/<主题>.md` 并重建 `外置大脑/知识/索引.md`(反向索引)。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .backends import Backend
from .fsutil import atomic_write_text

KNOW_REL = "外置大脑/设定集/40-知识"
INDEX_REL = "外置大脑/设定集/40-知识/索引.md"
INDEX_HEAD = "# 知识索引(反向:材料 → 知识)"

_RULES = """你是这本书的「知识顾问」。作者问的是与作品相关的知识性问题(时代、器物、职业、制度、
地理、技术可行性、设定自洽等)。你的活儿是:先给结论,再摆证据,最后说清哪里没把握。

硬规矩:
1. 只写「初步判断」,不要假装这是定论;凡涉及书内已有的设定/卷纲/章纲,先按书里的写法说,
   书里没写才给通用知识,并注明「书里未写」。
2. 证据分三类,必须标明来源:[书内] 引原文片段;[自附] 引作者给的资料;[联网] 带网址。
   没有证据的部分不要编 —— 写进「不确定」。
3. 结尾用「关联:」列出这条知识该挂到哪些材料上,只能写这些形状:
   人物/<名字>.md、世界观/<名字>.md、全书大纲.md、卷纲.md、卷纲#第N卷、卡章纲#第N章。
   不确定就别写,宁少不多。
4. 输出用这五个二级标题,顺序固定:
   ## 主题
   ## 结论
   ## 依据
   ## 不确定
   ## 关联
"""


def _strip_fm(text: str) -> str:
    """去掉顶部 YAML frontmatter(只认这种最简形状,与本项目 agents/*.md 一致)。"""
    t = text.lstrip("\ufeff")
    if not t.startswith("---"):
        return t
    end = t.find("\n---", 3)
    return t[end + 4:].lstrip("\n") if end > 0 else t


def rules_for(root: Path | str) -> str:
    """知识顾问的角色卡。书里写了 `agents/知识顾问.md` 就用它的正文(跟其他五棒同一套约定);
    没写 / 只有占位 -> 用内置规矩。**不因为一张空卡就让顾问胡说。**"""
    p = Path(root) / "agents" / "知识顾问.md"
    if p.is_file():
        try:
            body = _strip_fm(p.read_text(encoding="utf-8")).strip()
        except OSError:
            body = ""
        if len(body) >= 40:
            return body
    return _RULES


def _title_of(question: str) -> str:
    """问句 → 文件名用的主题(去掉标点与**路径分隔符**,截 24 字)。

    交叉审查 P1-③:原来只去标点,主题里带 `/` 就会在「知识」下建子目录,而 `_knowledge_docs`
    只 glob 顶层 ⇒ 文件写进去了、索引里没有、大纲师也读不到(入账即失联);带 `..` 还能写到书外。
    """
    t = re.sub(r"[\s?？。!！,，:：;；\"'“”‘’（）()【】\[\]…·/\\<>|:*\x00-\x1f]+", "", question or "")
    t = t.replace("..", "").strip(". ")
    return (t[:24] or "未命名")


def anchors_known(root: Path | str) -> set[str]:
    """这本书里真实存在的锚:设定集里的人物/世界观文件、全书大纲、卷纲、卡章纲的章号。

    新书只走设定集分区（外置大脑/设定集/10-世界观、20-人物）；旧目录仍认，
    免得已经写过知识条目的老书锚点全部显示成「未知」。
    """
    from . import paths
    root = Path(root)
    out: set[str] = set()
    for top in ("人物", "世界观"):
        d = root / "外置大脑" / top
        if d.is_dir():
            for p in d.rglob("*.md"):
                out.add(f"{top}/{p.name}")
    for sub in (paths.SET_WORLD_DIR_REL, paths.SET_CHARS_DIR_REL):
        d = root / sub
        if d.is_dir():
            for p in d.rglob("*.md"):
                out.add(f"{sub}/{p.name}")
    for name in ("全书大纲.md", "卷纲.md", "卡章纲.md"):
        if (root / "外置大脑" / name).is_file():
            out.add(name)
    card = root / "外置大脑" / "卡章纲.md"
    if card.is_file():
        for m in re.finditer(r"^[ \t]*[-*][ \t]*第\s*(\d+)\s*章", card.read_text(encoding="utf-8"), re.M):
            out.add(f"卡章纲#第{int(m.group(1))}章")
    vol = root / "外置大脑" / "卷纲.md"
    if vol.is_file():
        for m in re.finditer(r"^[ \t]*##[ \t]*第\s*([0-9一二三四五六七八九十百]+)\s*卷", vol.read_text(encoding="utf-8"), re.M):
            out.add(f"卷纲#第{m.group(1)}卷")
    return out


def _clip(text: str, n: int = 6000) -> str:
    """截断要留痕:模型必须知道「后面还有,只是我没给」,否则会把截断处当结论。"""
    t = text.strip()
    if len(t) <= n:
        return t
    return t[:n] + f"\n…(材料共 {len(t)} 字,这里只给了前 {n} 字,后面已截断)"


def ask_prompt(question: str, *, inside: str = "", attached: str = "", web: str = "") -> str:
    parts = [f"作者的问题:{question.strip()}"]
    if inside.strip():
        parts.append("【书内材料(节选)】\n" + _clip(inside))
    if attached.strip():
        parts.append("【作者自附资料】\n" + _clip(attached))
    if web.strip():
        parts.append("【联网检索结果(可能不可靠,引用时带网址)】\n" + _clip(web))
    if not (inside.strip() or attached.strip() or web.strip()):
        parts.append("(这次没有附任何资料:纯通用知识 + 请明确标出「书里未写」)")
    parts.append("按约定的五个二级标题输出。")
    return "\n\n".join(parts)


def _section(text: str, name: str) -> str:
    m = re.search(rf"^##\s*{name}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    return (m.group(1).strip() if m else "")


def parse_proposal(raw: str, *, known: set[str] | None = None) -> dict:
    """把模型原文解成提案。解析不出来就如实说,不硬猜。"""
    text = (raw or "").strip()
    topic = _section(text, "主题")[:60]
    concl = _section(text, "结论")
    basis = [ln.strip(" -•\t") for ln in _section(text, "依据").splitlines() if ln.strip(" -•\t")]
    unsure = [ln.strip(" -•\t") for ln in _section(text, "不确定").splitlines() if ln.strip(" -••\t")]
    links = []
    for ln in _section(text, "关联").splitlines():
        rel = ln.strip(" -•\t")
        if rel and rel not in links:
            links.append(rel)
    known = known if known is not None else set()
    good = [r for r in links if (not known) or r in known]
    bad = [r for r in links if known and r not in known]
    return {"ok": bool(concl), "主题": topic, "结论": concl, "依据": basis,
            "不确定": unsure, "关联": good, "关联没对上": bad,
            "说明": "" if concl else "模型的回复里没有「## 结论」这一节,没法当提案用"}


BROWSE_MAX = 3          # 一次最多抓几页
BROWSE_CHARS = 1800     # 每页留多少字

_BROWSE_RULES = """你要判断:回答这个问题**需不需要查网上的资料**。

需要 → 只输出 1–3 个你要抓的网址,一行一个,必须是 http/https,不许写别的字;
不需要 → 只输出一行:**不需要**。

只给网址;不要解释,不要给搜索结果页,不要给需要登录的链接。
"""


def parse_urls(raw: str, *, limit: int = BROWSE_MAX) -> list[str]:
    """从模型回话里挑出 http(s) 网址。别的字一律不要。"""
    out: list[str] = []
    for m in re.finditer(r"https?://[^\s<>\"')]+", raw or ""):
        u = m.group(0).rstrip(".,;、。)")
        if u not in out:
            out.append(u)
    return out[:limit]


def browse(root: Path | str, question: str, browser: Backend | None, *, limit: int = BROWSE_MAX) -> dict:
    """先问「要不要上网」,要就抓 1–3 页,把正文并进【联网】材料。

    `browser` 是「长上下文检索」那一槽(由 `websrc.fetch_page` 真的去抓)。
    没给 browser 就**一步都不做**,如实回「没做」。
    """
    if browser is None:
        return {"做没做": False, "为什么没做": "没有可用的抓网页后端"}
    raw = browser.complete(_BROWSE_RULES, f"作者的问题:{question.strip()}")
    urls = parse_urls(raw, limit=limit)
    if not urls:
        return {"做没做": False, "为什么没做": "顾问判断不需要上网查", "原话": (raw or "").strip()[:80]}
    from . import websrc

    pages, texts = [], []
    for u in urls:
        got = websrc.fetch_page(u, max_chars=BROWSE_CHARS)
        if not got.get("ok"):
            pages.append({"网址": u, "抓没抓到": False, "原因": got.get("reason", "")})
            continue
        pages.append({"网址": u, "抓没抓到": True, "标题": got.get("标题", ""),
                      "字符数": got.get("字符数", 0)})
        texts.append(f"[联网] {got.get('标题') or ''}\n{got['网址']}\n{got['正文']}")
    # 成功那条也要带上「为什么没做」的空串/原因 —— 否则界面分不清「没勾」「顾问说不用」和「点了但一页没抓到」
    _why = "" if texts else "顾问点了网址,但一页都没抓到(原因见每页)"
    return {"做没做": bool(texts), "页": pages, "文本": "\n\n".join(texts), "为什么没做": _why}


def ask(root: Path | str, question: str, backend: Backend, *,
        inside: str = "", attached: str = "", web: str = "", max_chars: int = 0,
        retrieve_backend: Backend | None = None, evidence: bool = False,
        browse_backend: Backend | None = None, browse_now: bool = False) -> dict:
    """出一份提案。**不落盘**(裁定 ②:勾选才入账)。

    `evidence=True` 且给了 `retrieve_backend`(「长上下文检索」槽)时先**跨章取证**:
    让它从章目录里挑出相关的几章,读那些章的正文,$再连同问题一起交给主问。
    取证挑不出来就照旧按原样问 —— 绝不编造「我查过了」。
    """
    root = Path(root)
    _bw: dict = {"做没做": False}
    if browse_now:
        _bw = browse(root, question, browse_backend)
        if _bw.get("做没做"):
            web = ((_bw.get("文本") or "") + ("\n\n【作者另外贴的检索结果】\n" + web.strip() if web.strip() else ""))
    _ev: dict = {"做没做": False}
    if evidence and retrieve_backend is not None:
        from . import retrieve as _rtr
        _pick = _rtr.pick(root, question, retrieve_backend)
        if _pick.get("ok") and _pick.get("章"):
            # 每章分到的预算:总上限 5500 字里按章均分,最少给 600(再少就看不出东西)
            _per = max(600, 5500 // max(1, len(_pick["章"])))
            _txt = _rtr.gather(root, _pick["章"], chars=_per)
            inside = (f"【以下是跨章取证取到的原文(第"
                      f"{'、'.join(str(n) for n in _pick['章'])} 章)】\n{_txt}"
                      + (f"\n\n【作者另给的材料】\n{inside}" if inside.strip() else ""))
            _ev = {"做没做": True, "章": _pick["章"], "跳过的章": _pick.get("跳过的章") or [],
                   "说明": _pick.get("说明", "")}
        else:
            _ev = {"做没做": False, "为什么没做": _pick.get("reason") or "长窗口那一槽没挑出相关章"}
    prompt = ask_prompt(question, inside=inside, attached=attached, web=web)
    raw = backend.complete(rules_for(root), prompt, max_chars=max_chars or None)
    p = parse_proposal(raw, known=anchors_known(root))
    p.update({"落盘": False, "模型原文长度": len(raw or ""), "生成于": _now(),
              "取证": _ev, "浏览": _bw})
    if not p.get("主题"):
        p["主题"] = _title_of(question)
    return p


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _doc(question: str, p: dict, *, attached: str = "", web: str = "") -> str:
    # 直取 p['主题'] 会在缺键时抛 KeyError,端点再把它报成「没能写进知识库:'主题'」——
    # 看着像用户错,其实是我们自己的键没兜住(P2-1)。
    lines = [f"# 知识 · {p.get('主题') or _title_of(question)}", "", f"> 生成于 {p.get('生成于') or _now()}。"
             "这是「知识顾问」给出的**初步判断**,不是 canon;要进设定请把它挪进对应设定文件。", "",
             "## 问", "", question.strip(), "", "## 结论(初步判断)", "", str(p.get("结论") or "").strip(), ""]
    lines += ["## 依据", ""]
    lines += [f"- {b}" for b in (p.get("依据") or [])] or ["- (模型没给依据 —— 这条要格外小心)"]
    lines += ["", "## 不确定", ""]
    lines += [f"- {u}" for u in (p.get("不确定") or [])] or ["- (模型说没有不确定 —— 但这是 AI 判断,仍需人看)"]
    lines += ["", "## 关联", ""]
    lines += [f"- {r}" for r in (p.get("关联") or [])] or ["- (没挂到任何材料上)"]
    if p.get("关联没对上"):
        lines += ["", "## 关联没对上(书里找不到这些材料)", ""]
        lines += [f"- {r}" for r in p["关联没对上"]]
    if attached.strip() or web.strip():
        lines += ["", "## 附录:这次给的资料", ""]
        if attached.strip():
            lines += ["### 作者自附", "", attached.strip(), ""]
        if web.strip():
            lines += ["### 联网检索结果", "", web.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def accept(root: Path | str, question: str, p: dict, *, attached: str = "", web: str = "") -> dict:
    """入账:写知识文件 + 重建反向索引。同名文件**不覆盖**,加序号。"""
    root = Path(root)
    if not p.get("结论"):
        return {"ok": False, "落盘": False, "reason": "这份提案没有结论,不能入账。"}
    d = root / KNOW_REL
    d.mkdir(parents=True, exist_ok=True)
    base = _title_of(p.get("主题") or question)
    if base in (".", "..", "") or "/" in base or "\\" in base:      # 双保险:净化漏了也写不进目录
        base = "未命名"
    name = base
    n = 2
    while (d / f"{name}.md").exists():
        name = f"{base}({n})"
        n += 1
    path = d / f"{name}.md"
    atomic_write_text(path, _doc(question, p, attached=attached, web=web))
    idx = rebuild_index(root)
    return {"ok": True, "落盘": True, "文件": path.relative_to(root).as_posix(),
            "索引": INDEX_REL, "索引条目": idx.get("条目数", 0)}


def _knowledge_docs(root: Path) -> list[Path]:
    d = Path(root) / KNOW_REL
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if p.name != "索引.md")


def rebuild_index(root: Path | str) -> dict:
    """从知识文件重建反向索引(材料 → 知识)。确定性,不读模型。"""
    root = Path(root)
    rows: list[tuple[str, str, str]] = []
    for p in _knowledge_docs(root):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        when = ""
        m = re.search(r"生成于\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
        if m:
            when = m.group(1)
        body = _section(text, "关联")
        for ln in body.splitlines():
            rel = ln.strip(" -•\t")
            if rel:
                rows.append((rel, f"知识/{p.name}", when))
    lines = [INDEX_HEAD, "",
             "> 反向索引:哪条知识挂在哪份材料上。**由知识文件重建**,别手改(改了下次会盖掉)。",
             "> 大纲师/设定师按 reads 读这一份,就知道写这一卷/这一章时有哪些知识可用。", "",
             "| 材料 | 知识 | 生成于 |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in sorted(rows)]
    if not rows:
        lines.append("| (还没有知识条目) | - | - |")
    path = root / INDEX_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "\n".join(lines) + "\n")
    return {"ok": True, "索引": INDEX_REL, "条目数": len(rows)}


def listing(root: Path | str) -> dict:
    root = Path(root)
    docs = []
    for p in _knowledge_docs(root):
        try:
            head = p.read_text(encoding="utf-8")[:400]
        except OSError:
            head = ""
        m = re.search(r"^# 知识 · (.+)$", head, re.M)
        docs.append({"文件": f"知识/{p.name}", "主题": (m.group(1).strip() if m else p.stem),
                     "字数": len(p.read_text(encoding="utf-8")) if p.is_file() else 0})
    idx = (root / INDEX_REL).read_text(encoding="utf-8") if (root / INDEX_REL).is_file() else ""
    links = [ln.strip() for ln in idx.splitlines() if ln.startswith("| ") and "---" not in ln]
    return {"ok": True, "知识": docs, "条数": len(docs), "索引行数": max(0, len(links) - 1),
            "有索引": bool(idx)}
