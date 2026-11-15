"""R23 审查模板(平台合规):

规格原文允许「默认无」——所以**不建文件、不改任何默认行为**;只有作者真在
`外置大脑/审查模板.md` 里写了规则,诊断(建议/门禁复审)才把这段要求带上。
写在这里而不是塞进 gates 的提示词里,是为了让「默认无」这件事能被一行代码证明:
没有文件 ⇒ `extra_critic()` 返回空串 ⇒ 复审提示词与从前**逐字节相同**。
"""

from __future__ import annotations

from pathlib import Path

from . import paths

REL = "审查模板.md"


def path_of(root: Path | str) -> Path:
    return Path(root) / paths.brain_rel(REL[:-3])


def load(root: Path | str) -> dict:
    """读作者的平台合规模板。**没有 = 默认无**,不是错误。"""
    p = path_of(root)
    if not p.is_file():
        return {"在": False, "标题": "", "规则": [], "原文": "",
                "说明": "没有审查模板,平台合规这一项不参与复审(规格默认如此)。"}
    raw = p.read_text(encoding="utf-8")
    rules: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        s = s.lstrip("-*").strip()
        if s:
            rules.append(s)
    title = ""
    for line in raw.splitlines():
        if line.strip().startswith("#"):
            title = line.strip().lstrip("#").strip()
            break
    return {"在": True, "标题": title, "规则": rules, "原文": raw,
            "说明": f"共 {len(rules)} 条合规规则,会带进复审提示词。"}


def extra_critic(root: Path | str) -> str:
    """要拼进复审提示词的那段。**默认空串**(没有模板时与从前完全一致)。"""
    info = load(root)
    if not info["在"] or not info["规则"]:
        return ""
    body = "\n".join(f"- {r}" for r in info["规则"])
    return ("【平台合规要求】(作者写在「外置大脑/审查模板.md」;只判这几条,别的不许自由发挥)\n"
            + body)
