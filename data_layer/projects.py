#!/usr/bin/env python3
"""项目与团队协同鉴权层：多 Agent 共享同一项目底稿知识库。

模型：
  projects          项目 = 团队 × 企业（owner_email 创建者，invite_code 邀请码）
  project_members   成员 = 邮箱（对应 FateBuddy 账号），owner / member 两角色
  sources.project_id 上传底稿归属项目；平台抓取数据 project_id IS NULL

鉴权规则（页面与 MCP 同一套）：
  - 平台抓取数据（PAT-ZHY / FIN-AKS / COR-TYC…）对所有登录用户可见
  - 上传底稿（provider='upload'）只对本项目成员的邮箱可见
  - WeKnora 检索白名单 = 可见 Source_ID 集合，跨项目不串库
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  project_id   TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  company_key  TEXT NOT NULL,
  company_name TEXT,
  owner_email  TEXT NOT NULL,
  invite_code  TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_company ON projects(company_key);
CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_email);
CREATE TABLE IF NOT EXISTS project_members(
  project_id TEXT NOT NULL,
  email      TEXT NOT NULL,
  role       TEXT NOT NULL DEFAULT 'member',
  added_at   TEXT NOT NULL,
  PRIMARY KEY(project_id, email)
);
CREATE INDEX IF NOT EXISTS idx_pm_email ON project_members(email);
"""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection):
    has_col = False
    for r in conn.execute("PRAGMA table_info(sources)"):
        if r[1] == "project_id":
            has_col = True
            break
    if not has_col:
        conn.execute("ALTER TABLE sources ADD COLUMN project_id TEXT")
    conn.executescript(SCHEMA)
    conn.executescript(WK_SCHEMA)
    conn.commit()


def create_project(conn, *, name: str, company_key: str, company_name: str,
                   owner_email: str) -> dict:
    ensure_schema(conn)
    pid = "PRJ-" + secrets.token_hex(5)
    code = "tc-" + secrets.token_hex(4)
    ts = _now()
    conn.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?)",
                 (pid, name, company_key, company_name, owner_email, code, ts))
    conn.execute("INSERT INTO project_members VALUES(?,?,?,?)",
                 (pid, owner_email, "owner", ts))
    conn.commit()
    return {"project_id": pid, "name": name, "company_key": company_key,
            "company_name": company_name, "owner_email": owner_email,
            "invite_code": code, "created_at": ts}


def get_project(conn, project_id: str):
    ensure_schema(conn)
    return conn.execute("SELECT * FROM projects WHERE project_id=?",
                        (project_id,)).fetchone()


def my_projects(conn, email: str) -> list:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT p.*, pm.role FROM projects p JOIN project_members pm "
        "ON pm.project_id=p.project_id WHERE pm.email=? ORDER BY p.created_at DESC",
        (email,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["members"] = [dict(m) for m in conn.execute(
            "SELECT email, role, added_at FROM project_members "
            "WHERE project_id=? ORDER BY added_at", (r["project_id"],))]
        d["n_sources"] = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE project_id=?",
            (r["project_id"],)).fetchone()[0]
        out.append(d)
    return out


def is_member(conn, project_id: str, email: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM project_members WHERE project_id=? AND email=?",
        (project_id, email)).fetchone() is not None


def add_member(conn, project_id: str, email: str, actor_email: str) -> tuple:
    """仅 owner 可加人；幂等。返回 (ok, msg)。"""
    ensure_schema(conn)
    p = get_project(conn, project_id)
    if not p:
        return False, "项目不存在"
    if p["owner_email"] != actor_email:
        return False, "仅项目负责人可添加成员"
    if is_member(conn, project_id, email):
        return True, "已是成员"
    conn.execute("INSERT INTO project_members VALUES(?,?,?,?)",
                 (project_id, email, "member", _now()))
    conn.commit()
    return True, f"已添加成员 {email}"


# ── 项目伴随设施：一个项目一个记忆服务、一个知识库 ──────────────

def kb_name(project_id: str) -> str:
    """项目专属 WeKnora 知识库名（团队上传底稿的物理隔离容器）。"""
    return f"techcredit-prj-{project_id}"


def memory_service(project_id: str) -> str:
    """项目专属记忆服务 ID（Memory Core service 级隔离，跨项目不可见）。"""
    return f"tc-prj-{project_id}"


def project_kbs(conn, company_key: str, viewer_email: str) -> list:
    """viewer 在该企业下可访问的项目专属知识库名列表。"""
    ensure_schema(conn)
    return [kb_name(r[0]) for r in conn.execute(
        "SELECT DISTINCT p.project_id FROM projects p "
        "JOIN project_members pm ON pm.project_id=p.project_id "
        "WHERE p.company_key=? AND pm.email=?", (company_key, viewer_email))]


# ── 可见性 ─────────────────────────────────────────────
VISIBLE_SQL = ("(provider!='upload' OR project_id IS NULL OR project_id IN "
               "(SELECT project_id FROM project_members WHERE email=?))")


