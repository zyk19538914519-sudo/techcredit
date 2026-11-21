#!/usr/bin/env python3
"""溯源存储层：SQLite 索引 + 原始 JSON 落盘 + 全局唯一 Source_ID。

Source_ID 规范：{KIND}-{PROVIDER}-{sha1(provider|natural_key)[:10]}
  - KIND:     PAT 专利 / BID 中标 / JUD 司法 / COR 工商 / PEN 处罚 / RSK 经营风险
  - PROVIDER: ZHY 智慧芽 / YD 元典 / TYC 天眼查
  - natural_key 取业务自然主键（专利公开号、案号、处罚决定书文号等），
    同一记录重复抓取幂等 upsert，内容变化时版本号递增并保留历史。

数据落库边界（2026-11-21 确立）：
  - 正式证据库（本模块 sources 表 + WeKnora 底稿 RAG）只承载两类数据：
    ① 平台已认证的公开粗筛名单底池（screened_companies，历史存量）；
    ② 客户明确交付/明确要求入库的底稿（upload_dossier、persist 提升落库）。
  - 前期筛选阶段（两段漏斗：①便宜海选 ②贵数据复查）产生的企业数据
    默认只进筛选暂存区（staging_* 函数族，独立 staging.db），
    不写正式 sources 表、不进 WeKnora RAG；
    只有客户明确表述要入库，才调用 promote_staging_to_main 提升落库。
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

KINDS = {"patent": "PAT", "bid": "BID", "judicial": "JUD",
         "company": "COR", "penalty": "PEN", "risk": "RSK", "web": "WEB",
         "financial": "FIN", "ipx": "IPX", "standard": "STD", "cert": "CER",
         "award": "AWD", "fund": "FUND", "rank": "RANK", "hr": "HR"}
PROVIDERS = {"zhihuiya": "ZHY", "yuandian": "YD", "tianyancha": "TYC",
             "jianyu": "JY", "tavily": "TAV", "firecrawl": "FC",
             "metaso": "MSO", "mx": "MX", "qcc": "QCC", "akshare": "AKS",
             "cnipr": "CNP", "ggzy": "GGZ", "cninfo": "CNI",
             "upload": "UPL"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  source_id        TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,
  provider         TEXT NOT NULL,
  company_key      TEXT NOT NULL,
  company_name     TEXT,
  natural_key      TEXT NOT NULL,
  title            TEXT,
  content_hash     TEXT NOT NULL,
  raw_path         TEXT NOT NULL,
  dossier_path     TEXT,
  version          INTEGER NOT NULL DEFAULT 1,
  first_fetched_at TEXT NOT NULL,
  last_fetched_at  TEXT NOT NULL,
  fetch_count      INTEGER NOT NULL DEFAULT 1,
  weknora_status   TEXT NOT NULL DEFAULT 'pending',
  weknora_doc_id   TEXT,
  meta_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sources_company ON sources(company_key, kind);
CREATE INDEX IF NOT EXISTS idx_sources_kind ON sources(kind);
CREATE INDEX IF NOT EXISTS idx_sources_weknora ON sources(weknora_status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sources_natural ON sources(provider, natural_key);
CREATE TABLE IF NOT EXISTS fetch_log(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  company_key  TEXT,
  provider     TEXT,
  endpoint     TEXT,
  params_json  TEXT,
  started_at   TEXT,
  finished_at  TEXT,
  status       TEXT,
  record_count INTEGER DEFAULT 0,
  note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_company ON fetch_log(company_key, provider);
"""

SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_source_id(kind: str, provider: str, natural_key: str) -> str:
    h = hashlib.sha1(f"{provider}|{natural_key}".encode("utf-8")).hexdigest()[:10]
    return f"{KINDS[kind]}-{PROVIDERS[provider]}-{h}"


def connect() -> sqlite3.Connection:
    """建立连接：WAL 日志 + 忙等超时，保证多 Agent 并发读写不互相锁死。"""
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    return conn


