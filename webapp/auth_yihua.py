#!/usr/bin/env python3
"""一画专利评估分析系统 · 独立账号体系（不依赖 FateBuddy）。

与 FateBuddy 账户层同构但完全独立：自有 SQLite 库、自有会话密钥、自有接入码签发。
  注册 POST /api/auth/signup   邮箱+密码（强度校验），成功即签发接入码并登录
  登录 POST /api/auth/signin   邮箱+密码，支持 next 回跳（限 /techcredit 前缀）
  退出 POST /api/auth/logout
  页面 GET /signup /signin /account（账户页展示接入码+MCP配置+复制）
会话 = HMAC-SHA256 签名 Cookie（HttpOnly/Secure/Lax，默认72h），密钥存 yihua_auth.env。
密码只存 scrypt 派生值；登录失败按 IP+邮箱 限流；注册按 IP 限流。
邮箱验证码通道预留：YH_VERIFY_MODE=auto 时启用（需先配好发信），默认 none=密码注册。
接入码 remaining=-1 表示不限额；MCP 侧只读校验、不扣减。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "yihua_auth.env"
DB_PATH = config.DATA_DIR / "yihua_accounts.db"

_CFG = {"secret": "", "cookie": "yh_session", "ttl_hours": 72, "verify_mode": "none"}


def _load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "YH_SECRET":
                _CFG["secret"] = v
            elif k == "YH_COOKIE":
                _CFG["cookie"] = v
            elif k == "YH_TTL_HOURS":
                _CFG["ttl_hours"] = int(v)
            elif k == "YH_VERIFY_MODE":
                _CFG["verify_mode"] = v
    if not _CFG["secret"]:
        _CFG["secret"] = secrets.token_hex(32)
        ENV_FILE.write_text(
            "# 一画账号体系会话密钥（独立于 FateBuddy，勿外泄）\n"
            f"YH_SECRET={_CFG['secret']}\nYH_COOKIE={_CFG['cookie']}\n"
            f"YH_TTL_HOURS={_CFG['ttl_hours']}\nYH_VERIFY_MODE={_CFG['verify_mode']}\n",
            encoding="utf-8")
        try:
            os.chmod(ENV_FILE, 0o600)
        except Exception:
            pass


_load_env()

router = APIRouter()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c


def ensure_schema():
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        email_verified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        last_login TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS keys(
        key TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        remaining INTEGER NOT NULL DEFAULT -1,
        used INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        expires_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'signup');
    CREATE INDEX IF NOT EXISTS idx_yh_keys_uid ON keys(user_id);
    CREATE TABLE IF NOT EXISTS auth_hits(k TEXT PRIMARY KEY, n INTEGER, win INTEGER);
    """)
    c.commit()
    c.close()


ensure_schema()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── 密码（scrypt，与 FateBuddy 同格式但独立存储）─────────────────
def hash_password(pwd: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pwd.encode(), salt=salt, n=2 ** 14, r=8, p=1,
                        dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(dk).decode())


