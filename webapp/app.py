#!/usr/bin/env python3
"""techcredit 科创企业雷达 Web 服务（FastAPI）。

首页地图+名单来自 screened_companies（免费数据粗筛，见 screening/screen_companies.py）；
点击企业 → 一键深度分析：复用 engine/run_engine.py（PQI+创新积分2.0+硬排雷+漏斗），
全部基于本地缓存与免费口径数据，不发起任何付费API调用。

部署：uvicorn app:app --host 127.0.0.1 --port 8090（nginx /techcredit/ 反代）。
"""
from __future__ import annotations

import json
import sqlite3
import mimetypes
mimetypes.add_type("font/woff2", ".woff2")

import sys
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "data_layer"))

import config            # noqa: E402
import innovation2       # noqa: E402
import run_engine        # noqa: E402
import dossier_pipeline  # noqa: E402
import projects          # noqa: E402

ANALYSIS_DIR = config.DATA_DIR / "analysis_results"
ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
CONTEXT_DIR = (config.DATA_DIR / "context").resolve()

JOBS: dict = {}
LOCK = threading.Lock()

IND_LABEL = {
    "RD_EXPENSE": ("研发费用金额", "万元", "创新投入"),
    "RD_RATIO": ("研发费用占营收比", "%", "创新投入"),
    "RD_STAFF_RATIO": ("研发人员占比", "%", "创新投入"),
    "INVENTION_GRANTS": ("发明专利授权量", "件", "创新产出"),
    "IP_PER_100_RD": ("每百人研发人员知识产权", "件", "创新产出"),
    "TECH_CONTRACT": ("技术合同成交额", "万元", "创新产出"),
    "MAIN_REVENUE": ("主营业务收入", "万元", "创新发展"),
    "REV_GROWTH": ("营业收入增长率", "%", "创新发展"),
    "ROE": ("净资产利润率", "%", "创新发展"),
}

app = FastAPI(title="科创企业雷达 · 创新积分2.0", root_path="/techcredit")

FB_BASE = "http://127.0.0.1:8750"          # FateBuddy 账户服务（复用其注册/登录/会话）
MCP_PUBLIC_URL = "https://fatebuddy.asia/techcredit-mcp/mcp"
MCP_TOOLS = ["search_companies", "get_company_profile", "analyze_company", "get_latest_analysis",
             "list_my_projects", "add_project_member", "search_project_facts",
             "project_memory_add", "project_memory_search", "upload_dossier", "get_context_pack",
             "submit_skill_outputs", "discover_prospects", "get_prospect_pack", "create_project",
             "screen_market", "screen_sme", "screen_sme_web", "screen_ipo", "risk_screen",
             "qualify_company", "deep_verify", "get_deep_verify", "persist_screening_result"]

# 一画双 MCP 拆分（2026-11-21）：调研 / 项目协作两个独立端点，旧端点保留全量兼容。
MCP_RESEARCH_URL = "https://fatebuddy.asia/techcredit-research-mcp/mcp"
MCP_COLLAB_URL = "https://fatebuddy.asia/techcredit-collab-mcp/mcp"
MCP_RESEARCH_TOOLS = ["search_companies", "get_company_profile", "analyze_company", "get_latest_analysis",
                      "discover_prospects", "get_prospect_pack", "screen_market", "screen_sme",
                      "screen_sme_web", "screen_ipo", "risk_screen", "qualify_company",
                      "deep_verify", "get_deep_verify", "persist_screening_result"]
MCP_COLLAB_TOOLS = ["list_my_projects", "create_project", "add_project_member", "search_project_facts",
                    "project_memory_add", "project_memory_search", "upload_dossier", "get_context_pack",
                    "submit_skill_outputs", "get_company_profile", "analyze_company", "get_latest_analysis"]


def fb_me(cookie: str):
    """转发浏览器 Cookie 到 FateBuddy /api/me 校验登录态（不复制其鉴权逻辑）。"""
    if not cookie:
        return None
    try:
        r = requests.get(FB_BASE + "/api/me", headers={"Cookie": cookie}, timeout=5)
        d = r.json()
        return d if r.status_code == 200 and d.get("ok") else None
    except Exception:
        return None


