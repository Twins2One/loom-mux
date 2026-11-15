"""引擎用例宿主:cli 与 server 共用的编排单点 + 每本书的写锁。

双入口各自编排引擎调用已经漂移过一次(learn 旧指纹基线 server 用 neutral_default()、
cli 用 "",同一缺陷只修了一边)。此后「先查什么、再调什么、报告怎么拼」只在这里写一次,
入口退化成参数解析 + 渲染(HTTP JSON / Rich)。

写锁:per-root 非阻塞,拿不到抛 ProjectBusyError(server 统一映射 409 project_busy)。
覆盖全部「跑模型/落盘」用例;【红线】PUT /api/file(外置大脑保存)豁免不锁——
整章生成要几分钟,期间作者必须能存世界观。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
import sys
import time
from typing import Callable, Iterator

from . import chapters as chap
from . import journey as journey_mod
from . import ledger, partner_store, paths, slots as slots_mod
from .agents import regen_outline as _regen_outline
from .agents import run_pipeline
from .backends import PROVIDERS, Backend, cheap_backend, get_backend, provider_catalog
from .chaptertext import body_changed, parse_title, strip_title
from .config import key_available, key_is_set, load_config, openai_compat_key_is_set
from .doctor import AGENT_FILES, BRAIN_FILES, OPTIONAL_BRAIN
from .draft import brain_ready, draft_brain as _draft_brain
from .enrich import extract_supplement
from .fingerprint import changed_rules
from .fingerprint import learn as fp_learn
from .fingerprint import (neutral_default, revert_learn, seed_from_inherit,
                          seed_from_reference, seed_from_samples)
from .fsutil import restore_history
from .rewrite import apply_rewrite
from .state import load_state
from .writelock import HeldLock

Progress = Callable[[dict], None]

def lock_held(root: Path | str) -> bool:
    """这本书的写锁现在被占着吗（编织进行中）。前端切写作模式前查它弹警告。"""
    try:
        lk = acquire_lock(Path(root))
        lk.release()
        return False
    except ProjectBusyError:
        return True
    except Exception:
        return False


def _noop(event: dict) -> None:
    pass


# ---------------------------------------------------------------- 写锁
BUSY_MESSAGE = "本书正在写作中,等这一次跑完再操作。"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class ProjectBusyError(RuntimeError):
    """写锁被占(同一本书正有跑模型/落盘任务)。刻意不继承 LoomBackendError/ValueError:
    入口的 400 网不该兜住它,busy 永远走 409。code 默认 project_busy(写锁);
    伙伴轮锁(见下)复用本类,传 code="partner_busy" 让前端区分两种「忙」。"""

    code = "project_busy"

    def __init__(self, message: str = BUSY_MESSAGE, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def is_busy(root: Path | str) -> bool:
    """这本书**此刻**有没有任务在跑。

    ⚠️ 不能用「锁文件存不存在」判:`writelock` 释放时只关句柄、不删文件,
    于是跑过一次任务之后那个文件永远在,任何书都会被误判成「一直在忙」。
    这里真的去抢一次锁:抢到说明没人用(立刻放掉),抢不到才叫忙。
    """
    held = try_lock(root)
    if held is None:
        return True
    try:
        held.release()
    except Exception as e:      # noqa: BLE001 - 放不掉要留痕,但别把保存堵死
        print(f"[写锁] 探测后释放失败:{type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return False


def try_lock(root: Path | str):
    """非阻塞拿这本书的写锁;拿不到返回 None。

    **两层**:进程内 threading.Lock(挡同进程并发)+ OS 文件锁(挡别的进程)。
    作者的实际局面是打包版 / CLI / 开发实例可能同时开着同一本书,只有前者时
    两边的写任务会一起往 .原稿/.历史/账本里写,快照互相覆盖。
    """
    with _locks_guard:
        lock = _locks.setdefault(str(Path(root).resolve()), threading.Lock())
    if not lock.acquire(blocking=False):
        return None
    from .writelock import FileLock

    fl = FileLock(lock_path_for(root, "write"))
    if not fl.acquire():
        lock.release()          # 别的进程正拿着:把进程内这把也退回去,别自己攒着
        return None
    return HeldLock(lock, fl)


def lock_path_for(root: Path | str, kind: str) -> Path:
    """锁文件路径(转发到 writelock,放这里是为了让 try_lock 少一个 import 环)。"""
    from .writelock import lock_path

    return lock_path(root, kind)


def acquire_lock(root: Path | str):
    """拿锁或抛 ProjectBusyError。给 write 流式场景:锁随 worker 线程持有,调用方自行 release。"""
    lock = try_lock(root)
    if lock is None:
        raise ProjectBusyError()
    return lock


def set_book_writing_mode(root: Path | str, mode: str) -> None:
    """书级写作模式覆盖：写进这本书的 loom.toml（writing_mode 字段）。

    全局默认在 ~/.loom/prefs.json（userconf），书没覆盖时跟随全局。
    toml 手写保序不现实——用「正则替换单键」而不是整文件重写，作者手排的注释不丢。
    """
    import re as _re
    from .config import WRITING_MODES
    if mode not in WRITING_MODES:
        raise ValueError(f"写作模式只有 {'/'.join(WRITING_MODES)}。")
    p = _paths.project_root(root) / "loom.toml" if False else Path(root) / "loom.toml"
    if not p.is_file():
        # 没有 loom.toml 的书（罕见）：最小写入
        p.write_text(f"writing_mode = \"{mode}\"\n", encoding="utf-8")
        return
    t = p.read_text(encoding="utf-8")
    if _re.search(r"^writing_mode\s*=", t, flags=_re.M):
        t = _re.sub(r"^writing_mode\s*=.*$", f'writing_mode = "{mode}"', t, flags=_re.M)
    else:
        # 2026-11-02 修复：追加到**文件尾会掉进最后一个 [section]**（实测 [gate]），
        # toml 里就成了 gate.writing_mode，顶层键 load_config 读不到——新书第一次设置必中此坑。
        # 顶层键必须落在任何 [section] 头之前：插在首个节头前；没有节头才追加到尾。
        m = _re.search(r"^\[", t, flags=_re.M)
        if m:
            t = t[:m.start()] + f'writing_mode = "{mode}"\n\n' + t[m.start():]
        else:
            t = t.rstrip("\n") + f'\nwriting_mode = "{mode}"\n'
    from .fsutil import atomic_write_text as _aw
    _aw(p, t)
@contextmanager
def write_lock(root: Path | str) -> Iterator[None]:
    lock = acquire_lock(root)
    try:
        yield
    finally:
        lock.release()


# ---------------------------------------------------------------- 伙伴轮锁(独立于写锁)
# say 端点复刻 /api/write 的「锁随 worker 线程」模式,但用独立字典 _partner_locks:
# 防两次并发 say 交错写同一 .伙伴对话/当前.jsonl;绝不碰 _locks(写锁)——织章持写锁
# 跑几分钟期间,say 完全不受影响,书房伙伴照样能聊(spec §10 锁裁量红线)。
PARTNER_BUSY_MESSAGE = "书房伙伴这轮还在应答,等它说完再问下一句。"

_partner_locks: dict[str, threading.Lock] = {}
_partner_locks_guard = threading.Lock()


def try_partner_lock(root: Path | str):
    """非阻塞拿这本书的伙伴对话轮锁;拿不到返回 None。独立于 try_lock(写锁)。

    同样两层:两个窗口各开一个伙伴对话,会把同一个 当前.jsonl 交错写坏。
    """
    with _partner_locks_guard:
        lock = _partner_locks.setdefault(str(Path(root).resolve()), threading.Lock())
    if not lock.acquire(blocking=False):
        return None
    from .writelock import FileLock

    fl = FileLock(lock_path_for(root, "partner"))
    if not fl.acquire():
        lock.release()
        return None
    return HeldLock(lock, fl)


def acquire_partner_lock(root: Path | str) -> threading.Lock:
    """拿伙伴轮锁或抛 ProjectBusyError(code="partner_busy")。给 say 流式场景:锁随
    worker 线程持有,调用方自行 release。"""
    lock = try_partner_lock(root)
    if lock is None:
        raise ProjectBusyError(PARTNER_BUSY_MESSAGE, code="partner_busy")
    return lock


# ---------------------------------------------------------------- write
def _has_loom_chapter(root: Path) -> bool:
    """本书有没有任何 Loom 织出的章(判据:存在 .原稿 快照)。导入章无快照 → 视作未织。"""
    return any(paths.snapshot_path(root, n).exists() for n in paths.chapter_numbers(root))


def review_pass_ok(root: Path | str, chapter: int, step: str) -> bool:
    """T1 判据的唯一实现在 `agents.review_pass_ok`；这里只做入口侧的稳定转发。"""
    from .agents import review_pass_ok as _rp
    return _rp(Path(root), chapter, step)


def write_precheck(root: Path | str, chapter: int, force: bool = False,
                   from_step: str = '') -> dict | None:
    """写前检查:None=放行;否则 {"error","code",...}(措辞即 server 409 响应体)。
    起书完整性硬门禁在最前:仅当本书还没有 Loom 织的章时启用,force 不越它。"""
    root = Path(root)
    if not _has_loom_chapter(root):
        ok, missing = journey_mod.writing_unlocked(root)
        if not ok:
            names = "、".join(missing)
            return {"error": f"开书资料还差:{names}。在伙伴面板答几题即可解锁"
                             f"(或手填 外置大脑/ 对应文件)。",
                    "code": "brain_incomplete", "missing": missing, "stage": missing[0]}
    if from_step:
        # 「从第 N 步起重算」(R24)本来就是重跑**已写完**的章,不该再要人勾「覆盖」;
        # 但手改过正文(drifted)仍要拦:重算会覆盖人的手改。
        if ledger.chapter_drifted(root, chapter):
            return {'error': f'第 {chapter} 章正文与上次记录不符(手改过?)。先 learn,或勾选覆盖以你的正文为准。',
                    'code': 'chapter_drifted'}
        return None
    if force or not paths.chapter_path(root, chapter).exists():
        return None
    if ledger.chapter_drifted(root, chapter):
        return {"error": f"第 {chapter} 章正文与上次记录不符(手改过?)。先 learn,或勾选覆盖以你的正文为准重写。",
                "code": "chapter_drifted"}
    return {"error": f"第 {chapter} 章已写完。要重写请勾选覆盖。", "code": "chapter_exists"}


# ---------- 工序 → 模型槽( 1A) ----------
# 保留字面量调用:自检按「get_backend_for( 之后 200 字里有没有这个用途」核对消费方,
# 用一张字典映射会让那条核对看不见(它扫调用点,不扫数据)。
def _cfg_fingerprint(cfg) -> tuple:
    """配置指纹:供应商/默认模型/接口地址 + 八槽路由。配置一变,指纹就变(缓存自然作废)。"""
    routes = getattr(cfg, "routes", None) or {}
    bits = tuple(sorted((str(k), str((v or {}).get("provider", "")), str((v or {}).get("model", "")),
                         str((v or {}).get("base_url", ""))) for k, v in routes.items()))
    return (getattr(cfg, "provider", ""), getattr(cfg, "model", ""),
            getattr(cfg, "base_url", ""), getattr(cfg, "cheap_model", ""), bits)


def drop_backend_cache() -> None:
    """配置改了之后调用:把按槽造过的后端实例全丢掉(否则要重启才生效)。"""
    backend_for_role._cache = {}


def backend_for_role(root, cfg, role: str):
    """这一棒该用哪个槽的后端。

    :**细纲三棒挪到「大纲」格**。
    原来它们走 else 落到「写作」格,跟改稿/润色共用一格 —— 想给润色换个强模型,
    细纲的模型也跟着变;想单独给细纲指定模型则做不到。细纲是**结构活**,跟大纲师同类。
    """
    from . import routes
    cache = getattr(backend_for_role, "_cache", None)
    key = (str(root), role, _cfg_fingerprint(cfg))
    if cache is not None and key in cache:
        return cache[key]
    if role == "写手":
        bk = routes.get_backend_for(cfg, "写手")
    elif role in ("责任编辑", "编辑", "润色师"):
        bk = routes.get_backend_for(cfg, "写作")
    elif role in ("章节细纲师", "章节设定师", "章节分镜师", "设定师", "大纲师"):
        # 细纲三棒 + 作者智库的定设定/拆结构两个角色,都吃「大纲」这一格
        bk = routes.get_backend_for(cfg, "大纲")
    else:
        bk = routes.get_backend_for(cfg, "写作")
    if cache is None:
        backend_for_role._cache = cache = {}
    cache[key] = bk
    return bk


def write_chapter(root: Path | str, chapter: int, progress: Progress, *,
                  force: bool = False, slow: float = 0.25,
                  from_step: str = "",
                  stop_after: str = "",
                  should_stop: Callable[[], bool] | None = None) -> None:
    """跑 5 Agent 写第 N 章。不拿锁:流式场景锁随 worker 线程(调用方 acquire_lock / write_lock)。

    should_stop 是「作者叫停」(语义见 cancel.py):每两棒之间问一次,true 就停在当前棒之后。
    stop_after（2026-11-03 混合流）：手动跑单棒时显式传（如「责任编辑」）——跑完这一棒即停；
    优先级高于 writing_mode 映射（单棒运行不受书写模式牵制）。
    """
    root = Path(root)
    cfg = load_config(root)
    # out 不存在=上次没跑完(断点),resume 跳过已落盘且上游未变的工序,省计费
    # from_step 必须配 resume=True:作废记录 + 续跑才有意义;force 相反(全量重跑)。
    # R21:走「写作」这一槽 —— 原来直接 get_backend(cfg),导致设置里改「写作」槽完全不生效
    # (交叉审查 P0-3)。没配这一槽时 get_backend_for 内部就是「跟随默认」,行为不变。
    from . import routes   # 原来这行缺失 ⇒ 写章第一步就 NameError(裸 500),自检从没碰到这条真路

    # 2026-11-02：写作模式 → stop_after。书级 loom.toml 的 writing_mode
    # 优先；映射：FullAuto=空(跑满六棒)、辅助=章节设定师(细纲+设定校验,无分镜)。
    # 教学模式到不了这里（server.py /api/write 已 403 硬拦），双保险不算多余。
    # 2026-11-03（混合流）：显式 stop_after 参数（手动跑单棒）优先于一切映射。
    from .config import writing_mode_to_stop_after as _mode2stop
    _mode_stop = _mode2stop(getattr(cfg, "writing_mode", ""))
    if stop_after:
        cfg.stop_after = stop_after
    elif _mode_stop and not getattr(cfg, "stop_after", ""):
        cfg.stop_after = _mode_stop

    run_pipeline(root, chapter, routes.get_backend_for(cfg, "写作"), cfg, progress, slow=slow,
                 backend_for=lambda role: backend_for_role(root, cfg, role),
                 resume=bool(from_step) or not force,
                 from_step=(from_step or None),
                 critic_backend=cheap_backend(cfg), should_stop=should_stop)
    # 跑完就把「这一章的上游材料指纹」记成基线 + 标草稿:此后改章纲/卷纲/设定才会被识别成脏(R9)。
    # 记账失败不能影响出稿 —— 走 agents._swallow 留归因,不裸吞。
    if paths.chapter_path(root, chapter).is_file():
        from . import agents as _agents
        from . import chapterstate

        def _stamp():
            chapterstate.stamp(root, chapter)

        try:
            _stamp()
        except Exception as e:      # noqa: BLE001 - 归因后照旧
            _agents._swallow("章状态落盘", e, progress)


# ---------------------------------------------------------------- learn
@dataclass
class LearnReport:
    """learn 的完整回执:server 直接 JSON 化,cli 挑着渲染。"""

    fingerprint: str   # 新指纹全文
    card: str          # 卡章纲全文(前端自己抠 recap,响应兼容)
    changes: dict      # {"added": [...], "removed": [...]}
    recap: str         # 第 N 章 [AI回顾] 块(app.js extractRecap 的 Python 版)
    world_supp: str    # 世界观 [AI补充] 块
    chars_supp: str    # 人物卡 [AI补充] 块
    warn: str          # 疑似磨短指纹的软提示(可一键撤销)


def learn_chapter(root: Path | str, chapter: int, backend: Backend, *,
                  appraisal_backend: Backend | None = None,
                  progress: Progress = _noop) -> LearnReport:
    """learn 全编排:记旧基线 → 蒸馏 → 拼报告。旧基线统一 neutral_default
    (曾经 cli 用 "" 漂移:缺指纹文件时 changes 全量误报)。"""
    root = Path(root)
    fp_file = root / paths.FINGERPRINT_REL
    with write_lock(root):
        old_fp = fp_file.read_text(encoding="utf-8") if fp_file.exists() else neutral_default()
        evs: list[dict] = []

        def tee(ev: dict) -> None:   # 转发给入口渲染,同时留底捞 shrink_warning
            evs.append(ev)
            progress(ev)

        fp_learn(root, chapter, backend, tee, appraisal_backend=appraisal_backend)
        new_fp = fp_file.read_text(encoding="utf-8")
        card_p = root / paths.CARD_REL
        # 双形态:[AI补充] 老书在单文件文末,目录形态在各自的 成长档案.md
        from .enrich import _supp_target
        world_p, _, _ = _supp_target(root, paths.WORLD_REL, paths.WORLD_DIR_REL)
        chars_p, _, _ = _supp_target(root, paths.CHARS_REL, paths.CHARS_DIR_REL)
        done = next((e for e in evs if e.get("type") == "learn_done"), {})
        return LearnReport(
            fingerprint=new_fp,
            card=card_p.read_text(encoding="utf-8") if card_p.exists() else "",
            changes=changed_rules(old_fp, new_fp),
            recap=_recap_block(root, chapter),
            world_supp=extract_supplement(world_p.read_text(encoding="utf-8"), chapter)
                       if world_p is not None and world_p.exists() else "",
            chars_supp=extract_supplement(chars_p.read_text(encoding="utf-8"), chapter)
                       if chars_p is not None and chars_p.exists() else "",
            warn=done.get("shrink_warning") or "",
        )


def _recap_block(root: Path, chapter: int) -> str:
    """卡章纲里第 N 章的 [AI回顾] 块(含标记、已去缩进):解析复用 studio.timeline。"""
    from .studio import timeline
    row = next((r for r in timeline(root) if r["n"] == chapter), None)
    return f"[AI回顾] {row['recap']}" if row and row.get("recap") else ""


def learn_revert(root: Path | str, chapter: int) -> Path | None:
    """撤销第 N 章最近一次 learn(锁内:防与写章竞态)。"""
    with write_lock(root):
        return revert_learn(Path(root), chapter)


def evolve_revert(root: Path | str, persona_name: str) -> Path | None:
    """撤销某人格最近一次「学改法」落盘(锁内:防与写章竞态)。

    终审④:`partner_confirm` 的「人格增补」分支 docstring 承诺 `evolve.confirm`
    「自带『历史/』快照供一键撤销」,但 `evolve.revert` 此前全仓没有任何生产调用点
    ——作者落盘后只能手工去编辑 `agents/<角色>.md`。这里补上唯一的生产入口(见
    server.py 的 `POST /api/evolve/revert`)。没有快照(没落过 / 已撤过)返回 None,
    是正常业务态,不是异常。
    """
    from . import evolve
    with write_lock(root):
        return evolve.revert(Path(root), persona_name)


# ---------------------------------------------------------------- seed
def seed_fingerprint(root: Path | str, *, text: str = "", inherit: Path | str | None = None,
                     reference: str = "", progress: Progress = _noop) -> Path:
    """seed 三源分发(优先级同 server 现状:参考范文 > 继承 > 样本);继承不建后端,免 key 可用。"""
    root = Path(root)
    with write_lock(root):
        if reference:
            return seed_from_reference(root, reference, get_backend(load_config(root)), progress)
        if inherit:
            return seed_from_inherit(root, Path(inherit).expanduser(), progress)
        return seed_from_samples(root, text or "", get_backend(load_config(root)), progress)


# ---------------------------------------------------------------- 其余落盘用例(锁内薄封装)
def advise_chapter(root: Path | str, chapter: int, *, progress: Progress = _noop) -> dict:
    """R14 建议:跑一次复审员,**只诊断、不落盘**(不需写锁)。

    走 R21 的「审稿」槽:槽里显式指定了模型就用它;若是「跟随默认」,沿用原有的便宜模型策略
    (`cheap_model` 配了就用它——审稿这类评估调用没必要花主模型的价)。
    """
    from . import advise, routes

    root = Path(root)
    cfg = load_config(root)
    backend = cheap_backend(cfg) if routes.resolve(cfg, "审稿")["跟随默认"] else None
    if backend is None:
        backend = routes.get_backend_for(cfg, "审稿")
    return advise.advise(root, chapter, backend, progress=progress)


def delete_chapter(root: Path | str, chapter: int, *, reason: str = "") -> dict:
    """R19 删章 = 软删(整章产物进回收站)。锁内做,先过护栏。"""
    from . import guardrail

    gate = guardrail.check("删章")
    if not gate["允许"]:
        return {"ok": False, "reason": gate.get("理由", "这个动作被禁止。")}
    with write_lock(Path(root)):
        return guardrail.soft_delete_chapter(Path(root), chapter, reason=reason)


def restore_trash(root: Path | str, name: str) -> dict:
    """从回收站一键还原(锁内;冲撞则整批拒绝,不覆盖)。"""
    from . import guardrail

    with write_lock(Path(root)):
        return guardrail.restore_chapter(Path(root), name)


def purge_trash(root: Path | str, token: str) -> dict:
    """清空回收站:不可还原,必须打字确认(护栏在 guardrail.purge_trash 里核)。"""
    from . import guardrail

    with write_lock(Path(root)):
        return guardrail.purge_trash(Path(root), token=token)


def export_book(root: Path | str, *, fmt: str = "txt", split: str = "书",
                out_dir: str = "") -> dict:
    """R18 导出(锁内;只写目标目录,不覆盖同名文件)。"""
    from . import export as export_mod

    with write_lock(Path(root)):
        return export_mod.export(Path(root), fmt=fmt, split=split, out_dir=out_dir)


def add_fact(root: Path | str, *, chapter: int, kind: str, subject: str,
             obj: str = "", value: str = "") -> dict:
    """R3/R4 手写一条事实(锁内)。三视图与知情矩阵立刻都能看到。"""
    from . import facts

    with write_lock(Path(root)):
        return facts.add(Path(root), chapter=chapter, kind=kind, subject=subject,
                         obj=obj, value=value)


def ingest_facts(root: Path | str) -> dict:
    """把卡章纲的伏笔灌进事实账本(锁内;只替换 来源=卡章纲 的那一批)。"""
    from . import facts

    with write_lock(Path(root)):
        return facts.ingest(Path(root))


def merge_preview(root: Path | str, chapter: int, mine: str | None, *, base_text: str = "") -> dict:
    """R10 三栏预览(纯读,不锁也不需要锁)。"""
    from . import merge

    return _merge_mod(merge).preview(Path(root), chapter, mine, base_text=base_text)


def merge_apply(root: Path | str, chapter: int, merged: str, *, expect_disk: str = "") -> dict:
    """R10 合入(锁内写;写前留快照;盘上漂移则拒绝)。"""
    from . import merge

    with write_lock(Path(root)):
        return merge.apply_merge(Path(root), chapter, merged, expect_disk=expect_disk)


def _merge_mod(mod):
    return mod


def arbitrate_conflicts(root: Path | str, conflicts: list) -> dict:
    """R5 材料冲突仲裁:设定 > 章纲 > 正文;有例外裁定后其余冲突逐处请示。**纯读。**"""
    from . import arbitrate

    return arbitrate.plan(Path(root), conflicts or [])


def record_override(root: Path | str, item: str, layer: str, *, reason: str = "") -> dict:
    """记一条例外裁定(锁内写 外置大脑/裁定记录.md)。**只记不执行**。"""
    from . import arbitrate

    with write_lock(Path(root)):
        return arbitrate.add_override(Path(root), item, layer, reason=reason)


def extract_facts(root: Path | str, chapter: int, *, purpose: str = "") -> dict:
    """Q10:让 AI 抽一版账本提案。**只给提案,一个字不写。**

    
