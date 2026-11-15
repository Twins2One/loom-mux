"""分批铺卡章纲:一次 50 章,按卷纲的起止算区间,人写过的行绝不覆盖。

为什么必须分批(作者 2026-10-18 裁定量级:一卷 200–400 章、2–3 卷):
一次让模型写 200 行章纲,输出上万字,必然中途塌方(重复/漏号/质量崩)。
所以按「块」铺:每块 50 章,块内一行一章,块与块之间用**已铺的最后一章**当上下文接住。

三条纪律:
  ① **只填空行、只追加缺行**——人写过的章行一个字都不动(复用 journey._apply_card_lines);
  ② **块边界来自卷纲的 `- 起止:第a-b章`**,不自己编章号;卷纲没写起止就报出来,不猜;
  ③ 铺完**回报每一块的区间与落了几行**,让人一眼看出哪块没铺成。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import events, paths
from .fsutil import atomic_write_text

BATCH = 50                      # 一块 50 章(作者 2026-10-18 定)
_VOL_HEAD = re.compile(r"^##\s*(第[^\s#]+\s*卷[^\n]*)$", re.M)
_RANGE = re.compile(r"起止\s*[:：]\s*第?\s*(\d+)\s*[-–—~～]\s*第?\s*(\d+)\s*章?")
# `[ \t]*` 而不是 `\s*`:后者会吃掉行尾换行,于是「- 第1章:」的空内容变成下一行
# 「- 第2章:」——空章行被判成"已填",偶数章被跳过,第 1 章永远铺不上(交叉审查 P0-1)。
_CARD = re.compile(r"^-\s*第(\d+)章[:：][ \t]*(.*)$", re.M)


def volume_ranges(root: Path | str) -> list[dict]:
    """从卷纲读出每卷的起止章。没写起止的卷会被标出来(不猜)。"""
    p = Path(root) / paths.VOLUMES_REL
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8")
    heads = [(m.start(), m.group(1).strip()) for m in _VOL_HEAD.finditer(text)]
    out = []
    for i, (pos, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        block = text[pos:end]
        m = _RANGE.search(block)
        if not m:
            out.append({"卷": name, "起": None, "止": None, "问题": "这一卷没写「起止:第a-b章」"})
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if b < a:
            out.append({"卷": name, "起": a, "止": b, "问题": f"起止写反了(第{a}-{b}章)"})
            continue
        out.append({"卷": name, "起": a, "止": b, "问题": ""})
    return out


def card_state(root: Path | str) -> dict[str, str]:
    """卡章纲里「第N章 → 内容」。内容为空串表示这行还空着。"""
    p = Path(root) / paths.CARD_REL
    if not p.is_file():
        return {}
    return {int(m.group(1)): m.group(2).strip() for m in _CARD.finditer(p.read_text(encoding="utf-8"))}


def next_batch(root: Path | str, size: int = BATCH) -> dict:
    """算出下一块该铺哪几章:从第一卷起、第一处「缺行或空行」开始连续取 size 章。"""
    vols = [v for v in volume_ranges(root) if v["起"] and v["止"]]
    if not vols:
        return {"ok": False, "code": "no_volumes",
                "reason": "卷纲里没有可用的「起止:第a-b章」,先写卷纲再铺章纲。"}
    have = card_state(root)
    for v in vols:
        want = [n for n in range(v["起"], v["止"] + 1) if not have.get(n)]
        if not want:
            continue
        batch = want[:size]
        return {"ok": True, "卷": v["卷"], "起": batch[0], "止": batch[-1],
                "章数": len(batch), "本卷还缺": len(want),
                "已铺到": batch[0] - 1,
                "前情": _prev_context(have, batch[0]),
                "落盘": False}
    return {"ok": False, "code": "all_done",
            "reason": "卷纲区间内的章行都铺满了。要开新卷,先往卷纲里加一卷的起止。"}


def batch_for(root: Path | str, lo: int, hi: int, *, cap: int = 120) -> dict:
    """**指定区间**的「这一块」——给联动链 ③b「重排这一块」用。

    为什么不能直接复用 `next_batch`：它是「从第一卷起、第一处缺行开始」的语义，**不保证**
    正好是卷纲刚变的那一块；用它，提示说「第 51–100 章」也可能铺到第 1–50 章去。
    返回形状与 `next_batch` 完全一致，所以 `/api/card/expand` 两条路共用后面所有代码
    （build_prompt / apply_batch / 报账 / 记账），不新增长在别处的第二套。

    硬纪律不变：**只填空行、只追加缺行**（apply_batch 保证），人写过的行一个字不动。
    这块**已经写满**时直接 `ok: False` 拒绝——不为「铺了 0 行」付一次模型调用。
    """
    lo = max(1, int(lo))
    hi = max(lo, int(hi))
    hi = min(hi, lo + max(1, int(cap)) - 1)      # 一块最多 120 章：再大必然塌方（同端点口径）
    have = card_state(root)
    vol = ""
    for v in volume_ranges(root):
        if v["起"] and v["起"] <= lo <= v["止"]:
            vol = f"{v['卷']}:第{v['起']}-{v['止']}章"
            break
    want = [n for n in range(lo, hi + 1) if not have.get(n)]
    if not want:
        return {"ok": False, "code": "all_filled",
                "reason": f"第 {lo}-{hi} 章的章行都写满了。铺章纲只补空行、不覆盖你写过的行;"
                          f"要整块重来,得先手动删掉这一块的章行。"}
    return {"ok": True, "卷": vol, "起": lo, "止": hi, "章数": hi - lo + 1,
            "本卷还缺": len(want), "已铺到": lo - 1,
            "前情": _prev_context(have, lo), "落盘": False}


def _prev_context(have: dict[int, str], start: int, back: int = 3) -> str:
    """取已铺的最后几行当上下文(接得住前情,又不会把提示词撑大)。"""
    got = [(n, have[n]) for n in sorted(have) if n < start and have[n]][-back:]
    return "\n".join(f"- 第{n}章:{t}" for n, t in got)


def build_prompt(root: Path | str, batch: dict) -> tuple[str, str]:
    """生成这一块的 system/user。要求:一行一章、只推进自己的节拍、不许提前收线。"""
    def _read(rel: str) -> str:
        f = Path(root) / rel
        return f.read_text(encoding="utf-8").strip() if f.is_file() else ""

    outline = _read(paths.OVERVIEW_REL)[:4000] or "(全书大纲还没写)"
    vol = ""
    for v in volume_ranges(root):
        if v["卷"] == batch["卷"] and v["起"]:
            vol = f"{v['卷']}:第{v['起']}-{v['止']}章"
            break
    system = ("你是长篇网文的章纲规划师。只输出「- 第N章:……」这样的行,"
              "一行一章,不写解释、不写小标题、不写总结。")
    user = (
        f"【全书大纲】\n{outline}\n\n"
        f"【本卷】{vol}(本块要铺 {batch['起']}–{batch['止']} 章,共 {batch['章数']} 章)\n\n"
        f"【已铺到的最后几章】\n{batch['前情'] or '(还没有,这块是开头)'}\n\n"
        f"请只输出第 {batch['起']} 到第 {batch['止']} 章的章纲,每章一整行,格式严格是:\n"
        f"- 第{batch['起']}章:这章完成什么(一个节拍)+ 章末钩子是什么\n\n"
        "硬要求:\n"
        "1) 一行一章,章号从起至止连续不断、不重复、不跳号;\n"
        "2) 每章**只推进一个节拍**,写清这章结束时「局面比上一章多走了哪一步」;\n"
        "3) **主线级冲突只许推、不许收**——不许把主线的核心问题在这一块里解决掉;\n"
        "4) 每 8–12 章允许一个小高潮;本卷的大高潮与卷末钩子**留到本卷最后几章**,本块不要提前放;\n"
        "5) 章末钩子指向下一章,不要写成「至此某某了结」这种收束句。"
    )
    return system, user


def apply_batch(root: Path | str, body: str, lo: int | None = None,
                hi: int | None = None) -> dict:
    """把模型这一块的输出落进卡章纲(只填空行/追加缺行,人写行不覆盖)。"""
    from .journey import _apply_card_lines

    # 形状校验:只允许「- 第 N 章:一句话」这一种行。模型爱在前后写「好的,这一块如下:」
    # 「以上为第 51-100 章」这类旁白,原样落进去就是**永久写进人写的卡章纲**(交叉审查 P1-3)。
    keep, dropped, out_of_range = [], [], []
    for line in (body or "").splitlines():
        t = line.strip()
        if not t:
            continue
        if _CARD.match(t):
            m = _CARD.match(t)
            try:
                n = int(m.group(1) if m.groups() else 0)
            except (TypeError, ValueError):
                n = 0
            # 越界章号不许落:模型常顺手把下一块的章也写了,落进去就污染别的卷的章纲,
            # 而且「这一块一个字没落」的刹车会因此永远不触发,一路烧到批次上限。
            if lo is not None and hi is not None and not (lo <= n <= hi):
                out_of_range.append(t)
                continue
            keep.append(t)
        else:
            dropped.append(t)
    body_clean = "\n".join(keep)
    before = {n: t for n, t in card_state(root).items() if t}
    landed = _apply_card_lines(Path(root), body_clean)
    after = {n: t for n, t in card_state(root).items() if t}
    # 到底铺上了几章?按「从空变有内容」的章数算 —— 只数行数会把「填进已有的空行」算成 0,
    # 界面就会把成功报成没动(交叉审查 P0-1 的连带)。
    added = len(set(after) - set(before))
    note = landed if added else "模型这轮没给出可用的章行(可能只回了旁白),卡章纲一个字没动。"
    if dropped:
        note += f"另丢掉 {len(dropped)} 行不成形的输出(不是「- 第N章:…」);头一行是:{dropped[0][:40]}"
    if out_of_range:
        note += f";另拒收 {len(out_of_range)} 行章号不在本块区间的(第一行:{out_of_range[0][:40]})"
    return {"ok": True, "落了几行": added, "已落盘": added > 0, "丢了几行": len(dropped),
            "丢了样例": dropped[:3], "越界几行": len(out_of_range),
            "越界样例": out_of_range[:3], "说明": note}


def fill_volume(root: Path | str, backend, progress=None, size: int = BATCH,
                max_batches: int = 24, should_stop=None) -> dict:
    """把**当前这一卷**剩下的章纲一路铺到卷末。

    这是给**领航员(agent)**用的工具:作者 2026-10-18 裁定「人手动一次只能 50 章,
    agent 可以一次铺满」。人机界面仍走一次一块,免得手滑连烧一串调用。

    三道刹车,任何一道都停:①本卷铺满 ②这一块一个字没落(模型在说旁白/重复)
    ③跑到 max_batches。停因写进返回值,绝不"看起来成功"。
    """
    root = Path(root)
    from . import events, paths as _p3

    _bulk_backup(root / _p3.CARD_REL)     # 铺一整卷最多上千行,先留底
    done_batches, done_chapters, notes = 0, 0, []
    stop_reason = ""
    for _ in range(max_batches):
        if should_stop is not None and should_stop():
            stop_reason = "被人叫停了"
            break
        nb = next_batch(root, size)
        if not nb.get("ok"):
            stop_reason = nb.get("reason") or nb.get("说明") or "没有可铺的章了"
            break
        total, left = nb.get("本卷总章数"), nb.get("本卷还缺")
        if total is not None and left == 0:
            stop_reason = "本卷已经铺满了"
            break
        sys_prompt, usr = build_prompt(root, nb)
        out = backend.complete(sys_prompt, usr, max_chars=max(2000, nb["章数"] * 120))
        landed = apply_batch(root, out or "", lo=int(nb["起"]), hi=int(nb["止"]))
        if not landed.get("已落盘"):
            stop_reason = f"第 {nb['起']}-{nb['止']} 章这一块一个字没落,停下(模型可能只回了旁白)"
            notes.append(landed.get("说明", ""))
            break
        done_batches += 1
        done_chapters += int(landed.get("落了几行") or 0)
        _lo, _hi = int(nb["起"]), int(nb["止"])     # 本块铺的是这几章
        try:                    # 每一块都是一串真金白银的调用,必须进运行账本
            from . import runlog as _rl
            # chars_out 在账本里是「输出字数」——落了几行不是字数,别往里塞假数。
            _rl.record(root, _lo, role="铺章纲", ok=True, ms=0, chars_out=0)
        except Exception as e:  # noqa: BLE001 - 记账失败不影响铺章纲,但要留痕
            print(f"[运行账本] 铺章纲记不上:{type(e).__name__}: {e}", file=sys.stderr)
        mark_pending(root, "章纲", list(range(_lo, _hi + 1)), note="领航员铺的章纲,等人过目")
        if progress is not None:
            try:
                progress(events.info(f"已铺 {done_batches} 批 / {done_chapters} 章(本卷还缺 {left} 章)"))
            except Exception:      # noqa: BLE001 - 进度报不出去不影响铺章纲
                pass
    else:
        stop_reason = f"到了 {max_batches} 批的上限,先停下让人看一眼"
    return {"ok": True, "铺了几批": done_batches, "铺了几章": done_chapters,
            "停下原因": stop_reason, "备注": notes}


PENDING_REL = "外置大脑/.待确认.json"


def _pending_path(root) -> Path:
    return Path(root) / PENDING_REL


def pending(root) -> dict:
    """还没被人确认过的 AI 产物。空 = 没有待确认的。"""
    p = _pending_path(root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def mark_pending(root, kind: str, chapters=None, note: str = "") -> dict:
    """记下「这些是 AI 写的、还没人确认」。

    为什么不在章纲行里加标记:那行会被原样读进提示词,加「(待确认)」会被当成剧情内容。
    所以标记单独存一份(外置大脑/.待确认.json),章纲行本身保持干净。
    """
    import time as _t

    cur = pending(root)
    box = cur.setdefault(kind, {})
    have = set(box.get("章") or [])
    if chapters:
        have |= {int(n) for n in chapters}
    box["章"] = sorted(have)
    box["时间"] = _t.strftime("%Y-%m-%d %H:%M:%S")
    if note:
        box["说明"] = note
    try:
        _pending_path(root).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_pending_path(root), json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        print(f"[待确认] 记不上:{e}", file=sys.stderr)
    return cur


def confirm_pending(root, kind: str | None = None) -> dict:
    """人看过了、认可了:清掉标记(不传 kind 清全部)。"""
    cur = pending(root)
    if kind:
        cur.pop(kind, None)
    else:
        cur = {}
    p2 = _pending_path(root)
    try:
        if cur:
            atomic_write_text(p2, json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
        elif p2.exists():
            p2.unlink()
    except OSError as e:
        print(f"[待确认] 清不掉:{e}", file=sys.stderr)
    return cur


def _rec(root, chapter: int, role: str, ok: bool, t0: float, note: str = "") -> None:
    """账本一行。**记账失败不影响主流程,但必须留痕**(不裸吞)。"""
    import time as _t3

    try:
        from . import runlog as _rl
        _rl.record(root, int(chapter or 0), role=role, ok=ok,
                   ms=int((_t3.monotonic() - t0) * 1000), error="" if ok else note)
    except Exception as e:      # noqa: BLE001
        print(f"[运行账本] {role} 记不上:{type(e).__name__}: {e}", file=sys.stderr)


def _bulk_backup(path: Path) -> Path | None:
    """批量改动前先复制一份到同目录 `.批量备份/`。

    事后被模型污染了,人还能把原来那份捞回来 —— 手改一千行的代价远大于一次复制。
    """
    import shutil
    import time as _time

    p = Path(path)
    if not p.is_file():
        return None
    try:
        dst_dir = p.parent / ".批量备份"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{p.stem}.{_time.strftime('%Y%m%d-%H%M%S')}{p.suffix}"
        shutil.copy2(p, dst)
        return dst
    except OSError:
        return None


def rebuild_from_chapters(root: Path | str, chunks: list[dict], backend, progress=None,
                          mode: str = "全部", recent: int = 30, step: int = 10,
                          max_chapters: int = 200, should_stop=None) -> dict:
    """拿一份「写到一半的书」的章节正文,逐章抽出一句话梗概,填进卡章纲(只填空行)。

    **这一步是花钱的那一步**:每抽一章 = 一次模型调用。所以:
    - `mode="抽样"`:每 `step` 章抽 1 章(默认 10)。给人一个便宜的先看骨架的办法。
    - `mode="只抽最近"`:只抽最后 `recent` 章(想续写、不想整理整本的人用这个)。
    - `mode="全部"`:每章都抽。

    **断点续抽**:已经有章纲行的章直接跳过 —— 中断了再点一次,不会重抽已经抽过的。

    **抽不出就留空**:模型回「未抽出」或回不成形的行,就记进「未抽出」清单,绝不编。
    """
    root = Path(root)
    from . import events, paths as _p

    _bulk_backup(root / _p.CARD_REL)      # 这次要往卡章纲里写几十/几百行,先留底
    have = {n for n, t in card_state(root).items() if t}
    todo = [c for c in chunks if int(c.get("号") or 0) and int(c["号"]) not in have]
    total_available = len(todo)
    if mode == "只抽最近":
        todo = todo[-max(1, recent):]
    elif mode == "抽样":
        todo = [c for i, c in enumerate(todo) if i % max(1, step) == 0]

    done, calls, failed = 0, 0, []
    landed = []          # 真的落进去的章号;不能用 todo[:done] 冒充
    capped = False
    cancelled = False
    for c in todo:
        if max_chapters and calls >= max_chapters:
            capped = True
            break
        if should_stop is not None and should_stop():
            cancelled = True
            break
        n = int(c["号"])
        body = (c.get("正文") or "")[:6000]
        sys_p = ("你是小说编辑。读下面这一章,只回一行,格式严格是 `- 第N章:一句话梗概`。\n"
                 "要求:①不超过 60 字;②只写这一章发生了啥,别评价好坏;③读不懂就回 "
                 "`- 第N章:(未抽出)`,**绝对不许编**。")
        usr = f"第{n}章 原文:\n{body}\n\n只回那一行。"
        calls += 1
        import time as _t2
        _t0 = _t2.monotonic()
        try:
            out = backend.complete(sys_p, usr, max_chars=200) or ""
        except Exception as e:      # noqa: BLE001 - 单章抽失败不该毁掉整次任务
            _rec(root, n, "逆推章纲", False, _t0, str(e)[:120])
            failed.append({"号": n, "原因": f"{type(e).__name__}: {e}"})
            continue
        line = ""
        for raw in out.splitlines():
            m = _CARD.match(raw.strip())
            if m:
                line = raw.strip()
                break
        if not line:
            failed.append({"号": n, "原因": "模型没给成形的章行"})
            continue
        if "(未抽出)" in line or "(未抽出)" in line.replace("（", "(").replace("）", ")"):
            failed.append({"号": n, "原因": "模型说读不懂"})
            continue
        _apply_card_line_from_text(root, line)
        _rec(root, n, "逆推章纲", True, _t0, "")
        landed.append(n)
        done += 1
        if progress is not None:
            try:
                progress(events.info(f"已抽 {done}/{len(todo)} 章(第 {n} 章)"))
            except Exception:      # noqa: BLE001 - 进度报不出去不影响抽章纲
                pass
    if landed:
        # 以前写的是 todo[:done]:done 是「成功几章」,todo[:done] 是「前几章」——
        # 中间有章失败时,这两者不是一回事:没抽出的被标成待确认,真抽出的反倒漏标。
        mark_pending(root, "章纲", landed,
                     note="从半截稿逆推的章纲,等人过目")
    stopped = ("到了单次上限,先停下让你看看" if capped
               else "你按了停" if cancelled else "")
    return {"ok": True, "要抽的章数": len(todo), "本来还缺": total_available,
            "抽了几章": done, "调用次数": calls, "未抽出": failed[:20],
            "未抽出章数": len(failed), "模式": mode,
            "停下原因": stopped, "还欠几章": max(0, len(todo) - done)}


def _apply_card_line_from_text(root: Path, line: str) -> None:
    """把一行 `- 第N章:…` 落进卡章纲(复用 journey 的「只填空行/不覆盖人写」语义)。"""
    from .journey import _apply_card_lines

    _apply_card_lines(Path(root), line)


def rebuild_volumes(root: Path | str, backend, limit: int = 8, progress=None) -> dict:
    """把卡章纲里已经抽好的章行,归纳成每一卷的「卷名」「该卷主线」。

    **只填空行**:某一卷已经有卷名或主线(人写的/上次填的),这一卷直接跳过 —— 人写的东西
    绝不被模型顶掉(Loom 铁律)。认不出起止的卷也跳过,并说清为什么。

    一部 200 万字的书按 50 章一卷是 40 卷,`limit` 兜住单次的花费上限(默认 8 卷)。
    """
    from .fsutil import atomic_write_text
    from . import events, paths as _paths

    root = Path(root)
    vpath = root / _paths.VOLUMES_REL
    if not vpath.is_file():
        return {"ok": False, "说明": "这本书还没有卷纲"}
    text = vpath.read_text(encoding="utf-8")
    cards = card_state(root)
    done, skipped, calls = [], [], 0
    for v in volume_ranges(root):
        name, a, b = v["卷"], v["起"], v["止"]
        if not a or not b:
            skipped.append({"卷": name, "原因": "没写起止,不知道这一卷管哪几章"})
            continue
        if len(done) >= limit:
            skipped.append({"卷": name, "原因": f"这次只做 {limit} 卷,剩下的下次再来"})
            continue
        missing = _volume_missing_fields(text, name)
        if not missing:
            skipped.append({"卷": name, "原因": "这一卷的卷名和主线都有人写过了,不动它"})
            continue
        lines = [f"第{n}章:{cards[n]}" for n in range(a, min(b, max(cards or [a])) + 1) if cards.get(n)]
        if not lines:
            skipped.append({"卷": name, "原因": "这一卷的章纲还没抽出来"})
            continue
        sys_p = ("你是小说编辑。下面是某一卷里每章的一句话梗概。只回两行,格式严格:\n"
                 "卷名:不超过 8 字\n该卷主线:不超过 80 字,写主角从哪儿走到哪儿\n"
                 "读不出就回 `卷名:(未抽出)` —— **绝对不许编**。")
        usr = f"{name}(第{a}-{b}章)的章梗概:\n" + "\n".join(lines[:120])
        calls += 1
        try:
            out = backend.complete(sys_p, usr, max_chars=300) or ""
        except Exception as e:      # noqa: BLE001 - 单卷失败不该毁掉整次
            skipped.append({"卷": name, "原因": f"模型调用失败:{type(e).__name__}"})
            continue
        fields = _parse_volume_fields(out)
        if not fields:
            skipped.append({"卷": name, "原因": "模型没按格式回"})
            continue
        _usable = {k: v for k, v in fields.items() if k in missing}
        if not _usable:
            # 模型只回了「已经有人写过的字段」:一个字都没改,不许报成「归纳了 1 卷」。
            skipped.append({"卷": name, "原因": "模型只回了已填的字段,没动文件"})
            continue
        _before = text
        text = _fill_volume_fields(text, name, _usable)
        if text == _before:
            skipped.append({"卷": name, "原因": "写完文件没变(字段其实没填上)"})
            continue
        done.append({"卷": name, **{k: v for k, v in _usable.items()}})
        if progress is not None:
            try:
                progress(events.info(f"已归纳 {len(done)} 卷:{name}"))
            except Exception:       # noqa: BLE001
                pass
    if done:
        _bulk_backup(vpath)
        atomic_write_text(vpath, text)
    return {"ok": True, "归纳了几卷": len(done), "归纳": done,
            "跳过": skipped[:20], "跳过卷数": len(skipped), "调用次数": calls}


def _volume_block(text: str, name: str) -> tuple[int, int] | None:
    """返回 `## {name}` 这一块的 [起, 止) 行号区间;找不到返回 None。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## ") and name in line:
            start = i
            continue
        if start is not None and line.strip().startswith("## "):
            return start, i
    if start is not None:
        return start, len(lines)
    return None


