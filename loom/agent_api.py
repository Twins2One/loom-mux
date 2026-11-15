# -*- coding: utf-8 -*-
r"""14 角色系统的 API 路由。

提供角色元数据、速记本、对话等接口。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import scratchpad
from .scratchpad import AUTHOR_ROLES, ENGINE_ROLES, ALL_ROLES, VISIBLE_AUTHOR_ROLES

# ── 角色元数据 ──────────────────────────────────────────────────────────────

def list_agents() -> dict:
    """返回所有角色的元数据（不含提示词正文）。"""
    return {
        "navigator": {
            "name": "领航员",
            "role": "navigator",
            "category": "partner",
            "description": "引导作者建地基，完成后缩成右下角球路由其他角色。无记忆。",
            "produces": "路由建议 / 建地基引导",
            "has_scratchpad": False,
        },
        "author": [
            {"name": n, "role": n, "category": "author", "has_scratchpad": True}
            for n in sorted(VISIBLE_AUTHOR_ROLES)
        ],
        "engine": [
            {"name": n, "role": n, "category": "engine", "has_scratchpad": True}
            for n in sorted(ENGINE_ROLES)
        ],
    }


def agent_meta(role: str) -> dict | None:
    """返回角色的元数据（不含提示词正文）。"""
    if role not in ALL_ROLES and role != "领航员":
        return None
    
    meta_map = {
        "领航员": {
            "name": "领航员", "role": "领航员", "category": "partner",
            "tag": "带路 · 分派",
            "description": "书房的入口。带你把地基打完，之后按你的意图把活分给对的人；自己不留记忆，每次重新判断。",
            "produces": "路由建议",
            "reads": ["外置大脑/全书大纲.md", "外置大脑/设定集"],
        },
        "设定师": {
            "name": "设定师", "role": "设定师", "category": "author",
            "tag": "定世界 · 立规矩",
            "description": "全书设定的源头。立世界规则、钉死硬约束、维护设定自洽；写作引擎每一步都拿它当准绳。",
            "produces": "设定集 · 世界规则与人物锚点",
            "reads": ["外置大脑/设定集/00-立项.md", "外置大脑/设定集/10-世界观", "外置大脑/设定集/20-人物"],
        },
        "大纲师": {
            "name": "大纲师", "role": "大纲师", "category": "author",
            "tag": "定卷 · 定章 · 布线",
            "description": "全书骨架的总设计师。定卷、定章、布爆点、埋长线，产出三级结构；单章怎么填不是它的活。",
            "produces": "全书大纲 / 卷纲 / 卡章纲",
            "reads": ["外置大脑/设定集", "外置大脑/全书大纲.md", "外置大脑/卷纲.md", "外置大脑/卡章纲.md"],
        },
        "知识顾问": {
            "name": "知识顾问", "role": "知识顾问", "category": "author",
            "tag": "查证 · 供料",
            "description": "给设定集供料的查证者。书内/联网/附件三路取证，出提案，绝不自动写进知识库。",
            "produces": "知识提案（你点头才入设定集）",
            "reads": ["外置大脑/设定集/40-知识/索引.md", "外置大脑/设定集", "外置大脑/全书大纲.md"],
        },
        "语料分析师": {
            "name": "语料分析师", "role": "语料分析师", "category": "author",
            "tag": "量文风",
            "description": "给文风上尺子的人。本地统计你的词频、句式、节奏（不花 token），产出数据报告；"
                           "还可以按你的话把偏好写进「写作指纹」、把口径写进去AI味豁免清单，不碰正文。",
            "produces": "词频/句式/节奏报告 · 去AI味口径",
            "reads": ["正文", "外置大脑/写作指纹.md", "外置大脑/分析/去AI味口径.md"],
        },
        "战斗设计师": {
            "name": "战斗设计师", "role": "战斗设计师", "category": "author",
            "tag": "战力立法",
            "description": "战力的立法者。定等级、定招式、定克制与地形，让每场打斗有账可查，绝不崩战力。",
            "produces": "设定集 · 战斗体系",
            "reads": ["外置大脑/设定集/10-世界观", "外置大脑/设定集/20-人物", "外置大脑/设定集/30-战斗"],
        },
        "情绪节奏设计师": {
            "name": "情绪节奏设计师", "role": "情绪节奏设计师", "category": "author",
            "tag": "编排节拍",
            "description": "节奏的编排者。设计章内与卷内的情绪曲线，把爽点、憋屈、爆发排进节拍，读者放不下。",
            "produces": "情绪曲线（并入细纲注释）",
            "reads": ["外置大脑/卡章纲.md", "外置大脑/卷纲.md", "正文"],
        },
        # 2026-11-12 修复审查 P1：市场分析师原本不在硬编码映射里 ⇒ agent_meta 返回 None，
        # `/api/agent/meta`、`/api/agent/chat`、`/api/agent/chat/new` 全部 404。
        # 口径严格对齐 app.js AUTHOR_META["市场分析师"] 与 templates/agents/市场分析师.md，不改职责。
        "市场分析师": {
            "name": "市场分析师", "role": "市场分析师", "category": "author",
            "tag": "市场判断",
            "description": "回答「这本书在市场上怎么样」。卖点四件套逐项过、题材趋势、上架与推荐时机、"
                           "违禁比对；判断必给依据，决定权在你。",
            "produces": "市场分析报告",
            "reads": ["外置大脑/设定集", "外置大脑/全书大纲.md", "外置大脑/卡章纲.md", "外置大脑/违禁词.md"],
        },
        # 2026-11-12 用户新需求：作者智库新增「人物侧写师」。
        # 这段必须与前端 app.js 的 AUTHOR_META 口径一致（tag/reads/produces/description），
        # 否则后端 `/api/agent/meta` 与界面卡片会各说各话。
        "人物侧写师": {
            "name": "人物侧写师", "role": "人物侧写师", "category": "author",
            "tag": "细化人物",
            "description": "和你一起细化角色设计与人物卡。结合世界观、情绪节奏等素材，"
                           "补足人物动机、关系与人物和设定集之间的互动细节。",
            "produces": "细化的人物卡 · 人物与设定集互动细节",
            "reads": ["外置大脑/设定集/20-人物", "外置大脑/设定集/10-世界观",
                      "外置大脑/设定集", "外置大脑/卡章纲.md"],
        },
        "编辑总监": {
            "name": "编辑总监", "role": "编辑总监", "category": "author",
            "tag": "全书守门",
            "description": "全书的质量守门人。跨章审结构、审一致性、审伏笔回收，只看全局；单章硬伤归责任编辑。",
            "produces": "全局质量报告",
            "reads": ["外置大脑/全书大纲.md", "外置大脑/卷纲.md", "正文", "外置大脑/设定集"],
        },
        "批评家": {
            "name": "批评家", "role": "批评家", "category": "author",
            "tag": "只挑不改",
            "description": "专挑硬伤的人。查 OOC、设定漂移、因果断裂，只出清单和确切位置，一个字都不改。",
            "produces": "问题清单",
            "reads": ["正文", "外置大脑/设定集", "外置大脑/卡章纲.md"],
        },
        "章节细纲师": {
            "name": "章节细纲师", "role": "章节细纲师", "category": "engine",
            "tag": "第1棒 · 填情节",
            "description": "流水线第一棒。拿卡章纲和情绪曲线，把一章填成有场次、有人物反应、有章末钩的完整细纲。",
            "produces": "本章细纲",
            "reads": ["外置大脑/卡章纲.md", "外置大脑/卷纲.md", "正文"],
            "pipeline_position": 1,
        },
        "章节设定师": {
            "name": "章节设定师", "role": "章节设定师", "category": "engine",
            "tag": "第2棒 · 校设定",
            "description": "流水线第二棒。拿设定集逐条对照细纲，补细节、纠漂移，保证要写的东西不越界。",
            "produces": "本章细纲(含设定)",
            "reads": ["外置大脑/设定集", "正文/.细纲"],
            "pipeline_position": 2,
        },
        "章节分镜师": {
            "name": "章节分镜师", "role": "章节分镜师", "category": "engine",
            "tag": "第3棒 · 定镜头",
            "description": "流水线第三棒。在细纲里标注镜头、视角、快慢切，用结构化注释写进细纲，给写手指方向。",
            "produces": "本章细纲(含分镜)",
            "reads": ["正文/.细纲", "外置大脑/设定集"],
            "pipeline_position": 3,
        },
        "写手": {
            "name": "写手", "role": "写手", "category": "engine",
            "tag": "第4棒 · 落字",
            "description": "流水线第四棒，真正落字的人。照细纲写、按你的写作指纹写，越写越像你。",
            "produces": "本章初稿",
            "reads": ["正文/.细纲", "外置大脑/写作指纹.md", "外置大脑/设定集/20-人物"],
            "pipeline_position": 4,
        },
        "责任编辑": {
            "name": "责任编辑", "role": "责任编辑", "category": "engine",
            "tag": "第5棒 · 挑瑕疵",
            "description": "流水线第五棒。盘爽点、钩子、OOC、设定漂移，当场改掉；文风不动，那是润色师的事。",
            "produces": "本章改稿",
            "reads": ["正文", "外置大脑/设定集", "外置大脑/状态账本.md"],
            "pipeline_position": 5,
        },
        "润色师": {
            "name": "润色师", "role": "润色师", "category": "engine",
            "tag": "第6棒 · 去AI味",
            "description": "流水线最后一棒。擦掉 AI 腔和套话，留住你的口头禅和怪句，读起来像真人。",
            "produces": "本章终稿",
            "reads": ["正文", "外置大脑/写作指纹.md"],
            "pipeline_position": 6,
        },
    }
    return meta_map.get(role)


def get_scratchpad(role: str, root: Path | str, max_entries: int = 5) -> dict:
    """读取角色速记本的最后 max_entries 条。

    领航员也在这里：它的记录本由 `scratchpad` 层放开（`is_scratchpad_role`），
    端点层必须用**同一判据**，否则会出现「底层能写、HTTP 通路全被拒」
    （审查 审查记录 实测：GET/POST `/api/scratchpad/领航员` 都被拒）。
    **注意**：领航员**不**并入 `ALL_ROLES`（那会污染名册与前端卡片渲染）。
    """
    if not scratchpad.is_scratchpad_role(role):
        return {"snippets": [], "error": f"未知角色: {role}"}
    
    root_path = Path(root)
    snippets = scratchpad.load_snippets(role, root_path, max_entries=max_entries)
    return {"role": role, "snippets": snippets}


def append_scratchpad(role: str, root: Path | str, content: str, note: str = "") -> dict:
    """追加一条记录到角色速记本（领航员同理，判据见 get_scratchpad）。"""
    if not scratchpad.is_scratchpad_role(role):
        return {"ok": False, "error": f"未知角色: {role}"}
    
    root_path = Path(root)
    try:
        scratchpad.append(role, root_path, content, author_note=note)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_scratchpad_roles(root: Path | str) -> list[str]:
    """返回项目里已有速记本的角色名列表。"""
    return scratchpad.list_scratchpads(root)
