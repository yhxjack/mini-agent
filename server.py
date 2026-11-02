#!/usr/bin/env python3
"""Agent-Web: FastAPI 服务。账号鉴权 + 按用户隔离 + 工作区/多会话 + SSE 流式。"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from agent_core import SYSTEM_PROMPT, ExecEnv, make_client, stream_agent_turn

HERE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = "/workspace/.agent_sessions"
DB = os.path.join(SESSIONS_DIR, "app.db")
TITLE_MODEL = os.environ.get("TITLE_MODEL") or os.environ.get("AGENT_MODEL") or "gpt-4o-mini"

client = make_client()   # 启动即校验 MODEL_API_KEY
app = FastAPI()


def _safe_id(x, kind="id"):
    if not x or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", x):
        raise HTTPException(status_code=400, detail=f"invalid {kind}")
    return x


def _nid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:12]


# ---------- SQLite ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200000).hex()


def _alloc_os_uid(c):
    return c.execute("SELECT COALESCE(MAX(os_uid),19999)+1 AS n FROM users").fetchone()["n"]


def _os_uid_of(uid):
    with db() as c:
        r = c.execute("SELECT os_uid FROM users WHERE uid=?", (uid,)).fetchone()
    return r["os_uid"] if r else None


def _ensure_userdir(uid, os_uid):
    d = os.path.join("/workspace/.userdata", _safe_id(uid, "uid"))
    os.makedirs(d, exist_ok=True)
    if os_uid:
        os.chown(d, os_uid, os_uid)
        os.chmod(d, 0o700)
    return d


def init_db():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            uid TEXT PRIMARY KEY, username TEXT UNIQUE, salt TEXT, hash TEXT,
            created REAL, is_admin INTEGER DEFAULT 0, os_uid INTEGER);
        CREATE TABLE IF NOT EXISTS tokens(
            token TEXT PRIMARY KEY, uid TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS workspaces(
            id TEXT PRIMARY KEY, uid TEXT, name TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS sessions(
            id TEXT PRIMARY KEY, wid TEXT, uid TEXT, title TEXT,
            created REAL, updated REAL, messages TEXT);
        """)
        # 迁移:老库补 os_uid 列
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "os_uid" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN os_uid INTEGER")
        au = os.environ.get("ADMIN_USER") or "admin"
        ap = os.environ.get("ADMIN_PASSWORD") or "admin@123"
        if not c.execute("SELECT 1 FROM users WHERE username=?", (au,)).fetchone():
            salt = secrets.token_hex(16)
            c.execute("INSERT INTO users(uid,username,salt,hash,created,is_admin,os_uid) "
                      "VALUES(?,?,?,?,?,1,?)",
                      (_nid("u"), au, salt, _hash(ap, salt), time.time(), _alloc_os_uid(c)))
        # 回填历史用户 os_uid
        for r in c.execute("SELECT uid FROM users WHERE os_uid IS NULL").fetchall():
            c.execute("UPDATE users SET os_uid=? WHERE uid=?",
                      (_alloc_os_uid(c), r["uid"]))


init_db()
# <!-- A1 -->


# ---------- 认证 ----------
class Cred(BaseModel):
    username: str = ""
    password: str = ""