def _volume_missing_fields(text: str, name: str) -> list[str]:
    """这一卷里**还空着的**字段(卷名 / 该卷主线)。

    按字段算,不按卷算:人写了卷名、主线还空着,那就只补主线 —— 补空不等于覆盖,
    但整卷跳过会让「人只填了一半」的卷永远填不上。
    """
    rng = _volume_block(text, name)
    if rng is None:
        return []
    filled = set()
    for line in text.splitlines()[rng[0]:rng[1]]:
        t = line.strip()
        for k in ("卷名", "该卷主线"):
            if re.match(rf"^- {k}[:：]", t) and re.split("[:：]", t, 1)[1].strip():
                filled.add(k)
    return [k for k in ("卷名", "该卷主线") if k not in filled]


def _parse_volume_fields(out: str) -> dict:
    got = {}
    for line in (out or "").splitlines():
        t = line.strip().lstrip("-").strip()
        for k in ("卷名", "该卷主线"):
            for sep in (":", "："):
                if t.startswith(k + sep):
                    v = t[len(k) + 1:].strip()
                    if v and "(未抽出)" not in v and "（未抽出）" not in v:
                        got[k] = v
    return got


def _fill_volume_fields(text: str, name: str, fields: dict) -> str:
    """只替换**空着的**字段行;有内容的一律原样保留。"""
    rng = _volume_block(text, name)
    if rng is None:
        return text
    lines = text.splitlines()
    for i in range(rng[0], rng[1]):
        t = lines[i].strip()
        for k, v in fields.items():
            if re.match(rf"^- {k}[:：]", t) and not re.split("[:：]", t, 1)[1].strip():
                lines[i] = f"- {k}:{v}"
    out = "\n".join(lines)
    return out + "\n" if text.endswith("\n") else out