def verify_password(pwd: str, stored: str) -> bool:
    if not stored or not pwd:
        return False
    try:
        parts = stored.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.urlsafe_b64decode(parts[4].encode())
        want = base64.urlsafe_b64decode(parts[5].encode())
        got = hashlib.scrypt(pwd.encode(), salt=salt, n=n, r=r, p=p,
                             dklen=len(want), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def check_password(pwd: str):
    if not pwd or len(pwd) < 8:
        return False, "密码至少 8 位"
    if len(pwd) > 128:
        return False, "密码过长"
    if not re.search(r"[A-Za-z]", pwd) or not re.search(r"\d", pwd):
        return False, "密码需包含字母和数字"
    return True, ""


# ── 会话 ──────────────────────────────────────────────────────
def _sign(uid: int, exp: int) -> str:
    raw = f"{uid}:{exp}"
    mac = hmac.new(_CFG["secret"].encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{raw}:{mac}".encode()).decode()


def _verify_tok(tok: str):
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        uid_s, exp_s, mac = raw.split(":")
        want = hmac.new(_CFG["secret"].encode(), f"{uid_s}:{exp_s}".encode(),
                        hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, want) or int(exp_s) < int(time.time()):
            return None
        return int(uid_s)
    except Exception:
        return None


def current_user(request: Request):
    tok = request.cookies.get(_CFG["cookie"], "")
    uid = _verify_tok(tok) if tok else None
    if not uid:
        return None
    c = _conn()
    r = c.execute("SELECT id,email,status,email_verified,created_at FROM users WHERE id=?",
                  (uid,)).fetchone()
    c.close()
    if not r or r["status"] != "active":
        return None
    return dict(r)


def active_key(uid: int):
    c = _conn()
    r = c.execute("SELECT * FROM keys WHERE user_id=? AND status='active' "
                  "AND (expires_at='' OR expires_at>?) ORDER BY created_at DESC LIMIT 1",
                  (uid, _now())).fetchone()
    c.close()
    return dict(r) if r else None


def _set_cookie(resp, uid: int):
    exp = int(time.time()) + _CFG["ttl_hours"] * 3600
    resp.set_cookie(_CFG["cookie"], _sign(uid, exp), max_age=_CFG["ttl_hours"] * 3600,
                    httponly=True, samesite="lax", secure=True, path="/")


def _rate(key: str, limit: int, window: int = 600) -> bool:
    now = int(time.time())
    c = _conn()
    r = c.execute("SELECT n,win FROM auth_hits WHERE k=?", (key,)).fetchone()
    if not r or now - r["win"] > window:
        c.execute("INSERT OR REPLACE INTO auth_hits VALUES(?,?,?)", (key, 1, now))
        c.commit()
        c.close()
        return True
    if r["n"] >= limit:
        c.close()
        return False
    c.execute("UPDATE auth_hits SET n=n+1 WHERE k=?", (key,))
    c.commit()
    c.close()
    return True


def _safe_next(nxt: str) -> str:
    nxt = (nxt or "").strip()
    return nxt if nxt.startswith("/techcredit") else "/techcredit/account"


def _ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return (xff.split(",")[-1].strip() if xff else "") or (request.client.host if request.client else "?")


# ── 接口 ──────────────────────────────────────────────────────
@router.post("/api/auth/signup")
async def api_signup(request: Request):
    b = await request.json()
    email = (b.get("email") or "").strip().lower()
    pwd = b.get("password") or ""
    nxt = _safe_next(b.get("next"))
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return JSONResponse({"ok": False, "error": "邮箱格式不正确"}, status_code=400)
    ok, why = check_password(pwd)
    if not ok:
        return JSONResponse({"ok": False, "error": why}, status_code=400)
    if not _rate("signup:" + _ip(request), 5):
        return JSONResponse({"ok": False, "error": "注册过于频繁，请稍后再试"}, status_code=429)
    c = _conn()
    if c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"ok": False, "error": "该邮箱已注册，请直接登录"}, status_code=409)
    cur = c.execute("INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)",
                    (email, hash_password(pwd), _now()))
    uid = cur.lastrowid
    key = "yh-" + secrets.token_urlsafe(18)
    c.execute("INSERT INTO keys(key,user_id,created_at) VALUES(?,?,?)", (key, uid, _now()))
    c.commit()
    c.close()
    resp = JSONResponse({"ok": True, "redirect": nxt, "key": key})
    _set_cookie(resp, uid)
    return resp


@router.post("/api/auth/signin")
async def api_signin(request: Request):
    b = await request.json()
    email = (b.get("email") or "").strip().lower()
    pwd = b.get("password") or ""
    nxt = _safe_next(b.get("next"))
    rl = "signin:" + _ip(request) + ":" + email
    if not _rate(rl, 10):
        return JSONResponse({"ok": False, "error": "尝试过于频繁，请 10 分钟后再试"}, status_code=429)
    c = _conn()
    r = c.execute("SELECT id,password_hash,status FROM users WHERE email=?", (email,)).fetchone()
    c.close()
    if not r or r["status"] != "active" or not verify_password(pwd, r["password_hash"]):
        return JSONResponse({"ok": False, "error": "邮箱或密码不正确"}, status_code=401)
    c = _conn()
    c.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), r["id"]))
    c.commit()
    c.close()
    resp = JSONResponse({"ok": True, "redirect": nxt})
    _set_cookie(resp, r["id"])
    return resp


