"""书房（0.8.0 前端壳）。

**新增包，不依赖任何既有模块**——旧的 Loom-mux 一行都不用改。
路由通过 `router` 暴露，由 `loom.server` 挂载（只加 include_router 一行）。

当前只实现「读」：知识库扫描 + 按需读一册。
写作、蒸馏、取稿等一律走原 Loom-mux，不在本包内。

分层意图（作者 2026-10-26）：
    书房 = 学习与思维进化（本包）
    织布机 = 原 Loom-mux（灵感与编织）
"""

from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .library import library_root, read_volume, scan_library
from . import account as _account          # 2026-11-03 账户壳

router = APIRouter(prefix="/api/study", tags=["study"])
# 注意：account_router 不嵌进上面——嵌了会叠成 /api/study/api/account/*。
# 它由 loom/server.py 与 study_router 平级 include（自带 /api/account 前缀）。


@router.get("/health")
def health() -> dict:
    """书房模块存活探针。前端启动时可先打这一枪，确认壳已就位。"""
    return {"ok": True, "module": "study", "version": "0.8.0-dev"}


@router.get("/library")
def library_index() -> dict:
    """知识库总览：分类 → 科目 → 册。只读，现场扫描，不缓存。"""
    return scan_library()


@router.get("/volume")
def volume(path: str = Query(..., description="册文件绝对路径")) -> dict:
    """按需读一册正文。

    两道闸：① 路径必须落在知识库根目录内（防越界）；② 只放行 .md。
    知识库根下还有 _索引.md / _映射表.json / 迁移记录，不卡后缀等于开了个下载口子。
    """
    base = library_root().resolve()
    try:
        target = Path(path).resolve()
    except Exception:  # noqa: BLE001 - 非法路径直接拒
        return {"error": "路径非法"}
    # 越界检查：只允许读知识库内的文件
    try:
        target.relative_to(base)
    except ValueError:
        return {"error": "拒绝读取知识库之外的文件"}
    if target.suffix.lower() != ".md":
        return {"error": "只允许读取 .md 文件"}
    #2026-10-30：读一册就记一次，给「最近读过」那一栏用。
    got = read_volume(target)
    try:
        from . import recent as _r
        parent = target.parent.name
        kind = "教材" if "教材" in str(target) else ("素材" if "素材" in str(target) else ("语料" if "语料库" in str(target) else ""))
        _r.touch(str(target), target.stem, kind, parent)
    except Exception:
        pass
    return got


# ── 书房伙伴 · 真对话「书房伙伴要做真对话」）──────────────
class AskBody(BaseModel):
    question: str
    # [{role:"user"|"assistant", text:"…"}]，**前端带、服务端不存**（书房不落盘）
    history: list[dict] = []
    # 正在读的那一册的绝对路径（可选）。「边读边问」：
    # 抽屉里问的东西多半冲着眼前这一册，整册随问题递过去比让模型自己检索准。
    # 越界/不是 .md/读不到 → 当没给（不报错）。
    volume: str = ""
    # 2026-11-01：书房三角色各自独立 agent。空=书房伙伴（原行为）；
    # 知识顾问 / 语料分析师 = 各自的 system + 工具面（走 partner_tools 子集）。
    role: str = ""
    # ── 任务准备架构（2026-11-08,§13.1:书房当前服务端无状态 → 补 library 任务持久化）──
    conversation_id: str = ""   # 书房会话身份（前端生成/保留;空=「默认」）
    task_id: str = ""           # 承接既有任务;空=服务端定位/简单问答不建任务
    goal: str = ""              # 明确任务目标（前端在明确指令时传;空=简单问答）


@router.get("/ask/ready")
def ask_ready() -> dict:
    """**零花费**就绪检查：接了模型没有。界面据此决定显不显示输入框。

    不打模型、不碰网络，只读用户级配置与 key 环境变量。
    """
    from . import ask as _ask

    try:
        out = {"ok": True, **_ask.ready()}
        #2026-10-30：语义检索就绪与机器压力一并报出去，界面据此决定能不能提供退化。
        #两个都是零花费、零网络的本地读数。
        try:
            from . import semantic as _sem
            out["语义"] = _sem.ready()
            out["性能"] = _sem.perf()
        except Exception: # noqa: BLE001 - 语义层坏了不能把书房拖下水
            out["语义"] = {"ok": True, "可语义检索": False, "缺": ["semantic"]}
        return out
    except Exception as e:      # noqa: BLE001 - 探活失败只回报，不抛
        return {"ok": True, "ready": False, "provider": "", "model": "",
                "hint": f"检查没做成：{type(e).__name__}"}