def require_user(request: Request):
    tok = request.cookies.get("session") or ""
    with db() as c:
        row = c.execute("SELECT uid FROM tokens WHERE token=?", (tok,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="未登录")
    return row["uid"]


def _issue(uid, username, response):
    tok = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("INSERT INTO tokens VALUES(?,?,?)", (tok, uid, time.time()))
    response.set_cookie("session", tok, httponly=True, samesite="lax", path="/")
    return {"username": username}


@app.post("/api/register")
def register(body: Cred, response: Response):
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", body.username or ""):
        raise HTTPException(400, "用户名需3-32位字母数字下划线")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少6位")
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
            raise HTTPException(409, "用户名已存在")
        salt = secrets.token_hex(16)
        uid = _nid("u")
        c.execute("INSERT INTO users(uid,username,salt,hash,created,is_admin,os_uid) "
                  "VALUES(?,?,?,?,?,0,?)",
                  (uid, body.username, salt, _hash(body.password, salt),
                   time.time(), _alloc_os_uid(c)))
    return _issue(uid, body.username, response)


@app.post("/api/login")
def login(body: Cred, response: Response):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    if not u or not hmac.compare_digest(u["hash"], _hash(body.password or "", u["salt"])):
        raise HTTPException(401, "用户名或密码错误")
    return _issue(u["uid"], u["username"], response)


@app.post("/api/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get("session") or ""
    with db() as c:
        c.execute("DELETE FROM tokens WHERE token=?", (tok,))
    response.delete_cookie("session", path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    tok = request.cookies.get("session") or ""
    with db() as c:
        row = c.execute(
            "SELECT u.username FROM tokens t JOIN users u ON t.uid=u.uid WHERE t.token=?",
            (tok,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="未登录")
    return {"username": row["username"]}
# <!-- A2 -->


# ---------- 会话/工作区存储(按 uid 隔离) ----------
def load_index(uid):
    with db() as c:
        ws = c.execute(
            "SELECT id,name,created FROM workspaces WHERE uid=? ORDER BY created",
            (uid,)).fetchall()
        out = []
        for w in ws:
            ss = c.execute(
                "SELECT id,title,created,updated FROM sessions WHERE uid=? AND wid=?",
                (uid, w["id"])).fetchall()
            out.append({"id": w["id"], "name": w["name"], "created": w["created"],
                        "sessions": [dict(s) for s in ss]})
    return {"workspaces": out}


def load_session(uid, sid):
    _safe_id(sid)
    with db() as c:
        row = c.execute("SELECT messages FROM sessions WHERE uid=? AND id=?",
                        (uid, sid)).fetchone()
    if row and row["messages"]:
        try:
            return json.loads(row["messages"])
        except Exception:
            pass
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def save_session(uid, sid, messages):
    with db() as c:
        c.execute("UPDATE sessions SET messages=?, updated=? WHERE uid=? AND id=?",
                  (json.dumps(messages, ensure_ascii=False), time.time(), uid, sid))


def _gen_title(first_msg):
    try:
        r = client.chat.completions.create(
            model=TITLE_MODEL, max_tokens=24,
            messages=[{"role": "user", "content":
                "用不超过12个字概括下面这个任务,只返回标题本身,不要引号或标点:\n\n"
                + first_msg[:500]}])
        t = (r.choices[0].message.content or "").strip().strip('"\'「」')
        return t[:20] or "未命名"
    except Exception:
        return first_msg.strip()[:20] or "未命名"


def touch_session(uid, sid, first_msg):
    with db() as c:
        row = c.execute("SELECT title FROM sessions WHERE uid=? AND id=?",
                        (uid, sid)).fetchone()
        if not row:
            return
        title = row["title"]
        if title in (None, "", "未命名") and first_msg:
            title = _gen_title(first_msg)
        c.execute("UPDATE sessions SET updated=?, title=? WHERE uid=? AND id=?",
                  (time.time(), title, uid, sid))


# ---------- 命令审批 ----------
PENDING = {}
SAFE = {"ls", "cat", "pwd", "wc", "head", "tail", "grep", "find", "stat",
        "echo", "date", "whoami", "env", "tree", "du", "df", "git"}
BAD = set(";|&><`$()\n")


def _is_safe_cmd(cmd):
    if any(c in BAD for c in cmd):
        return False
    head = cmd.strip().split()[:1]
    return bool(head) and head[0] in SAFE


class WebApprover:
    def needs(self, name, args):
        return name == "run_command" and not _is_safe_cmd(args.get("command", ""))

    def open(self, name, args):
        rid = _nid("ap")
        PENDING[rid] = {"ev": threading.Event(), "ok": False}
        return rid

    def wait(self, rid, timeout=300):
        p = PENDING.get(rid)
        if not p:
            return False
        got = p["ev"].wait(timeout)
        ok = p["ok"] if got else False
        PENDING.pop(rid, None)
        return ok
# <!-- A3 -->


# ---------- 接口 ----------
class WsIn(BaseModel):
    name: str = "新工作区"


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/api/workspaces")
def get_workspaces(uid: str = Depends(require_user)):
    return load_index(uid)


@app.post("/api/workspaces")
def new_workspace(body: WsIn, uid: str = Depends(require_user)):
    wid = _nid("w")
    with db() as c:
        c.execute("INSERT INTO workspaces VALUES(?,?,?,?)",
                  (wid, uid, (body.name or "新工作区")[:40], time.time()))
    return {"id": wid}


@app.post("/api/workspaces/{wid}/sessions")
def new_session(wid: str, uid: str = Depends(require_user)):
    _safe_id(wid, "workspace_id")
    with db() as c:
        if not c.execute("SELECT 1 FROM workspaces WHERE uid=? AND id=?",
                         (uid, wid)).fetchone():
            raise HTTPException(status_code=404, detail="workspace not found")
        now = time.time()
        sid = _nid("s")
        c.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                  (sid, wid, uid, "未命名", now, now, None))
    return {"session_id": sid}


@app.get("/api/session/{sid}")
def get_session(sid: str, uid: str = Depends(require_user)):
    return JSONResponse(load_session(uid, sid))


@app.post("/api/approve")
def approve(request_id: str, decision: str, uid: str = Depends(require_user)):
    p = PENDING.get(request_id)
    if not p:
        raise HTTPException(status_code=404, detail="no such request")
    p["ok"] = (decision == "allow")
    p["ev"].set()
    return {"ok": True}


@app.get("/api/chat")
def chat(session_id: str, message: str, uid: str = Depends(require_user)):
    sid = _safe_id(session_id)
    ou = _os_uid_of(uid)
    env = ExecEnv(_ensure_userdir(uid, ou), ou)
    messages = load_session(uid, sid)
    messages.append({"role": "user", "content": message})

    def gen():
        try:
            for ev in stream_agent_turn(client, messages,
                                        approver=WebApprover(), env=env):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            save_session(uid, sid, messages)
            touch_session(uid, sid, message)
        except Exception as e:
            err = {"type": "error", "data": f"{type(e).__name__}: {e}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        yield 'data: {"type": "done"}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream")


