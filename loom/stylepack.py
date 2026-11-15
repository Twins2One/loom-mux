"""R22 文风档案:别人的文章 → 逐篇蒸出「句级 / 篇级」两层规则 → 按层分权注入。

与 `fingerprint` 的分工(ADR 0002 铁律不变):
  · 指纹 = **动态收敛**,唯一学习信号是人的手改(`learn`),且喂的是**作者自己写的字**;
  · 文风档案 = **静态参考**,喂的是**别人的文章**,蒸出规则给写手/审查照着写。
  两者不许互相顶替:自己的习惯进《写作指纹》,别人的腔调进《文风档案》。

产物形态(书里只引用档案名,不复制内容):

    <书>/style-packs/<档案名>/
        文档/<标题>.md      待蒸馏的原文,原样保留(不做任何清洗)
        蒸馏/<标题>.md      该篇蒸出的两层规则(逐篇独立,)
        状态.json           {标题: {蒸馏于, 细致度}} —— 界面上据此显示「蒸没蒸过」
        档案.md             **派生**:把所有 蒸馏/*.md 合并成一份,供注入链直接读
        meta.json           合并时间 / 篇数 / 模型

    <书>/外置大脑/文风档案.md   指针:用哪个档案 + **注入量**(弱/中/强)

**两个「强度」不是一回事**(拆开并写明):
  · 蒸馏细致度(`DETAIL_LEVELS` 粗/中/细)= 蒸的时候要求多少条、要不要例句锚点;
  · 注入量(`STRENGTH_BUDGET` 弱/中/强)= 写作时把规则**塞多少字**给模型。

注入矩阵(§6.2):篇级 → 细纲三棒 / 细纲审查;句级+篇级 → 写手 / 责任编辑 / 润色师 / 两道闸。
档案.md 是派生物 ⇒ 注入侧(`prompt_block`)完全不用知道「逐篇蒸馏」这件事。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .fsutil import atomic_write_text
from .request_turn import tool_commit, commit_effect
from .paths import brain_rel

PACKS_REL = "style-packs"           # **老落点（书内）**：只为兼容读保留，不再往里写
POINTER_REL = brain_rel("文风档案")  # 书内指针:用哪个档案 + 注入量(弱/中/强) —— 这个仍跟书走
LAYERS = ("句级", "篇级")

# ── 三层落点：汇总到 `知识库\语料库\`，不跟书走）─────────
# 为什么改：文风档案是"作者/别人文章"的蒸馏产物，跟书无关；放在每本书里会导致
# 同一份语料在不同书里各蒸一遍、还查不出哪份是最新的。作者裁定汇总到知识库。
# 目录名按前端规格逐字对齐（`原文` / `蒸馏原稿` / `写手参考.md`），别再叫 docs/distilled。
LIBRARY_SUBDIR = "语料库"            # 知识库根下的分类目录（与 study 的三层一致）
DOCS_SUB = "原文"
DISTILLED_SUB = "蒸馏原稿"
STATUS_FILE = "_状态.json"           # 下划线开头 = 元数据，界面不显示
ARCHIVE_FILE = "写手参考.md"          # 唯一进 prompt 的那一层

DEFAULT_LIB = "主档案"


def library_root() -> Path:
    """知识库根。**口径唯一收在 `loom/libroot.py`**（LOOM_LIBRARY_ROOT 可覆盖）。

    2026-11-11：原先这里是"问 study 拿不到就自己写死一份 E:\\Book\\知识库"，
    等于两处默认值靠运气对齐；现在两边都只调 libroot，不再各自持有缺省路径。
    """
    from . import libroot

    return libroot.library_root()


def archives_root() -> Path:
    return library_root() / LIBRARY_SUBDIR

# 注入量:每层最多给多少字(档位越强给得越全)。**这是「用的时候给多少」,不是蒸馏细致度。**
STRENGTH_BUDGET = {"弱": 900, "中": 3200, "强": 9000}
DEFAULT_STRENGTH = "中"

# 单篇上限：列表与读取用同一个数（2026-10-27 对方复核指出「列表里有、点开却被拒」）。
# 超过它的不进列表、也不收新导入——读不了的东西不该摆在列表上给人点。
MAX_DOC_BYTES = 2 * 1024 * 1024

# 蒸馏细致度:蒸的时候要求多少条 / 要不要例句锚点。**这是「蒸多细」,不是注入量。**
DETAIL_LEVELS = ("粗", "中", "细")
DEFAULT_DETAIL = "中"

# 谁读哪一层(§6.2 注入矩阵)。角色名照 PIPELINE 里的中文名,外加审查侧的两个闸。
ROLE_LAYERS: dict[str, tuple[str, ...]] = {
    "章节细纲师": ("篇级",),
    "章节设定师": ("篇级",),
    "章节分镜师": ("篇级",),
    "写手": ("篇级", "句级"),
    "责任编辑": ("篇级", "句级"),      # 正文审查
    "润色师": ("篇级", "句级"),
    "设定师": (),                  # 作者智库角色：设定不该被文风带偏
    "细纲审查": ("篇级",),
    # 两道闸:去 AI 味与质检都是**正文审查**,按矩阵读两层
    "去AI味": ("篇级", "句级"),
    "质检": ("篇级", "句级"),
}

_NAME_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')

_FORMAT = """按下面的结构输出(两个一级标题都要有,别加别的小标题):

