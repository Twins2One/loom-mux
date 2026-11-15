# -*- coding: utf-8 -*-
r"""书房伙伴的真工具：「看多个教材不该调用工具吗」）。

在此之前，书房伙伴是"检索引擎先挑几段塞进提示词、模型一次答完"——它自己不能翻书。
问得深了就只剩一句"库里没讲到"。这里给它三把工具，让它自己决定看哪几册、看多少。

与书内那套（partner_tools.REGISTRY）的区别，以及为什么不直接复用：
-书内工具绑着"书"：落点白名单、设定集槽位、候选卡……书房没有当前书，那些参数无从填起。
-书房只需要"读"：列目录、取一册、搜一次。三把全只读，不写盘、不提案、不改书。
-但形状照抄书内那套（同样的 name/desc/params + OpenAI tools schema），
这样两边对模型的说话方式一致，将来要合并也容易。

一条硬边界：这些工具绝不写盘。书房是"学"和"查"的地方，产出要落盘时由作者自己点
「落库」（那是另一条已经存在的路径），工具不替他做主。
"""
from __future__ import annotations

from pathlib import Path

MAX_VOLUME_CHARS =6000
MAX_HITS =6
MAX_EXCERPT =900


def _lib():
    from . import library as _l
    return _l


def _list_volumes():
    try:
        lib = _lib().scan_library()
    except Exception:
        return []
    out = []
    for cat in lib.get("categories") or []:
        for subj in cat.get("subjects") or []:
            for vol in subj.get("volumes") or []:
                out.append({
                    "分类": cat.get("name") or "",
                    "科目": subj.get("name") or "",
                    "名称": vol.get("name") or "",
                    "路径": vol.get("path") or "",
                })
    return out


def _safe_volume_path(raw):
    p = Path(str(raw or "").strip())
    if not p.is_file() or p.suffix.lower() != ".md":
        return None
    try:
        root = Path(_lib().library_root()).resolve()
        p.resolve().relative_to(root)
    except Exception:
        return None
    return p


def list_books():
    vols = _list_volumes()
    if not vols:
        return "知识库里还没有书册。"
    by_subject = {}
    for v in vols:
        key = (v["分类"] + " / " + v["科目"]).strip(" /")
        by_subject.setdefault(key, []).append(v["名称"])
    lines = ["书房里共有 %d册：" % len(vols)]
    for key in sorted(by_subject):
        lines.append("- %s：%s" % (key, "、".join(by_subject[key])))
    return chr(10).join(lines)


def read_book(name="", path=""):
    target = None
    raw_path = str(path or "").strip()
    if raw_path:
        target = _safe_volume_path(raw_path)
    else:
        want = str(name or "").strip()
        if not want:
            return "要看书哪一册？给我册名（可先「列书册」看看有哪些）。"
        for v in _list_volumes():
            if v["名称"] == want or want in v["名称"]:
                target = _safe_volume_path(v["路径"])
                break
    if target is None:
        return "没找到这一册。可以先「列书册」看准确名字。"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return "这一册读不出来：%s" % e
    if len(text) > MAX_VOLUME_CHARS:
        return text[:MAX_VOLUME_CHARS] + chr(10) + chr(10) + "……（这一册太长，先给前 %d字；要看后面再说）" % MAX_VOLUME_CHARS
    return text


def search_books(query=""):
    q = str(query or "").strip()
    if not q:
        return "要搜什么？给我一个问题或几个关键词。"
    from . import ask as _ask
    got = _ask.search(q, top_k=MAX_HITS)
    picked = got.get("节选") or []
    if not picked:
        return "书房里没搜到相关的段落。可以换个说法，或者「列书册」看看有没有对的那一册。"
    how = "字面+语义" if (got.get("统计") or {}).get("语义") else "字面"
    lines = ["搜到 %d段（%s）：" % (len(picked), how)]
    for i, s in enumerate(picked,1):
        body = str(s.get("正文") or "")[:MAX_EXCERPT]
        lines.append(chr(10) + "[%d] %s / %s" % (i, s.get("科目", ""), s.get("节", "")))
        lines.append(body)
    return chr(10).join(lines)