def rebuild_overview(root: Path | str, backend, progress=None) -> dict:
    """把各卷的「卷名 + 该卷主线」合成一份**全书大纲的草稿**。

    落在《全书大纲.md》末尾一个**独立小节**里,不动人写的任何一行:
    小节标题带「AI 归纳,待你确认」,人确认前它只是草稿,不冒充 canon。
    一条卷主线都没有时干脆不调模型(不花钱)。
    """
    from .fsutil import atomic_write_text
    from . import events, paths as _paths

    root = Path(root)
    vpath = root / _paths.VOLUMES_REL
    opath = root / _paths.OVERVIEW_REL
    if not vpath.is_file():
        return {"ok": False, "说明": "这本书还没有卷纲"}
    text = vpath.read_text(encoding="utf-8")
    vols = []
    for v in volume_ranges(root):
        name = v["卷"]
        rng = _volume_block(text, name)
        if rng is None:
            continue
        got = {}
        for line in text.splitlines()[rng[0]:rng[1]]:
            t = line.strip()
            for k in ("卷名", "该卷主线"):
                if re.match(rf"^- {k}[:：]", t):
                    val = re.split("[:：]", t, 1)[1].strip()
                    if val:
                        got[k] = val
        if got.get("该卷主线") or got.get("卷名"):
            vols.append({"卷": name, "起": v["起"], "止": v["止"], **got})
    if not vols:
        return {"ok": True, "已写入": False,
                "说明": "一卷都没填「卷名/该卷主线」,先归纳卷纲再来", "调用次数": 0}
    lines = []
    for v in vols:
        span = f"第{v['起']}-{v['止']}章" if v.get("起") and v.get("止") else "起止未定"
        lines.append(f"{v['卷']}({span}) 卷名:{v.get('卷名', '')} 主线:{v.get('该卷主线', '')}")
    sys_p = ("你是小说编辑。下面是各卷的名字和主线。只回三行:\n"
             "一句话主线:不超过 40 字\n"
             "主角弧光:不超过 60 字\n"
             "全书节拍:不超过 80 字,写清哪几卷是小高潮、哪一卷是大高潮\n"
             "读不出就回 `一句话主线:(未抽出)` —— 不许编。")
    try:
        out = backend.complete(sys_p, "\n".join(lines), max_chars=400) or ""
    except Exception as e:      # noqa: BLE001 - 归纳失败不该毁掉书
        return {"ok": False, "说明": f"模型调用失败:{type(e).__name__}: {e}", "调用次数": 1}
    body = "\n".join(l.strip() for l in out.splitlines() if l.strip())
    if not body or "未抽出" in body:
        # ok 只表示「这一趟跑完没出错」;有没有真写进文件,由 已写入 说(账本信这个)
        return {"ok": True, "已写入": False,
                "说明": "模型没读出成形的全书大纲,没往文件里写", "调用次数": 1}
    block = ("\n\n## 全书主线(AI 归纳,待你确认)\n\n"
             "> 这一节是由各卷的卷名和主线自动合成的**草稿**。确认/改写前它不算 canon。\n\n"
             + body + "\n")
    old = opath.read_text(encoding="utf-8") if opath.is_file() else "# 全书大纲\n"
    head = "## 全书主线(AI 归纳,待你确认)"
    _bulk_backup(opath)
    # 只替换**这一节本身**:以前用 split(head)[0] 截断,会把「AI 小节之后」人写的整段删掉。
    _hit = re.search(rf"^{re.escape(head)}.*?(?=^## |\Z)", old, re.S | re.M)
    if _hit:
        new_text = old[:_hit.start()] + block.rstrip("\n") + "\n\n" + old[_hit.end():]
    else:
        new_text = old.rstrip("\n") + "\n\n" + block
    _bulk_backup(opath)
    atomic_write_text(opath, new_text if new_text.endswith("\n") else new_text + "\n")
    return {"已写入": True, "ok": True, "归纳了几卷": len(vols), "调用次数": 1, "小节": head}


