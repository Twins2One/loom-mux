"""Conversation request generations. Locks cover local effects, never model calls."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
import uuid
import inspect
from functools import wraps
from contextvars import ContextVar

_tool_ticket = ContextVar("loom_tool_request", default=None)


class StaleRequest(ValueError):
    pass


@contextmanager
def tool_credentials(ticket):
    marker = _tool_ticket.set(ticket)
    try:
        yield
    finally:
        _tool_ticket.reset(marker)


def tool_scope(fn):
    """Propagate request credentials through synchronous tool helpers."""
    @wraps(fn)
    def guarded(*args, request_ticket=None, **kwargs):
        marker = _tool_ticket.set(request_ticket if request_ticket is not None else _tool_ticket.get())
        try:
            with commit_effect():
                pass
            return fn(*args, **kwargs)
        except StaleRequest:
            return {"t": "result", "cancelled": True}
        finally:
            _tool_ticket.reset(marker)
    return guarded


@contextmanager
def commit_effect():
    """Guard a local commit in a tool; model/embedding work stays outside."""
    ticket = _tool_ticket.get()
    if ticket is None:
        yield
        return
    with effect(ticket) as allowed:
        if not allowed:
            raise StaleRequest("请求已由新一轮接替")
        yield


def tool_commit(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with commit_effect():
            return fn(*args, **kwargs)
    return guarded


def local_effect(fn):
    """Guard a task projection helper using the request attached to its emitter."""
    signature = inspect.signature(fn)

    @wraps(fn)
    def guarded(*args, **kwargs):
        emitter = signature.bind(*args, **kwargs).arguments.get("_emit")
        ticket = getattr(emitter, "_request_ticket", None)
        if ticket is None:
            return fn(*args, **kwargs)
        with effect(ticket) as allowed:
            if allowed:
                return fn(*args, **kwargs)
        return None

    return guarded

_guard = threading.RLock()
_current: dict[tuple[str, str, str], str] = {}


def begin(root, role: str, conversation: str, *, authorize=None, scoped_conversation=False):
    # Book histories are role-wide; study histories have independent conversations.
    if scoped_conversation:
        from .task_state import sanitize_conversation_id, sanitize_role
        role = sanitize_role(role)
        conversation = sanitize_conversation_id(conversation)
    key = (str(Path(root).resolve()).casefold(), role,
           conversation if scoped_conversation else "")
    with _guard:
        if authorize is not None and not authorize():
            return None
        token = uuid.uuid4().hex
        _current[key] = token
        return key, token


def retire(root, role: str):
    return begin(root, role, "")


def preparation(fn):
    @wraps(fn)
    def guarded(*args, request_ticket=None, **kwargs):
        from .task_state import _scope_lock

        def prepare_locked():
            with _scope_lock(kwargs["base_dir"]):
                return fn(*args, **kwargs)

        if request_ticket is None:
            return prepare_locked()
        with effect(request_ticket) as allowed:
            if not allowed:
                return {"task": None, "task_binding": "refused", "events": []}
            return prepare_locked()
    return guarded


def refresh(prepared, base):
    if not prepared or not prepared.get("task"):
        return prepared
    from . import task_state
    state = task_state.load_snapshot(base, prepared["task"]["task_id"])
    if state is None:
        return prepared
    updated = dict(prepared)
    updated["task"] = state.to_dict()
    updated["constraints"] = [
        {"content": c["content"], "kind": c.get("kind", "显式"),
         "待核": c.get("kind", "显式") != "显式"}
        for c in state._d.get("constraints", []) if not c.get("superseded")][:8]
    updated["plan_progress"] = state._d.get("plan") or {}
    updated["pending_questions"] = state._d.get("pending_questions") or []
    return updated


def current(ticket) -> bool:
    with _guard:
        return _current.get(ticket[0]) == ticket[1]


@contextmanager
def effect(ticket):
    """Atomically check generation and perform one local persistence effect."""
    with _guard:
        yield _current.get(ticket[0]) == ticket[1]