outes 必须在这里 import:usecases 顶部没有导入它,原来这行一直是 NameError,
    于是「资料账本 → AI 抽提案」这个按钮**从来没跑通过一次**(点下去就是 500)。
    """
    from . import facts, routes

    cfg = load_config(Path(root))
    backend = routes.get_backend_for(cfg, "摘要")
    return facts.extract(root, chapter, backend)


def extract_conflicts(root: Path | str, chapter: int) -> dict:
    """R5 补:让 AI 读一遍正文与设定/章纲,给**冲突提案**。一个字不写,人核过才算数。

    确定性检测只能比「同一个键写了两个值」;正文是散文,矛盾要读懂句子才看得见 —— 那只有模型能做。
    所以这里只回提案,每条都带正文原句,查不到原句的就标出来。
    """
    from . import conflicts, paths as _paths, routes   # routes 在别处都是函数内 import,这里也要自己拿

    # 先看有没有正文:没人写过的章,不该为了「没有可读的东西」去构造 backend(缺 key 时那会直接抛错)
    p = _paths.chapter_path(Path(root), chapter)
    body = p.read_text(encoding="utf-8") if p.is_file() else ""
    if not body.strip():
        return {"ok": False, "落盘": False, "冲突": [],
                "reason": f"第 {chapter} 章没有正文,没有可读的东西。"}
    cfg = load_config(Path(root))
    backend = routes.get_backend_for(cfg, "摘要")
    return conflicts.extract(root, chapter, backend, body=body)


def accept_facts(root: Path | str, chapter: int, picks: list, proposal: list | None = None) -> dict:
    """把人点过的提案写进账本(证据必须在正文里)。锁内写。"""
    from . import facts

    with write_lock(Path(root)):
        return facts.accept(root, chapter, picks, proposal=proposal)


def set_scope_switch(root: Path | str, kind: str, on: bool) -> dict:
    """R20 开/关一个**全局共享项**(锁内写本书的共享开关表)。书内项没有开关。"""
    from . import scope

    with write_lock(Path(root)):
        return scope.set_enabled(Path(root), kind, on)


def set_route_slot(root: Path | str, purpose: str, *, provider: str = "", model: str = "",
                   base_url: str = "", tool_protocol: str = "", context_len: int = 0) -> dict:
    """R21 改一个模型槽:锁内写 loom.toml。留空 = 恢复「跟随默认」。

    `base_url` 是裁定⑦的「接第三方 / 本地模型系统」;`tool_protocol` 是 R25 的协议切换。
    两个字段此前只在 `routes.set_route()` 里有、端点没接——于是「能配」只是代码里的能力。
    """
    from . import routes

    root = Path(root)
    with write_lock(root):
        cfg = load_config(root)
        r = routes.set_route(cfg, purpose, provider=provider, model=model,
                             base_url=base_url, tool_protocol=tool_protocol,
                             context_len=context_len)
        if not r.get("ok"):
            return r
        new_cfg = routes.save_routes(root, cfg, r["routes"])
    return {"ok": True, "槽": routes.slots(new_cfg), "落盘": True}


def repair_apply(root: Path | str, chapter: int, picks: list) -> dict:
    """R17 一键 AI 修:锁内落地(与写章同一个写锁,避免和流水线撞车)。"""
    from . import repair

    with write_lock(Path(root)):
        return repair.apply(Path(root), chapter, picks)


def draft_brain(root: Path | str, idea: str) -> dict:
    """AI 起草外置大脑初稿(只覆盖空白/模板,不动人写的)。"""
    root = Path(root)
    with write_lock(root):
        return _draft_brain(root, idea, get_backend(load_config(root)))


def regen_outline(root: Path | str, chapter: int) -> str:
    """重新生成第 N 章细纲(设定师→大纲师),不碰正文。"""
    root = Path(root)
    with write_lock(root):
        cfg = load_config(root)
        from . import routes

        # 细纲是大纲师的产物 ⇒ 该走「大纲」槽(原来借了「写作」,那一槽现在只代表改稿)
        # 注意:这行必须在 with 里面 —— 原来写在锁外,跑模型那几分钟没人持锁(交叉审查 P1-2)。
        return _regen_outline(root, chapter, routes.get_backend_for(cfg, "大纲"), cfg,
                              backend_for=lambda role: backend_for_role(root, cfg, role))


def debug_chapter(root: Path | str, chapter: int) -> dict:
    """手动除虫第 N 章(老章节可跑,逐章补建账本)。锁内:写留痕+账本。走便宜模型(评估类)。"""
    root = Path(root)
    with write_lock(root):
        from .agents import _hardfacts_for
        from .continuity import scan_chapter
        p = paths.chapter_path(root, chapter)
        if not p.is_file():
            raise FileNotFoundError(f"第 {chapter} 章还没写,先写完再除虫。")
        cfg = load_config(root)
        backend = cheap_backend(cfg) or get_backend(cfg)
        body = strip_title(p.read_text(encoding="utf-8")).strip()
        return scan_chapter(root, chapter, body, backend, hardfacts=_hardfacts_for(root))


def rewrite_apply(root: Path | str, chapter: int, content: str, old_span: str, new_span: str) -> None:
    """应用局部重写:落盘正文 + 外科式同步 .原稿 快照(锁内:新增覆盖)。"""
    with write_lock(root):
        apply_rewrite(Path(root), chapter, content, old_span, new_span)

def chapter_delete(root: Path | str, n: int) -> dict:
    with write_lock(root):
        return chap.delete_chapter(root, n)


def chapter_insert(root: Path | str, n: int) -> dict:
    with write_lock(root):
        return chap.insert_after(root, n)


def chapter_move(root: Path | str, n: int, direction: str) -> dict:
    with write_lock(root):
        return chap.move_chapter(root, n, direction)


def history_restore(root: Path | str, rel: str, snap_id: str) -> str:
    """回滚单章历史版本(锁内:防与写章竞态;回滚前会先把当前版本也存一份)。"""
    with write_lock(root):
        return restore_history(root, rel, snap_id)


# ---------------------------------------------------------------- 项目状态
# 编辑器展示的 skills 清单(外置大脑可改、skills/agents 只读展示)
_SKILLS = ["世界观引擎.md", "故事引擎.md", "网文大神.md", "黄金开篇.md", "评估自检.md", "去AI味.md", "金手指.md"]


def _brain_entries(root: Path) -> list[dict]:
    """外置大脑侧栏清单（设定集结构，作者 2026-10-20 裁定 3）。

    新结构：00-立项 + 10-世界观/ + 20-人物/ + 30-战斗/ + 40-知识/ + 根目录单体文件。
    分区目录给 children 分组，其余单行。

    ⚠️ 旧书（只有 外置大脑/世界观/、人物/、立项卡.md）不在新结构里——按作者「不做迁移」
    的裁定，它们的条目会消失；doctor 会报缺，作者可选择手动补。
    """
    from .paths import (CHARS_REL, SET_BATTLE_DIR_REL, SET_CARD_REL, SET_CHARS_DIR_REL,
                        SET_CUSTOM_DIR_REL, SET_KNOW_DIR_REL, SET_WORLD_DIR_REL, WORLD_REL,
                        brain_dir_files)

    out: list[dict] = []
    # 00-立项
    if (root / SET_CARD_REL).is_file():
        out.append({"rel": SET_CARD_REL, "name": "立项"})

    # 设定集各分区：**双形态**,与 paths.brain_form 同一张嘴。
    #   目录形态（10-世界观/ 一节一文件）→ children 分组；
    #   单文件形态（10-世界观.md / 20-人物卡.md）→ 一行。
    # 2026-10-21 修:这里原来只认目录形态,结果单文件形态在整个产品里被
    # doctor / agents._hardfacts_for / draft / enrich / brainedit / studio 都当一等公民读,
    # 唯独侧栏里整个消失 —— 作者明明有世界观和人物卡,左栏却看不见。
    for label, dir_rel, file_rel, file_name in (
        ("世界观", SET_WORLD_DIR_REL, WORLD_REL, "世界观"),
        ("人物", SET_CHARS_DIR_REL, CHARS_REL, "人物卡"),
        ("战斗", SET_BATTLE_DIR_REL, None, "战斗"),
        ("知识", SET_KNOW_DIR_REL, None, "知识"),
        # 50-自定义:角色按作者想法长出来的新项,一格一文件(2026-10-23)。
        ("自定义", SET_CUSTOM_DIR_REL, None, "自定义"),
    ):
        if (root / dir_rel).is_dir():
            kids = [{"rel": f"{dir_rel}/{f.name}", "name": f.stem}
                    for f in brain_dir_files(root, dir_rel)]
            out.append({"name": label, "children": kids})
        elif file_rel and (root / file_rel).is_file():
            out.append({"rel": file_rel, "name": file_name})

    # 根目录的单体文件（大纲层 / 文风层 / 状态层）
    for n in ("卡章纲", "写作指纹", "全书大纲", "卷纲", "文风参考", "违禁词", "状态账本"):
        rel = paths.brain_rel(n)
        if (root / rel).is_file():
            out.append({"rel": rel, "name": n})

    # 分析产物（2026-10-20）：语料报告 / 情绪曲线。
    # 刻意不放进设定集——它们是「派生分析」不是「静态事实」，混进去会让事实源变脏。
    analysis_dir = root / paths.ANALYSIS_DIR_REL
    if analysis_dir.is_dir():
        kids = [{"rel": f"{paths.ANALYSIS_DIR_REL}/{f.name}", "name": f.stem}
                for f in sorted(analysis_dir.glob("*.md"))]
        if kids:
            out.append({"name": "分析", "children": kids})

    # ── 旧结构回退「侧栏设定集打不开」）──────────────────
    # 作者的书还没迁移（他说过不做迁移）。若设定集根本不存在，就按旧路径把已有文件列出来，
    # 免得侧栏一片空白。**只读、不改文件**；条目名字带「旧」字提醒作者这是过渡结构。
    if not (root / SET_CARD_REL).is_file():
        for label, dir_rel in (("世界观", paths.WORLD_DIR_REL), ("人物", paths.CHARS_DIR_REL)):
            legacy_dir = root / "外置大脑" / label
            if legacy_dir.is_dir():
                kids = [{"rel": f"外置大脑/{label}/{f.name}", "name": f.stem}
                        for f in sorted(legacy_dir.glob("*.md"))]
                out.append({"name": f"{label}(旧)", "children": kids})
        for n in ("世界观", "人物卡", "立项卡"):
            rel = paths.brain_rel(n)
            if (root / rel).is_file():
                out.append({"rel": rel, "name": f"{n}(旧)"})
    return out


def project_state(root: Path | str) -> dict:
    """项目全景(后端配置/章节/外置大脑清单):server 各端点的统一响应体。"""
    root = Path(root)
    cfg = load_config(root)
    st = load_state(root)
    chapters = paths.chapter_numbers(root)
    chs = []
    for n in chapters:
        out, snap = paths.chapter_path(root, n), paths.snapshot_path(root, n)
        out_text = out.read_text(encoding="utf-8")
        # 「改过」只看正文体(去掉标题再比):改标题不算手改、不该亮「改过」徽标(与 learn/drift 同口径)
        edited = snap.exists() and body_changed(out_text, snap.read_text(encoding="utf-8"))
        chs.append({"n": n, "title": parse_title(out_text), "written": True,
                    "edited": edited, "learned": n in set(st.get("learned", []))})
    _wu = journey_mod.writing_unlocked(root)
    return {
        "root": str(root),
        "title": cfg.title,
        "backend": {"provider": cfg.provider, "model": cfg.model, "base_url": cfg.base_url,
                    "chapter_chars": cfg.chapter_chars, "context_len": cfg.context_len,
                    "context_limit": cfg.context_len or 128000,
                    "context_len_source": "手填" if cfg.context_len else "默认",
                    "key_set": key_is_set(root),
                    "openai_compat_key_set": openai_compat_key_is_set(root),
                    # 每个供应商的 key 是否可用(本书 .env 或继承的用户级默认 .env;cli 类免 key 恒 True)
                    "keys_set": {pid: (spec["kind"] == "cli" or
                                       key_available(root, spec.get("key_env", "")))
                                 for pid, spec in PROVIDERS.items()},
                    "providers": provider_catalog()},
        "fingerprint_source": st.get("fingerprint_source", "default"),
        "brain_ready": brain_ready(root),   # 弱判据:铺过底(保留供旧前端,门禁判据用下面两项)
        "writing_unlocked": _wu[0] or _has_loom_chapter(root),
        "foundation_ready": _wu[0],  # Discussion has no existing-chapter exemption.
        "missing": _wu[1],
        "brain": _brain_entries(root),   # 双形态:单文件=一行;目录=分组(children 子文件)
        "skills": [{"rel": f"skills/{n}", "name": n[:-3]} for n in _SKILLS],
        "agents": [{"rel": f"agents/{n}.md", "name": n} for n in AGENT_FILES],
        "chapters": chs,
        "has_body": bool(chapters),   # 有正文章(诊断动作出现条件之一)
        "next_chapter": (chapters[-1] + 1) if chapters else 1,
    }


# ---- 创作旅程(伙伴面板;spec docs/superpowers/specs/2026-07-10-journey-partner-design.md) ----

def journey_state(root: Path | str) -> dict:
    """纯读派生视图,无锁(同 project_state)。"""
    return journey_mod.journey_state(Path(root))


def journey_card(root: Path | str) -> dict:
    """出下一张问题卡;评估类调用走 cheap_model(空则主模型)。"""
    root = Path(root)
    with write_lock(root):
        cfg = load_config(root)
        return journey_mod.next_card(root, cheap_backend(cfg) or get_backend(cfg))


def journey_answer(root: Path | str, answer: str) -> dict:
    """收作者答案:整形(必要时一次消化调用)→ 落外置大脑 → 清待答卡。"""
    root = Path(root)
    with write_lock(root):
        cfg = load_config(root)
        return journey_mod.land_answer(root, answer, cheap_backend(cfg) or get_backend(cfg))


def journey_goto(root: Path | str, stage: str, skip: bool = False) -> dict:
    root = Path(root)
    with write_lock(root):
        return journey_mod.goto(root, stage, skip=skip)


# ---- 整书诊断(导入旧书;spec T4 scan 半——候选不落盘,commit 在别处) ----

def diagnose_scan(root: Path | str) -> dict:
    """整书诊断:读采样正文出候选(不落盘)。评估类走 cheap_model。"""
    root = Path(root)
    from . import diagnose
    cfg = load_config(root)
    return diagnose.scan(root, cheap_backend(cfg) or get_backend(cfg))


def diagnose_commit(root: Path | str, picks: dict) -> dict:
    """整书诊断 commit 半:作者确认的候选落盘(不调 LLM,候选已整形)。"""
    root = Path(root)
    with write_lock(root):
        from . import diagnose
        return diagnose.commit(root, picks)


# ---- 书房伙伴(对话拍板;docs/superpowers/plans/2026-07-16-partner-p3-wire-and-migrate.md) ----
# 红线:对话里的「提设定」只产 proposal 事件(loom/partner_tools.py),不落盘;
# 唯一落盘出口是 partner_confirm。


def partner_history(root: Path | str, *, tail: int | None = None) -> dict:
    """纯读派生视图,无锁(同 journey_state/project_state)。"""
    return {"events": partner_store.read_events(Path(root), tail=tail)}


def _slot_preview(root: Path, slot_id: str) -> str | None:
    """现扫 stage_slots 取 slot_id 当前 preview;槽已不存在(改名/删除)返回 None。

    占位模板算「空」("" 非 None):与 partner_tools._handle_tishe 的 before 同口径(filled
    门)——否则占位落点的 before="" 会和这里的 current(占位preview)对不上,误判 stale。
    "" = 存在但无实质内容;None = 槽已消失,两者语义不同(guard 分别处理)。"""
    for spec in journey_mod.STAGES:
        found = next((s for s in slots_mod.stage_slots(root, spec) if s.id == slot_id), None)
        if found is not None:
            return found.preview if found.filled else ""
    return None


def partner_confirm(root: Path | str, pid: str, *, ts: str) -> dict:
    """拍板落盘:找 proposal → 按它的形状分流落盘 → 记一条 confirm 事件。

    候选卡有两种形状,落点/落法都不同:
    - 「提设定」(kind 缺省):slot/content 定址,走快照比对 + `_land_slot`(见下方主分支)。
    - 「人格增补」(终审①,evolve.propose 出的卡):角色/内容,没有 slot 概念,落点固定是
      `agents/<角色>.md` 的个人增补区,走 `evolve.confirm`(它自带「历史/」快照供一键撤销,
      与这里的 slot 快照守卫是两套不同机制,不重复做)。

    幂等(防双击/重发对 file 类落点二次追加):jsonl 里已有该 pid 的 confirm 事件,
    直接返已落盘结果,不重跑落盘。proposal 过期(find_proposal 返 None)或
    落点冲突/未知(落盘函数抛 ValueError,如 filename 撞车、槽位不存在)都不崩,
    返 {"error": ...}——两种情况都不追加 confirm 事件,原 proposal 仍可重试。

    快照守卫(收敛范围;原内部报告不在发行树内,口径以本段为准):proposal 产生时
    (partner_tools._handle_tishe)记落点当时的 preview 到 "before";这里落盘前重新扫一次
    现在的 preview,不一致就当这一格在提案挂起期间被作者手改过,拒绝落盘、不追加 confirm
    事件(拍板可重试——领航员会重新看一眼再提)。preview 只取前 24 字,能可靠检测 row/line/
    h2 这类「替换型」落点的改动(现实中的破坏性场景:作者手改覆盖了旧值,双方值都短);
    对 file 型「追加型」落点(如世界观小节正文),占位模板文案常年 ≥24 字、preview 早已
    饱和,快照测不出「作者又追加了别的」——这是已知的低危缺口,故意不堵:追加型双落盘
    最坏后果只是重复内容(非数据丢失),且要触发它需要「提案挂起期间、精确在扫描后落盘前
    这个窄窗口内」手改同一格,概率极低,不值得为它换更贵的整段内容快照。
    """
    root = Path(root)
    with write_lock(root):
        existing = next((e for e in partner_store.read_events(root)
                          if e.get("t") == "confirm" and e.get("id") == pid), None)
        if existing is not None:
            return {"landed": existing.get("landed"), "state": journey_mod.journey_state(root)}
        proposal = partner_store.find_proposal(root, pid)
        if proposal is None:
            return {"error": "提案已过期,重新问一次"}
        if proposal.get("kind") == "人格增补":
            # 终审①:evolve.propose 出的候选卡形状与「提设定」不同——没有 slot/content,
            # 只有 角色/内容;落点固定是 agents/<角色>.md 的个人增补区,走 evolve.confirm。
            role = proposal.get("角色")
            content = proposal.get("内容")
            if not role or not content:   # 同下方主分支:字段缺一律当过期,不许 KeyError 崩
                return {"error": "提案已过期,重新问一次"}
            from . import evolve
            landed_path = evolve.confirm(root, role, content)
            try:
                landed = str(landed_path.relative_to(root))
            except ValueError:   # 理论兜底:root 非绝对路径等边界情况,退化为原样字符串
                landed = str(landed_path)
            partner_store.append_event(root, {"t": "confirm", "id": pid, "ts": ts, "landed": landed})
            _link_decision_to_task(root, proposal, pid, accepted=True, ts=ts)
            return {"landed": landed, "state": journey_mod.journey_state(root)}
        if proposal.get("kind") == "写作指纹":
            # 2026-10-25:「改指纹」的候选卡。落点固定是 外置大脑/写作指纹.md 的某一小节,
            # 走 journey._land_fingerprint(只增不删 + guard_write 校验),不是槽位落盘。
            section = proposal.get("段")
            content = proposal.get("content")
            if not section or not content:
                return {"error": "提案已过期,重新问一次"}
            try:
                landed = journey_mod._land_fingerprint(root, str(section), str(content))
            except ValueError as e:
                return {"error": str(e)}
            partner_store.append_event(root, {"t": "confirm", "id": pid, "ts": ts, "landed": landed})
            _link_decision_to_task(root, proposal, pid, accepted=True, ts=ts)
            return {"landed": landed, "state": journey_mod.journey_state(root)}
        # .get() 取字段:proposal 损坏/缺字段(旧版本残留、手改 jsonl)一律当过期处理,
        # 不许 KeyError 崩(slot/content 是 _land_slot 的必需参数,缺一都没法落盘)。
        slot_id = proposal.get("slot")
        content = proposal.get("content")
        if not slot_id or not content:
            return {"error": "提案已过期,重新问一次"}
        before = proposal.get("before")   # 无此字段(旧 proposal)→ 向后兼容,跳过快照比对
        if before is not None:
            current = _slot_preview(root, slot_id)
            # current is None:槽已不存在(改名/删除),留给下面 _land_slot 报「未知槽位」,
            # 不在这里误判成 stale——两种失败原因不同,措辞不该混在一起。
            if current is not None and current != before:
                return {"error": "这一格刚改过,我重新看看再给你提", "stale": True}
        # T3 结构化事实账本：**落盘新事实/草案之前**比对提案正文。
        # 从正文抽取实体事实，而不是把落点 slot 冒充实体；只提示不硬拦，任何异常吞掉。
        fact_conflicts: list[dict] = []
        fact_hints: list[str] = []
        fact_entries: list[dict] = []
        _ledger = None
        _fl = None
        try:
            from . import facts_ledger as _fl
            _ledger = _fl
            _cfg = load_config(root)
            try:
                _backend = cheap_backend(_cfg) or get_backend(_cfg)
            except Exception as _be:
                _backend = None   # 抽取侧会打一行 stderr 说明未提供后端
                print(f"[拍板前事实比对] 取抽取后端失败：{type(_be).__name__}: {_be}",
                      file=sys.stderr, flush=True)
            fact_entries = []
            for e in _fl.extract_entries(content, _backend):
                # extract_entries 的默认状态是草案；拍板语义必须显式升为确认。
                e = dict(e)
                e["状态"] = "确认"
                got = _fl.normalize_entry(e, source=f"拍板落盘/{pid}")
                if got:
                    fact_entries.append(got)
            fact_conflicts = _fl.compare(_fl.load(root), fact_entries)
            if fact_conflicts:
                fact_hints = _fl.conflict_lines(fact_conflicts)
        except Exception as _e:
            # 比对失败不阻断落盘，但要留痕：静默会让「没冲突」与「没比过」分不开
            print(f"[拍板前事实比对] 本次跳过：{type(_e).__name__}: {_e}", file=sys.stderr, flush=True)
            fact_entries = []
        try:
            # 2026-10-23:地基里没有的新项(设定集内新小节 / 50-自定义 新文件)走另一条落盘链,
            # 它只追加/新建、永不覆盖已有正文。proposal 里的 "new" 就是提设定当时算好的计划。
            if proposal.get("new"):
                landed = journey_mod._land_new_slot(root, slot_id, content)
            else:
                landed = journey_mod._land_slot(root, slot_id, content)
        except ValueError as e:
            return {"error": str(e)}
        event = {"t": "confirm", "id": pid, "ts": ts, "landed": landed}
        if proposal.get("new"):
            event["new"] = True
        if fact_conflicts:
            event["事实冲突"] = fact_conflicts
            event["事实冲突提示"] = fact_hints
        if fact_entries and _ledger is not None:
            # 事实检查完成且实际落盘成功后才登记，避免失败的提案污染账本。
            _ledger.record(root, fact_entries)
        partner_store.append_event(root, event)
        # 任务准备架构(2026-11-08,§8/§13.3):confirm 集中在伙伴流水,必须据提案来源
        # 关联回专家任务——按 proposal 的 role 找该角色的任务目录,折叠 decision 事件。
        _link_decision_to_task(root, proposal, pid, accepted=True, ts=ts)
        return {"landed": landed, "state": journey_mod.journey_state(root)}


def _link_decision_to_task(root: Path, proposal: dict, pid: str, *,
                           accepted: bool, ts: str, note: str = "") -> dict:
    """把作者决策(接受/拒绝)关联到提案来源角色的任务状态(§8 决策跨通道关联)。

    F2/F3 修复（2026-11-08 分片3，历史会话 深度审查）——回连**不再猜**：
    - proposal 携带服务端绑定的 task_id（agent_chat._emit_tool_result 现在写入）
      → 只回连该任务，别的一律不碰；
    - 无 task_id 的旧卡（兼容期）→ **明确未关联**（linked=False），不再按
      「role+最近 updated_ms」猜目标（旧卡接受会记到更新的任务头上——确定误关联）。
    返回 {linked: bool, task_id: str} 供调用方如实上报。
    """
    out = {"linked": False, "task_id": ""}
    try:
        from . import task_state as _ts_mod
        from .paths import TASKS_DIR
        tasks_root = root / TASKS_DIR
        if not tasks_root.is_dir():
            return out
        kind = "accepted" if accepted else "rejected"
        # ① 优先：proposal 携带的绑定 task_id（服务端写入，可证明归属）
        tid = str((proposal.get("task_id") or "") or "").strip()
        conv = str((proposal.get("conversation_id") or "默认") or "默认").strip()
        if tid:
            base = tasks_root / _ts_mod.sanitize_conversation_id(conv)
            target = _ts_mod.load_snapshot(base, tid) or _ts_mod.load_or_rebuild(base, tid)
            if target is None or not target.is_active():
                # 绑定的任务已终态/消失 → 留决策到日志不推进（旧卡不续新任务，§8）
                return out
        else:
            # ② 无绑定（旧卡/无主卡）→ 明确未关联，不猜。
            return out
        # 决策重复折叠守卫:同 pid 同 kind 只记一次(§8 去重)
        if any(d.get("proposal_id") == pid and d.get("kind") == kind
               for d in target._d.get("decisions") or []):
            out.update(linked=True, task_id=target.task_id)
            return out
        ev = {"kind": "decision", "proposal_id": pid, "decision_kind": kind,
              "source_event": f"confirm:{pid}", "ts": _ts_mod.now_ms(), "note": note}
        _ts_mod.commit(base, target, ev, expected_revision=target.revision)
        if accepted and not target._d.get("resumed_once"):
            # 一次接受最多安排一次续轮:打标记(实际续轮仍由前端 agentResumeIfPending 发起,
            # 服务端标记防"同一事件两端各触发一次"§13.3)
            _ts_mod.commit(base, target, {"kind": "resume_flag"},
                           expected_revision=target.revision)
        out.update(linked=True, task_id=target.task_id)
        return out
    except Exception:      # noqa: BLE001 - 决策关联失败不拦确认主流程
        import logging
        logging.getLogger(__name__).warning("任务决策关联失败 pid=%s", pid, exc_info=True)
        return out


def task_reject(root: Path | str, proposal_id: str, role: str, *,
                original_text: str, ts: str = "") -> dict:
    """作者明确原话拒绝(§13.3 必做):记录拒绝对象+原话定位,跨轮承接进任务状态。

    original_text 是作者拒绝的原话(前端从消息流取,服务端不猜测)。
    F3 修复(分片3):返回值如实区分 recorded(记了原话)与 linked(关联到任务)——
    旧卡/无归属卡 recorded=True 但 linked=False,不再冒充承接成功。
    """
    from . import task_state as _ts_mod
    from . import task_state as _ts_san
    from .paths import TASKS_DIR
    root = Path(root)
    base = root / TASKS_DIR / _ts_san.sanitize_conversation_id("默认")
    rec = {"ok": True, "recorded": bool((original_text or "").strip()),
           "linked": False, "task_id": ""}
    if not (original_text or "").strip():
        return {"ok": False, "recorded": False, "linked": False,
                "error": "缺少可定位的拒绝原话,本轮只记录未承接"}
    ev = {"t": "proposal_rejected", "id": proposal_id, "role": role,
          "原话": original_text[:400], "ts": ts or time.strftime("%Y%m%d-%H%M%S")}
    (root / ".伙伴对话").mkdir(parents=True, exist_ok=True)
    with (root / ".伙伴对话" / "agent-拒绝记录.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    rec["记录"] = ".伙伴对话/agent-拒绝记录.jsonl"
    # 关联到任务(计划据此调整:折叠 decision=rejected)——只认 proposal 绑定的
    # task_id,没有绑定(旧卡/不存在)就如实 linked=False,不猜最近。
    try:
        prop = partner_store.find_proposal(root, proposal_id) or {}
        got = _link_decision_to_task(root, prop, proposal_id, accepted=False,
                                     ts=ts, note=original_text[:200])
        rec.update(linked=bool(got.get("linked")),
                   task_id=str(got.get("task_id") or ""))
    except Exception:      # noqa: BLE001
        pass
    return rec


def agent_new(root: Path | str, role: str, stamp: str) -> dict:
    """某个作者智库角色「另起一段对话」:归档 jsonl,不动书内容。

    与 partner_new 一样先拿写锁,避免和正在跑的 worker 抢同一个文件。
    """
    from . import agent_chat
    root = Path(root)
    with write_lock(root):
        return agent_chat.archive_current(root, role, stamp)


def partner_new(root: Path | str, *, stamp: str) -> dict:
    """归档当前伙伴对话,另起一段(不动书内容,只挪 jsonl 文件)。

    先拿伙伴轮锁再归档(blocker 修复,spec §10.2):say worker 从头到尾持轮锁,
    「停」在前端提前清 _partnerBusy 后 worker 可能仍在写尾巴事件——此时归档若改名
    当前.jsonl,worker 的 append 会重建一个没有 user 事件的孤儿文件,旧轮尾巴串进
    新会话。轮锁被占 → 409 partner_busy,挡住归档直到 worker 收尾放锁。
    """
    root = Path(root)
    guard = try_partner_lock(root)
    if guard is None:
        raise ProjectBusyError(PARTNER_BUSY_MESSAGE, code="partner_busy")
    try:
        with write_lock(root):
            partner_store.archive_current(root, stamp)
            return {"ok": True}
    finally:
        guard.release()
