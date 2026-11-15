"""交付契约 + 截断信号的可审计性(2026-10-20)。

这两件事是同一个病根的两半,详见 `docs/流水线诊断-v2-审查.md`:

1. **交付契约没有机检**。责任编辑该交「改好后的整章稿 + 改动留痕」,但旧代码对
   "没有留痕块"的处置是当成全文正文收下(`split_edit_note` 返回 `(text, "")`)。
   真机代价:一篇 2424 字的自述分析被当成改稿,经 `drop_superseded` 顶掉写手初稿、
   再经 `_revise_contract` 算出"上游超 203%",终稿 395 字。

2. **截断信号在流式路径上被丢弃**。`_chunk_obj` 不解析 `finish_reason`,
   而本地 Qwen 走的正是 lite 流式路径 ⇒ "这一棒是不是被腰斩"事后无法审计。
"""
from __future__ import annotations

from loom.agents import _missing_edit_note
from loom.backends import (_THINK_BUDGET, OpenAICompatBackend, _chunk_obj,
                          _LiteOpenAI)
from loom import runlog
from loom.config import Config


# ── 1. 交付契约的判据 ─────────────────────────────────────────────────────

def test_没有留痕块的输出算没交改稿():
    """真机形态:整篇都是"让我先分析""用户要求我"这类过程文字。"""
    analysis = ("用户要求我作为责任编辑，对第1章进行质量把关。\n"
                "让我先分析初稿，对照自检清单逐项检查：\n**1. 爽点**：有。\n")
    assert _missing_edit_note(analysis) is True


def test_带留痕块的输出算交了():
    body = "矿道尽头火把的光斑扫过湿冷石壁。\n\n<LOOM:EDIT-NOTE>\n- 无硬伤\n</LOOM:EDIT-NOTE>\n"
    assert _missing_edit_note(body) is False


def test_旧单点哨兵也算交了():
    """兼容旧哨兵:迁移期的稿子不该被判成没交付。"""
    from loom.parse import EDIT_NOTE_SENTINEL
    assert _missing_edit_note("正文在此。\n" + EDIT_NOTE_SENTINEL + "\n留痕") is False


def test_空输出算没交():
    assert _missing_edit_note("") is True
    assert _missing_edit_note(None) is True


# ── 2. finish_reason 必须穿过 lite 流式解析层 ─────────────────────────────

def test_lite_流式帧带出_finish_reason():
    """旧版只取 delta.content,末帧的 finish_reason 在解析层就丢了。"""
    ev = _chunk_obj({"choices": [{"delta": {"content": ""}, "finish_reason": "length"}]})
    assert ev.choices[0].finish_reason == "length"
    ev2 = _chunk_obj({"choices": [{"delta": {"content": "正文"}}]})
    assert ev2.choices[0].finish_reason is None       # 中途帧照旧是 None,不能崩


# ── 3. 正文非空但被腰斩,也要补预算重来 ──────────────────────────────────

class _Msg:
    def __init__(self, content, reasoning=None):
        self.content = content
        if reasoning is not None:
            self.reasoning_content = reasoning


class _Choice:
    def __init__(self, content, finish, reasoning=None):
        self.message = _Msg(content, reasoning)
        self.finish_reason = finish


class _Resp:
    def __init__(self, content, finish, reasoning=None):
        self.choices = [_Choice(content, finish, reasoning)]


class _Script:
    def __init__(self, items):
        self.script = list(items)
        self.budgets: list[int] = []

    def create(self, **kw):
        self.budgets.append(kw["max_tokens"])
        return self.script.pop(0)


def _be(monkeypatch, script) -> tuple[OpenAICompatBackend, _Script]:
    monkeypatch.setenv("LOOM_OPENAI_COMPAT_KEY", "sk-test")
    be = OpenAICompatBackend(Config(provider="openai_compat", model="qwen3-local",
                                    base_url="http://127.0.0.1:8000/v1"), "openai_compat")
    comp = _Script(script)
    be._client = type("C", (), {"chat": type("X", (), {"completions": comp})()})()
    return be, comp


def test_正文非空但被腰斩时补满预算重来(monkeypatch):
    """旧版 `if text: return text` —— 正文非空就当成写完了。
    真机代价:2424 字断在"林砚",下游照收。"""
    be, comp = _be(monkeypatch, [
        _Resp("矿道尽头火把的光斑扫过湿冷石壁。林砚", "length"),
        _Resp("矿道尽头火把的光斑扫过湿冷石壁。林砚脊背贴着冷石。", "stop"),
    ])
    out = be.complete("s", "u", max_chars=800)
    assert out.endswith("脊背贴着冷石。"), "半截不该当完整产出交出去"
    assert len(comp.budgets) == 2
    assert comp.budgets[0] < _THINK_BUDGET and comp.budgets[1] == _THINK_BUDGET
    assert be.last_finish == "stop"


def test_正常收尾不额外多打一次(monkeypatch):
    be, comp = _be(monkeypatch, [_Resp("正文在此。", "stop")])
    assert be.complete("s", "u", max_chars=800) == "正文在此。"
    assert len(comp.budgets) == 1
    assert be.last_finish == "stop"


def test_补预算被端点拒时保留半截而不是把这一棒搞崩(monkeypatch):
    """有的 OpenAI 兼容端点输出上限低,给 65536 会 400。此时宁可留半截,别让整章失败。"""
    class _Reject(_Script):
        def create(self, **kw):
            if kw["max_tokens"] >= _THINK_BUDGET:
                raise RuntimeError("max_tokens too large")
            return super().create(**kw)

    monkeypatch.setenv("LOOM_OPENAI_COMPAT_KEY", "sk-test")
    be = OpenAICompatBackend(Config(provider="openai_compat", model="qwen3-local",
                                    base_url="http://127.0.0.1:8000/v1"), "openai_compat")
    comp = _Reject([_Resp("半截正文。", "length")])
    be._client = type("C", (), {"chat": type("X", (), {"completions": comp})()})()
    assert be.complete("s", "u", max_chars=800) == "半截正文。"


# ── 4. 账本要真的记下 usage 与 finish ────────────────────────────────────

def test_账本记下_tokens_与_finish(tmp_path):
    row = runlog.record(tmp_path, 1, role="责任编辑", ok=True, ms=1234,
                        chars_in=5000, chars_out=2400,
                        tokens={"prompt_tokens": 100, "completion_tokens": 900,
                                "total_tokens": 1000},
                        finish="length")
    assert row["tokens"]["total_tokens"] == 1000
    assert row["finish"] == "length"

    back = runlog.entries(tmp_path, 1)
    assert back[-1]["finish"] == "length"

    card = runlog.card(tmp_path, 1)
    assert card["截断次数"] == 1
    assert card["tokens"] == 1000
    assert "实际值合计" in card["tokens说明"]


def test_账本没回报_usage_时不编数(tmp_path):
    runlog.record(tmp_path, 1, role="写手", ok=True, ms=10, chars_out=800)
    card = runlog.card(tmp_path, 1)
    assert card["tokens"] is None
    assert "不按字数估" in card["tokens说明"]
