# -*- coding: utf-8 -*-
r"""教材重拆：把一册拆成「理论」与「应用（拆解卡）」两份。

：
-写作分析课 =抽象理论（有哪些写法、各自起什么作用）
-个人图书馆 =应用层（哪些书/电影/视频用了什么手法、营造了什么效果）
-应用按「载体 -项目名」两层落库

五类归属：
theory理论 →留在教材（原地不动）
apply 应用 →写进知识库/个人图书馆/<载体>/<项目名>/
head 头段 → **两边都不放**（每册开头的科目/来源/字数块）
xref 备注 → **附到相关段落**（「和另外几支的呼应」这类交叉引用）
"""
from __future__ import annotations

import re
from pathlib import Path

WORK_RE = re.compile(r"《([^》]{2,40})》")
THEORY_HINTS = ("可搬", "搬进", "对写作", "怎么写", "方法", "技巧", "立论", "定义",
    "操作化", "操作", "结构层", "清单", "综纲", "判准", "原则", "要素",
    "怎么做", "如何", "为什么", "分析", "节奏", "描写", "人设",
    "原理", "内核", "结构", "要点", "总表", "三招", "流水线", "组合法", "特质", "建议")
CARRIER_WORDS = {
    "电影": ("电影", "片名", "院线", "导演", "镜头"),
    "小说": ("小说", "网文", "章节", "长篇"),
    "视频": ("视频", "UP", "up", "BV", "支视频", "这期"),
    "游戏": ("游戏", "玩家", "关卡", "通关", "存档"),
    "剧本": ("剧本", "编剧", "台词"),
}


def split_sections(text):
    #不能只认 `## `：实测29册里25册用的是 `#### `（四级）。
    lines = text.split(chr(10))
    hash_counts = [len(l) - len(l.lstrip(chr(35))) for l in lines if l.startswith(chr(35) *2)]
    levels = [n for n in hash_counts if n >=2]
    depth = min(levels or [2])
    prefix = chr(35) * depth + chr(32)
    marks = [k for k, l in enumerate(lines) if l.startswith(prefix)]
    if not marks:
        return [("", text)]
    out = []
    if marks[0] >0:
        head = chr(10).join(lines[:marks[0]]).strip()
        if head:
            out.append(("（开头：科目 /来源 /一句话）", head))
    for idx, k in enumerate(marks):
        end = marks[idx +1] if idx +1 < len(marks) else len(lines)
        out.append((lines[k][depth +1:].strip(), chr(10).join(lines[k:end]).strip()))
    return out


def classify(title, body):
    """判一段归哪：theory / apply / head / xref。七条判据是逐条裁定的。"""
    t = title.strip()
    #项目定义里点名的例外（看标题像拆解/像建议，实际归属由作者定）
    if "打斗三招" in t:
        return "theory"
    if "案例（" in t:
        return "theory"
    if "她的两条建议" in t:
        return "apply"
    if "用道具讲政治" in t:
        return "apply"
    #1、开头元信息块
    if t.startswith("（开头") or (not t and len(body) <1200):
        return "head"
    #2、交叉引用
    if "呼应" in t:
        return "xref"
    #3、方法论关键词先判
    if any(w in t for w in THEORY_HINTS):
        return "theory"
    #4、真在拆作品
    if t.startswith("拆") or "拆《" in t or "案例诊断" in t:
        return "apply"
    if "案例" in t and WORK_RE.search(body):
        return "apply"
    #5、拿不准归理论
    return "theory"


def guess_carrier(text):
    best, score = "其它",0
    for name, words in CARRIER_WORDS.items():
        hit = sum(text.count(w) for w in words)
        if hit > score:
            best, score = name, hit
    return best


SPECIAL_PROJECTS = (
    #：这4条本来就不是"某部作品"，统一归 其它/，
    #项目名用「这是什么材料」来定，不拿正文里的书名号硬猜。
    ("摄像头式", "其它", "改稿案例·摄像头式写法"),
    ("案例诊断", "其它", "投稿诊断·场景太大"),
    ("她的两条建议", "其它", "女频平台红线·过来人的建议"),
    ("用道具讲政治", "其它", "用道具讲政治·九组实例"),
)


def pick_special(title):
    """项目定义里点名的特殊应用段。命中返回 (载体, 项目名)，否则 None。"""
    t = title.strip()
    for key, carrier, name in SPECIAL_PROJECTS:
        if key in t:
            return carrier, name
    return None


def pick_project(body):
    """给一条应用段定项目名：用正文里出现最多的书名号作品名。"""
    names = WORK_RE.findall(body)
    if not names:
        return ""
    counts = {n: names.count(n) for n in set(names)}
    best = max(counts, key=lambda n: counts[n])
    return "《" + best + "》拆解"


def split_book(path):
    """拆一册，返回五类。**不写盘**。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    secs = split_sections(text)
    if len(secs) <=1:
        return {"册名": p.stem, "路径": str(p), "理论": secs, "应用": [],
            "头": [], "备注": [], "统计": {"段":1, "理论段":1, "应用段":0,
            "头段":0, "备注段":0, "原字数": len(text)}}
    theory, apply_items, heads, xrefs = [], [], [], []
    for title, body in secs:
        kind = classify(title, body)
        if kind == "apply":
            special = pick_special(title)
            apply_items.append({"称呼": title, "作品": WORK_RE.findall(body),
                "载体": special[0] if special else guess_carrier(body),
                "正文": body,
                "项目": special[1] if special else pick_project(body)})
        elif kind == "head":
            heads.append((title, body))
        elif kind == "xref":
            xrefs.append((title, body))
        else:
            theory.append((title, body))
    return {"册名": p.stem, "路径": str(p), "理论": theory, "应用": apply_items,
        "头": heads, "备注": xrefs,
        "统计": {"段": len(secs), "理论段": len(theory), "应用段": len(apply_items),
        "头段": len(heads), "备注段": len(xrefs), "原字数": len(text)}}
