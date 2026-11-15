# -*- coding: utf-8 -*-
"""书房账户壳（2026-11-03：找个现成壳子先糊弄，账户系统正式版后置）。

**定位：占位壳，不是真账户系统。**
- 本地 SQLite 单文件（~/.loom/account.sqlite），pbkdf2_hmac 存哈希（标准库，零新依赖）
- 登录态 = HMAC 签名 cookie（secret 首次生成落 ~/.loom/account.secret，0600）
- 书房页面**不强制登录**（boot 默认直接进）——壳只提供"有账户这回事"的骨架，
  强制鉴权等正式账户系统再做（避免半夜把作者锁在外面）

2026-11-04 体验八点裁定扩充：
- 密保问题/答案（答案 pbkdf2 存哈希）→ 忘记密码走「密保问答 → 签发一次性重置令牌 → 重置」
- 历史账户列表（登录页下拉）
- 记住密码：remember cookie 存 HMAC(name+uid) 签名令牌（**不存明文密码**——浏览器侧
  自动回填由前端 localStorage 存混淆串完成，本地单用户场景可接受；服务端只认令牌续签）
密钥派生与校验全部标准库，无新依赖、无网络。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Response
from pydantic import BaseModel

from ..config import user_config_dir

router = APIRouter(prefix="/api/account", tags=["account"])

_COOKIE = "loom_session"
_REMEMBER = "loom_remember"
_TTL = 30 * 24 * 3600          # 30 天
_ITER = 200_000                # pbkdf2 轮数（OWASP 2023 建议 60 万，壳子阶段 20 万够）
_SECRET_PATH = None
_RESET_TTL = 600               # 重置令牌 10 分钟有效


def _secret() -> bytes:
    """HMAC 签名密钥：首次生成写入 ~/.loom/account.secret（0600）。"""
    global _SECRET_PATH
    if _SECRET_PATH is None:
        _SECRET_PATH = user_config_dir() / "account.secret"
    p = _SECRET_PATH
    if p.exists():
        try:
            return p.read_bytes().strip()
        except Exception:
            pass
    s = secrets.token_hex(32).encode()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(s)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except Exception:
        pass                       # 读不了配置目录就进程内临时的，重启需重登（壳可接受）
    return s


def _db() -> sqlite3.Connection:
    d = user_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "account.sqlite", timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS account(
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        pwhash BLOB NOT NULL,
        salt BLOB NOT NULL,
        created INTEGER NOT NULL,
        sec_q TEXT DEFAULT '',
        sec_hash BLOB DEFAULT NULL,
        sec_salt BLOB DEFAULT NULL,
        kind TEXT DEFAULT 'password')""")
    # 旧库补列（2026-11-04 密保三列 + 无密码账户 kind）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(account)")]
    if "sec_q" not in cols:
        conn.execute("ALTER TABLE account ADD COLUMN sec_q TEXT DEFAULT ''")
    if "sec_hash" not in cols:
        conn.execute("ALTER TABLE account ADD COLUMN sec_hash BLOB DEFAULT NULL")
    if "sec_salt" not in cols:
        conn.execute("ALTER TABLE account ADD COLUMN sec_salt BLOB DEFAULT NULL")
    if "kind" not in cols:
        conn.execute("ALTER TABLE account ADD COLUMN kind TEXT DEFAULT 'password'")
    conn.commit()
    return conn


def _hash_pw(pw: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _ITER)


class _Auth(BaseModel):
    name: str
    password: str


class _Register(_Auth):
    sec_q: str = ""                # 密保问题（注册时选填；不填则该账户不能走忘记密码）
    sec_a: str = ""                # 密保答案原文（服务端立刻哈希，不落盘原文）
    kind: str = "password"         # "password" | "nopass"：无密码账户）


class _NoPassCreate(BaseModel):
    name: str


class _SecSetup(BaseModel):
    sec_q: str
    sec_a: str


class _ResetAsk(BaseModel):
    name: str


class _ResetVerify(BaseModel):
    name: str
    sec_a: str


class _ResetDo(BaseModel):
    name: str
    token: str
    new_password: str


def _sign(uid: int, exp: int) -> str:
    msg = f"{uid}.{exp}".encode()
    return msg.decode() + "." + hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def _verify(token: str) -> int | None:
    """校验签名 cookie → uid；过期/伪造返回 None。"""
    try:
        msg, _, sig = token.rpartition(".")
        uid_s, exp_s = msg.split(".")
        want = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None
        if int(exp_s) < int(time.time()):
            return None
        return int(uid_s)
    except Exception:
        return None


def current_uid(token: str | None) -> int | None:
    return _verify(token) if token else None