def _active_key(me: dict):
    now = _now()
    for k in me.get("keys", []):
        if k.get("status") == "active" and (not k.get("expires_at") or k["expires_at"] > now):
            return k
    return None


FACADE_DB = "/root/.hermes/agent_api.db"   # FateBuddy 账户库（只读，仅取每日领取状态）


def _trial_state(email: str, k: Optional[dict]) -> dict:
    """账户页每日领取状态：daily/cap 取公开 trial-info；claimed_today 只读查账户库，
    口径与 facade 一致（Asia/Shanghai 自然日）；remaining<0 为平台不限次哨兵。"""
    daily, cap = 10, 30
    try:
        ti = requests.get(FB_BASE + "/api/trial-info", timeout=5).json()
        daily, cap = int(ti.get("daily", 10)), int(ti.get("max_accum", 30))
    except Exception:
        pass
    claimed = False
    try:
        c = sqlite3.connect("file:" + FACADE_DB + "?mode=ro", uri=True, timeout=5)
        row = c.execute("SELECT trial_claim_date FROM users WHERE email=?", (email or "",)).fetchone()
        c.close()
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        claimed = bool(row and (row[0] or "") == today)
    except Exception:
        pass
    rem = k.get("remaining") if k else None
    return {"daily": daily, "cap": cap, "claimed_today": claimed,
            "unlimited": rem is not None and rem < 0}


def require_login(request: Request):
    me = fb_me(request.headers.get("cookie", ""))
    if not me:
        raise HTTPException(401, "详细分析需登录：请先用 FateBuddy 账号注册/登录")
    return me


def db():
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_company(r: sqlite3.Row, with_detail=False):
    d = {
        "company_key": r["company_key"], "name": r["name"], "full_name": r["full_name"],
        "code": r["code"], "domain": r["domain"], "stage": r["stage"],
        "industry": r["industry"], "province": r["province"], "city": r["city"],
        "lat": r["lat"], "lng": r["lng"],
        "core_score": r["core_score"], "bonus_score": r["bonus_score"],
        "total_score": r["total_score"], "grade": r["grade"],
        "cached_patents": r["cached_patents"], "is_cached_entity": bool(r["is_cached_entity"]),
        "report_period": r["report_period"], "updated_at": r["updated_at"],
        "missing": json.loads(r["missing_json"] or "[]"),
    }
    if with_detail:
        d["inputs"] = json.loads(r["inputs_json"] or "{}")
        d["metrics"] = json.loads(r["metrics_json"] or "[]")
        d["profile"] = json.loads(r["profile_json"] or "{}")
        d["source_note"] = r["source_note"]
    return d


@app.get("/api/health")
def health():
    return {"ok": True, "time": _now()}


@app.get("/api/auth/me")
def auth_me(request: Request):
    me = fb_me(request.headers.get("cookie", ""))
    if not me:
        return {"logged_in": False, "signin": "/signin", "signup": "/signup"}
    k = _active_key(me)
    return {"logged_in": True, "email": me.get("email"),
            "key": k["key"] if k else None,
            "remaining": k.get("remaining") if k else None,
            "expires_at": k.get("expires_at") if k else None,
            "trial": _trial_state(me.get("email"), k),
            "account_url": "/account"}


