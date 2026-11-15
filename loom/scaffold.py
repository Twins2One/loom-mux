"""loom init:纯离线铺项目骨架(永不联网、永不因缺 key 失败)。

只负责建文件、返回路径。怎么展示(CLI 树 / GUI 列表)交给前端。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import _toml_str
from .fingerprint import neutral_default
from .fsutil import atomic_write_text
from .paths import BODY_DIR, FINGERPRINT_REL, SNAPSHOT_DIR, PROJECT_CARD_REL

# init 靠下面的 copytree 把整份 templates/ 铺进项目。外置大脑里的
# 立项卡 / 文风参考 / 违禁词 三份都是【人手维护、init 拷一次、loom 从不回写】的可选卡——
# 拷进去后作者随便改,引擎只读不写。唯一例外是 写作指纹.md:它被特判、由 neutral_default() 现写一份
# 中性默认(见下方 init 末尾),因为它要随 seed/learn 演进,不能只拷模板。
TEMPLATES_DIR = Path(__file__).parent / "templates"
HIGHLIGHT = FINGERPRINT_REL  # 全片卖点
GENRE_DIR = "skills/题材"  # 题材库目录(相对项目根)


# 别名归一:键=别名,值=正式题材文件名(不含 .md)
# 2026-10-21:**依据起点手机端「分类」页的一手截图**校准(见 templates/skills/题材/README.md)。
# ⚠ 一旦某个名字有了自己的 .md,别名就不该再抢它 —— 见 _resolve_genre 的"文件优先"规则。
GENRE_ALIASES = {
    # 通用口语名
    "修真": "修仙", "玄幻修仙": "修仙",
    "都市修真": "都市异能",
    "游戏电竞": "电竞", "电竞文": "电竞",
    "直播": "直播文", "主播": "直播文", "直播带货": "直播文",
    "克系": "克苏鲁", "克系悬疑": "克苏鲁",
    # 玄幻的二级
    "东方玄幻": "玄幻", "异世大陆": "玄幻", "王朝争霸": "玄幻", "高武世界": "玄幻",
    # 武侠的二级
    "传统武侠": "武侠", "武侠幻想": "武侠", "国术": "武侠", "国术无双": "武侠",
    "古武未来": "武侠", "武侠同人": "武侠",
    # 仙侠的二级
    "修真文明": "修仙", "幻想修仙": "修仙", "现代修真": "修仙",
    "神话修真": "修仙", "古典仙侠": "修仙",
    # 军事的二级
    "军旅生涯": "军事", "军事战争": "军事", "战争幻想": "军事",
    "抗战烽火": "抗战谍战", "谍战特工": "抗战谍战",
    # 历史的二级(都归历史古代;架空类另见 历史脑洞)
    "历史传记": "历史古代", "秦汉三国": "历史古代", "上古先秦": "历史古代",
    "两晋隋唐": "历史古代", "五代十国": "历史古代", "两宋元明": "历史古代",
    "清史民国": "历史古代", "外国历史": "历史古代", "民间传说": "历史古代",
    # 游戏的二级
    "虚拟网游": "电竞", "游戏异界": "电竞", "游戏系统": "电竞", "游戏主播": "直播文",
    # 科幻的二级
    "古武机甲": "科幻", "未来世界": "科幻", "星际文明": "科幻",
    "超级科技": "科幻", "时空穿梭": "科幻", "进化变异": "科幻", "末世危机": "末世",
    # 悬疑灵异的二级
    "诡异神秘": "悬疑灵异", "侦探推理": "悬疑灵异", "灵异民俗": "悬疑灵异", "惊悚微恐": "悬疑灵异",
    # 诸天无限的二级 —— ⚠ 起点把「综漫」放在**男频·诸天无限**下面,不在女频
    "无限": "诸天无限", "诸天": "诸天无限", "万界": "诸天无限", "综漫": "诸天无限",
    # 轻小说的二级
    "原生幻想": "轻小说", "现代幻想": "轻小说", "衍生同人": "轻小说",
    "搞笑吐槽": "轻小说", "恋爱日常": "轻小说",
    "二次元": "N次元", "同人": "N次元",
    # 短篇的二级
    "民俗怪谈": "短篇", "多元科幻": "短篇", "古今奇谭": "短篇", "短故事": "短篇",
    # 女频(起点女生网)
    "女频仙侠": "仙侠奇缘", "仙侠言情": "仙侠奇缘",
    "全息网游": "游戏竞技", "网游言情": "游戏竞技", "网游情缘": "游戏竞技",
    "星际": "科幻空间", "机甲": "科幻空间", "虫族": "科幻空间",
    "星际恋歌": "科幻空间", "星空恋歌": "科幻空间",
    # 起点女频 古代言情 的二级
    "古代情缘": "古言", "古代言情": "古言", "宫闱宅斗": "宫斗宅斗", "经商种田": "种田",
    "女尊王朝": "古言", "穿越奇情": "古言", "清穿民国": "古言", "西方时空": "古言",
    # 起点女频 现代言情 的二级(只收能明确落到一张表的)
    "豪门世家": "豪门总裁", "婚恋情缘": "豪门总裁", "民国情缘": "民国言情", "异国情缘": "豪门总裁",
    # 起点女频 浪漫青春 / 玄幻言情 / 悬疑推理
    "青春疼痛": "青春甜宠", "叛逆成长": "青春甜宠", "青春纯爱": "青春甜宠",
    "异族恋情": "幻想言情", "魔法幻情": "幻想言情",
    "推理侦探": "女频悬疑", "诡秘惊险": "女频悬疑", "悬疑探险": "女频悬疑",
    "神秘文化": "女频悬疑", "幽情奇缘": "女频悬疑",
    # 起点女频 仙侠奇缘 的二级
    "仙侣奇缘": "仙侠奇缘", "武侠情缘": "仙侠奇缘",
    # 起点女频 现实生活(2026-10-21 新增的一级分类)
    "家与情感": "女频现实", "行业人生": "女频现实", "人文博览": "女频现实",
    # 晋江「纯爱」频道
    "纯爱": "纯爱", "BL": "纯爱",
    "现代都市纯爱": "纯爱", "现代幻想纯爱": "纯爱",
    "未来幻想纯爱": "纯爱", "古代纯爱": "纯爱",
    # 晋江「衍生」频道 + 起点女频轻小说
    "衍生纯爱": "衍生同人", "衍生言情": "衍生同人", "衍生无CP": "衍生同人",
    "二次元言情": "衍生同人", "原创轻小说": "衍生同人",
    "同人衍生": "衍生同人", "古典衍生": "衍生同人", "影视衍生": "衍生同人",
    "动漫衍生": "衍生同人", "其他衍生": "衍生同人", "唯美幻想": "衍生同人", "萌系变身": "衍生同人",
    # 晋江「无CP+」频道
    "无CP": "无CP", "无CP小说": "无CP", "男主无CP": "无CP", "女主无CP": "无CP", "多元小说": "无CP",
    # 晋江「百合」频道
    "百合": "百合", "百合小说": "百合",
    # 说明:都市/现实那批二级(爱情婚姻、都市生活、青春校园、家庭伦理、成功励志…)
    # 横跨多个速查表,**故意不建别名**,免得把作者指错地方。
}


def _resolve_genre(genre: str | None) -> str | None:
    """归一题材名;命中不到模板就返回 None(永不报错,守离线铁律)。

    **文件优先于别名**(2026-10-21):某个名字一旦有了自己的 .md(例如新增的 `玄幻.md`),
    别名表里残留的旧映射就不许再把它抢走 —— 否则"选了玄幻,拷进来的却是修仙"。
    """
    if not genre:
        return None
    name = genre.strip()
    if not (TEMPLATES_DIR / GENRE_DIR / f"{name}.md").exists():
        name = GENRE_ALIASES.get(name, name)
    return name if (TEMPLATES_DIR / GENRE_DIR / f"{name}.md").exists() else None


def available_genres() -> list[str]:
    """题材库里有哪些题材(供 CLI/WebUI 列选项)。"""
    d = TEMPLATES_DIR / GENRE_DIR
    return sorted(p.stem for p in d.glob("*.md") if p.stem != "README")


_ILLEGAL_DIRCHARS = re.compile(r'[\\/:*?"<>|]')  # Windows 目录名禁用字符(Mac/Linux 允许,故只 Win CI 暴露)


def _safe_dirname(name: str) -> str:
    """书名清成 Windows 也能建的目录名:非法字符→·、去尾部点/空格(Win 会静默剥、致路径不符)。
    书名本身原样存 loom.toml title(见 init 落 _toml_str(name)),不受此清洗影响。"""
    safe = _ILLEGAL_DIRCHARS.sub("·", name).rstrip(" .")
    return safe or "新书"


def init(name: str, parent: Path | None = None, genre: str | None = None,
         outline: str = "",
         idea: str = "", platform: str = "", stop_after: str = "") -> Path:
    """在 parent(默认当前目录)下建一个名为 name 的项目骨架,返回项目根路径。

    genre 命中题材库时,只把那一份题材速查拷进项目(没选/拼错则不拷,行为与不选一致)。
    idea(一句话设定)存 loom.toml 供 AI 铺底稿;platform 写立项卡平台行——都是作者在建书框
    填的,loom 只代为落盘(立项卡「从不自动写」红线不涉:内容所有权在作者)。
    """
    target = (parent or Path.cwd()) / _safe_dirname(name)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"目录 {target} 已存在且非空,换个名字或先清空。")

    # 全拷骨架,但排除整个题材目录(37 份不一把铺,按选中题材单拷)
    def _ignore(dir_path: str, names: list[str]) -> set[str]:
        if Path(dir_path).resolve() == (TEMPLATES_DIR / "skills").resolve():
            return {"题材"} & set(names)
        return set()

    shutil.copytree(TEMPLATES_DIR, target, dirs_exist_ok=True, ignore=_ignore)

    # 题材命中 → 只拷选中那一份(可手改、file-as-truth)
    chosen = _resolve_genre(genre)
    if chosen:
        dst = target / GENRE_DIR
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TEMPLATES_DIR / GENRE_DIR / f"{chosen}.md", dst / f"{chosen}.md")

    # loom.toml 填上书名;一句话设定紧随 title 落行(压单行,引号/反斜杠交给 _toml_str 正经转义)
    toml = target / "loom.toml"
    clean = " ".join(idea.split())          # 压单行;引号/反斜杠交给 _toml_str 正经转义
    content = toml.read_text(encoding="utf-8").replace('title = "__TITLE__"', f"title = {_toml_str(name)}")
    if clean:
        content = content.replace(f"title = {_toml_str(name)}\n",
                                  f"title = {_toml_str(name)}\nidea  = {_toml_str(clean)}\n", 1)
    atomic_write_text(toml, content)

    # 平台写进立项卡的可解析行(违禁词自检读它定基线)
    if platform.strip():
        card = target / PROJECT_CARD_REL
        atomic_write_text(card, re.sub(r"^平台:.*$", lambda m: f"平台:{platform.strip()}",
                                       card.read_text(encoding="utf-8"), count=1, flags=re.M))

    # 建书框选的题材代落立项卡「## 题材」格(同平台行先例:作者填的,loom 只代为落盘)
    if genre and genre.strip():
        card = target / PROJECT_CARD_REL
        text = card.read_text(encoding="utf-8")
        text = re.sub(r"(^##\s*题材\s*$).*?(?=^##\s|\Z)",
                      lambda m: f"{m.group(1)}\n{genre.strip()}\n\n", text, count=1, flags=re.M | re.S)
        atomic_write_text(card, text)

    # 停在哪一站(可选):插进 [novel] 段。模板里 [gate] 一定在它后面,所以锚 [gate] 插入最稳;
    # 空值不写这一行 → 老项目文件形态不变(默认跑满五步)
    if stop_after.strip():
        text = toml.read_text(encoding="utf-8")
        line = f'"停在哪一站" = {_toml_str(stop_after.strip())}\n'
        text = text.replace("\n[gate]", f"{line}\n[gate]", 1)
        atomic_write_text(toml, text)

    # 卷纲文件从建书第一天就在盘上，但新书不预设任何卷。
    # 只有作者手工填写，或领航员/大纲师的候选经作者确认落盘后，才产生可解析卷结构。
    from .paths import brain_rel as _brain_rel

    outline_rel = _brain_rel("全书大纲")
    outline_path = target / outline_rel
    outline_path.parent.mkdir(parents=True, exist_ok=True)
    seed = (outline or "").strip()
    if seed:
        head = "# 全书大纲\n\n> 建书时作者填的底稿(以后可以随便改;loom 只在你要求时才动它)。\n\n"
        atomic_write_text(outline_path, head + seed + "\n")
    elif not outline_path.is_file():
        atomic_write_text(outline_path, "# 全书大纲\n\n- 一句话主线:\n- 主角的一条线:\n- 结局想落在哪:\n")

    volume_rel = _brain_rel("卷纲")
    volume_path = target / volume_rel
    if not volume_path.is_file():                 # 模板没铺到就补一份不可解析的空说明
        atomic_write_text(volume_path, (
            "# 卷纲\n\n"
            "> 还没有分卷。请先和领航员或大纲师确认卷纲；也可以由作者手工填写。\n"
            "> 确认前 Loom 不猜卷数、卷名或章节范围。\n"))

    # 写作指纹.md 离线落一份中性默认(不联网、不播种)
    atomic_write_text(target / FINGERPRINT_REL, neutral_default())
    # 正文/.原稿 先建好(AI 原稿快照将落在这里)
    (target / SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)
    gitkeep = target / BODY_DIR / ".gitkeep"
    if gitkeep.exists():
        gitkeep.unlink()

    return target