@router.post("/ask")
def ask_route(b: AskBody):
    """一轮问答，ndjson 流。事件：`status` / `sources` / `delta` / `done` / `error`。

    **一次调用 = 一次模型调用 = 花一次钱**，只准作者点「发」的时候打。
    历史由前端带（≤6 轮）；显式任务按角色和会话持久化约束、计划及答复摘要。
    """
    from . import ask as _ask

    q: queue.Queue = queue.Queue()
    cancel = threading.Event()
    from .. import request_turn as _requests
    from .library import library_root as _request_root
    _ticket = _requests.begin(_request_root() / ".loom-tasks", b.role or "书房伙伴",
                              b.conversation_id or "默认", scoped_conversation=True)

    def _cancelled():
        return cancel.is_set() or not _requests.current(_ticket)

    # 任务准备架构（§13.1）:书房在 library 运行区建任务子区,按 role+conversation 隔离。
    # 准备失败发诊断回退,不拦问答（§6）。事件与 payload 走 t 信封,与对话域同构。
    _task_events: list[dict] = []
    _prepared = None
    try:
        from .. import turn_prepare as _tp, task_state as _tstate
        from ..study.library import library_root as _lroot
        base = _lroot() / ".loom-tasks" / _tstate.sanitize_role(b.role or "书房伙伴") / _tstate.sanitize_conversation_id(b.conversation_id)
        _prepared = _tp.prepare_turn(
            base_dir=base, domain="study", role=b.role or "书房伙伴",
            conversation_id=b.conversation_id or "默认", task_id=b.task_id,
            # §5:goal 只认前端显式任务目标;普通提问=简单问答,不建任务、不发 task_prepared
            # （旧消费者契约:kinds[0]=="status" 不破,§9）
            goal=(b.goal or "").strip(), user_text=b.question,
            conversation_tail=b.history, request_id=None, request_ticket=_ticket)
        _task_events = _prepared.get("events") or []
        if _prepared.get("task"):
            from ..agent_chat import _extract_constraints
            with _requests.effect(_ticket) as allowed, _tstate._scope_lock(base):
                if allowed:
                    state = _tstate.load_snapshot(base, _prepared["task"]["task_id"])
                    if state and state.status in ("ready", "running", "waiting_author", "blocked"):
                        for content in _extract_constraints(b.question):
                            _tstate.commit(base, state, {"kind": "constraint", "content": content})
                        _prepared = _requests.refresh(_prepared, base)
    except Exception as exc:      # noqa: BLE001 - 准备层失效:回退普通问答
        _prepared = None
        _task_events.append({"t": "status", "text": f"任务准备暂不可用({type(exc).__name__})，答复照常。"})

    def worker():
        # F9 回执整改（历史回执 审查记录 P2）：outcome 摘要 = 全部 delta 拼接
        # （原来只取最后一条 delta，流式终答只剩末几个字；非流式后端一条 delta
        # 都没有）；ask 抛 error 事件时**不落 outcome**（失败不算产出）。
        _all_delta: list[str] = []
        _saw_error = False
        _put = q.put

        def _emit_capture(ev: dict) -> None:
            nonlocal _saw_error
            if _cancelled():
                return
            if isinstance(ev, dict):
                if ev.get("t") == "delta" and ev.get("text"):
                    _all_delta.append(str(ev["text"]))
                elif ev.get("t") == "error":
                    _saw_error = True
            _put(ev)

        try:
            for ev in _task_events:
                q.put(ev)
            # F9（分片4）：任务事实进模型（goal/约束/计划/待决）——
            # 原来只给 UI 发事件，模型不知情（跨轮约束承接名不副实）。
            with _requests.tool_credentials(_ticket):
                _ask.ask(b.question, _emit_capture, history=b.history, volume=b.volume,
                         should_cancel=_cancelled, role=b.role,
                         task=(_prepared or {}).get("task"),
                         constraints=(_prepared or {}).get("constraints"),
                         plan_progress=(_prepared or {}).get("plan_progress"),
                         pending_questions=(_prepared or {}).get("pending_questions"))
            # 任务收尾（§5）:有任务则 outcome+状态推进,失败只留诊断
            if _prepared is not None and _prepared.get("task") is not None and not _saw_error:
                try:
                    from .. import turn_prepare as _tp2, task_state as _ts2
                    from ..study.library import library_root as _lroot2
                    base = _lroot2() / ".loom-tasks" / _ts2.sanitize_role(b.role or "书房伙伴") / _ts2.sanitize_conversation_id(b.conversation_id)
                    st = _ts2.load_snapshot(base, _prepared["task"]["task_id"])
                    if st is not None and st.is_active():
                        _summary = "".join(_all_delta).strip()
                        with _requests.effect(_ticket) as allowed:
                            if allowed and not cancel.is_set():
                                st = _ts2.load_snapshot(base, st.task_id)
                                if st is not None and st.status in ("ready", "running", "waiting_author"):
                                    with _ts2._scope_lock(base):
                                        st = _ts2.load_snapshot(base, st.task_id)
                                        if st is not None and st.status in ("ready", "running", "waiting_author"):
                                            upd = _tp2.parse_plan_update("".join(_all_delta))
                                            if upd:
                                                steps = [{"id": f"s{i+1}", **step} for i, step in enumerate(upd["steps"])]
                                                _ts2.commit(base, st, {"kind": "plan_update", "steps": steps,
                                                                      "reason": upd["reason"]})
                                                q.put(_ts2.t_event("plan_updated", st,
                                                                  text=_tp2.plan_update_event_text(st, upd["reason"])))
                                            # R-04（2026-11-10）：剥离 TASK_PLAN 协议段再交摘要。
                                            _ts2.commit(base, st, {"kind": "outcome",
                                                                   "summary": _tp2.strip_plan_segment(_summary)})
                                            _ts2.commit(base, st, {"kind": "status", "status": "waiting_author",
                                                                  "stop_reason": "答复完成，待作者接续"})
                                            q.put(_ts2.t_event("task_status", st, status=st.status))
                except Exception as exc:      # noqa: BLE001
                    if not _cancelled():
                        q.put({"t": "status", "text": f"任务收尾暂不可用({type(exc).__name__})，答复已保留。"})
        except Exception as e:      # noqa: BLE001 - 兜底，别让流挂死
            q.put({"t": "error", "text": f"意外错误：{e}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        try:
            while True:
                ev = q.get()
                if ev is None:
                    break
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        finally:
            # 客户端断连 → 生成器被关 → 置取消旗，下一个 chunk 起不再 emit
            cancel.set()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.get("/ask/task")
def ask_task_ep(role: str = "", conversation_id: str = "默认", task_id: str = ""):
    """书房任务快照读取。**纯只读、零模型**（§13.2）。按 role+conversation 隔离。"""
    from .. import task_state as _ts_mod
    from .library import library_root
    base = library_root() / ".loom-tasks" / _ts_mod.sanitize_role(role or "书房伙伴") / _ts_mod.sanitize_conversation_id(conversation_id or "默认")
    if task_id:
        st = _ts_mod.load_or_rebuild(base, task_id)
        return {"ok": st is not None, "task": st.to_dict() if st else None}
    out = []
    if base.is_dir():
        for candidate_id in _ts_mod.task_ids(base):
            st = _ts_mod.load_snapshot(base, candidate_id)
            if st is not None:
                out.append(st.to_dict())
    return {"ok": True, "tasks": out}


# ── 知识顾问 · 取稿（bv.py 子进程，要视觉理解）──────────────
@router.get("/video/ready")
def video_ready() -> dict:
    """取稿能力就绪情况（**零花费、零网络**）：能不能取文字、能不能看画面、缺什么。

    只读文件系统。界面用它在按钮旁写一句人话，别让作者点了才发现缺 ffmpeg。
    """
    from . import video as _v

    try:
        return _v.probe()
    except Exception as e:      # noqa: BLE001 - 探活失败只回报，不抛
        return {"ok": True, "取稿": False, "画面": False, "缺失": [],
                "hint": f"检查没做成：{type(e).__name__}"}


class VideoFetchBody(BaseModel):
    target: str                    # 视频地址
    start: float | None = None     # 只取这一段（可选）
    end: float | None = None
    ocr: bool = True


@router.post("/video/fetch")
def video_fetch(b: VideoFetchBody):
    """取稿：跑本机 `bv.py`（纯文字路径），把证据包拿回来。

    **这是本版唯一会访问 B 站的动作**，只在作者点「取稿」时发生；启动与巡检都不碰网络。
    事件：`status` / `done`（带证据）/ `error`（可读原因）。
    """
    from . import video as _v

    q: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def worker():
        try:
            q.put({"t": "status", "text": "正在取稿（下载字幕 / 本地转写）…"})
            out = _v.fetch_text(b.target, start=b.start, end=b.end, ocr=b.ocr)
            if out.get("ok"):
                ev = out.get("证据") or {}
                q.put({"t": "done", "证据": ev, "文本": _v.evidence_text(ev),
                       "字数": len(_v.evidence_text(ev))})
            else:
                #2026-11-04（使用向导 L2 落地）：没填凭据时取稿失败/字幕少 → 顺带标记
                #bili-credential 引导（前端下次 poll 会弹卡）。不是硬拦——公开信息仍能取。
                try:
                    if not (os.environ.get("BILI_SESSDATA") or "").strip():
                        from . import guides as _g
                        _g.mark_seen("bili-credential")
                except Exception:
                    pass
                q.put({"t": "error", "code": out.get("code") or "fetch_failed",
                       "text": out.get("text") or "取稿失败。"})
        except Exception as e:      # noqa: BLE001 - 兜底，别让流挂死
            q.put({"t": "error", "code": "unexpected", "text": f"意外错误：{e}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        try:
            while True:
                ev = q.get()
                if ev is None:
                    break
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        finally:
            cancel.set()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


class LocalTranscribeBody(BaseModel):
    path: str                      # 本机音视频文件（绝对路径）


@router.post("/video/transcribe")
def video_transcribe(b: LocalTranscribeBody):
    """把**本机**一个音视频文件转成文字（自带 ffmpeg + 本地模型，全离线、零花费）。

    为什么单开一条：作者手里常有下载好的课程/访谈视频，不一定非从 B 站取稿。
    这条路不联网、不调模型，所以**不需要确认弹窗**；长音频靠 `status` 事件报进度。
    事件：`status` / `done`（带文本与段）/ `error`（可读原因）。
    """
    from . import asr as _asr

    q: queue.Queue = queue.Queue()

    def worker():
        try:
            out = _asr.transcribe(b.path, progress=lambda text: q.put({"t": "status", "text": text}))
            if out.get("ok"):
                q.put({"t": "done", "文本": out["文本"], "字数": out["字数"],
                       "段": out["段"], "音频秒数": out["音频秒数"], "耗时": out["耗时"]})
            else:
                q.put({"t": "error", "code": out.get("code") or "asr_failed",
                       "text": out.get("text") or "转写失败。"})
        except Exception as e:      # noqa: BLE001 - 兜底，别让流挂死
            q.put({"t": "error", "code": "unexpected", "text": f"意外错误：{e}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ── 知识顾问 · 最小可用后半段（③理解 → ④沉淀，）──────────
class MaterialBody(BaseModel):
    title: str = ""
    text: str
    subject: str = "取稿笔记"     # 落点：素材/<科目>/
    source: str = ""             # 来源（视频地址/片名，可选，只写进提示词）


@router.post("/material")
def material_route(b: MaterialBody):
    """把一份字幕/转写整理成笔记并落进「个人图书馆」。

    **一次请求 = 一次模型调用 = 花一次钱**，只准作者在界面上点头后打。
    事件：`status` / `delta` / `done`（带落盘路径）/ `error`。
    只写 `知识库/素材/<科目>/`，重名加后缀不覆盖。
    """
    from . import material as _mat

    q: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def worker():
        try:
            _mat.ingest(b.title, b.text, q.put, subject=b.subject, source=b.source,
                        should_cancel=cancel.is_set)
        except Exception as e:      # noqa: BLE001 - 兜底，别让流挂死
            q.put({"t": "error", "text": f"意外错误：{e}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        try:
            while True:
                ev = q.get()
                if ev is None:
                    break
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        finally:
            cancel.set()

    return StreamingResponse(stream(), media_type="application/x-ndjson")




#2026-10-30：阅读笔记。按册存、不写字稿。
@router.get("/notes")
def notes_get(path: str = Query(..., description="册文件绝对路径")) -> dict:
    from . import notes as _n
    data = _n.load(path)
    return {"ok": True, "笔记": data.get("笔记") or []}


class NoteBody(BaseModel):
    path: str
    book: str = ""
    para: int = 0
    text: str = ""


@router.post("/notes")
def notes_add(b: NoteBody) -> dict:
    from . import notes as _n
    item = _n.add(b.path, b.book, b.para, b.text)
    if not item:
        return {"ok": False, "reason": "笔记内容空着呢"}
    return {"ok": True, "笔记": item}


class NoteDelBody(BaseModel):
    path: str
    id: str


@router.post("/notes/delete")
def notes_delete(b: NoteDelBody) -> dict:
    from . import notes as _n
    ok = _n.remove(b.path, b.id)
    return {"ok": ok}


@router.get("/notes/search")
def notes_search(q: str = Query("", description="关键词，空就列全部")) -> dict:
    from . import notes as _n
    return {"ok": True, "笔记": _n.search(q)}




#2026-10-30：最近读过的前三本。
@router.get("/recent")
def recent_books() -> dict:
    from . import recent as _r
    return {"ok": True, "书": _r.load()}


#2026-10-30：综合搜索——教材/素材/语料+笔记，语义检索。
@router.get("/find")
def find(q: str = Query("", description="短句或单词")) -> dict:
    """综合搜索。只接一个短句，**不开放详细参数**——那些留给书房伙伴的「搜书房」工具。"""
    from . import ask as _ask
    from . import notes as _n
    word = (q or "").strip()
    if not word:
        return {"ok": True, "命中": [], "词": ""}
    out = []
    try:
        got = _ask.search(word, top_k=8)
        for s in got.get("节选") or []:
            out.append({"类型": "书", "册": s.get("卷") or "", "科目": s.get("科目") or "",
                        "分类": s.get("分类") or "", "节": s.get("节") or "",
                        "路径": s.get("路径") or "",
                        "片": (s.get("正文") or "")[:220]})
    except Exception:
        pass
    try:
        for n in _n.search(word):
            out.append({"类型": "笔记", "册": n.get("册") or "", "科目": "",
                        "分类": "", "节": "第 %d 段" % (int(n.get("段") or 0) + 1),
                        "路径": n.get("路径") or "", "片": n.get("文") or "", "id": n.get("id")})
    except Exception:
        pass
    return {"ok": True, "命中": out, "词": word}




#2026-10-30：索引状态。前端拿它决定搜索按钮灰不灰。
#**懒触发**：第一次问状态且索引没建好时，才开后台线程去建。
#这样不用书房的人不必白等那 250 秒。
@router.get("/embed/status")
def embed_status(autostart: bool = Query(True, description="没建好时是否开后台建")) -> dict:
    from . import semantic as _sem
    out = {"ok": True, "可用": False, "就绪": False, "建中": False, "段": 0, "进度": 0}
    try:
        if not _sem.ready()["可语义检索"]:
            return out
        from . import ask as _ask
        secs = _ask._all_sections()
        heads = [((s.get("节") or "") + chr(10) + s["正文"])[:400] for s in secs]
        fp = _sem.fingerprint(heads)
        st = _sem.index_status(len(heads), fp)
        if autostart and st["可用"] and not st["就绪"] and not st["建中"]:
            _sem.start_build(heads, len(heads), fp)
            st = dict(st)
            st["建中"] = True
        out.update(st)
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, e)
    return out




#2026-10-31：B站凭据——**每个人用的时候自己初始化**。
#放 `~/.loom/.env`（用户目录，**不进包**，跟着人走不跟着软件走）。
#没填时功能照常可用（公开信息能取），只是 CC 字幕会少——降级优先，不弹红字。
@router.get("/bili/credential")
def bili_credential_get() -> dict:
    import os
    raw = (os.environ.get("BILI_SESSDATA") or "").strip()
    return {"ok": True, "已填": bool(raw),
            "尾四位": (raw[-4:] if len(raw) >= 4 else ""), "值": ""}


class BiliCredBody(BaseModel):
    sessdata: str = ""


@router.put("/bili/credential")
def bili_credential_put(b: BiliCredBody) -> dict:
    import os
    from ..config import user_env_path
    val = (b.sessdata or "").strip()
    p = user_env_path()
    lines = []
    if p.is_file():
        lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if not l.strip().startswith("BILI_SESSDATA=")]
    if val:
        lines.append("BILI_SESSDATA=" + val)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "text": "写不进去：" + str(e)}
    #热生效：本轮进程立刻能看到，不用重启
    os.environ["BILI_SESSDATA"] = val
    return {"ok": True, "已填": bool(val),
            "尾四位": (val[-4:] if len(val) >= 4 else "")}


__all__ = ["router", "library_root", "scan_library", "read_volume"]