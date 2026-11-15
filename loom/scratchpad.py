# -*- coding: utf-8 -*-
r"""速记本管理器。

每个角色（作者智库 8 个 + 写作引擎 6 个）有一个独立的速记本文件。
文件结构:每条记录 = 时间戳 + 角色名头 + 正文,角色之间不交叉阅读。
领航员有专属记录本（2026-11-11 用户裁定）：书间隔离，可记「世界信息缺口观察」。

存储位置: `{项目根}/外置大脑/速记本-{角色名}.md`
"""
from __future__ import annotations

import time
from pathlib import Path

SCRATCHPAD_DIR = "外置大脑/设定集/90-速记本"

# 全部角色名(,不得硬编码在其他模块)
# 作者智库 10 个（高级卡片样式）
# 2026-11-06 市场分析师入册（作者批准：研究草案 1改1增1改）
# 2026-11-12 人物侧写师入册（用户新需求：讨论细化人物卡 + 人物与设定集其他部分的互动细节）
AUTHOR_ROLES = frozenset([
    "设定师",
    "大纲师",
    "知识顾问",
    "语料分析师",
    "战斗设计师",
    "情绪节奏设计师",
    "人物侧写师",    # 人物细化者：人物卡讨论细化 + 人物与设定集的互动细节（2026-11-12 增）
    "编辑总监",      # 全局质量守门人，与写作引擎的"责任编辑"区分
    "批评家",
    "市场分析师",    # 市场判断者：卖点/题材趋势/上架时机/违禁比对（2026-11-06 增）
])
# 写作引擎 6 个（次级卡片样式）
ENGINE_ROLES = frozenset([
    "章节细纲师",
    "章节设定师",
    "章节分镜师",
    "写手",
    "责任编辑",      # 单章质量把关人，与作者智库的"编辑总监"区分
    "润色师",
])
ALL_ROLES = AUTHOR_ROLES | ENGINE_ROLES

# 领航员专属记录本（2026-11-11 用户裁定）：书间隔离，可记「世界信息缺口观察」。
# 刻意**不**并入 ALL_ROLES——那张表驱动智库/引擎名册与卡片，领航员不是其中一员；
# 这里只放开速记本的读写入口（路径仍按项目根拼 ⇒ 天然书间隔离）。
NAVIGATOR_ROLE = "领航员"


def is_scratchpad_role(role: str) -> bool:
    """速记本是否接受这个角色：智库/引擎名册 + 领航员。

    **对外公开这个名字**，让 HTTP 端点层（`agent_api.get_scratchpad` /
    `append_scratchpad`）也用同一判据。教训（审查 审查记录）：
    只改本模块的校验、端点层仍用 `ALL_ROLES`，会造成「底层能写、真实 HTTP 通路全被拒」。
    """
    return role in ALL_ROLES or role == NAVIGATOR_ROLE


# 兼容内部旧名（本模块内已有调用点用 `_known_role`）
_known_role = is_scratchpad_role

# ──对外展示时要藏起来的角色────────────────────────────
#需求背景:「后端『作者智库』的『8人』记得更新,『知识顾问』记得删(或隐藏)」。
#
# ⚠️铁律:知识顾问**不能**从 AUTHOR_ROLES里删掉 ——它的速记本、对话记录、
#端点、数据全要留着(0.8.1才做 A/B阶段下线)。这里只影响「给界面看的清单」,
#不影响任何按名字找角色的地方(scratchpad_path / agent_meta仍认它)。
#2026-10-31：**语料分析师也转为工具了**，与知识顾问并列隐藏。
#两个角色的能力已经变成工具（查知识 / 看数据 / 改指纹），**所有智能体都能调**。
#名册因此从 8 人变成 6 人：设定师、大纲师、战斗设计师、情绪节奏设计师、编辑总监、批评家。
#2026-11-06：**市场分析师入册**，名册 6 人变 7 人（作者批准，依据 2026-11-05 四编辑口述研究：
#缺口=签约/续写裁判、上架 PK 时机、市场趋势——原归"编辑总监+领航员"的空档）。
#铁律不变：两个名字**不从 AUTHOR_ROLES 删**——速记本、对话记录、端点、数据全要留着。
HIDDEN_ROLES = frozenset(["知识顾问", "语料分析师"])

#界面上该看到的作者智库角色（2026-10-31 起是 **6 人**；2026-11-06 市场分析师入册 → **7 人**；
#2026-11-12 人物侧写师入册 → **8 人**）。文案里的「N 人」一律用它算，别再写死数字。
VISIBLE_AUTHOR_ROLES = frozenset(AUTHOR_ROLES - HIDDEN_ROLES)


def scratchpad_path(root: Path | str, role: str) -> Path:
    """角色速记本路径。role 必须是 ALL_ROLES 里的名称。"""
    if not _known_role(role):
        raise ValueError(f'未知角色名:{role!r} (必须是 {" / ".join(sorted(ALL_ROLES))} 或 {NAVIGATOR_ROLE})')
    root = Path(root)
    d = root / SCRATCHPAD_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"速记本-{role}.md"


