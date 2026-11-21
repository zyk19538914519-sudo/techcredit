#!/usr/bin/env python3
"""为 techcredit 数据层创建 WeKnora 空间级 API Key 并更新配置。

用服务器数据库中有效的 Owner 会话 JWT 调 POST /api/v1/tenants/:id/api-keys；
新 Key 只写入 weknora-mcp.env（先备份），不打印明文。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8080"
ENV_FILE = Path("/root/weknora-mcp/weknora-mcp.env")


def psql(query: str) -> str:
    out = subprocess.run(
        ["docker", "exec", "WeKnora-postgres", "psql", "-U", "postgres",
         "-d", "WeKnora", "-tAc", query],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if out.returncode != 0:
        raise SystemExit("psql 失败: " + out.stderr.decode()[:200])
    return out.stdout.decode().strip()


def main():
    # 选持有有效 access_token 的最近登录活跃用户
    row = psql("SELECT u.id, u.username, u.tenant_id, u.is_system_admin FROM users u "
               "JOIN auth_tokens t ON t.user_id = u.id "
               "WHERE t.token_type='access_token' AND t.is_revoked=false "
               "AND t.expires_at > now() AND u.is_active=true AND u.deleted_at IS NULL "
               "ORDER BY t.created_at DESC LIMIT 1;")
    if not row:
        raise SystemExit("无持有有效会话的用户，请先在 WeKnora 前端登录")
    uid, uname, tenant_id, is_admin = row.split("|")
    print(f"使用账号: {uname} (tenant={tenant_id}, admin={is_admin})")
    token = psql(f"SELECT token FROM auth_tokens WHERE user_id='{uid}' "
                 f"AND token_type='access_token' AND is_revoked=false "
                 f"AND expires_at > now() ORDER BY created_at DESC LIMIT 1;")
    if not token:
        raise SystemExit("该用户无有效 access_token，请重新登录前端")

    body = json.dumps({"name": "techcredit-pipeline", "full_access": True}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/v1/tenants/{tenant_id}/api-keys", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"创建 API Key 失败 HTTP {exc.code}: {exc.read().decode()[:300]}")
    data = result.get("data") or {}
    # 明文在 token 字段；api_key 字段可能是 enc:v1 加密存储格式，不可用作凭证
    candidates = [data.get("token"), data.get("api_key")]
    api_key = next((v for v in candidates
                    if isinstance(v, str) and v and not v.startswith("enc:")), None)
    if not api_key:
        raise SystemExit(f"响应无明文 key: {str(result)[:300]}")

    bak = Path(str(ENV_FILE) + ".bak-techcredit")
    if not bak.exists():
        shutil.copy2(ENV_FILE, bak)
    lines = ENV_FILE.read_text().splitlines()
    out, replaced = [], False
    for line in lines:
        if line.startswith("WEKNORA_API_KEY="):
            out.append("WEKNORA_API_KEY=" + api_key)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append("WEKNORA_API_KEY=" + api_key)
    ENV_FILE.write_text("\n".join(out) + "\n")
    print(f"API Key 已创建（id={data.get('id')}）并写入 {ENV_FILE}（旧文件已备份），明文不回显")


if __name__ == "__main__":
    main()