@app.get("/api/mcp-config")
def mcp_config(request: Request):
    me = require_login(request)
    k = _active_key(me)
    if not k:
        raise HTTPException(403, "账户下无有效接入码：请到 /account 领取当日额度或检查到期时间")
    cfg = {"mcpServers": {"techcredit-analysis": {
        "type": "streamable-http", "url": MCP_PUBLIC_URL,
        "headers": {"Authorization": "Bearer " + k["key"]}}}}
    bearer = {"Authorization": "Bearer " + k["key"]}
    servers = [
        {"id": "yihua-research", "label": "调研 MCP",
         "desc": "找企业、做分析：海选漏斗 / 潜客侦察 / 贵复查 / 分析评分",
         "url": MCP_RESEARCH_URL, "tools": MCP_RESEARCH_TOOLS,
         "config": {"mcpServers": {"yihua-research": {
             "type": "streamable-http", "url": MCP_RESEARCH_URL, "headers": bearer}}}},
        {"id": "yihua-collab", "label": "项目协作 MCP",
         "desc": "带团队、做认证：项目/成员/共享记忆/知识库 + 底稿4-Skill闭环",
         "url": MCP_COLLAB_URL, "tools": MCP_COLLAB_TOOLS,
         "config": {"mcpServers": {"yihua-collab": {
             "type": "streamable-http", "url": MCP_COLLAB_URL, "headers": bearer}}}},
    ]
    combined = {"mcpServers": {s["id"]: s["config"]["mcpServers"][s["id"]] for s in servers}}
    return {"url": MCP_PUBLIC_URL, "token": k["key"], "config": cfg, "tools": MCP_TOOLS,
            "servers": servers, "combined_config": combined,
            "note": "接入码即 FateBuddy 账户接入码（复用注册登录签发）；techcredit 调用不扣减 FateBuddy 额度。"
                    "旧端点 /techcredit-mcp/mcp（全量24工具）仍可用，已接入客户不受影响。"}


@app.get("/api/meta")
def meta():
    conn = db()
    total = conn.execute("SELECT COUNT(*) c FROM screened_companies").fetchone()["c"]
    grades = {r["grade"]: r["c"] for r in
              conn.execute("SELECT grade, COUNT(*) c FROM screened_companies GROUP BY grade")}
    doms = {r["domain"]: r["c"] for r in
            conn.execute("SELECT domain, COUNT(*) c FROM screened_companies GROUP BY domain")}
    as_of = conn.execute("SELECT MAX(report_period) p, MAX(updated_at) u FROM screened_companies").fetchone()
    conn.close()
    return {
        "total": total, "grades": grades, "domains": doms,
        "as_of": as_of["p"], "updated_at": as_of["u"],
        "indicators": [{"code": k, "label": v[0], "unit": v[1], "category": v[2]}
                       for k, v in IND_LABEL.items()],
        "domain_options": innovation2.DOMAINS[1:],
        "methodology": {
            "title": "创新积分制2.0 近似粗筛（免费数据口径）",
            "basis": "科技部《“创新积分制”工作指引（2.0版）》（2025-10）：9项核心指标100分+加分项30分，A≥85/B≥70/C≥60/D<60",
            "sources": [
                "东方财富业绩报表（2025年报，批量免费接口）：营收/增速/ROE/行业",
                "巨潮资讯公司画像（免费）：成立日期/注册地址/主营业务",
                "新浪财经利润表（免费）：最新年报研发费用",
                "本地已缓存专利底稿（历史抓取，仅读缓存）：发明专利口径",
            ],
            "limits": [
                "研发人员占比、每百人知识产权、技术合同成交额无免费公开源 → 缺失值动态调权",
                "加分项（科技项目/奖励/标准、融资、资质榜单）未采集，计0分",
                "基准线为本次上市科创样本同期群分位数（p25/p50/p75/p90），非官方20万家样本",
                "粗筛分用于发现潜在企业，不构成授信或投资依据；深度分析以引擎正式口径为准",
            ],
            "no_paid_api": True,
        },
    }


@app.get("/api/companies")
def companies(domain: Optional[str] = None, grade: Optional[str] = None, q: Optional[str] = None):
    conn = db()
    sql = "SELECT * FROM screened_companies WHERE 1=1"
    args = []
    if domain:
        sql += " AND domain=?"
        args.append(domain)
    if grade:
        sql += " AND grade=?"
        args.append(grade)
    if q:
        sql += " AND (name LIKE ? OR full_name LIKE ? OR city LIKE ? OR code LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += " ORDER BY core_score DESC"
    rows = conn.execute(sql, args).fetchall()
    run_keys = {r["company_key"] for r in
                conn.execute("SELECT DISTINCT company_key FROM evaluation_runs")}
    conn.close()
    out = []
    for r in rows:
        d = _row_to_company(r)
        d["has_run"] = r["company_key"] in run_keys
        out.append(d)
    return {"companies": out, "count": len(out)}


