"""S6 上下文预算:确定性折叠/丢弃的单元 + 30+ 章 prompt 回放断言(评审要求的真护栏)。"""
from __future__ import annotations

from loom import paths
from loom.agents import run_pipeline
from loom.budget import WINDOW, drop_superseded, fold_recaps, fold_supplements, learn_budget
from loom.config import load_config
from loom.gates import CRITIC_去AI味, CRITIC_质检
from tests.conftest import FakeBackend

# ---------- 单元:折叠与丢弃 ----------

def _card(n_ch: int) -> str:
    lines = ["# 卡章纲", ""]
    body = "本章发生了很多值得记住的事,句子写得够长以便折叠预览截不到尾部。"
    for i in range(1, n_ch + 1):
        lines.append(f"- 第{i}章:第{i}章的人写规划行")
        lines.append(f"    [AI回顾] 摘要:{body}尾标TAILREC{i}。")
        lines.append(f"    - [埋设] 第{i}章的伏笔FS{i}")
    return "\n".join(lines)


def test_fold_recaps_window_and_human_lines():
    card = _card(30)
    folded = fold_recaps(card, 31)
    for i in range(31 - WINDOW, 31):          # 近窗口章:回顾全文在(含尾部)
        assert f"TAILREC{i}" in folded
    assert "TAILREC1" not in folded and "FS1" not in folded  # 远章:全文与伏笔行折掉,只留 40 字预览头
    assert "[AI回顾·已折叠]" in folded
    for i in range(1, 31):                     # 人写规划行永远逐字保留
        assert f"第{i}章的人写规划行" in folded
    assert len(folded) < len(card)
    assert fold_recaps(card, 5) == card        # 没超窗口 → 原样


def test_fold_supplements_keeps_human_body():
    # 块必须用 enrich 真实写盘函数造(### 头 + AI补充区头),防折叠正则与写侧格式漂移
    from loom.enrich import _append_supplement
    text = "# 世界观\n\n## 力量体系\n- 凡→化神\n"
    text = _append_supplement(text, 1, "- 远章补充SUPP1")
    text = _append_supplement(text, 29, "- 近章补充SUPP29")
    folded = fold_supplements(text, 31)
    assert "SUPP1" not in folded and "(已折叠" in folded
    assert "SUPP29" in folded and "凡→化神" in folded
    assert fold_supplements(text, 5) == text     # 没超窗口 → 原样


def test_fold_supplements_tolerates_hand_written_h2():
    text = "# 世界观\n\n## [AI补充·第1章]\n远章补充SUPP1\n\n## [AI补充·第29章]\n近章补充SUPP29\n"
    folded = fold_supplements(text, 31)
    assert "SUPP1" not in folded and "SUPP29" in folded


def test_fold_supplements_dir_form_growth_file(project):
    # 目录形态:AI 补充住 世界观/成长档案.md(rel 不是 WORLD_REL),进 prompt 也必须折叠
    from loom.agents import _knowledge_prompt
    from loom.enrich import _append_supplement
    text = _append_supplement("", 1, "- 远章补充GROWSUPP1")
    text = _append_supplement(text, 29, "- 近章补充GROWSUPP29")
    (project / paths.WORLD_DIR_REL / paths.GROWTH_NAME).write_text(text, encoding="utf-8")
    _, knowledge = _knowledge_prompt(project, 31, "设定师")
    assert "GROWSUPP29" in knowledge, "近窗口补充全文在场"
    assert "GROWSUPP1" not in knowledge, "远章补充必须折叠"


def test_drop_superseded_keeps_anchors_and_latest_draft():
    """6 棒改造后「细纲」有三版接力产出(本章细纲→含设定→含分镜),每版都覆盖写同一份稿,
    所以它们和初稿/改稿同构:**同类只留最后一版**。老口径(细纲逐版全留)会让下游同时看到
    三份互相矛盾的细纲;而「锚点逐字保留」的防漂移初衷不受损——最新一版细纲本身就含着
    前两版累积的场次骨架与设定依据(见 budget.drop_superseded 的 docstring)。"""
    ws = [("本章细纲", "一"), ("本章细纲(含设定)", "锚"), ("本章细纲(含分镜)", "纲"),
          ("本章初稿", "DRAFT_OLD"), ("本章改稿", "DRAFT_NEW")]
    out = drop_superseded(ws)
    assert [p for p, _ in out] == ["本章细纲(含分镜)", "本章改稿"]
    # 只交了一版细纲(锚点)时,它必须原样留着:累积非纯链式,别把唯一那份锚也丢了
    only_anchor = drop_superseded([("本章细纲(含设定)", "锚"), ("本章初稿", "DRAFT")])
    assert [p for p, _ in only_anchor] == ["本章细纲(含设定)", "本章初稿"]