def _uid_by_name(name: str) -> int | None:
    conn = _db()
    try:
        row = conn.execute("SELECT id FROM account WHERE name=?", (name.strip(),)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@router.post("/register")
def register(body: _Register, response: Response) -> dict:
    """注册。2026-11-04 裁定④：注册成功**不再自动发会话**——前端跳回登录页。
    密保随注册一并收（选填）。"""
    name = body.name.strip()
    if not (1 <= len(name) <= 32):
        raise HTTPException(400, "用户名 1-32 字。")
    if len(body.password) < 4:
        raise HTTPException(400, "密码至少 4 位（壳阶段先这么着）。")
    salt = secrets.token_bytes(16)
    h = _hash_pw(body.password, salt)
    sec_q = body.sec_q.strip()
    sec_salt = sec_a_hash = None
    if sec_q and body.sec_a.strip():
        sec_salt = secrets.token_bytes(16)
        sec_a_hash = _hash_pw(body.sec_a.strip().lower(), sec_salt)   # 答案统一小写比对
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO account(name, pwhash, salt, created, sec_q, sec_hash, sec_salt, kind)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (name, h, salt, int(time.time()), sec_q, sec_a_hash, sec_salt, body.kind))
        conn.commit()
        uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(400, "这个名字已经有人用了。")
    finally:
        conn.close()
    # 不 set_cookie（裁定④：跳回登录）
    return {"ok": True, "name": name, "uid": uid, "跳转": "登录"}