def search_one_book(册="", 查询=""):
    """在**某一册**（或某一科目的全部册）里搜。

    2026-10-31：书籍的信息要让**对应书籍的角色**便于搜——
    全库搜会把别的书里同主题的段也捅进来，噪声大。限定范围后，
    命中的都是这一本书自己的。
"""
    q = str(查询 or "").strip()
    want = str(册 or "").strip()
    if not q:
        return "要搜什么？给我一个问题或几个关键词。"
    if not want:
        return "限定哪一册？给册名或科目名（可先「列书册」）。"
    from . import ask as _ask
    got = _ask.search(q, top_k=40)   # 多取一些，再按册过滤
    picked = [s for s in (got.get("节选") or [])
          if want in str(s.get("科目") or "") or want in str(s.get("节") or "")]
    if not picked:
        return "「%s」范围内没搜到相关段落。可以去掉限定用「搜书房」全库搜，或换个说法。" % want
    picked = picked[:MAX_HITS]
    how = "字面+语义" if (got.get("统计") or {}).get("语义") else "字面"
    lines = ["在「%s」里搜到 %d 段（%s）：" % (want, len(picked), how)]
    for i, s in enumerate(picked, 1):
        body = str(s.get("正文") or "")[:MAX_EXCERPT]
        lines.append(chr(10) + "[%d] %s / %s" % (i, s.get("科目", ""), s.get("节", "")))
        lines.append(body)
    return chr(10).join(lines)

def _run_du_volume(args):
    return read_book(name=args.get("册名", ""), path=args.get("路径", ""))


TOOLS = {
 "列书册": {"desc": "列出书房里有哪些教材/素材（按科目分组）。不知道有哪些书时先用它。只读。",
    "params": (), "run": lambda args: list_books()},
 "搜书房": {"desc": "在全部教材/素材里搜一次，返回几段节选（字面+语义）。适合“有没有讲过 X”。一次调用只搜一次。",
    "params": ("查询",), "run": lambda args: search_books(args.get("查询", ""))},
 "读一册": {"desc": "取某一册的正文（按名字）。太长的册只给前 %d字。要通读某一册或需要原文细节时用它。" % MAX_VOLUME_CHARS,
    "params": ("册名",), "run": _run_du_volume},
 #2026-10-31：书籍的信息要让**对应书籍的角色**便于搜——全库搜噪声大，
 #限定范围后命中的都是这一本书自己的。
 "查这本书": {"desc": "在**指定的一册（或一个科目的全部册）**里搜。和「搜书房」的区别：那把是全书乱枪打鸟，这把限定范围、命中都是这一本书的。适合「这本教材里怎么讲悬念」这类只关心一本书的问题。书名可先「列书册」看准确名字。只读。",
    "params": ("册", "查询"), "run": lambda args: search_one_book(args.get("册", ""), args.get("查询", ""))},
}


#2026-10-31（实测）：中转商对**中文工具名**报 `the tool name is invalid or duplicated`
#（HTTP 503/code 11152）—— 原生 function calling 出口必须用 ASCII 名。
#文本协议不受影响（那是我们自己解析的）。入口再映射回中文，单一真相仍是 TOOLS。
TOOL_ASCII = {
    "列书册": "list_books",
    "搜书房": "search_library",
    "读一册": "read_volume",
    "查这本书": "search_one_book",
}
ASCII_TO_TOOL = {v: k for k, v in TOOL_ASCII.items()}

def schema():
    # 0.9.0 工具合一：schema 生成走 partner_tools.schema_of 单一实现（dict spec 先归一成四元组）
    from ..partner_tools import schema_of
    return schema_of([(name, tuple(spec.get("params") or ()), spec["desc"], TOOL_ASCII.get(name, name))
                      for name, spec in TOOLS.items()])


def run(name, args):
    name = ASCII_TO_TOOL.get(str(name or "").strip(), str(name or "").strip())
    spec = TOOLS.get(str(name or ""))
    if spec is None:
        return {"ok": False, "text": "没有这个工具：%s（可用：%s）" % (name, " / ".join(TOOLS))}
    try:
        text = spec["run"](dict(args or {}))
        return {"ok": True, "text": str(text)}
    except Exception as e:
        return {"ok": False, "text": "这个工具没跑成：%s" % e}


def contract():
    # 0.9.0 工具合一：契约文本走 partner_tools.contract_of 单一实现
    from ..partner_tools import contract_of
    return contract_of(
        [(name, tuple(spec.get("params") or ()), spec["desc"], TOOL_ASCII.get(name, name))
         for name, spec in TOOLS.items()],
        header="你可以用这些工具（一轮只做一件事，拿到结果再决定下一步）：",
        footer="写法：单独一行 `用：工具名`，参数各占一行 `键：值`。不需要工具就直接说话。")