@app.get("/api/company/{key}")
def company(key: str):
    conn = db()
    r = conn.execute("SELECT * FROM screened_companies WHERE company_key=?", (key,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, "企业不在粗筛名单中")
    d = _row_to_company(r, with_detail=True)
    d["cached_sources"] = [dict(x) for x in conn.execute(
        "SELECT kind, COUNT(*) c FROM sources WHERE company_key=? GROUP BY kind", (key,))]
    # 公开层只展示平台口径运行（viewer 为空），团队项目运行不外泄
    run = conn.execute(
        "SELECT run_id, created_at, mode, core_score, bonus_score, total_score,"
        " pqi_composite, final_score, grade, veto, viewer_email FROM evaluation_runs"
        " WHERE company_key=? AND (viewer_email IS NULL OR viewer_email='')"
        " ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
    d["latest_run"] = dict(run) if run else None
    conn.close()
    with LOCK:
        d["job"] = JOBS.get(key)
    return d


def _finance_from_inputs(inputs: dict) -> dict:
    m = {
        "RD_EXPENSE": "rd_expense_wan", "RD_RATIO": "rd_ratio_pct",
        "RD_STAFF_RATIO": "rd_staff_ratio_pct", "INVENTION_GRANTS": "invention_grants",
        "IP_PER_100_RD": "ip_per_100_rd", "TECH_CONTRACT": "tech_contract_wan",
        "MAIN_REVENUE": "main_revenue_wan", "REV_GROWTH": "rev_growth_pct", "ROE": "roe_pct",
    }
    return {fin: inputs[ind] for ind, fin in m.items()
            if inputs.get(ind) is not None}


def _run_analysis(key: str, domain: str, stage: str, finance: dict, name: str):
    try:
        if domain not in innovation2.DOMAINS:
            domain = "通用"
        if stage not in innovation2.STAGES:
            stage = "成长期"
        base = run_engine.run(key, domain, stage, finance, {}, patent_top=5)
        run_id = base["run_id"]
        conn = db()
        conn.execute("UPDATE evaluation_runs SET company_name=? WHERE run_id=?", (name, run_id))
        conn.commit()
        conn.close()
        payload = {k: base[k] for k in ("run_id", "company", "pqi", "innovation",
                                        "risk_flags", "funnel", "packs")}
        payload["company"]["company_name"] = name
        (ANALYSIS_DIR / f"{run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        with LOCK:
            JOBS[key] = {"status": "done", "run_id": run_id, "finished_at": _now()}
    except Exception as exc:
        with LOCK:
            JOBS[key] = {"status": "error", "error": repr(exc)[:400], "finished_at": _now()}


@app.post("/api/company/{key}/analyze")
def analyze(key: str, request: Request):
    require_login(request)
    conn = db()
    r = conn.execute("SELECT * FROM screened_companies WHERE company_key=?", (key,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404, "企业不在粗筛名单中")
    with LOCK:
        if JOBS.get(key, {}).get("status") == "running":
            return {"status": "running", "message": "分析进行中"}
        JOBS[key] = {"status": "running", "started_at": _now()}
    inputs = json.loads(r["inputs_json"] or "{}")
    finance = _finance_from_inputs(inputs)
    t = threading.Thread(target=_run_analysis,
                         args=(key, r["domain"], r["stage"], finance, r["full_name"] or r["name"]),
                         daemon=True)
    t.start()
    return {"status": "running", "message": "深度分析已启动（本地缓存+免费口径，零付费调用）"}


@app.get("/api/company/{key}/analysis")
def analysis(key: str, request: Request):
    me = require_login(request)
    email = me.get("email", "")
    with LOCK:
        job = JOBS.get(key)
    conn = db()
    projects.ensure_schema(conn)
    run = projects.latest_visible_run(conn, key, email)
    result = None
    metrics, flags = [], []
    if run:
        fp = ANALYSIS_DIR / f"{run['run_id']}.json"
        if fp.exists():
            result = json.loads(fp.read_text(encoding="utf-8"))
        metrics = [dict(x) for x in conn.execute(
            "SELECT * FROM metric_values WHERE run_id=?", (run["run_id"],))]
        flags = [dict(x) for x in conn.execute(
            "SELECT * FROM risk_flags WHERE run_id=?", (run["run_id"],))]
    conn.close()
    return {"job": job, "run": dict(run) if run else None,
            "result": result, "metrics": metrics, "risk_flags": flags}


@app.get("/api/pack")
def pack(path: str):
    p = Path(path).resolve()
    if not str(p).startswith(str(CONTEXT_DIR)) or not p.is_file():
        raise HTTPException(403, "仅允许下载引擎生成的上下文包")
    return FileResponse(p, filename=p.name, media_type="application/json")


# ---------- 第二层：底稿分析工作台（SQLite 落库 + WeKnora 事实 + 引擎评分） ----------

ALLOWED_EXT = {".md", ".txt", ".json", ".markdown"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_FILES = 6


@app.get("/page2")
def page2(request: Request):
    """底稿分析工作台：登录后才能用（未登录重定向回地图层）。"""
    me = fb_me(request.headers.get("cookie", ""))
    if not me:
        return RedirectResponse("/techcredit/", status_code=303)
    return FileResponse(HERE / "page2.html", media_type="text/html")


@app.post("/api/dossier/analyze")
async def dossier_analyze(request: Request,
                          company_key: str = Form(...),
                          company_name: str = Form(""),
                          domain: str = Form("通用"),
                          stage: str = Form("成长期"),
                          project_id: str = Form(""),
                          files: list[UploadFile] = File(...),
                          kinds: list[str] = Form(None)):
    me = require_login(request)
    email = me.get("email", "")
    # 团队鉴权：挂项目时必须已是该团队成员（防越权把底稿塞进他人项目）
    pid = (project_id or "").strip()
    if pid:
        conn = db()
        try:
            projects.ensure_schema(conn)
            if not projects.get_project(conn, pid):
                raise HTTPException(404, "项目不存在")
            if not projects.is_member(conn, pid, email):
                raise HTTPException(403, "非项目成员，无法向该项目上传底稿")
        finally:
            conn.close()
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"一次最多上传 {MAX_UPLOAD_FILES} 份底稿")
    blobs, kind_list, rejected = [], kinds or [], []
    for i, f in enumerate(files):
        blob = await f.read()
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT or not blob or len(blob) > MAX_UPLOAD_BYTES:
            rejected.append({"file": f.filename, "reason": "仅支持 ≤2MB 的 .md/.txt/.json"})
            continue
        kind = kind_list[i] if i < len(kind_list) else "web"
        blobs.append((f.filename, blob, kind))
    if not blobs:
        raise HTTPException(400, "无有效底稿文件：" +
                            "; ".join(r["reason"] for r in rejected))
    try:
        result = dossier_pipeline.analyze_dossiers(
            company_key=company_key, company_name=company_name,
            domain=domain, stage=stage,
            files=[(n, b) for n, b, _ in blobs],
            kinds=[k for _, _, k in blobs],
            actor_email=email, project_id=pid or None)
    except Exception as exc:
        raise HTTPException(500, f"分析管线异常: {exc!r}"[:300])
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "分析失败"))
    result["rejected"] = rejected
    result["account"] = email
    return result


# ---------- 第三层：项目团队协同（多 Agent 共享同一项目知识库） ----------


@app.get("/api/projects")
def list_projects(request: Request):
    """我的项目列表（成员、角色、邀请码、底稿数）。"""
    me = require_login(request)
    email = me.get("email", "")
    conn = db()
    try:
        return {"ok": True, "email": email,
                "projects": projects.my_projects(conn, email)}
    finally:
        conn.close()


@app.post("/api/projects")
def create_project(request: Request, name: str = Form(...),
                   company_key: str = Form(...), company_name: str = Form("")):
    """创建项目：创建者即项目负责人，拿到邀请码分享给团队成员。"""
    me = require_login(request)
    email = me.get("email", "")
    key = dossier_pipeline._clean_company_key(company_key)
    if not dossier_pipeline._valid_key(key):
        raise HTTPException(400, "请填写有效的18位统一社会信用代码（USCC）")
    if not (name or "").strip():
        raise HTTPException(400, "请填写项目名称")
    conn = db()
    try:
        p = projects.create_project(conn, name=name.strip(), company_key=key,
                                    company_name=company_name.strip(),
                                    owner_email=email)
        return {"ok": True, "project": p,
                "note": "把邀请码发给团队成员即可共享该项目的底稿知识库"}
    finally:
        conn.close()


@app.post("/api/projects/join")
def join_project(request: Request, invite_code: str = Form(...)):
    """凭邀请码加入项目（成为 member，多 Agent 协同入口）。"""
    me = require_login(request)
    email = me.get("email", "")
    code = (invite_code or "").strip()
    conn = db()
    try:
        projects.ensure_schema(conn)
        row = conn.execute("SELECT project_id FROM projects WHERE invite_code=?",
                           (code,)).fetchone()
        if not row:
            raise HTTPException(404, "邀请码无效")
        pid = row["project_id"]
        if not projects.is_member(conn, pid, email):
            conn.execute("INSERT INTO project_members VALUES(?,?,?,?)",
                         (pid, email, "member", _now()))
            conn.commit()
        p = projects.get_project(conn, pid)
        return {"ok": True, "project": dict(p),
                "note": "已加入项目，团队内各 Agent 凭各自接入码即可调用本项目知识库"}
    finally:
        conn.close()


@app.post("/api/projects/{pid}/members")
def add_member(pid: str, request: Request, email: str = Form(...)):
    """添加成员（仅项目负责人可操作）。"""
    me = require_login(request)
    actor = me.get("email", "")
    conn = db()
    try:
        ok, msg = projects.add_member(conn, pid, (email or "").strip().lower(), actor)
        if not ok:
            raise HTTPException(403, msg)
        return {"ok": True, "message": msg}
    finally:
        conn.close()


@app.get("/api/projects/{pid}/sources")
def project_sources(pid: str, request: Request):
    """项目底稿清单（仅成员可见）：团队共享的上传底稿 + 平台公共数据统计。"""
    me = require_login(request)
    email = me.get("email", "")
    conn = db()
    try:
        projects.ensure_schema(conn)
        p = projects.get_project(conn, pid)
        if not p:
            raise HTTPException(404, "项目不存在")
        if not projects.is_member(conn, pid, email):
            raise HTTPException(403, "非项目成员，无法访问该项目知识库")
        uploads = [dict(r) for r in conn.execute(
            "SELECT source_id, kind, title, company_key, weknora_status, "
            "last_fetched_at FROM sources WHERE project_id=? ORDER BY last_fetched_at DESC",
            (pid,))]
        public = [dict(r) for r in conn.execute(
            "SELECT kind, COUNT(*) c FROM sources WHERE company_key=? "
            "AND (provider!='upload' OR project_id IS NULL) GROUP BY kind",
            (p["company_key"],))]
        members = [dict(r) for r in conn.execute(
            "SELECT email, role, added_at FROM project_members WHERE project_id=?",
            (pid,))]
        return {"ok": True, "project": dict(p), "members": members,
                "uploads": uploads, "public_sources": public}
    finally:
        conn.close()


# ---------- WeKnora 一账号桥接：一画登录态 → WeKnora 同邮箱账号 → 共享工作区 ----------
# 机制（全部走 WeKnora 现有 API，不改 WeKnora 配置、不重启 WeKnora）：
#   1) 首次点击：/auth/register 自动创建同邮箱 WeKnora 账号（随机密码存本库 weknora_accounts 表）
#   2) /tenants/{shared}/members 以 contributor 加入共享工作区（幂等，already exists 视为成功）
#   3) /auth/login 静默登录 → /auth/switch-tenant 切到共享工作区（techcredit 全部知识库所在租户）
#   4) 同源桥接页写入 WeKnora 前端 localStorage 登录态 → 跳转 /platform/knowledge-bases
# 边界：共享工作区内 WeKnora UI 可见全部项目库；MCP/Agent 检索路径的按项目隔离不受影响。

import secrets as _secrets
import hashlib as _hashlib
import html as _html
from fastapi.responses import HTMLResponse

WK_BASE = "http://127.0.0.1:8080/api/v1"
_WK_TENANT_CACHE = {"id": None}


def _wk_api_key() -> str:
    env = config.load_env_file(config.WEKNORA_ENV_FILE)
    return (env.get("WEKNORA_API_KEY") or "").strip()


def _wk_shared_tenant(key: str) -> int:
    if _WK_TENANT_CACHE["id"]:
        return _WK_TENANT_CACHE["id"]
    r = requests.get(WK_BASE + "/tenants", headers={"X-API-Key": key}, timeout=15)
    items = (r.json().get("data") or {}).get("items") or []
    if not items:
        raise RuntimeError("WeKnora 共享工作区不可用")
    _WK_TENANT_CACHE["id"] = items[0]["id"]
    return _WK_TENANT_CACHE["id"]


def _wk_err_msg(d: dict) -> str:
    e = d.get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e.get("details") or "")
    return str(d.get("message") or e or "")


def _wk_bridge_page(payload, err: str = "", redirect: str = "/platform/knowledge-bases") -> HTMLResponse:
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        body = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>一画 · 进入底稿管理</title></head>"
                "<body style=\"background:#040b15;color:#a7bccf;font-family:-apple-system,'PingFang SC',sans-serif;"
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0\">"
                "<div style=\"text-align:center;font-size:14px\">◉ 正在以一画账号进入 WeKnora 底稿管理…</div>"
                "<script>const D=" + data + ";"
                "try{for(const k in D){const v=D[k];localStorage.setItem(k,typeof v===\"string\"?v:JSON.stringify(v));}}catch(e){}"
                "location.replace(" + json.dumps(redirect) + ");"
                "</script></body></html>")
    else:
        body = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>一画 · 底稿管理</title></head>"
                "<body style=\"background:#040b15;color:#eef5fc;font-family:-apple-system,'PingFang SC',sans-serif;"
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0\">"
                "<div style=\"max-width:420px;text-align:center;font-size:14px;line-height:2\">"
                "<div style=\"color:#f87171;margin-bottom:12px\">" + _html.escape(err) + "</div>"
                "<a href=\"/login\" style=\"color:#7dd3fc\">手动登录 WeKnora</a> · "
                "<a href=\"/techcredit/mcp\" style=\"color:#7dd3fc\">返回一画账户页</a>"
                "</div></body></html>")
    return HTMLResponse(body)