@router.post("/login")
def login(body: _Auth, response: Response) -> dict:
    conn = _db()
    try:
        row = conn.execute("SELECT id, pwhash, salt, kind FROM account WHERE name=?",
                           (body.name.strip(),)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(401, "用户名或密码不对。")
    # 2026-11-04：无密码账户——名字匹配即进，不校验密码。
    if row[3] == "nopass":
        uid = row[0]
    else:
        if not row[1] or not hmac.compare_digest(row[1], _hash_pw(body.password, row[2])):
            raise HTTPException(401, "用户名或密码不对。")
        uid = row[0]
    exp = int(time.time()) + _TTL
    response.set_cookie(_COOKIE, _sign(uid, exp), max_age=_TTL, httponly=True, samesite="lax")
    return {"ok": True, "name": body.name.strip()}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(loom_session: str | None = Cookie(default=None)) -> dict:
    """前端问当前账户。cookie 注入必须用 fastapi.Cookie——普通默认值参数 FastAPI 只当
    query 参数收（2026-11-07 手测 #4 根因：浏览器 HttpOnly cookie 永远到不了这里，
    登录 30 天形同虚设，每次进书房都掉回登录页）。查询串兜底仍有效（FastAPI 会再收同名 query）。"""
    return _me_impl(loom_session)


def _me_impl(loom_session: str | None) -> dict:
    uid = current_uid(loom_session)
    if uid is None:
        return {"ok": False, "name": ""}
    conn = _db()
    try:
        row = conn.execute("SELECT name FROM account WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    return {"ok": bool(row), "name": row[0] if row else ""}


# ── 2026-11-04 体验八点 ─────────────────────────────────────────────

@router.get("/list")
def list_accounts() -> dict:
    """历史账户名单（登录页下拉）。只回名字与是否有密保，不回任何哈希。"""
    conn = _db()
    try:
        rows = conn.execute("SELECT name, sec_q, kind FROM account ORDER BY id").fetchall()
    finally:
        conn.close()
    return {"ok": True, "账户": [{"name": r[0], "有密保": bool(r[1]),
                                  "无密码": r[2] == "nopass"} for r in rows]}


@router.post("/nopass/create")
def nopass_create(body: _NoPassCreate) -> dict:
    """创建无密码账户：只有名字，登录时名字匹配即进（作者 2026-11-04 裁定，
    适合不想记密码的用户）。本机单用户场景，防线=开这台电脑的人。"""
    name = body.name.strip()
    if not (1 <= len(name) <= 32):
        raise HTTPException(400, "用户名 1-32 字。")
    salt = secrets.token_bytes(16)
    # pwhash 存随机废值（列 NOT NULL）——无密码账户永远不走密码校验路径
    dummy = _hash_pw(secrets.token_hex(16), salt)
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO account(name, pwhash, salt, created, kind) VALUES(?,?,?,?, 'nopass')",
            (name, dummy, salt, int(time.time())))
        conn.commit()
        uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(400, "这个名字已经有人用了。")
    finally:
        conn.close()
    return {"ok": True, "name": name, "uid": uid, "跳转": "登录"}


@router.post("/security/setup")
def security_setup(body: _SecSetup, loom_session: str | None = Cookie(default=None)) -> dict:
    """设置/更换密保问题（须已登录）。cookie 注入同 /me（fastapi.Cookie）。"""
    uid = current_uid(loom_session)
    if uid is None:
        raise HTTPException(401, "先登录再设密保。")
    q = body.sec_q.strip()
    a = body.sec_a.strip()
    if not q or not a:
        raise HTTPException(400, "问题和回答都要填。")
    salt = secrets.token_bytes(16)
    h = _hash_pw(a.lower(), salt)
    conn = _db()
    try:
        conn.execute("UPDATE account SET sec_q=?, sec_hash=?, sec_salt=? WHERE id=?",
                     (q, h, salt, uid))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.post("/reset/ask")
def reset_ask(body: _ResetAsk) -> dict:
    """忘记密码第一步：报名字，回密保问题（没设密保的账户明确说不能走这路）。"""
    name = body.name.strip()
    conn = _db()
    try:
        row = conn.execute("SELECT id, sec_q FROM account WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "没有这个账户。")
    if not row[1]:
        raise HTTPException(400, "这个账户没设密保问题，走不了忘记密码。请联系……本机数据，"
                                 "实在不行删掉 ~/.loom/account.sqlite 重新注册（稿子不受影响）。")
    return {"ok": True, "问题": row[1]}


@router.post("/reset/verify")
def reset_verify(body: _ResetVerify) -> dict:
    """第二步：验密保答案 → 签发一次性重置令牌（10 分钟）。"""
    name = body.name.strip()
    conn = _db()
    try:
        row = conn.execute("SELECT id, sec_hash, sec_salt FROM account WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if not row or not row[1]:
        raise HTTPException(400, "这个账户没设密保。")
    if not hmac.compare_digest(row[1], _hash_pw(body.sec_a.strip().lower(), row[2])):
        raise HTTPException(401, "答案不对。")
    # 一次性令牌 = HMAC(uid.exp.nonce)；nonce 存内存（壳阶段单进程够用，重启即失效=更安全）
    nonce = secrets.token_hex(8)
    exp = int(time.time()) + _RESET_TTL
    msg = f"reset.{row[0]}.{exp}.{nonce}".encode()
    sig = hmac.new(_secret(), msg, hashlib.sha256).hexdigest()[:24]
    _RESET_NONCES[nonce] = exp
    return {"ok": True, "token": f"{exp}.{nonce}.{sig}", "有效秒": _RESET_TTL}


_RESET_NONCES: dict[str, int] = {}


@router.post("/reset/confirm")
def reset_confirm(body: _ResetDo) -> dict:
    """第三步：令牌 + 新密码 → 落库。令牌一次性用废。"""
    name = body.name.strip()
    uid = _uid_by_name(name)
    if uid is None:
        raise HTTPException(404, "没有这个账户。")
    try:
        exp_s, nonce, sig = body.token.split(".")
        exp = int(exp_s)
    except Exception:
        raise HTTPException(400, "令牌格式不对。")
    msg = f"reset.{uid}.{exp}.{nonce}".encode()
    want = hmac.new(_secret(), msg, hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, want):
        raise HTTPException(401, "令牌不对。")
    if exp < int(time.time()):
        raise HTTPException(401, "令牌过期了，重新走一遍忘记密码。")
    if _RESET_NONCES.get(nonce) != exp:
        raise HTTPException(401, "令牌已用过。")
    if len(body.new_password) < 4:
        raise HTTPException(400, "密码至少 4 位。")
    del _RESET_NONCES[nonce]                     # 一次性
    salt = secrets.token_bytes(16)
    h = _hash_pw(body.new_password, salt)
    conn = _db()
    try:
        conn.execute("UPDATE account SET pwhash=?, salt=? WHERE id=?", (h, salt, uid))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "跳转": "登录"}


@router.post("/remember")
def remember_issue(body: _Auth, response: Response) -> dict:
    """「记住密码」的续签令牌：登录成功后前端若勾了记住，再调这个换 remember cookie。
    令牌 = HMAC(uid.remember.exp)，前端存 localStorage 的混淆凭据只作回填提示；
    服务端不存明文密码。"""
    conn = _db()
    try:
        row = conn.execute("SELECT id, pwhash, salt FROM account WHERE name=?",
                           (body.name.strip(),)).fetchone()
    finally:
        conn.close()
    if not row or not hmac.compare_digest(row[1], _hash_pw(body.password, row[2])):
        raise HTTPException(401, "用户名或密码不对。")
    exp = int(time.time()) + 90 * 24 * 3600      # 90 天
    response.set_cookie(_REMEMBER, _sign(row[0], exp), max_age=90 * 24 * 3600,
                        httponly=True, samesite="lax")
    return {"ok": True, "name": body.name.strip()}


@router.get("/remember/check")
def remember_check(loom_remember: str | None = None) -> dict:
    """进登录页时问一声：有没有有效的记住令牌。有→回账户名（前端自动填）。"""
    uid = current_uid(loom_remember) if loom_remember else None
    if uid is None:
        return {"ok": False, "name": ""}
    conn = _db()
    try:
        row = conn.execute("SELECT name FROM account WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    return {"ok": bool(row), "name": row[0] if row else ""}
