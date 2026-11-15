"""旧书单角色模板补缺：只读检查、进程内确认票据、Windows no-replace 发布。"""
from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .agents import _parse_frontmatter, load_agent
from .config import user_config_dir
from .scaffold import TEMPLATES_DIR

ROLE = "人物侧写师"
REL = Path("agents") / f"{ROLE}.md"
_TICKETS: dict[str, "_Ticket"] = {}
_LOCK = threading.RLock()


class InstallError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class _Ticket:
    root: str
    root_id: tuple[int, int]
    agents_id: tuple[int, int]
    template_hash: str
    result: dict | None = None


def _identity(path: Path) -> tuple[int, int]:
    st = path.stat(follow_symlinks=False)
    return int(st.st_dev), int(st.st_ino)


def _win_dir_handle(path: Path):
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    invalid = wintypes.HANDLE(-1).value
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3,
                                  0x02000000 | 0x00200000, None)
    if handle == invalid:
        raise InstallError("publication_unsupported", f"无法绑定目录：winerror={ctypes.get_last_error()}")
    return kernel32, handle


def _win_handle_identity(kernel32, handle) -> tuple[int, int]:
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [("attrs", wintypes.DWORD), ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_hi", wintypes.DWORD),
                    ("size_lo", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_hi", wintypes.DWORD), ("index_lo", wintypes.DWORD)]

    info = Info()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise InstallError("publication_unsupported", f"无法核验目录句柄：winerror={ctypes.get_last_error()}")
    if int(info.attrs) & 0x400:
        raise InstallError("target_conflict", "agents 目录是重解析点。")
    return int(info.volume), (int(info.index_hi) << 32) | int(info.index_lo)


def _directory_identity(path: Path) -> tuple[int, int]:
    if os.name != "nt":
        return _identity(path)
    kernel32, handle = _win_dir_handle(path)
    try:
        return _win_handle_identity(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def _is_reparse_or_link(path: Path) -> bool:
    st = path.lstat()
    attrs = int(getattr(st, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attrs & reparse)


def _book(root: Path | str) -> Path:
    p = Path(root).expanduser().absolute()
    if not (p / "loom.toml").is_file():
        raise InstallError("not_a_book", f"{p} 不是 Loom 项目。", status=400)
    if _is_reparse_or_link(p):
        raise InstallError("target_conflict", "书根不能是链接或重解析点。")
    return p


def _template() -> tuple[Path, bytes, str]:
    path = TEMPLATES_DIR / REL
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise InstallError("template_missing", "安装包里没有人物侧写师模板。") from exc
    except OSError as exc:
        raise InstallError("template_unreadable", f"人物侧写师模板读不了：{exc}") from exc
    try:
        text = raw.decode("utf-8")
        meta, body = _parse_frontmatter(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError("template_invalid", f"人物侧写师模板格式无效：{exc}") from exc
    if not body.strip() or meta.get("name") != ROLE or meta.get("role") != "作者智库":
        raise InstallError("template_invalid", "人物侧写师模板缺少正确角色元数据或正文。")
    return path, raw, hashlib.sha256(raw).hexdigest()


def _target_state(root: Path) -> tuple[str, str]:
    agents_dir = root / "agents"
    if not agents_dir.exists():
        return "target_conflict", "书内 agents 目录不存在。"
    if _is_reparse_or_link(agents_dir) or not agents_dir.is_dir():
        return "target_conflict", "书内 agents 目录不是可绑定的普通目录。"
    target = root / REL
    try:
        st = target.lstat()
    except FileNotFoundError:
        return "missing", "缺少人物侧写师模板。"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or _is_reparse_or_link(target):
        return "target_conflict", "人物侧写师目标已被链接或非普通文件占用。"
    try:
        agent = load_agent(root, ROLE)
        meta, body = _parse_frontmatter(target.read_text(encoding="utf-8"))
        if not body.strip() or meta.get("name", ROLE) != ROLE or meta.get("role", "作者智库") != "作者智库":
            raise ValueError("角色元数据或正文无效")
        if not agent.system_prompt.strip():
            raise ValueError("正文为空")
    except UnicodeDecodeError:
        return "present_unreadable", "人物侧写师文件不是 UTF-8。"
    except (OSError, ValueError) as exc:
        return "present_invalid", f"人物侧写师文件不可用：{exc}"
    return "present_valid", "人物侧写师文件已存在且可用。"


def inspect(root: Path | str, role: str = ROLE) -> dict:
    if role != ROLE:
        return {"ok": False, "code": "role_not_installable", "role": role,
                "status": "unsupported", "can_install_template": False}
    try:
        book = _book(root)
        status, message = _target_state(book)
        try:
            _, _, digest = _template()
            template_valid, template_code = True, "package_template_valid"
        except InstallError as exc:
            digest, template_valid, template_code = "", False, exc.code
            if status == "missing":
                message = str(exc)
        can_install = status == "missing" and template_valid
        return {"ok": status in ("missing", "present_valid"), "code": template_code if status == "missing" else status,
                "role": ROLE, "root": str(book), "rel": REL.as_posix(), "status": status,
                "message": message, "template_sha256": digest,
                "can_install_template": can_install}
    except InstallError as exc:
        return {"ok": False, "code": exc.code, "role": ROLE, "status": "conflict",
                "message": str(exc), "can_install_template": False}


def confirm(root: Path | str, role: str = ROLE) -> dict:
    book = _book(root)
    state = inspect(book, role)
    if not state.get("can_install_template"):
        raise InstallError(state.get("code", "target_conflict"), state.get("message", "当前不可补齐。"))
    agents = book / "agents"
    _, _, digest = _template()
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _TICKETS[token] = _Ticket(str(book), _identity(book), _directory_identity(agents), digest)
    return {**state, "ok": True, "code": "confirmation_required", "confirmation_ticket": token}


def reset_tickets_for_test() -> None:
    """模拟进程重启；仅供隔离测试。"""
    with _LOCK:
        _TICKETS.clear()


def _write_staging(raw: bytes, target_volume: int) -> tuple[Path, str]:
    staging_dir = user_config_dir() / "template-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    if _directory_identity(staging_dir)[0] != target_volume:
        raise InstallError("cross_device", "Loom 暂存目录与书目录不在同一卷，当前平台不支持安全发布。")
    try:
        fd, name = tempfile.mkstemp(prefix="character-profiler-", suffix=".tmp", dir=staging_dir)
    except OSError as exc:
        raise InstallError("write_failed", f"无法创建私有暂存文件：{exc}") from exc
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise InstallError("write_failed", f"无法写完私有暂存文件：{exc}") from exc
    return path, "pending"


def _win_publish(staging: Path, agents_dir: Path, name: str, expected_dir_id: tuple[int, int],
                 *, after_open=None) -> None:
    if os.name != "nt":
        raise InstallError("publication_unsupported", "当前平台尚未证明目录绑定的 no-replace 发布原语。")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    INVALID = wintypes.HANDLE(-1).value
    GENERIC_READ, DELETE = 0x80000000, 0x00010000
    SHARE = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    BACKUP, OPEN_REPARSE = 0x02000000, 0x00200000
    FILE_RENAME_INFO_CLASS = 10  # NT FILE_INFORMATION_CLASS.FileRenameInformation

    CreateFileW = kernel32.CreateFileW
    CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    CreateFileW.restype = wintypes.HANDLE
    CloseHandle = kernel32.CloseHandle
    ntdll = ctypes.WinDLL("ntdll")

    class IoStatus(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    NtSetInfo = ntdll.NtSetInformationFile
    NtSetInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(IoStatus), wintypes.LPVOID,
                          wintypes.ULONG, ctypes.c_int]
    NtSetInfo.restype = ctypes.c_long

    dir_handle = CreateFileW(str(agents_dir), GENERIC_READ, SHARE, None, OPEN_EXISTING,
                             BACKUP | OPEN_REPARSE, None)
    if dir_handle == INVALID:
        raise InstallError("publication_unsupported", f"无法绑定 agents 目录：winerror={ctypes.get_last_error()}")
    try:
        if _win_handle_identity(kernel32, dir_handle) != expected_dir_id:
            raise InstallError("path_changed", "agents 目录对象已变化，请重新确认。")
        if after_open:
            after_open(agents_dir)
    except Exception:
        CloseHandle(dir_handle)
        raise
    src_handle = CreateFileW(str(staging), DELETE | GENERIC_READ, SHARE, None, OPEN_EXISTING, 0, None)
    if src_handle == INVALID:
        CloseHandle(dir_handle)
        raise InstallError("write_failed", f"无法打开完整暂存文件：winerror={ctypes.get_last_error()}")
    try:
        encoded = name.encode("utf-16-le")
        # FILE_RENAME_INFO: BOOLEAN + padding + HANDLE + DWORD + UTF-16 filename.
        ptr_size = ctypes.sizeof(wintypes.HANDLE)
        root_off = 8 if ptr_size == 8 else 4
        len_off = root_off + ptr_size
        name_off = len_off + 4
        buf = ctypes.create_string_buffer(name_off + len(encoded))
        ctypes.memset(buf, 0, len(buf))
        ctypes.c_ubyte.from_buffer(buf, 0).value = 0  # ReplaceIfExists = FALSE
        ctypes.c_void_p.from_buffer(buf, root_off).value = int(dir_handle)
        ctypes.c_uint32.from_buffer(buf, len_off).value = len(encoded)
        ctypes.memmove(ctypes.addressof(buf) + name_off, encoded, len(encoded))
        iosb = IoStatus()
        status = int(NtSetInfo(src_handle, ctypes.byref(iosb), buf, len(buf),
                               FILE_RENAME_INFO_CLASS))
        if status != 0:
            unsigned = status & 0xFFFFFFFF
            if unsigned in (0xC0000035, 0xC00000BA):
                raise InstallError("already_present", "人物侧写师文件已由另一请求或作者创建。")
            if unsigned == 0xC00000D4:
                raise InstallError("cross_device", "暂存文件与书目录不在同一卷。")
            raise InstallError("publication_unsupported", f"目录绑定发布失败：ntstatus=0x{unsigned:08X}")
    finally:
        CloseHandle(src_handle)
        CloseHandle(dir_handle)


def install(root: Path | str, ticket: str, role: str = ROLE, *, before_publish=None,
            after_open=None, cleanup=None) -> dict:
    if role != ROLE:
        raise InstallError("role_not_installable", "只有人物侧写师模板允许补齐。", status=400)
    with _LOCK:
        saved = _TICKETS.get(ticket)
        if saved is None:
            raise InstallError("confirmation_expired", "确认已失效，请重新检查并确认。")
        if saved.result is not None:
            return dict(saved.result)
        book = _book(root)
        agents_dir = book / "agents"
        if str(book) != saved.root or _identity(book) != saved.root_id:
            raise InstallError("confirmation_expired", "确认不属于当前这本书。")
        if _is_reparse_or_link(agents_dir) or _directory_identity(agents_dir) != saved.agents_id:
            raise InstallError("path_changed", "agents 目录已变化，请重新确认。")
        state, message = _target_state(book)
        if state != "missing":
            result = {"ok": state.startswith("present_"),
                      "code": "already_present" if state.startswith("present_") else state,
                      "role": ROLE, "root": str(book), "rel": REL.as_posix(),
                      "status": "already_present" if state.startswith("present_") else state,
                      "health": state, "message": message, "installed": False,
                      "maintenance": "not_needed"}
            saved.result = result
            return dict(result)
        _, raw, digest = _template()
        if digest != saved.template_hash:
            raise InstallError("confirmation_expired", "安装包模板已变化，请重新确认。")
        staging, maintenance = _write_staging(raw, saved.agents_id[0])
        outcome = "installed"
        try:
            if before_publish:
                before_publish(book, agents_dir, staging)
            if _is_reparse_or_link(agents_dir) or _directory_identity(agents_dir) != saved.agents_id:
                raise InstallError("path_changed", "agents 目录已变化，请重新确认。")
            _win_publish(staging, agents_dir, f"{ROLE}.md", saved.agents_id,
                         after_open=after_open)
        except InstallError as exc:
            if exc.code != "already_present":
                raise
            outcome = "already_present"
        finally:
            if staging.exists():
                try:
                    if cleanup:
                        cleanup(staging)
                    else:
                        staging.unlink()
                    maintenance = "clean"
                except OSError as exc:
                    maintenance = f"cleanup_failed:{type(exc).__name__}"
            else:
                maintenance = "clean"
        result = {"ok": True, "code": outcome, "role": ROLE, "root": str(book),
                  "rel": REL.as_posix(), "status": outcome, "installed": outcome == "installed",
                  "template_sha256": digest, "maintenance": maintenance,
                  "auto_resumed": False}
        saved.result = result
        return dict(result)


def chat_preflight(root: Path | str, role: str) -> dict | None:
    if role != ROLE:
        return None
    state = inspect(root, role)
    if state.get("status") == "present_valid":
        return None
    return state
