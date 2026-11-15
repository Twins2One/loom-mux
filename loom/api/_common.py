"""共享助手:server.py 拆分后各域端点模块从这里 import,不复制。

(2026-11-07 从 server.py 纯剪切搬来,函数体一字不改。)
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import paths, usecases
from ..backends import LoomBackendError
from ..usecases import ProjectBusyError

WEBUI_DIR = Path(__file__).parent.parent / "webui"


# 测试 monkeypatch loom.server.get_backend / loom.server.cheap_backend 兼容:
# 各域端点经这里**动态**取 loom.server 上的名字,补丁才能生效。
def get_backend(cfg):
    from .. import server as _srv
    return _srv.get_backend(cfg)


def cheap_backend(cfg):
    from .. import server as _srv
    return _srv.cheap_backend(cfg)


# (原 _project_busy 已删——交叉审查 P2-2：组装层 server.py 重新注册了自己的
#  exception handler，_common 这份不在异常链上是死代码。)

def _is_project(p: Path) -> bool:
    return (p / "loom.toml").exists()


def _project_root(raw: str) -> Path:
    """把请求里的 root 收成一本**真书**(必须有 loom.toml)。不是书就 400,绝不拿它当路径去写。

    为什么要有这个:端点里凡是拿 root 拼路径的地方,一旦 root 传成书目录以外的路径(例如 `C:\\Windows`),
    `root / "正文" / "第1章.md"` 就可能落到书目录之外。写入类端点必须挡住。
    """
    root = Path(str(raw or "")).expanduser()
    if not _is_project(root):
        raise HTTPException(status_code=400,
                            detail=f"{root} 不是 loom 项目(没有 loom.toml)。")
    return root


def _foundation_response(block: dict) -> JSONResponse:
    """地基硬门阻断的统一响应（审查 审查记录）。

    配置不可用（loom.toml 缺失或损坏）与既有「不是书」同码 400——两种情况响应码
    一致、都在开流前返回、都不进 worker；地基缺项才是 409 foundation_incomplete。
    """
    status = 400 if block.get("code") in ("not_a_book", "config_unreadable") else 409
    return JSONResponse(block, status_code=status)


def _world_gate(root: Path, body) -> JSONResponse | None:
    """软门（世界信息充足度）：未达标且作者未确认 → 409 提示体（**不是拒绝**）。

    适用范围（2026-11-11 需求裁定）：**只对生成类操作生效**。
    - `step` 为空（整章生成，含 `/api/write`）→ 评估软门；
    - `step` 非空（单棒）→ **跳过**：审稿不产出新正文，世界信息充分度不影响它，
      且软门问句「确定现在开始织第一章吗」对审稿本就不成立（硬门已对审稿做了豁免）。

    顺序纪律：调用方必须在**硬门之后**调它（硬门未过不弹"确定吗"），且在开 worker /
    开流之前。作者带 `confirm_world=true` 重发 → 落一条开织确认记录后放行。

    2026-11-11 补一处漏洞（实例验收抓到）：`/api/write` 的 Body **没有 `step` 字段**，
    它用 `from_step` 表达"从哪一棒起重算"（R24）。原来只读 `step`，于是
    「`/api/write` + `from_step=责任编辑`」这条**单棒审稿**被软门误拦。
    现在编辑类 `from_step` 也跳过；但 `from_step=写手` 这类**真在生成正文**的不跳。
    """
    from ..navigator import record_world_confirmation, world_block, world_readiness

    try:
        if str(getattr(body, "step", "") or "").strip():
            return None      # 单棒（编辑/润色/任意单棒）不是"开始织第一章"
        # `/api/write` 走 from_step：只有编辑类（不产新正文）才跳过软门
        if str(getattr(body, "from_step", "") or "").strip() in ("责任编辑", "润色师"):
            return None
        block = world_block(root, getattr(body, "chapter", 0))
        if not block:
            return None
        if getattr(body, "confirm_world", False):
            record_world_confirmation(root, getattr(body, "chapter", 0),
                                      world_readiness(root, getattr(body, "chapter", 0)),
                                      choice="continue")
            return None
        return JSONResponse(block, status_code=409)
    except Exception as exc:      # noqa: BLE001 - 软门判定自身出问题不得阻塞写作
        print(f"[世界信息软门] 判定失败，按放行处理:{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _checked_root(root: str):
    """只读端点也要过一遍「这真是本书」。返回 (Path, None) 或 (None, 400 JSONResponse)。

    交叉审查 P2-3:`/api/chapter/dirty`、`/api/chapter/impact`、`/api/run/card` 原来直接
    `cs.dirty_now(root)`,root 传成非书路径时静默返回空表 —— 界面会以为「一章都没脏 / 一次都不用花」。
    空结果和「路径错了」在界面上长得一模一样,这才是要修的地方。
    """
    try:
        return _project_root(root), None
    except HTTPException as e:
        return None, JSONResponse({"error": e.detail, "code": "not_a_book"}, status_code=400)


def _err_json(e: Exception, status: int = 400) -> JSONResponse:
    """非流式端点的错误响应单点:LoomBackendError 带 code(错误目录键)透传给前端附提示。"""
    body: dict = {"error": str(e)}
    if isinstance(e, LoomBackendError) and e.code:
        body["code"] = e.code
    return JSONResponse(body, status_code=status)


# 项目全景响应体单一真相在 usecases.project_state,server 只留薄壳
_state = usecases.project_state


class RootBody(BaseModel):
    root: str


def _shelve(root: Path, state: dict) -> dict:
    """开书成功 → 记上书架(新建/导入/样例统一入架,欢迎页点选,不再输路径)。"""
    from .. import userconf
    userconf.record_project(root, state.get("title", ""))
    return state


def _chapter_nums(root: Path) -> list[int]:
    """正文目录里已存在的章号(升序)。只认「第N章.md」,别的一概不算。"""
    out: list[int] = []
    d = root / paths.BODY_DIR
    if not d.is_dir():
        return out
    for p in d.glob("第*章.md"):
        stem = p.stem
        if stem.startswith("第") and stem.endswith("章") and stem[1:-1].isdigit():
            out.append(int(stem[1:-1]))
    return sorted(out)


_BAD_CHAPTER = {"error": "章号不能是负数(第 0 章是楔子)。", "code": "bad_chapter"}


def _bad_chapter(n) -> JSONResponse | None:
    """负章号挡在门口(交叉审查 P2-3:以前能造出「第-3章.md」)。**0 是楔子,放行。**"""
    try:
        if int(n) >= 0:
            return None
    except (TypeError, ValueError):
        pass
    return JSONResponse(_BAD_CHAPTER, status_code=400)


def _fail(e: BaseException, what: str) -> JSONResponse:
    """付费端点失败时回哪一种错 —— 别把「我们自己的 bug」说成「模型调用失败」。

    模型侧失败(连不上/鉴权/超时/返回不合规)回 502 backend_failed,前端可以劝人换模型或重试;
    其余异常(KeyError/ValueError/OSError 这类**是我们代码的错**)回 500 internal_error,
    如实说「这是 Loom 自己出错了」,不要让人白折腾。
    """
    from ..backends import LoomBackendError

    if isinstance(e, LoomBackendError):
        # 原样带上具体码(deepseek_auth / key_missing / insufficient_balance …):
        # 压成 backend_failed 会让前端丢掉「检查 API Key」那半句提示(N-4)
        _code = getattr(e, "code", "") or "backend_failed"
        return JSONResponse({"error": f"模型调用失败:{e}", "code": _code,
                             "backend_failed": True}, status_code=502)
    return JSONResponse({"error": f"{what}时 Loom 自己出错了({type(e).__name__}): {e}",
                         "code": "internal_error"}, status_code=500)


def _log_op(root, chapter: int, role: str, ok: bool, t0: float, note: str = "") -> None:
    """付费/写盘类操作的运行账本。**记账失败不影响主流程**,但必须留痕(不裸吞)。"""
    try:
        from .. import runlog as _rl
        _rl.record(root, chapter, role=role, ok=ok,
                   ms=int((time.monotonic() - t0) * 1000),
                   error="" if ok else note)
    except Exception as e:      # noqa: BLE001
        print(f"[运行账本] 记不上 {role}: {type(e).__name__}: {e}", file=sys.stderr)


def _card_rest_hint(r) -> dict:
    """导入完:报「本卷还有多少章没有章行」。**只报,不铺** —— 铺章纲必须人手动确认。

    以前这里是空白:导完一摞稿子,人不知道后面几十章要不要铺、从哪铺;
    但自动铺就是一次几十次付费调用的盲花钱,所以只给数字和入口。
    """
    try:
        from .. import volplan
        nb = volplan.next_batch(r, 1)
    except Exception:      # noqa: BLE001 - 报账失败不该污染导入结果
        return {}
    if nb.get("ok"):
        return {"本卷还缺章行": nb.get("本卷还缺"), "下一块从": nb.get("起"),
                "提示": (f"本卷还有 {nb.get('本卷还缺')} 章没有章行。要铺,请自己点"
                       f"「卡章纲 → 铺下一块」(会花一次模型调用);我不替你自动铺。")}
    return {"本卷还缺章行": 0, "提示": nb.get("reason") or ""}
