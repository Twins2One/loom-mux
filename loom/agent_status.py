# -*- coding: utf-8 -*-
"""角色指示灯()。

需求背景:「当前多角色并发思考的时候偶尔会忘之前有哪个还有欠账,把『含未读消息』的和
『正在思考』的角色在侧边栏角色框右上角加个指示灯现实吗」

两盏灯,两种**事实**,都不猜:

- **红点闪(在思考)**:这个角色的工作线程正在跑。这是**进程内实时状态**,由运行方
  (对话端点 / 议事派发)在开跑与收尾时登记。进程重启就清空 —— 这符合事实:
  重启后没有任何东西在跑。
- **黄点定(有欠账)**:它最后一条是「我说的话」,而作者之后没再回它 ——
  也就是它在等你。这是**盘上的事实**,从各自的对话流水尾巴直接读,不估。

## 为什么不放数据库、不做推送

这本书的对话流水本来就是人可读的 jsonl(作者要能手动删)。这里只**读尾巴几行**,
不新造状态文件 —— 多一份状态就多一个会跟正文对不上的地方。轮询是 4 秒一次读 9 个小文件,
比一次模型调用的零头还便宜。

## 为什么不给写作引擎六棒挂灯

它们是流水线工人,没有"对话欠账"这回事(跑完就交产物)。硬挂一盏灯只会让人以为
它们随时可以聊。要盯流水线进度,用运行浮窗。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .scratchpad import AUTHOR_ROLES, VISIBLE_AUTHOR_ROLES

# 正在思考的角色(角色名 → 在跑几个)。用计数而不是布尔:议事厅里同一个角色理论上
# 可能被两次派发(极端情况),计数能让"两个都在跑时收掉一个"不误判成空闲。
_BUSY: dict[str, int] = {}
_BUSY_LOCK = threading.Lock()

# 「近况」窗口(分钟),。
# 起因:只看"最后一条是谁说的",于是一张没点的候选卡能挂一整夜 —— 隔了很多轮对话,
# 灯还是亮着,"欠账"变成了永久状态,反而没人信。现在加一道时间窗:
#   - 30 分钟内的「它说完了、你没回」/「有卡待拍板」才点灯;
#   - 过期就不点(RECENT_MINUTES 之外的事,应该发生在对话里,不该占着一颗灯);
#   - **「正在思考」不受这道窗约束** —— 那是实时事实,线程在跑就是跑着。
RECENT_MINUTES = 30

_TS_FORMATS = ("%Y%m%d-%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")


def mark_busy(role: str, on: bool = True) -> None:
    """运行方登记:开跑 = True,收尾 = False。必须成对调用(finally 里关)。"""
    r = (role or "").strip()
    if not r:
        return
    with _BUSY_LOCK:
        if on:
            _BUSY[r] = _BUSY.get(r, 0) + 1
        else:
            n = _BUSY.get(r, 0) - 1
            if n > 0:
                _BUSY[r] = n
            else:
                _BUSY.pop(r, None)


def busy_roles() -> dict[str, bool]:
    with _BUSY_LOCK:
        return {r: (n > 0) for r, n in _BUSY.items()}


def _tail_events(path: Path, n: int = 8) -> list[dict]:
    """只读文件**尾部**(大文件别整个读:一本书的对话流水能到几百 KB)。"""
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        want = 65536
        with path.open("rb") as f:
            if size > want:
                f.seek(size - want)
                f.readline()          # 丢掉可能被截断的半行
            raw = f.read()
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out[-n:]


def _agent_rel(role: str) -> str:
    return f".伙伴对话/agent-{role}.jsonl"


def _partner_rel() -> str:
    from . import paths

    return paths.PARTNER_CUR_REL


def _parse_ts(raw: str):
    """把事件里的 ts 解成 datetime。认不出就返回 None(调用方按"不点灯"处理)。"""
    import datetime as _d

    s = (raw or "").strip()
    if not s:
        return None
    for fmt in _TS_FORMATS:
        try:
            return _d.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def age_minutes(raw: str):
    """这条事件距今多少分钟。认不出时间 → None(不拿未知去点亮一盏灯)。"""
    import datetime as _d

    t = _parse_ts(raw)
    if t is None:
        return None
    delta = (_d.datetime.now() - t).total_seconds() / 60.0
    return max(0.0, delta)


def one(root: Path | str, role: str) -> dict:
    """一个角色的状态。只读。"""
    root = Path(root)
    role = (role or "").strip()
    rel = _partner_rel() if role == "领航员" else _agent_rel(role)
    evs = _tail_events(root / rel)
    last = evs[-1] if evs else {}
    last_t = (last.get("t") or "").strip()
    age = age_minutes(last.get("ts") or "")
    fresh = age is not None and age <= RECENT_MINUTES
    # 「欠账」= 最后一句是它说的,作者还没回 —— 而且得是**近况**(见 RECENT_MINUTES)。
    # 候选卡也是它说的(等你拍板);出错的轮次也算(那一轮没跑成,得让人回去看)。
    due = last_t in ("assistant", "proposal", "error") and fresh
    return {
        "角色": role,
        "在思考": busy_roles().get(role, False),
        "有欠账": due,
        "待拍板": bool(last_t == "proposal" and fresh),
        "近况": fresh,
        "距今分钟": (round(age, 1) if age is not None else None),
        "窗口分钟": RECENT_MINUTES,
        "最后一条": (last.get("ts") or "") or "",
        "最后一条是谁": ("我说" if due else ("你说" if last_t == "user" else "")),
        "有记录": bool(evs),
    }


def snapshot(root: Path | str) -> dict:
    """全部有对话意义的角色的灯。领航员 + 作者智库八人(写作引擎六棒不挂灯,见模块头)。"""
    #：知识顾问藏起来后它的灯也不该点——
    #点着一盏你点不开、看不见的灯只会让人以为界面坏了。AUTHOR_ROLES本体一字不动。
    roles = ["领航员", *sorted(VISIBLE_AUTHOR_ROLES)]
    return {r: one(root, r) for r in roles}


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
