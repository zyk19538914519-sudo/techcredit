#!/usr/bin/env python3
"""techcredit 登录/MCP 链路自测：种子一个显式标记的测试账户 → 输出会话Cookie与接入码 → --cleanup 删除。
ACCOUNT_SECRET 仅在本机读取，不外泄。测试行带 selftest 标记，清理按标记精确删除。"""
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
import time
from pathlib import Path

DB = "/root/.hermes/agent_api.db"
ENV = Path("/root/.hermes/facade/accounts.env")
EMAIL = "techcredit-selftest@local.test"
KEY = "XJ-TC-SELFTEST-0001"
PWD = "TcSelftest2026x"
TTL = 72 * 3600


def hash_pwd(pwd):
    """与 accounts.hash_password 同格式：scrypt$n$r$p$salt_b64$dk_b64。"""
    salt = __import__("os").urandom(16)
    dk = hashlib.scrypt(pwd.encode(), salt=salt, n=2 ** 14, r=8, p=1,
                        dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(dk).decode())


def secret():
    # 1) 运行中 facade 进程的环境变量（与线上签名完全一致）
    import glob
    import subprocess
    try:
        pids = subprocess.run(["pgrep", "-f", "card_admin"], capture_output=True,
                              text=True).stdout.split()
        for pid in pids:
            try:
                env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            except Exception:
                continue
            for item in env:
                if item.startswith(b"ACCOUNT_SECRET="):
                    val = item.split(b"=", 1)[1].decode().strip()
                    if val:
                        return val
    except Exception:
        pass
    # 2) env 文件显式配置
    for line in ENV.read_text().splitlines():
        if line.startswith("ACCOUNT_SECRET="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                return val
    # 3) accounts.configure 的回退派生：启动时 DB 前 64KB 的 sha256（种子前快照）
    return _DERIVED_AT_START


_DERIVED_AT_START = hashlib.sha256(Path(DB).read_bytes()[:65536]).hexdigest()


def cookie(uid):
    sec = secret().encode()
    exp = int(time.time()) + TTL
    sig = hmac.new(sec, f"{uid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{uid}:{exp}:{sig}".encode()).decode()


def seed():
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn = sqlite3.connect(DB, timeout=15)
    conn.execute("DELETE FROM user_keys WHERE key=?", (KEY,))
    conn.execute("DELETE FROM keys WHERE key=?", (KEY,))
    conn.execute("DELETE FROM users WHERE email=?", (EMAIL,))
    cur = conn.execute(
        "INSERT INTO users (email,password_hash,status,email_verified,created_at,invite_code,password_set_at) "
        "VALUES (?,?,?,1,?,?,?)", (EMAIL, hash_pwd(PWD), "active", now, "techcredit-selftest", now))
    uid = cur.lastrowid
    conn.execute("INSERT INTO keys (key,remaining,used,status,card_type,expires_at,tier,created_at,brand) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (KEY, 999, 0, "active", "selftest", "", "free", now, "fatebuddy"))
    conn.execute("INSERT INTO user_keys (key,user_id,bound_at,source) VALUES (?,?,?,?)",
                 (KEY, uid, now, "techcredit-selftest"))
    conn.commit()
    conn.close()
    # 走 FateBuddy 真实密码登录接口拿会话 Cookie（不伪造签名）
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:8750/api/signin/password",
        data=json.dumps({"email": EMAIL, "password": PWD}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
        setc = resp.headers.get_all("Set-Cookie") or []
    ck = ""
    for c in setc:
        if c.startswith("fb_session="):
            ck = c.split(";", 1)[0].split("=", 1)[1]
    print(json.dumps({"ok": body.get("ok"), "cookie": ck, "key": KEY,
                      "uid": uid, "email": EMAIL, "password": PWD}, ensure_ascii=False))


def cleanup():
    conn = sqlite3.connect(DB, timeout=15)
    n1 = conn.execute("DELETE FROM user_keys WHERE key=?", (KEY,)).rowcount
    n2 = conn.execute("DELETE FROM keys WHERE key=?", (KEY,)).rowcount
    n3 = conn.execute("DELETE FROM users WHERE email=?", (EMAIL,)).rowcount
    conn.commit()
    conn.close()
    for f in ("/tmp/tc_selftest.json", "/tmp/tc_headers.txt", "/tmp/tc_signin.json"):
        Path(f).unlink(missing_ok=True)
    print(json.dumps({"deleted": {"user_keys": n1, "keys": n2, "users": n3}}))


if __name__ == "__main__":
    cleanup() if "--cleanup" in sys.argv else seed()