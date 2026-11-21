#!/usr/bin/env python3
"""SQLite 数据供给层：引擎不再直读 raw JSON，全部改从 SQLite 取数。

为 sources 表新增 payload_json 列（迁移 v1→v2），把 raw 文件里的 payload
一次性回填进库；新增 data_provider 模块，供引擎以 SQL 查询取得：
  - 专利记录（替代 patent_quality.parse_patents 直读文件）
  - 财务指标（AKShare 利润表/财务指标 → 创新积分2.0 财务口径）
  - Source_ID 白名单
引擎侧唯一入口：load_patents(company_key) / load_finance(company_key) /
load_allowed_source_ids(company_key)。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = Path("/root/projects/techcredit/data/techcredit.db")



def migrate_v2(conn: sqlite3.Connection) -> dict:
    """加列 + 从 raw 文件回填 payload_json。幂等。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sources)")]
    added = "payload_json" in cols
    if not added:
        conn.execute("ALTER TABLE sources ADD COLUMN payload_json TEXT")
    backfilled, missing = 0, 0
    rows = conn.execute(
        "SELECT source_id, raw_path FROM sources "
        "WHERE payload_json IS NULL OR payload_json=''").fetchall()
    for sid, raw_path in rows:
        try:
            env = json.loads(Path(raw_path).read_text(encoding="utf-8"))
            payload = env.get("payload")
            conn.execute(
                "UPDATE sources SET payload_json=? WHERE source_id=?",
                (json.dumps(payload, ensure_ascii=False), sid))
            backfilled += 1
        except (json.JSONDecodeError, OSError):
            missing += 1
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return {"column_added": not added, "backfilled": backfilled,
            "missing_raw": missing}


def load_patents(company_key: str, conn: sqlite3.Connection | None = None,
                 viewer_email: str | None = None) -> list:
    """从 SQLite 取专利记录，经 patent_quality.normalize_payload 标准化
    （与文件路径 parse_patents 输出结构完全一致）。
    viewer_email 给定时按团队鉴权过滤：上传底稿仅项目成员可见。"""
    import patent_quality
    conn = conn or _connect()
    if viewer_email is None:
        rows = conn.execute(
            "SELECT source_id, payload_json FROM sources "
            "WHERE company_key=? AND kind='patent' ORDER BY natural_key",
            (company_key,)).fetchall()
    else:
        import projects as _proj
        _proj.ensure_schema(conn)
        rows = conn.execute(
            "SELECT source_id, payload_json FROM sources "
            "WHERE company_key=? AND kind='patent' AND " + _proj.VISIBLE_SQL +
            " ORDER BY natural_key", (company_key, viewer_email)).fetchall()
    patents = []
    for sid, pj in rows:
        if not pj:
            continue
        rec = patent_quality.normalize_payload(json.loads(pj), sid)
        patents.append(rec)
    return patents


def load_finance(company_key: str, conn: sqlite3.Connection | None = None,
                 viewer_email: str | None = None) -> dict:
    """从 SQLite 财务源合成创新积分2.0财务口径（研发费用/营收/增速/ROE）。

    AKShare 利润表 fields → rd_expense_wan / main_revenue_wan / rev_growth_pct / roe_pct
    多期记录取最新一期；研发占营收比同时回填 rd_ratio_pct。
    viewer_email 给定时按团队鉴权过滤上传财务底稿。
    """
    conn = conn or _connect()
    if viewer_email is None:
        rows = conn.execute(
            "SELECT payload_json, natural_key FROM sources "
            "WHERE company_key=? AND kind='financial' ORDER BY natural_key DESC",
            (company_key,)).fetchall()
    else:
        import projects as _proj
        _proj.ensure_schema(conn)
        rows = conn.execute(
            "SELECT payload_json, natural_key FROM sources "
            "WHERE company_key=? AND kind='financial' AND " + _proj.VISIBLE_SQL +
            " ORDER BY natural_key DESC", (company_key, viewer_email)).fetchall()
    out = {}
    latest_income = None
    latest_ind = None
    for pj, nk in rows:
        if not pj:
            continue
        p = json.loads(pj)
        stmt = p.get("statement")
        if stmt == "利润表" and latest_income is None:
            latest_income = p
        elif stmt == "财务指标" and latest_ind is None:
            latest_ind = p
    if latest_income:
        f = latest_income.get("fields") or {}
        if f.get("研发费用") is not None:
            out["rd_expense_wan"] = round(float(f["研发费用"]) / 1e4, 2)
        if f.get("营业收入") is not None:
            out["main_revenue_wan"] = round(float(f["营业收入"]) / 1e4, 2)
        if f.get("研发费用占营收比(%)") is not None:
            out["rd_ratio_pct"] = float(f["研发费用占营收比(%)"])
    if latest_ind:
        f = latest_ind.get("fields") or {}
        if f.get("净资产收益率(%)") is not None:
            out["roe_pct"] = float(f["净资产收益率(%)"])
        if f.get("主营业务收入增长率(%)") is not None:
            out["rev_growth_pct"] = float(f["主营业务收入增长率(%)"])
    return out


def load_allowed_source_ids(company_key: str, conn: sqlite3.Connection | None = None) -> set:
    conn = conn or _connect()
    return {r[0] for r in conn.execute(
        "SELECT source_id FROM sources WHERE company_key=?", (company_key,))}


def _connect():
    import sys
    sys.path.insert(0, "/root/projects/techcredit/data_layer")
    import store
    return store.connect()


def main():
    conn = _connect()
    info = migrate_v2(conn)
    print("迁移:", info)
    for ck in ("91350900587527783P", "91110302801786752A"):
        patents = load_patents(ck, conn)
        fin = load_finance(ck, conn)
        allowed = load_allowed_source_ids(ck, conn)
        print(f"{ck}: 专利{len(patents)}件 财务口径{fin} 白名单{len(allowed)}个")


if __name__ == "__main__":
    main()