@router.post("/api/auth/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_CFG["cookie"], path="/")
    return resp


# ── 页面 ──────────────────────────────────────────────────────
_CSS = """
body{margin:0;background:#0a1120;color:#dbe6f5;font:14px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{width:400px;max-width:92vw;background:#0d1729;border:1px solid #1d2c47;border-radius:14px;padding:28px}
h1{font-size:18px;margin:0 0 4px;color:#fff}
.sub{color:#7d8db0;font-size:12px;margin-bottom:18px}
label{display:block;font-size:12px;color:#9fb0d0;margin:12px 0 4px}
input{width:100%;box-sizing:border-box;background:#0a1120;border:1px solid #223354;color:#dbe6f5;
border-radius:8px;padding:10px 12px;font-size:14px;outline:none}
input:focus{border-color:#38bdf8}
button{width:100%;margin-top:18px;background:#2563eb;border:none;color:#fff;border-radius:8px;
padding:11px;font-size:14px;cursor:pointer}
button:hover{background:#1d4ed8}
button.ghost{background:none;border:1px solid #223354;color:#9fb0d0;margin-top:10px}
.err{color:#f87171;font-size:12px;margin-top:10px;min-height:16px}
a{color:#38bdf8;text-decoration:none;font-size:12px}
.row{display:flex;justify-content:space-between;margin-top:14px}
.kv{background:#0a1120;border:1px solid #223354;border-radius:8px;padding:10px 12px;margin-top:8px;
font-family:ui-monospace,Menlo,monospace;font-size:12px;word-break:break-all}
pre{background:#0a1120;border:1px solid #223354;border-radius:8px;padding:10px 12px;font-size:11px;
overflow:auto;max-height:180px}
.tag{display:inline-block;background:#12305a;color:#7dd3fc;border-radius:6px;padding:2px 8px;font-size:11px}
"""


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{_CSS}</style></head><body>{body}</body></html>")


@router.get("/signup", response_class=HTMLResponse)
def signup_page(next: str = ""):
    nxt = _safe_next(next)
    return _page("一画 · 注册", f"""
<div class=card>
<h1>◉ 一画专利评估分析系统</h1>
<div class=sub>创建账号 · 注册即签发详细分析 MCP 接入码</div>
<label>邮箱</label><input id=email type=email placeholder="you@company.com" autocomplete=email>
<label>密码（至少8位，含字母和数字）</label><input id=pwd type=password autocomplete=new-password>
<button id=go>注册并登录</button>
<div class=err id=err></div>
<div class=row><a href="signin?next={nxt}">已有账号 → 登录</a><a href="./">返回首页</a></div>
</div>
<script>
const NEXT={json.dumps(nxt)};
document.getElementById('go').onclick=async()=>{{
  const r=await fetch('api/auth/signup',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{email:document.getElementById('email').value,password:document.getElementById('pwd').value,next:NEXT}})}});
  const d=await r.json();
  if(d.ok)location.href=d.redirect;else document.getElementById('err').textContent=d.error||'注册失败';
}};
</script>""")


@router.get("/signin", response_class=HTMLResponse)
def signin_page(next: str = ""):
    nxt = _safe_next(next)
    return _page("一画 · 登录", f"""
<div class=card>
<h1>◉ 一画专利评估分析系统</h1>
<div class=sub>登录后可用详细分析与 MCP 接入配置</div>
<label>邮箱</label><input id=email type=email placeholder="you@company.com" autocomplete=email>
<label>密码</label><input id=pwd type=password autocomplete=current-password>
<button id=go>登录</button>
<div class=err id=err></div>
<div class=row><a href="signup?next={nxt}">创建账号 →</a><a href="./">返回首页</a></div>
</div>
<script>
const NEXT={json.dumps(nxt)};
document.getElementById('go').onclick=async()=>{{
  const r=await fetch('api/auth/signin',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{email:document.getElementById('email').value,password:document.getElementById('pwd').value,next:NEXT}})}});
  const d=await r.json();
  if(d.ok)location.href=d.redirect;else document.getElementById('err').textContent=d.error||'登录失败';
}};
document.getElementById('pwd').addEventListener('keydown',e=>{{if(e.key==='Enter')document.getElementById('go').click();}});
</script>""")


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    if not current_user(request):
        return HTMLResponse(_page("一画 · 登录", """
<div class=card><h1>◉ 一画专利评估分析系统</h1><div class=sub>请先登录</div>
<button onclick="location.href='signin?next=/techcredit/account'">去登录</button>
<div class=row><a href="signup?next=/techcredit/account">创建账号 →</a></div></div>"""))
    return _page("一画 · 账户", """
<div class=card>
<h1>◉ 一画专利评估分析系统</h1>
<div class=sub>账户与 MCP 接入</div>
<div id=info></div>
<label>接入码（MCP Bearer）</label><div class=kv id=key></div>
<button class=ghost id=ck>复制接入码</button>
<label>MCP 配置（粘进客户端即可）</label><pre id=cfg></pre>
<button class=ghost id=cc>复制 MCP 配置</button>
<label>MCP 端点</label><div class=kv id=url></div>
<label>安装指引（拿接入码 → 粘进 Agent）</label>
<div class=sub>1. 复制上方 MCP 配置（已含端点与 Bearer 接入码）；<br>
2. 打开你的 Agent 的 MCP / 连接器设置（豆包工作版、WorkBuddy、Claude Desktop、Cursor 等），整段粘贴；仅支持手填的客户端：类型 streamable-http、URL 填上方端点、请求头 Authorization 填 Bearer 接入码；<br>
3. 保存后开始对话，Agent 即可调用 search_companies / get_company_profile / analyze_company / get_latest_analysis 四个工具。接入码等同账户凭证，请妥善保管。</div>
<button id=lo style="background:#7f1d1d">退出登录</button>
<div class=row><a href="./">返回首页</a><span class=tag id=st></span></div>
</div>
<script>
async function load(){
  const me=await (await fetch('api/auth/me')).json();
  if(!me.logged_in){location.href='signin?next=/techcredit/account';return;}
  document.getElementById('info').innerHTML='<span class=tag>'+me.email+'</span>';
  document.getElementById('st').textContent='状态 有效';
  const mc=await (await fetch('api/mcp-config')).json();
  document.getElementById('key').textContent=mc.token||'（无有效接入码）';
  document.getElementById('cfg').textContent=JSON.stringify(mc.config,null,2);
  document.getElementById('url').textContent=mc.url;
  window._K=mc.token;window._C=JSON.stringify(mc.config,null,2);
}
function copy(s){navigator.clipboard.writeText(s).then(()=>alert('已复制')).catch(()=>prompt('手动复制：',s));}
document.getElementById('ck').onclick=()=>copy(window._K);
document.getElementById('cc').onclick=()=>copy(window._C);
document.getElementById('lo').onclick=async()=>{await fetch('api/auth/logout',{method:'POST'});location.href='./';};
load();
</script>""")