def folding_warnings(root: Path | str) -> dict:
    """折卷会不会静默失效 —— 长书最容易踩的坑。

    `budget.volumes()` 要求每一卷**填了「卷名」或「该卷主线」**才认(否则模板自带的起止会被
    当成「已经分好卷」)。只填起止、不填卷名 ⇒ 引擎当这本书没分卷 ⇒ 卡章纲整份原样进 prompt
    (实测 1200 章约 3.0 万字,折了只要 2.3 千字)。**不报警,人就永远不知道自己在烧上下文。**
    """
    from .budget import volumes as _budget_volumes

    if _budget_volumes(root):
        return {"分卷生效": True, "警告": []}
    spans = [v for v in volume_ranges(root) if v["起"] and v["止"]]
    if not spans:
        return {"分卷生效": False, "警告": []}
    return {"分卷生效": False, "警告": [
        "卷纲里写了「起止」,但每一卷的「卷名」和「该卷主线」都空着 —— 引擎认为这本书没分卷,"
        "卡章纲会整份原样喂进模型(1200 章约 3 万字)。随便填一卷的卷名或该卷主线即可生效。"]}


def coverage(root: Path | str) -> dict:
    """铺到哪了:每卷区间内还剩多少章没章纲行。纯只读。"""
    vols = volume_ranges(root)
    have = card_state(root)
    rows = []
    for v in vols:
        if not (v["起"] and v["止"]):
            rows.append({"卷": v["卷"], "问题": v.get("问题") or "没写起止"})
            continue
        total = v["止"] - v["起"] + 1
        done = sum(1 for n in range(v["起"], v["止"] + 1) if have.get(n))
        rows.append({"卷": v["卷"], "起": v["起"], "止": v["止"],
                     "章数": total, "已铺": done, "未铺": total - done})
    return {"ok": True, "各卷": rows, "折卷": folding_warnings(root), "落盘": False}
