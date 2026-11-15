"""界面状态标记剥离：候选卡上那句「✓ 已落库 → 路径」不许被当成作者的话。

真实缺陷（2026-11-12 用户报）：作者把角色给的候选卡整块复制到输入框、改几个字发回来，
卡片上那句「✓ 已落库 → 外置大脑/设定集/00-立项.md」跟着进了正文 ⇒ 模型读到「已落库」，
以为这一格已经落盘，于是把作者改过的内容当成"已确认"，不再当新提案复核。

判据分两面，缺一面都不算修好：
  ① 正例：界面标记的各种形态必须被剥掉，且剥完不能留下空行塌陷；
  ② **负例**：作者自己在讨论「已落库」这件事时，那句话**必须原样保留**——
     只剥有明确标记形状的（带箭头/冒号/括号/独立 ✓），不做关键词一刀切。
"""
from __future__ import annotations

from loom.agent_chat import strip_ui_marks


def test_带箭头的落库行被剥掉():
    src = "立项·分区\n都市·都市异能\n✓ 已落库 → 外置大脑/设定集/00-立项.md"
    out, n = strip_ui_marks(src)
    assert n == 1
    assert "已落库" not in out
    assert "外置大脑/设定集/00-立项.md" not in out
    assert out == "立项·分区\n都市·都市异能"


def test_没有勾也带箭头也算界面标记():
    out, n = strip_ui_marks("内容一\n已落库 → 外置大脑/设定集/00-立项.md\n内容二")
    assert n == 1
    assert out == "内容一\n内容二"


def test_冒号形态与全角箭头也认():
    for line in ("已落库：外置大脑/设定集/00-立项.md",
                 "已落库: 外置大脑/设定集/00-立项.md",
                 "✓ 已落库　→　外置大脑/设定集/00-立项.md"):
        out, n = strip_ui_marks("头部\n" + line + "\n尾部")
        assert n == 1, line
        assert out == "头部\n尾部", line


def test_行内括号形态被剥掉():
    out, n = strip_ui_marks("这条已经写进去了（已落库），你看下措辞。")
    assert n == 1
    assert out == "这条已经写进去了，你看下措辞。"


def test_独立一行勾被剥掉():
    out, n = strip_ui_marks("第一行\n✓\n第三行")
    assert n == 1
    assert out == "第一行\n第三行"


def test_不留下塌陷的空行():
    src = "A\n\n✓ 已落库 → x.md\n\nB"
    out, _ = strip_ui_marks(src)
    assert "\n\n\n" not in out
    assert out.startswith("A") and out.endswith("B")


# ── 负例：作者真的在说这件事 ───────────────────────────────────────────
def test_作者讨论落库本身时不许误剥():
    for text in ("我点了落库，但你说没落进去？",
                 "这条已落库了吧？帮我确认一下。",
                 "已落库的那个文件路径是哪个",
                 "我不想落库，先放着"):
        out, n = strip_ui_marks(text)
        assert n == 0, text
        assert out == text, text


def test_普通行里带箭头但不是落库标记不算():
    src = "从第 3 章 → 第 15 章这条暗线要收掉"
    out, n = strip_ui_marks(src)
    assert n == 0
    assert out == src


def test_空输入不炸():
    assert strip_ui_marks("") == ("", 0)
    assert strip_ui_marks(None) == ("", 0)
