"""开机后台维护：随开机自启，但不得造成明显硬件压力）。

**边界（外部 AI 审阅确认的硬规则）**：本模块属于 core 层，**不得 import 任何 ui/web 层**
（pywebview / uvicorn / fastapi / .server）。这样 `Loom.exe --maintain-only` 的 import
链里完全不存在 WebView2 依赖，WebView2 未装也不会让维护跑不起来。

无压力四条（都在这一个文件里）：
1. 进程优先级降到 BELOW_NORMAL —— 前台干活时自动让路（psutil 没装就跳过，不算错误）
2. 逐书节流：每本书之间 sleep 随机 5~15 秒，不连续抢占磁盘
3. 增量优先：只有 stale（速记本变了）的书才重建 —— 没动过的书毫秒级跳过，
   绝大多数日子实际工作量约等于 0
4. 启动先 wal_checkpoint(TRUNCATE)：清掉上次被强杀时残留的 WAL

调用方式：任务计划程序（登录触发 + 延迟 3 分钟 + 电池供电时不启动 + 防重入），
**不勾"空闲才启动"** —— 写作者一直在动键鼠，空闲条件会永远不满足，维护就永远不跑。
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path


def _log_paths() -> list[str]:
    """日志候选落点（按可靠性排序）。

    打包版是无终端构建：%TEMP% 在某些启动方式下拿不到/不可见，所以**先试 exe 同目录**
    ——那是作者一定找得到的地方（安装目录 / dist 目录）。
    """
    out = []
    try:
        if getattr(sys, "frozen", False):
            out.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "loom-maintain.log"))
    except Exception:
        pass
    try:
        base = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
        out.append(os.path.join(base, "loom-maintain.log"))
    except Exception:
        pass
    try:
        out.append(os.path.join(os.path.expanduser("~"), "loom-maintain.log"))
    except Exception:
        pass
    return out


def _log(msg: str) -> None:
    """无终端构建里唯一的排查线索：追加一行到第一个能写的日志文件。"""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n"
    for p in _log_paths():
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except Exception:
            continue


def _lower_priority() -> str:
    """降到 BELOW_NORMAL。返回一句可入日志的说明（失败也不阻断维护）。"""
    try:
        import psutil  # 打包版自带；开发态可能没装

        p = psutil.Process(os.getpid())
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        return "优先级=BELOW_NORMAL"
    except ImportError:
        return "优先级=跳过(无 psutil)"
    except Exception as e:  # noqa: BLE001
        return "优先级=跳过(%s)" % e


def _checkpoint() -> str:
    """清残留 WAL（上次进程被强杀时可能留下），顺带把库压回主文件。"""
    try:
        from . import memory as _m

        with _m._LOCK:  # noqa: SLF001 - 同包内共用一把锁，避免与写入并发
            conn = _m._connect()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        return "checkpoint=ok"
    except Exception as e:  # noqa: BLE001
        return "checkpoint=跳过(%s)" % e


def run_maintenance(*, full: bool = False, gap: tuple[float, float] = (5.0, 15.0)) -> dict:
    """跑一轮维护。返回统计（也会写日志）。

    full=False（默认，日常）：只重建 stale 的书。
    full=True（可选，每周全量）：所有书都重建一遍。
    gap：每本书之间的随机等待区间（秒）。传 (0, 0) 可关掉节流（测试用）。
    """
    started = time.time()
    stats = {
        "优先级": _lower_priority(),
        "checkpoint": _checkpoint(),
        "书": [],
        "重建": 0,
        "跳过": 0,
        "失败": 0,
        "full": bool(full),
        "秒": 0.0,
    }
    try:
        from . import memory as _m
        from . import userconf

        projects = []
        try:
            projects = userconf.list_projects()
        except Exception as e:  # noqa: BLE001
            _log("读书架失败：%s" % e)

        for i, p in enumerate(projects):
            if i and gap[1] > 0:
                time.sleep(random.uniform(max(0.0, gap[0]), max(0.0, gap[1])))
            root = str(p.get("root") or "").strip()
            # 注意：userconf.list_projects() 只回 {root,title,last_opened}——**没有 exists**
            # 字段（exists 是 /api/projects 端点现算的）。这里必须自己判目录在不在，
            # 否则会把每一本书都误判成"不存在"而整轮空转（2026-11-03 实测踩到）。
            if not root or not Path(root).is_dir():
                stats["书"].append({"root": root, "结果": "跳过(不存在)"})
                stats["跳过"] += 1
                continue
            name = str(p.get("title") or Path(root).name)
            try:
                need = bool(full) or _m.stale(root)
                if not need:
                    stats["书"].append({"书": name, "结果": "跳过(未变)"})
                    stats["跳过"] += 1
                    continue
                got = _m.rebuild(root)
                n = int(got.get("建了") or 0)
                stats["书"].append({"书": name, "结果": "重建", "条": n})
                stats["重建"] += 1
            except Exception as e:  # noqa: BLE001 - 一本书炸了不能拖垮整轮
                stats["书"].append({"书": name, "结果": "失败", "原因": str(e)[:120]})
                stats["失败"] += 1
    except Exception as e:  # noqa: BLE001
        stats["错误"] = str(e)[:200]

    stats["秒"] = round(time.time() - started, 2)
    try:
        _log(
            "维护完成 %s：重建 %d / 跳过 %d / 失败 %d / %.2fs"
            % (
                "全量" if full else "增量",
                stats["重建"],
                stats["跳过"],
                stats["失败"],
                stats["秒"],
            )
        )
    except Exception:
        pass
    return stats


def _cfg_value(name: str, default):
    """读用户配置里的一个值（容错：配置读不出来就用缺省）。"""
    try:
        from . import userconf

        return userconf.load_prefs().get(name, default)
    except Exception:
        return default


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：`Loom.exe --maintain-only [--full]`。

    只看参数、只跑维护、不碰窗口与 HTTP 服务。退出码：0=跑完（含部分书失败）。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--maintain-only" not in args:
        return 2  # 调用方走错了入口
    full = "--full" in args

    # 作者没同意就不做任何后台维护（prefs.memory_startup 默认关）
    if not _cfg_value("memory_startup", False):
        _log("维护跳过：作者未开启「开机后台维护记忆库」")
        return 0

    stats = run_maintenance(full=full)
    _log("汇总：" + str(stats))
    return 0


if __name__ == "__main__":  # 直接 python -m loom.maintenance --maintain-only
    raise SystemExit(main())