# 句级
- 句式长短:…(给可执行的口径,例如「七成句子 ≤ 22 字」)
- 连接与过渡:…
- 标点与断句:…
- 对话与叙述比例:…
- 口头禅/习惯用语:…
- 应避免:…(这份稿子里明显不用的词或腔调)
- 例句锚点:…(从原文里抄 3~5 句最能代表语感的原句,逐字抄)

# 篇级
- 段落长度:…
- 场景切分与切换:…
- 信息释放顺序:…
- 单场字数区间:…
- 章末收束方式:…
- 转折点位置:…"""

# 「蒸多细」的三档口径(做成可选项并写明)
_DETAIL_SPEC = {
    "粗": "**这一档只要粗轮廓**:每层只写 3 条,挑最能代表这份稿子的三点,不要凑数;"
          "不写「例句锚点」。",
    "中": "**这一档要中等详细**:每层写 4~6 条,覆盖格式里列出的各项;"
          "确实看不出来的项写「无明显特征」,不要编;「例句锚点」抄 3 句。",
    "细": "**这一档要尽量细**:每层写 6~9 条,尽量给可执行口径(能落成数字就落成数字,"
          "例如「段落 3~5 句」「对话占三成」);「例句锚点」必须从原文**逐字**抄 5 句。",
}

_SYSTEM = ("你是资深文学编辑。只做**归纳**,不写创作建议,不评价好坏;"
           "所有结论必须能从给定稿子里指出来,给不出依据的宁可不写。")


def _safe_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        raise ValueError("名字不能空。")
    if _NAME_BAD.search(n) or n in (".", ".."):
        raise ValueError(f"「{n}」里有不能进文件名的字符。")
    return n


def pack_dir(root: Path | str | None, name: str = DEFAULT_LIB) -> Path:
    """一个档案的目录。

    新落点：`<知识库>/语料库/<档案名>/`）。
    兼容：若新落点没有、而**老落点**（`<书>/style-packs/<档案名>/`）有，就继续用老的——
    今天盘上零实例，这条纯粹是保险：万一哪本书里已经有档案，读它不会失明。

    `root` 允许为 None（2026-10-27 加）：书房首页没有"当前书"，档案本来跨书共享，
    没有书上下文时只看新落点。
    """
    safe = _safe_name(name or DEFAULT_LIB)
    new = archives_root() / safe
    if new.exists() or not root:
        return new
    old = Path(root) / PACKS_REL / safe
    if old.exists():
        return old
    return new          # 都没有 → 新落点（第一次写时自动建）


def _docs_dir(root, name) -> Path:
    return pack_dir(root, name) / DOCS_SUB


def _dist_dir(root, name) -> Path:
    return pack_dir(root, name) / DISTILLED_SUB


def _status_path(root, name) -> Path:
    return pack_dir(root, name) / STATUS_FILE


def _read_status(root, name) -> dict:
    p = _status_path(root, name)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (ValueError, OSError):
        return {}


def _write_status(root, name, data: dict) -> None:
    atomic_write_text(_status_path(root, name), json.dumps(data, ensure_ascii=False, indent=2))


def _check_book(root) -> dict | None:
    if root is None:
        return None        # 书房首页没有当前书：只读知识库那一侧，不做"这是不是一本书"的检查
    if not (Path(root) / "loom.toml").is_file():
        return {"ok": False, "code": "not_a_book", "reason": f"{root} 不是一本书。"}
    return None


def _doc_title(filename: str) -> str:
    n = (filename or "").strip()
    return n[:-3] if n.endswith(".md") else n


# ---------------------------------------------------------------- 文档库(读)
def list_library(root: Path | str, name: str = DEFAULT_LIB) -> dict:
    """这份档案的文档库:每篇的字数、蒸没蒸过、什么时候蒸的。**纯只读。**

    两个口径（2026-10-27 对方复核提出，界面会露馅的地方）：
    - **超过 `MAX_DOC_BYTES` 的篇不进列表**（计进 `超大跳过`）：读不动的文件不该摆在列表上，
      否则界面会渲染出一篇点开即被拒的文档。
    - **`存在` 区分「档案没建」与「建了但空」**：前者 `False`、后者 `True`，界面因此能说人话。
    """
    d_docs, d_dist = _docs_dir(root, name), _dist_dir(root, name)
    status = _read_status(root, name)
    docs, oversized = [], 0
    if d_docs.is_dir():
        for p in sorted(d_docs.glob("*.md")):
            try:
                if p.stat().st_size > MAX_DOC_BYTES:
                    oversized += 1
                    continue
            except OSError:
                continue                     # 拿不到大小就当它不可读，不列
            title = _doc_title(p.name)
            st = status.get(title) or {}
            try:
                n = len(re.sub(r"\s+", "", p.read_text(encoding="utf-8")))
            except OSError:
                n = 0
            docs.append({
                "标题": title,
                "字数": n,
                "已蒸馏": (d_dist / p.name).is_file(),
                "蒸馏于": st.get("蒸馏于", ""),
                "细致度": st.get("细致度", ""),
            })
    return {
        "档案": name,
        "文档": docs,
        "篇数": len(docs),
        "已蒸馏": sum(1 for x in docs if x["已蒸馏"]),
        "有档案": (pack_dir(root, name) / ARCHIVE_FILE).is_file(),
        "存在": pack_dir(root, name).is_dir(),
        "超大跳过": oversized,
    }


def list_packs(root: Path | str | None) -> list[dict]:
    """这本书有哪些档案(兼容旧调用:把「篇数」映射到原来的「原始篇数」)。

    2026-10-27 起档案住在 `<知识库>/语料库/`，**不再跟书走**；所以这里除了新落点，
    还要把该书老落点（`<书>/style-packs/`）里的档案一并列出——两份都算"可用的档案"。
    `root` 为 None 时（书房首页没有当前书）只列新落点。
    """
    seen: set[str] = set()
    dirs: list[Path] = []
    bases = [archives_root()] + ([Path(root) / PACKS_REL] if root else [])
    for base in bases:
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir() and d.name not in seen:
                seen.add(d.name)
                dirs.append(d)
    out = []
    for d in dirs:
        lib = list_library(root, d.name)
        out.append({"名字": d.name, "原始篇数": lib["篇数"],
                    "已蒸馏": lib["已蒸馏"], "有档案": lib["有档案"]})
    return out


def list_archives(root: Path | str) -> list[dict]:
    """给前端 `GET /api/style/archives` 用的形状（三层各自的份数 + 有没有写手参考）。

    形状（与前端规格逐字对齐）：
        [{"name":"档案名","layer_counts":{"raw":3,"distilled":3,"writer":1},
          "has_writer":true,"mtime":1793030422}]
    **不带正文**——点开再取；不分页。
    """
    out: list[dict] = []
    for item in list_packs(root):
        name = item["名字"]
        try:
            pd = pack_dir(root, name)
            raw = len(list(_docs_dir(root, name).glob("*.md"))) if _docs_dir(root, name).is_dir() else 0
            dis = (len(list(_dist_dir(root, name).glob("*.md")))
                   if _dist_dir(root, name).is_dir() else 0)
            writer = 1 if (pd / ARCHIVE_FILE).is_file() else 0
            mtime = int(pd.stat().st_mtime) if pd.exists() else 0
        except OSError:
            raw = dis = writer = mtime = 0
        out.append({"name": name,
                    "layer_counts": {"raw": raw, "distilled": dis, "writer": writer},
                    "has_writer": bool(writer), "mtime": mtime})
    return out


# ---------------------------------------------------------------- 文档库(写)
@tool_commit
def add_doc(root: Path | str, name: str, title: str, text: str) -> dict:
    """往文档库里加一篇(粘贴进来的短文档也走这条路)。原样保留,不做任何清洗。"""
    bad = _check_book(root)
    if bad:
        return bad
    if not (text or "").strip():
        return {"ok": False, "code": "empty", "reason": "这篇是空的,没加。"}
    if len(text.encode("utf-8")) > MAX_DOC_BYTES:
        return {"ok": False, "code": "too_big",
                "reason": "这一篇超过 2 MB，不收。请分段：取样要的是片段，不是整本书。"}
    try:
        t = _safe_name(title or "未命名")
    except ValueError as e:
        return {"ok": False, "code": "bad_title", "reason": str(e)}
    d = _docs_dir(root, name)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{t}.md"
    if target.exists():
        return {"ok": False, "code": "exists",
                "reason": f"「{t}」已经在库里了。换个标题,或先删掉旧的。"}
    atomic_write_text(target, text)
    return {"ok": True, "标题": t, "字数": len(re.sub(r"\s+", "", text)),
            "库": list_library(root, name)}


def import_docs(root: Path | str, name: str, paths: list[str]) -> dict:
    """把外面选中的文件**复制**进文档库(不移动、不改原件)。支持一次多个。"""
    bad = _check_book(root)
    if bad:
        return bad
    d = _docs_dir(root, name)
    d.mkdir(parents=True, exist_ok=True)
    added, skipped = [], []
    for raw in paths or []:
        p = Path(str(raw)).expanduser()
        if not p.is_file():
            skipped.append({"文件": str(p), "原因": "找不到这个文件"})
            continue
        if p.suffix.lower() not in (".md", ".txt"):
            skipped.append({"文件": p.name, "原因": "只收 .md / .txt"})
            continue
        try:
            if p.stat().st_size > MAX_DOC_BYTES:
                skipped.append({"文件": p.name, "原因": "超过 2 MB，请分段再导"})
                continue
        except OSError as e:
            skipped.append({"文件": p.name, "原因": f"读不了:{e}"})
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            skipped.append({"文件": p.name, "原因": f"读不了:{e}"})
            continue
        if not text.strip():
            skipped.append({"文件": p.name, "原因": "空文件"})
            continue
        title = p.stem
        target = d / f"{title}.md"
        n = 2
        while target.exists():           # 同名不覆盖:自动加后缀,原件一个字不动
            target = d / f"{title}({n}).md"
            n += 1
        atomic_write_text(target, text)
        added.append(target.name)
    return {"ok": True, "导入": added, "跳过": skipped, "库": list_library(root, name)}


def delete_doc(root: Path | str, name: str, title: str) -> dict:
    """从库里删一篇:原文、该篇的蒸馏产物、状态记录一起清掉,再重算合并档案。"""
    bad = _check_book(root)
    if bad:
        return bad
    try:
        t = _safe_name(title)
    except ValueError as e:
        return {"ok": False, "code": "bad_title", "reason": str(e)}
    gone = []
    for p in (_docs_dir(root, name) / f"{t}.md", _dist_dir(root, name) / f"{t}.md"):
        if p.is_file():
            p.unlink()
            gone.append(p.name)
    st = _read_status(root, name)
    if t in st:
        st.pop(t, None)
        _write_status(root, name, st)
    _rebuild_archive(root, name)
    return {"ok": True, "删除": gone, "库": list_library(root, name)}


# ---------------------------------------------------------------- 蒸馏
def _distill_one(root, name, title, backend, model: str, detail: str) -> dict:
    """蒸一篇。输出不合格就**不写**这一篇的产物(宁缺,不写假规则)。"""
    src = _docs_dir(root, name) / f"{title}.md"
    if not src.is_file():
        return {"ok": False, "code": "no_doc", "title": title, "reason": f"库里没有「{title}」。"}
    text = src.read_text(encoding="utf-8")
    spec = _DETAIL_SPEC.get(detail) or _DETAIL_SPEC[DEFAULT_DETAIL]
    user = (f"下面是一篇参考稿(共 {len(re.sub(chr(92)+'s+', '', text))} 字),请据此蒸出文风规则:\n\n"
            f"{text}\n\n{_FORMAT}\n\n{spec}")
    out = backend.complete(_SYSTEM, user, max_chars=4000)
    layers = parse_archive(out or "")
    missing = [x for x in LAYERS if not layers.get(x, "").strip()]
    if missing:
        # 原因里**不再自带标题**（2026-10-27 晚对方复核）：调用方把「标题」和「原因」分两栏显示，
        # 自带标题会渲染成「验收样本 「验收样本」没蒸出…」。标题由 `title` 字段单独给。
        return {"ok": False, "code": "bad_output", "title": title,
                "reason": f"没蒸出「{'、'.join(missing)}」这一层,不落盘。",
                "原文前 200 字": (out or "")[:200]}
    d = _dist_dir(root, name)
    body = ("---\n" + f"来源: {title}\n细致度: {detail}\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M')}\n模型: {model or '(没记)'}\n---\n\n")
    for layer in LAYERS:
        body += f"# {layer}\n{layers[layer].strip()}\n\n"
    with commit_effect():
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(d / f"{title}.md", body)
        st = _read_status(root, name)
        st[title] = {"蒸馏于": time.strftime("%Y-%m-%d %H:%M"), "细致度": detail}
        _write_status(root, name, st)
    return {"ok": True, "title": title,
            "行数": {k: len(v.splitlines()) for k, v in layers.items()}}


def distill_docs(root: Path | str, name: str, titles: list[str], backend,
                 model: str = "", detail: str = DEFAULT_DETAIL, progress=None) -> dict:
    """逐篇蒸馏(:每篇各蒸一份,库里累积多份)。

    每篇一次调用 ⇒ 调用次数 = 选中篇数。中途某篇失败不影响已成功的,
    最后统一重算合并档案(档案.md)并回报成功/失败清单。
    """
    bad = _check_book(root)
    if bad:
        return bad
    if detail not in DETAIL_LEVELS:
        return {"ok": False, "code": "bad_detail",
                "reason": f"细致度只有 {'/'.join(DETAIL_LEVELS)} 三档。"}
    want = [(_doc_title(t) if str(t).endswith(".md") else str(t)).strip() for t in (titles or [])]
    want = [t for t in want if t]
    if not want:
        return {"ok": False, "code": "no_selection", "reason": "先勾选要蒸馏的文档。"}
    ok, failed = [], []
    for i, t in enumerate(want, 1):
        if progress:
            progress({"kind": "info", "text": f"正在蒸馏 {i}/{len(want)}:《{t}》…"})
        try:
            r = _distill_one(root, name, t, backend, model, detail)
        except Exception as e:      # noqa: BLE001 - 单篇失败不拖垮整批
            r = {"ok": False, "code": "backend_failed", "title": t,
                 "reason": f"模型调用失败:{type(e).__name__}: {e}"}
        (ok if r.get("ok") else failed).append(r)
    _rebuild_archive(root, name, model=model)
    return {"ok": bool(ok), "成功": [r.get("title") for r in ok],
            "失败": [{"标题": r.get("title"), "原因": r.get("reason")} for r in failed],
            "成功数": len(ok), "失败数": len(failed),
            "库": list_library(root, name),
            "档": read_archive(root, name)}


@tool_commit
def _rebuild_archive(root, name, model: str = "") -> None:
    """把 蒸馏/*.md 合并成 档案.md(派生)。注入侧只读这一份,不用知道逐篇这回事。"""
    d = _dist_dir(root, name)
    merged: dict[str, list[str]] = {k: [] for k in LAYERS}
    used = []
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            layers = parse_archive(p.read_text(encoding="utf-8"))
            if not layers:
                continue
            used.append(_doc_title(p.name))
            for layer in LAYERS:
                if layers.get(layer):
                    merged[layer].append(f"—— 来自《{_doc_title(p.name)}》 ——\n{layers[layer].strip()}")
    body = ("---\n" + f"档案: {name}\n合并来源: {len(used)} 篇\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M')}\n模型: {model or '(没记)'}\n---\n\n")
    for layer in LAYERS:
        body += f"# {layer}\n" + ("\n\n".join(merged[layer]) if merged[layer] else "(暂无)") + "\n\n"
    atomic_write_text(pack_dir(root, name) / ARCHIVE_FILE, body)
    meta = {"名字": name, "合并来源": used,
            "时间": time.strftime("%Y-%m-%d %H:%M"), "模型": model or ""}
    atomic_write_text(pack_dir(root, name) / "meta.json",
                      json.dumps(meta, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 读档案 / 指针
def parse_archive(text: str) -> dict:
    """把档案.md 拆成 {层名: 该层正文}。两层都缺 → 空 dict(调用方据此拒绝入档)。"""
    out: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^#\s+(句级|篇级)\s*$", line.strip())
        if m:
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), []
            continue
        # 0.9.0 B2 卡片集六节(设计稿 §5):新版写手参考是卡片集形态——
        # 按节映射到注入层:句级←文风规则+高频句式库;篇级←节奏/人物/叙事手法+禁用清单
        m2 = re.match(r"^##\s+(文风规则|节奏规则|人物塑造|叙事手法|禁用清单|高频句式库)\s*$",
                      line.strip())
        if m2:
            if cur:
                # 同层多节合并(文风规则+高频句式库都是句级,不互相覆盖)
                prev = out.get(cur, "")
                out[cur] = (prev + "\n" + "\n".join(buf).strip()).strip()
            sec = m2.group(1)
            cur, buf = ("句级" if sec in ("文风规则", "高频句式库") else "篇级"), []
            continue
        if line.strip() == "---" and cur is None:
            continue
        if cur:
            buf.append(line)
    if cur:
        out[cur] = (out.get(cur, "") + "\n" + "\n".join(buf).strip()).strip()
    return {k: v for k, v in out.items() if v}


def read_archive(root: Path | str, name: str = DEFAULT_LIB) -> dict:
    f = pack_dir(root, name) / ARCHIVE_FILE
    if not f.is_file():
        return {}
    return parse_archive(f.read_text(encoding="utf-8"))


def read_pointer(root: Path | str) -> dict:
    """`外置大脑/文风档案.md` 里那两行人话。缺文件/缺行 → 空(等于不注入)。"""
    f = Path(root) / POINTER_REL
    if not f.is_file():
        return {}
    name, strength = "", ""
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*档案\s*[:：]\s*(.+?)\s*$", line)
        if m:
            name = m.group(1)
        m = re.match(r"^\s*注入量\s*[:：]\s*(.+?)\s*$", line)
        if not m:
            m = re.match(r"^\s*强度\s*[:：]\s*(.+?)\s*$", line)   # 旧写法兼容
        if m:
            strength = m.group(1)
    if strength not in STRENGTH_BUDGET:
        strength = DEFAULT_STRENGTH
    return {"档案": name, "强度": strength} if name else {}


def set_pointer(root: Path | str, name: str, strength: str = DEFAULT_STRENGTH) -> dict:
    if strength not in STRENGTH_BUDGET:
        return {"ok": False, "code": "bad_strength",
                "reason": f"注入量只有 {'/'.join(STRENGTH_BUDGET)} 三档。"}
    if not (pack_dir(root, name) / ARCHIVE_FILE).is_file():
        return {"ok": False, "code": "no_archive",
                "reason": f"「{name}」还没有蒸出合并档案,先蒸一篇再指过来。"}
    body = (f"# 文风档案(给谁看:这条只影响提示词,不改正文)\n\n"
            f"档案: {_safe_name(name)}\n注入量: {strength}\n\n"
            "改这两行就是换档案/换注入量;删掉「档案:」那行等于不用档案。\n"
            "注入量三档差别(写作时**塞多少字**给模型):弱=只给摘要与少量例句;"
            "中=两层规则的大部分;强=两层规则 + 更多原句锚点。\n")
    atomic_write_text(Path(root) / POINTER_REL, body)
    return {"ok": True, "档案": name, "强度": strength}


def prompt_block(root: Path | str, role: str) -> str:
    """按注入矩阵给这一棒拼一段「文风档案」块;没有档案/这棒不读 → 空串。"""
    layers = ROLE_LAYERS.get(role, ())
    if not layers and role:
        # 角色名可能带后缀(「去AI味(退回)」这种),按前缀再认一次
        for k, v in ROLE_LAYERS.items():
            if role.startswith(k):
                layers = v
                break
    if not layers:
        return ""
    ptr = read_pointer(root)
    if not ptr:
        return ""
    arch = read_archive(root, ptr["档案"])
    if not arch:
        return ""
    budget = STRENGTH_BUDGET.get(ptr["强度"], STRENGTH_BUDGET[DEFAULT_STRENGTH])
    # 交叉审查 P1-5:①budget 是**总预算**;②按**整行**截断(不切半句);③句级先分 60%,篇级拿剩下的。
    alloc: dict[str, int] = {}
    left = budget
    order = sorted([l for l in ("句级", "篇级") if l in layers],
                   key=lambda x: ("句级", "篇级").index(x))
    for i, layer in enumerate(order):
        want = len((arch.get(layer) or "").strip())
        if i == len(order) - 1:
            share = left
        else:
            share = min(max(200, int(budget * 0.6)), max(want, 200))
        share = min(share, left)
        alloc[layer] = share
        left -= share
    parts: list[str] = []
    for layer in order:
        text = (arch.get(layer) or "").strip()
        share = alloc.get(layer, 0)
        if not text or share <= 0:
            continue
        if len(text) > share:
            kept, used = [], 0
            for line in text.splitlines():
                if used + len(line) + 1 > share:
                    break
                kept.append(line)
                used += len(line) + 1
            total = len([l for l in text.splitlines() if l.strip()])
            kept_n = len([l for l in kept if l.strip()])
            text = "\n".join(kept).rstrip()
            if total > kept_n:
                text += f"\n…(注入量{ptr['强度']}只放得下这些,另有 {total - kept_n} 行没塞进来;调高注入量能看到)"
        parts.append(f"【文风档案 · {layer}】(照这个写,别照抄内容)\n{text}")
    if not parts:
        return ""
    head = f"【文风档案「{ptr['档案']}」· 注入量{ptr['强度']}】"
    return head + "\n" + "\n\n".join(parts)