def test_learn_budget_grows_with_fingerprint():
    assert learn_budget(300) == 1800            # 地板不变(旧行为)
    assert learn_budget(3000) == 4000           # 随体量长:3000*1.2+400
    assert learn_budget(99999) == 4096          # 封顶


# ---------- 回放断言:30+ 章工程下的 prompt 有界且保护槽逐字在场 ----------

def test_long_book_prompt_replay(project):
    toml = project / "loom.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('"章节字数" = 800', '"章节字数" = 120'),
                    encoding="utf-8")
    (project / paths.CARD_REL).write_text(_card(30), encoding="utf-8")
    # 上一章(第30章)与指纹 anchor、硬设定逐字块
    paths.chapter_path(project, 30).parent.mkdir(parents=True, exist_ok=True)
    paths.chapter_path(project, 30).write_text("# 三十\n\n前章结尾留了倒计时钩。\n", encoding="utf-8")
    fp = project / paths.FINGERPRINT_REL
    fp.write_text(fp.read_text(encoding="utf-8") + "\n> 这是我的anchor签名句ANCHORX。\n", encoding="utf-8")
    wv = project / paths.WORLD_DIR_REL / "力量体系.md"   # 脚手架已是目录形态:硬设定按文件名直送
    wv.write_text(wv.read_text(encoding="utf-8") + "\n- 凡境→蜕凡HARDFACTX\n", encoding="utf-8")

    seq = {"i": 0}

    def responder(s, u):
        if s in (CRITIC_质检, CRITIC_去AI味):
            return "通过"
        seq["i"] += 1
        body = f"第{seq['i']}棒产出STEP{seq['i']},长度足够过每棒非空闸。" * 2
        # 责任编辑这一棒的交付契约要求交出 <LOOM:EDIT-NOTE> 留痕块:不带标记 = 「没交改稿」,
        # 会被追问一次(多花一次调用、还退回上一棒原稿),那样这条回放断言测的就不是它想测的东西了。
        if "产出【本章改稿】" in u:
            body += ("\n\n<LOOM:EDIT-NOTE>\n《本章改动留痕》\n"
                     "- 删了两处套话,情绪点名改成动作。\n</LOOM:EDIT-NOTE>")
        return body

    be = FakeBackend(responder)
    run_pipeline(project, 31, be, load_config(project), resume=False)

    def _user_of(produces: str) -> str:
        """按【产物名】认棒,不按下标数——工序表从五棒变六棒、或某一棒被追问答一次,
        都不该让这条回放断言悄悄错位到隔壁那一棒的 prompt 上。"""
        hits = [u for _, u in be.calls if f"产出【{produces}】" in u]
        assert hits, f"没有哪一棒产出【{produces}】"
        return hits[-1]

    outliner = _user_of("本章细纲")
    assert f"TAILREC{30}" in outliner and "TAILREC1" not in outliner, "近窗口回顾全文、远章折叠"
    assert "第1章的人写规划行" in outliner, "人写规划行永远在场"
    writer = _user_of("本章初稿")
    assert "凡境→蜕凡HARDFACTX" in writer, "硬设定逐字保护槽"
    assert "这是我的anchor签名句ANCHORX" in writer, "anchor 例句逐字保护槽"
    polisher = _user_of("本章终稿")
    # 6 棒顺序:细纲师1 → 设定师2 → 分镜师3 → 写手4 → 责编5 → 润色师
    assert "STEP5" in polisher, "最新全文稿(改稿)必须在场"
    assert "STEP4" not in polisher, "被改稿取代的初稿不再下传润色师"
    assert "STEP3" in polisher, "最新一版细纲(含分镜)仍逐字保留(累积非纯链式)"
    assert "STEP1" not in polisher and "STEP2" not in polisher, "被分镜版取代的旧细纲不再下传"
    # 总量有界:大纲师 prompt 不应把 30 章回顾全文塞进来(粗界:折叠后 < 原卡章纲全文塞入的量)
    assert len(outliner) < len(_card(30)) + 3000