def visible_source_ids(conn, company_key: str, viewer_email: str | None) -> set:
    """该企业下 viewer 可见的 Source_ID 集合（viewer=None 表示内部全量）。"""
    if viewer_email is None:
        return {r[0] for r in conn.execute(
            "SELECT source_id FROM sources WHERE company_key=?", (company_key,))}
    return {r[0] for r in conn.execute(
        "SELECT source_id FROM sources WHERE company_key=? AND " + VISIBLE_SQL,
        (company_key, viewer_email))}


def run_visible(conn, run_row, email: str) -> bool:
    """运行结果可见性：平台运行（viewer为空）对全员可见；
    团队运行仅对运行者本人、或项目作用域 ⊆ 本人项目的成员可见。"""
    import json as _json
    viewer = run_row["viewer_email"] if "viewer_email" in run_row.keys() else None
    if not viewer or viewer == email:
        return True
    raw = run_row["project_ids_json"] if "project_ids_json" in run_row.keys() else None
    pids = _json.loads(raw or "[]")
    if not pids:
        return True
    mine = {r[0] for r in conn.execute(
        "SELECT project_id FROM project_members WHERE email=?", (email,))}
    return set(pids) <= mine


def latest_visible_run(conn, company_key: str, email: str):
    """按创建时间倒序返回该企业下 caller 可见的最近一次运行（或 None）。"""
    for r in conn.execute(
            "SELECT * FROM evaluation_runs WHERE company_key=? ORDER BY created_at DESC",
            (company_key,)):
        if run_visible(conn, r, email):
            return r
    return None


# ── 项目级 WeKnora 工作区：一个项目 = 一个独立租户（硬隔离） ──────────────
# 服务账号 prj-{pid}@techcredit.local 持有项目租户；管线密钥仅限该租户。
# 平台公共库留在管理员租户；桥接用户在公共库为 viewer、在项目库为 contributor。

WK_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_weknora(
  project_id   TEXT PRIMARY KEY,
  tenant_id    INTEGER NOT NULL,
  api_key      TEXT NOT NULL,
  svc_email    TEXT NOT NULL,
  svc_password TEXT NOT NULL,
  kb_id        TEXT,
  created_at   TEXT NOT NULL
);
"""

WK_BASE = "http://127.0.0.1:8080/api/v1"
WK_CAPS = ["ingest", "retrieve", "manage_kbs", "manage_models",
           "manage_members", "manage_vector_stores", "manage_tenant_settings"]


def get_project_weknora(conn, project_id: str):
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM project_weknora WHERE project_id=?",
                       (project_id,)).fetchone()
    return dict(row) if row else None


def set_project_kb_id(conn, project_id: str, kb_id: str):
    conn.execute("UPDATE project_weknora SET kb_id=? WHERE project_id=?",
                 (kb_id, project_id))
    conn.commit()


def ensure_weknora_workspace(conn, project_id: str) -> dict:
    """幂等：为项目创建独立 WeKnora 工作区（服务账号+租户+受限密钥）。"""
    existing = get_project_weknora(conn, project_id)
    if existing:
        return existing
    import requests
    p = get_project(conn, project_id)
    if not p:
        raise ValueError(f"项目不存在: {project_id}")
    svc_email = f"prj-{project_id.lower()}@techcredit.local"
    pw = secrets.token_urlsafe(24)
    r = requests.post(WK_BASE + "/auth/register", timeout=20,
                      json={"email": svc_email,
                            "username": f"prj-{project_id}"[:40], "password": pw}).json()
    if not r.get("success"):
        raise RuntimeError(f"项目工作区账号创建失败: {str(r)[:200]}")
    tid = r["user"]["tenant_id"]
    lg = requests.post(WK_BASE + "/auth/login", timeout=15,
                       json={"email": svc_email, "password": pw}).json()
    tok = lg.get("token")
    if not tok:
        raise RuntimeError(f"项目工作区登录失败: {str(lg)[:200]}")
    hdr = {"Authorization": "Bearer " + tok}
    try:  # 工作区命名（失败不阻塞）
        requests.put(WK_BASE + f"/tenants/{tid}", headers=hdr, timeout=10,
                     json={"name": f"{p['name']}｜{p['company_name'] or p['company_key']}"})
    except Exception:
        pass
    kr = requests.post(WK_BASE + f"/tenants/{tid}/api-keys", headers=hdr, timeout=15,
                       json={"name": "techcredit-pipeline", "capabilities": WK_CAPS}).json()
    api_key = (kr.get("data") or {}).get("token") or ""
    if not api_key:
        raise RuntimeError(f"项目API密钥创建失败: {str(kr)[:200]}")
    conn.execute("INSERT OR REPLACE INTO project_weknora VALUES(?,?,?,?,?,?,?)",
                 (project_id, tid, api_key, svc_email, pw, None, _now()))
    conn.commit()
    return get_project_weknora(conn, project_id)