@app.get("/api/weknora-sso")
def weknora_sso(request: Request):
    """一账号直连：一画登录态换 WeKnora 同邮箱账号登录态，落地知识库管理页。"""
    me = fb_me(request.headers.get("cookie", ""))
    if not me:
        return RedirectResponse("/techcredit/mcp", status_code=303)
    email = (me.get("email") or "").strip().lower()
    try:
        key = _wk_api_key()
        if not key:
            return _wk_bridge_page(None, "WeKnora API Key 未配置，请联系管理员")
        conn = db()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS weknora_accounts("
                         "email TEXT PRIMARY KEY, password TEXT NOT NULL, created_at TEXT NOT NULL)")
            row = conn.execute("SELECT password FROM weknora_accounts WHERE email=?",
                               (email,)).fetchone()
            pw = row["password"] if row else None
            if not pw:
                pw = _secrets.token_urlsafe(24)
                uname = email.split("@")[0][:24] + "-" + _hashlib.sha1(email.encode()).hexdigest()[:4]
                r = requests.post(WK_BASE + "/auth/register",
                                  json={"email": email, "username": uname, "password": pw},
                                  timeout=20).json()
                if not r.get("success"):
                    m = _wk_err_msg(r)
                    if any(w in m.lower() for w in ("exist", "already", "registered", "duplicate")):
                        return _wk_bridge_page(None, "该邮箱已有 WeKnora 账号（非一账号桥接创建），请到 /login 用原密码手动登录")
                    return _wk_bridge_page(None, "WeKnora 账号创建失败：" + m[:120])
                conn.execute("INSERT OR REPLACE INTO weknora_accounts VALUES(?,?,?)",
                             (email, pw, _now()))
                conn.commit()
        finally:
            conn.close()
        pub_tid = _wk_shared_tenant(key)
        want = (request.query_params.get("project") or "").strip()
        conn = db()
        try:
            my = projects.my_projects(conn, email)
            if want and not any(p["project_id"] == want for p in my):
                return _wk_bridge_page(None, "非该项目成员，无法进入其底稿工作区")
            target_tid, target_kb = None, None
            for p in my:
                wk = projects.ensure_weknora_workspace(conn, p["project_id"])
                mr = requests.post(WK_BASE + f"/tenants/{wk['tenant_id']}/members",
                                   headers={"X-API-Key": wk["api_key"]},
                                   json={"email": email, "role": "contributor"}, timeout=15).json()
                if not mr.get("success") and "already" not in _wk_err_msg(mr).lower():
                    return _wk_bridge_page(None, f"加入项目工作区失败（{p['name']}）：" + _wk_err_msg(mr)[:100])
                if p["project_id"] == want or target_tid is None:
                    target_tid, target_kb = wk["tenant_id"], wk.get("kb_id")
        finally:
            conn.close()
        # 平台公共库：只读 viewer（已是更高角色则降为 viewer）
        r = requests.post(WK_BASE + f"/tenants/{pub_tid}/members", headers={"X-API-Key": key},
                          json={"email": email, "role": "viewer"}, timeout=15).json()
        if not r.get("success") and "already" in _wk_err_msg(r).lower():
            try:
                ml = requests.get(WK_BASE + f"/tenants/{pub_tid}/members",
                                  headers={"X-API-Key": key}, timeout=10).json()
                for m in (ml.get("data") or {}).get("members", []):
                    if m.get("email") == email and m.get("role") not in ("viewer", "owner"):
                        for meth in ("patch", "put"):
                            rr = getattr(requests, meth)(
                                WK_BASE + f"/tenants/{pub_tid}/members/{m['user_id']}",
                                headers={"X-API-Key": key}, json={"role": "viewer"}, timeout=10)
                            if rr.status_code < 300:
                                break
            except Exception:
                pass
        elif not r.get("success"):
            return _wk_bridge_page(None, "加入公共底稿工作区失败：" + _wk_err_msg(r)[:120])
        tid = target_tid or pub_tid
        lg = requests.post(WK_BASE + "/auth/login",
                           json={"email": email, "password": pw}, timeout=15).json()
        if not lg.get("success") or not lg.get("token"):
            return _wk_bridge_page(None, "WeKnora 静默登录失败（密码可能被外部修改），请到 /login 手动登录")
        sw = requests.post(WK_BASE + "/auth/switch-tenant",
                           headers={"Authorization": "Bearer " + lg["token"]},
                           json={"tenant_id": tid}, timeout=15).json()
        s = sw if (sw.get("success") and sw.get("token")) else lg
        at = s.get("active_tenant") or lg.get("active_tenant") or {}
        payload = {
            "weknora_token": s["token"],
            "weknora_refresh_token": s.get("refresh_token") or lg.get("refresh_token") or "",
            "weknora_user": s.get("user") or lg.get("user") or {},
            "weknora_tenant": at,
            "weknora_memberships": s.get("memberships") or lg.get("memberships") or [],
            "weknora_selected_tenant_id": str(at.get("id") or ""),
            "weknora_selected_tenant_name": at.get("name") or "",
        }
        dest = "/platform/knowledge-bases"
        if want and target_kb:
            dest += "/" + target_kb
        return _wk_bridge_page(payload, redirect=dest)
    except Exception as exc:
        return _wk_bridge_page(None, f"桥接异常：{exc!r}"[:160])
app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")