def upsert_source(conn, *, kind, provider, company_key, company_name,
                  natural_key, title, payload: dict, meta: dict | None = None) -> str:
    """落盘原始 JSON 并登记/更新索引，返回 Source_ID。"""
    sid = make_source_id(kind, provider, natural_key)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    chash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    cdir = config.RAW_DIR / company_key
    cdir.mkdir(parents=True, exist_ok=True)
    raw_path = cdir / f"{sid}.json"
    ts = now()
    row = conn.execute("SELECT content_hash, version, first_fetched_at, fetch_count "
                       "FROM sources WHERE source_id=?", (sid,)).fetchone()
    envelope = {"source_id": sid, "kind": kind, "provider": provider,
                "company_key": company_key, "company_name": company_name,
                "natural_key": natural_key, "fetched_at": ts, "payload": payload}
    if row is None:
        raw_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
        conn.execute(
            "INSERT INTO sources(source_id,kind,provider,company_key,company_name,natural_key,"
            "title,content_hash,raw_path,version,first_fetched_at,last_fetched_at,fetch_count,meta_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,1,?)",
            (sid, kind, provider, company_key, company_name, natural_key, title,
             chash, str(raw_path), ts, ts, json.dumps(meta or {}, ensure_ascii=False)))
    else:
        version = row["version"]
        if row["content_hash"] != chash:
            version += 1
            hist = cdir / f"{sid}.v{row['version']}.json"   # 保留旧版本底稿
            if not hist.exists() and raw_path.exists():
                hist.write_text(raw_path.read_text(encoding="utf-8"), encoding="utf-8")
            raw_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
        conn.execute(
            "UPDATE sources SET content_hash=?, version=?, last_fetched_at=?, fetch_count=?, "
            "title=?, weknora_status=CASE WHEN content_hash!=? THEN 'pending' ELSE weknora_status END, "
            "meta_json=? WHERE source_id=?",
            (chash, version, ts, row["fetch_count"] + 1, title, chash,
             json.dumps(meta or {}, ensure_ascii=False), sid))
    conn.commit()
    return sid


def log_fetch(conn, *, company_key, provider, endpoint, params, started_at,
              status, record_count=0, note=""):
    conn.execute(
        "INSERT INTO fetch_log(company_key,provider,endpoint,params_json,started_at,finished_at,"
        "status,record_count,note) VALUES(?,?,?,?,?,?,?,?,?)",
        (company_key, provider, endpoint, json.dumps(params, ensure_ascii=False),
         started_at, now(), status, record_count, note))
    conn.commit()


def list_sources(conn, company_key=None, kind=None):
    q, args = "SELECT * FROM sources", []
    conds = []
    if company_key:
        conds.append("company_key=?"); args.append(company_key)
    if kind:
        conds.append("kind=?"); args.append(kind)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    return conn.execute(q + " ORDER BY kind, source_id", args)


# ---------- 前期筛选阶段暂存区（数据落库边界） ----------
# 前期筛选（①便宜海选 ②贵数据复查）产生的企业数据默认写入 staging 库
# （独立 SQLite：screening_staging/staging.db + staging raw 目录），
# 与正式证据库 techcredit.db 的 sources 表物理隔离，也不进入 WeKnora 底稿 RAG。
# 只有客户明确表述要入库时，才调用 promote_staging_to_main 提升落库。

STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging_sources(
  source_id        TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,
  provider         TEXT NOT NULL,
  company_key      TEXT NOT NULL,
  company_name     TEXT,
  natural_key      TEXT NOT NULL,
  title            TEXT,
  content_hash     TEXT NOT NULL,
  raw_path         TEXT NOT NULL,
  dossier_path     TEXT,
  version          INTEGER NOT NULL DEFAULT 1,
  first_fetched_at TEXT NOT NULL,
  last_fetched_at  TEXT NOT NULL,
  fetch_count      INTEGER NOT NULL DEFAULT 1,
  meta_json        TEXT,
  promoted         INTEGER NOT NULL DEFAULT 0,   -- 0=仅暂存 1=客户明确要求后已提升入正式库
  promoted_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_staging_company ON staging_sources(company_key, kind);
CREATE TABLE IF NOT EXISTS staging_fetch_log(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  company_key  TEXT,
  provider     TEXT,
  endpoint     TEXT,
  params_json  TEXT,
  started_at   TEXT,
  finished_at  TEXT,
  status       TEXT,
  record_count INTEGER DEFAULT 0,
  note         TEXT
);
"""


def staging_connect() -> sqlite3.Connection:
    """打开筛选暂存区独立库（自动建表）。前期筛选阶段的所有抓取写这里。"""
    conn = sqlite3.connect(config.STAGING_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(STAGING_SCHEMA)
    return conn


def staging_upsert_source(conn, *, kind, provider, company_key, company_name,
                          natural_key, title, payload: dict, meta: dict | None = None) -> str:
    """筛选暂存区版本：落盘到 staging raw 目录并登记暂存索引。
    Source_ID 规范与正式库一致（确定性幂等），但 promoted=0，
    即数据此时不在正式 SQL sources 表、不在任何底稿 RAG。"""
    sid = make_source_id(kind, provider, natural_key)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    chash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    cdir = config.STAGING_RAW_DIR / company_key
    cdir.mkdir(parents=True, exist_ok=True)
    raw_path = cdir / f"{sid}.json"
    ts = now()
    envelope = {"source_id": sid, "kind": kind, "provider": provider,
                "company_key": company_key, "company_name": company_name,
                "natural_key": natural_key, "fetched_at": ts, "payload": payload,
                "staging": True, "promoted": False}
    row = conn.execute("SELECT content_hash, version, fetch_count FROM staging_sources "
                       "WHERE source_id=?", (sid,)).fetchone()
    if row is None:
        raw_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
        conn.execute(
            "INSERT INTO staging_sources(source_id,kind,provider,company_key,company_name,natural_key,"
            "title,content_hash,raw_path,version,first_fetched_at,last_fetched_at,fetch_count,meta_json,promoted) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,1,?,0)",
            (sid, kind, provider, company_key, company_name, natural_key, title,
             chash, str(raw_path), ts, ts, json.dumps(meta or {}, ensure_ascii=False)))
    else:
        conn.execute(
            "UPDATE staging_sources SET content_hash=?, last_fetched_at=?, fetch_count=?, title=?, meta_json=? "
            "WHERE source_id=?",
            (chash, ts, row["fetch_count"] + 1, title,
             json.dumps(meta or {}, ensure_ascii=False), sid))
    conn.commit()
    return sid


def staging_log_fetch(conn, *, company_key, provider, endpoint, params, started_at,
                      status, record_count=0, note=""):
    conn.execute(
        "INSERT INTO staging_fetch_log(company_key,provider,endpoint,params_json,started_at,finished_at,"
        "status,record_count,note) VALUES(?,?,?,?,?,?,?,?,?)",
        (company_key, provider, endpoint, json.dumps(params, ensure_ascii=False),
         started_at, now(), status, record_count, note))
    conn.commit()


def staging_render_dossier(conn, company_id_or_key: str = "", company_key: str = "") -> int:
    """暂存底稿预览：仅渲染到 STAGING_DOSSIER_DIR 供客户经理查看，
    绝不进入 WeKnora 底稿 RAG（正式底稿只有提升落库时才渲染入库）。"""
    key = company_id_or_key or company_key
    rows = conn.execute("SELECT * FROM staging_sources WHERE company_key=?", (key,)).fetchall()
    n = 0
    for row in rows:
        envelope = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))
        payload = envelope["payload"]
        cdir = config.STAGING_DOSSIER_DIR / row["company_key"]
        cdir.mkdir(parents=True, exist_ok=True)
        path = cdir / f"{row['source_id']}.md"
        lines = [
            f"# 筛选暂存底稿（未入库） {row['source_id']}｜{row['title'] or row['natural_key']}",
            "",
            "> ⚠ 本底稿处于前期筛选暂存区：未进入正式 SQL 证据库，未进入 WeKnora 底稿 RAG。",
            "> 如需作为正式证据入库，须由客户明确要求后执行 persist（提升落库）。",
            "",
            "## 溯源信息",
            f"- Source_ID: `{row['source_id']}`",
            f"- 企业: {row['company_name']}（{row['company_key']}）",
            f"- 记录类型: {row['kind']}｜数据商: {row['provider']}",
            f"- 自然主键: `{row['natural_key']}`",
            f"- 抓取时间: {row['last_fetched_at']}｜版本: v{row['version']}",
            "",
            "## 记录内容",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=1),
            "```",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        conn.execute("UPDATE staging_sources SET dossier_path=? WHERE source_id=?",
                     (str(path), row["source_id"]))
        n += 1
    conn.commit()
    return n


def promote_staging_to_main(company_key: str) -> dict:
    """客户明确表述要入库后：把该企业暂存数据提升到正式证据库。
    1) staging_sources 逐条经 upsert_source 写入正式 SQL sources 表；
    2) 渲染正式 Markdown 底稿（此后才会由 ingest_weknora 统一入库 RAG，与
       平台既有底稿同规范）；
    3) 回写 staging_sources.promoted=1 留痕。
    返回 {promoted: n, weknora: 'pending'}——WeKnora 入库仍走既有
    ingest_weknora.py 流程（weknora_status=pending），保持幂等。"""
    sconn = staging_connect()
    mconn = connect()
    try:
        rows = sconn.execute(
            "SELECT * FROM staging_sources WHERE company_key=? AND promoted=0", (company_key,)).fetchall()
        n = 0
        for row in rows:
            envelope = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))
            sid = upsert_source(
                mconn, kind=row["kind"], provider=row["provider"],
                company_key=row["company_key"], company_name=row["company_name"],
                natural_key=row["natural_key"], title=row["title"],
                payload=envelope["payload"], meta={"promoted_from": "screening_staging"})
            import dossier as _dossier
            _dossier.render(mconn, sid)
            sconn.execute("UPDATE staging_sources SET promoted=1, promoted_at=? WHERE source_id=?",
                          (now(), row["source_id"]))
            n += 1
        sconn.commit()
        return {"company_key": company_key, "promoted": n,
                "weknora": "pending（正式底稿已生成，待 ingest_weknora 入 RAG，与平台底稿统一流程）"}
    finally:
        mconn.close()
        sconn.close()