def append(role: str, root: Path | str,
           content: str, *, author_note: str = "") -> None:
    """追加一条记录到角色速记本。

    每条格式:
        ### [{时间}] {角色名} · {author_note 或 "工作记录"}

        {content}

    author_note: 可选备注,如"低阈值触发" / "定稿结论" 等
    """
    if not _known_role(role):
        raise ValueError(f"未知角色:{role!r}")
    path = scratchpad_path(root, role)
    ts = time.strftime("%Y-%m-%d %H:%M")
    label = f"### [{ts}] {role}"
    if author_note:
        label += f" · {author_note}"
    entry = f"{label}\n\n{content.strip()}\n\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)


def append_labeled(role: str, root: Path | str,
                   content: str, *, author_note: str = "") -> str:
    """同 append，但：① 时间戳带**秒**（同一分钟两条才能区分——向量记忆库的
    anchor 靠它定位原文，重复就定位到同一段）；② **返回这条的 label**，
    给 memory.remember() 当 anchor 用。2026-10-31 随记忆库新增。
    """
    if not _known_role(role):
        raise ValueError(f"未知角色:{role!r}")
    path = scratchpad_path(root, role)   # 2026-11-06 修：原为 (role, root)，与签名 (root, role) 相反——附带 A 复验时暴露
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    label = f"### [{ts}] {role}"
    if author_note:
        label += f" · {author_note}"
    entry = f"{label}\n\n{content.strip()}\n\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    return label

def load_snippets(role: str, root: Path | str,
                  max_entries: int = 5) -> list[dict]:
    """读取角色速记本的最后 max_entries 条,返回结构化片段。

    返回:[{"time":"时间戳","note":"备注","body":"内容"}]
    不返回全文,避免把整个历史全塞进上下文。
    """
    if not _known_role(role):
        return []
    path = scratchpad_path(root, role)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    # 每条以 ### [时间] 角色名 开头
    import re
    pieces = re.split(r"(?=^### )", text, flags=re.M)
    entries = []
    for p in pieces:
        if not p.strip():
            continue
        # 秒级兼容(2026-11-06 终审附A):append_labeled 写 %H:%M:%S,旧正则只认分钟级
        # → 角色新记的速记本条目被静默跳过。(?::\d{2})? 两级都认。
        m = re.match(r"^### \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\] (\S+)((?: · .+)?)", p)
        if m:
            time_s, _role_s, note_s = m.group(1), m.group(2), m.group(3).lstrip(" ·")
            body = p[m.end():].strip()
            entries.append({"time": time_s, "note": note_s, "body": body})
    return entries[-max_entries:]


def list_scratchpads(root: Path | str) -> list[str]:
    """返回项目里已有速记本的角色名列表。

    只列名册角色：领航员记录本（2026-11-11）虽可读写，但**刻意不进这个列表**——
    该列表用于按角色卡片渲染，而领航员不是智库/引擎名册成员。
    """
    root = Path(root)
    d = root / SCRATCHPAD_DIR
    if not d.is_dir():
        return []
    import re
    names = []
    for f in d.iterdir():
        if f.is_file() and f.suffix == ".md":
            m = re.match(r"速记本-(.+)\.md", f.name)
            if m and m.group(1) in ALL_ROLES:
                names.append(m.group(1))
    return sorted(names)


def find_by_anchor(root: Path | str, role: str, anchor: str) -> str:
    """按 label（### [时间] 角色 · 备注）在速记本里定位条目，返回正文。

    2026-10-31 随向量记忆库新增：检索命中后回**事实源**取原文，不取摘要。
    找不到返回空串（调用方自行降级显示 anchor）。
    """
    # 2026-10-31：不走 scratchpad_path——它有 ALL_ROLES 校验，而领航员/书房伙伴
    # 的记忆也要能回源（它们的速记本由 memory._append_fact 直写，不在名册里）。
    # 2026-11-06 两处都找：append_labeled 修好参数序后 ALL_ROLES 角色写
    # 设定集/90-速记本/（scratchpad_path），而降级期(10-31~11-06)的历史条目在
    # 外置大脑/ 直写落点——两处都查,历史记忆照常回源。
    root_p = Path(root)
    import re
    want = str(anchor or "").strip()
    want_tail = want.split("]", 1)[-1].strip()
    # 两路径都先严格匹配再宽松——严格跨路径优先,防宽松回退误命中别的文件
    for strict in (True, False):
        for path in (root_p / "外置大脑" / "设定集" / "90-速记本" / ("速记本-" + role + ".md"),
                     root_p / "外置大脑" / ("速记本-" + role + ".md")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            pieces = re.split(r"(?=^### )", text, flags=re.M)
            for p in pieces:
                first = p.strip().split("\n")[0].strip() if p.strip() else ""
                if strict and first == want:
                    return "\n".join(p.split("\n")[1:]).strip()
                if not strict and want_tail and (first.endswith(want_tail) or want_tail in first):
                    return "\n".join(p.split("\n")[1:]).strip()
    return ""
