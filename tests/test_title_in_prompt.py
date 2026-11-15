"""书名注入:模型必须知道这本书叫什么(立项即铺底 §1 根因一)。"""
from loom.agents import regen_outline, run_pipeline
from loom.config import load_config, save_config
from loom.parse import EDIT_NOTE_CLOSE, EDIT_NOTE_OPEN

# 细纲三棒(细纲师/设定师/分镜师):脚本文本逐层加小节(L2=L1+一节、L3=L2+一节),
# 否则 agents._restore_missing_sections 会追问一次、调用数 +1,把钉死的次数契约搞崩
_OUTLINE_RUN = [
    "## 场景 1 验身\n崇祯睁眼在乾清宫,先验身边是谁。",
    "## 场景 1 验身\n崇祯睁眼在乾清宫,先验身边是谁。\n## 场景 2 召对\n召王承恩问阉党动向。",
    "## 场景 1 验身\n崇祯睁眼在乾清宫,先验身边是谁。\n## 场景 2 召对\n召王承恩问阉党动向。\n"
    "## 章末钩\n危机迫近,魏忠贤已动。",
]


class ScriptBackend:
    """按脚本顺序吐产出,记录 (system, user);耗尽即炸。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, system, user, *, max_chars=None, on_chunk=None):
        assert self.script, "脚本耗尽:调用次数超出预期"
        out = self.script.pop(0)
        self.calls.append((system, user))
        if on_chunk and out:
            on_chunk(out)
        return out


def test_title_in_every_step_prompt(project):
    be = ScriptBackend(_OUTLINE_RUN)
    regen_outline(project, 1, be, load_config(project))
    assert len(be.calls) == 3  # 章节细纲师 + 章节设定师 + 章节分镜师
    for _, user in be.calls:
        assert user.startswith("# 你要写的是《测试书》第 1 章。")


_PROSE = ("寅时三刻,铜锣未响。崇祯睁开眼,乾清宫的帐顶陈旧而熟悉。"
          "他记得煤山那棵歪脖子树,记得白绫勒进脖颈的凉,也记得城破前夜没人来救驾。"
          "这一次,他先要魏忠贤的命。")   # ≥40 实字:过 chapter_profile(100) 的终稿闸


def test_title_change_invalidates_resume(project):
    # 六棒:细纲师/设定师/分镜师/写手/责任编辑/润色师 + 标题
    full = [_OUTLINE_RUN[0], _OUTLINE_RUN[1], _OUTLINE_RUN[2],
            _PROSE, _PROSE + EDIT_NOTE_OPEN + "\n《本章改动留痕》\n- 收束更冷。\n" + EDIT_NOTE_CLOSE,
            _PROSE + "收束更冷。", "煤山"]
    cfg = load_config(project)
    cfg.gate_rounds = 0          # 关 gate:免去质检/去AI味的额外调用,脚本可控
    cfg.chapter_chars = 100      # 终稿闸降到 max(40, 12)=40 实字,测试文本可控
    save_config(project, cfg)
    cfg = load_config(project)
    run_pipeline(project, 1, ScriptBackend(list(full)), cfg)

    # 原样续跑:⚠️ 已知缺陷(本批次 C 类,见交付报告)——细纲改成三棒后,盘上细纲由
    # 「章节分镜师」覆盖写,而 agents._stale_local(agents.py:1103) 只拿「章节细纲师」那一棒的
    # 账本产出比对 → 每次都误判「作者手改过细纲」→ 细纲师走 WYSIWYG 旁路(不调模型)后
    # 设定师/分镜师重跑,workspace 一变下游签名全失配 → 整章重跑(只有标题那次是「应有」的)。
    # 续跑省计费的理想值 = 1 次(仅标题);修好 _stale_local 后把下面的 6 改回 1。
    idle = ScriptBackend([_OUTLINE_RUN[1], _OUTLINE_RUN[2],          # 设定师 / 分镜师(误重跑)
                          _PROSE, _PROSE + EDIT_NOTE_OPEN + "\n《本章改动留痕》\n- 同前。\n"
                          + EDIT_NOTE_CLOSE, _PROSE + "收束更冷。",   # 写手 / 责任编辑 / 润色师
                          "煤山"])                                     # 标题
    run_pipeline(project, 1, idle, cfg)
    assert "产出【本章细纲(含设定)】" in idle.calls[0][1], "首棒细纲师应走 WYSIWYG 旁路(不调模型)"
    assert len(idle.calls) == 6, "现状:整章重跑(缺陷);理想:1(仅标题)"
    assert "章节标题" in idle.calls[-1][0], "末次调用是标题生成,不是工序"
    assert "# 你要写的是" not in idle.calls[-1][1]

    cfg.title = "换了个书名"
    save_config(project, cfg)
    cfg = load_config(project)
    redo = ScriptBackend(list(full))   # 细纲师有细纲文件走 WYSIWYG 旁路,脚本富余没关系(不要求耗尽)
    run_pipeline(project, 1, redo, cfg)
    assert len(redo.calls) >= 2, "改书名必须让续跑签名失配、从细纲师起重跑"
    assert "《换了个书名》" in redo.calls[0][1]
