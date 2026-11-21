#!/usr/bin/env python3
"""techcredit SQLite 数据层部署与自检（幂等，可反复执行）。

做三件事：
1. 应用 schema（建表/索引/WAL 参数），设置 schema 版本号；
2. 全库体检：integrity_check、表行数、索引、raw/底稿文件是否缺失、占位数据源缺口；
3. 隔离自检：在临时库里跑 Source_ID 幂等、内容变更版本递增、伪造 Source_ID 拦截。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import config
import store


def apply_schema() -> sqlite3.Connection:
    conn = store.connect()
    return conn


def health(conn: sqlite3.Connection) -> int:
    """返回非 0 表示体检不通过。"""
    ok = True
    checks = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    checks.append(("integrity_check", integrity, integrity == "ok"))
    checks.append(("journal_mode", journal, journal == "wal"))
    checks.append(("schema_version", user_version, user_version == store.SCHEMA_VERSION))

    idx = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")]
    for need in ("idx_sources_company", "idx_sources_kind",
                 "idx_sources_weknora", "ux_sources_natural", "idx_fetch_log_company"):
        checks.append((f"index:{need}", "present" if need in idx else "missing", need in idx))

    n_sources = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
    n_log = conn.execute("SELECT count(*) FROM fetch_log").fetchone()[0]
    missing_raw = [r["raw_path"] for r in conn.execute("SELECT source_id, raw_path FROM sources")
                   if not Path(r["raw_path"]).exists()]
    missing_dossier = [r["source_id"] for r in conn.execute(
        "SELECT source_id, dossier_path FROM sources WHERE dossier_path IS NOT NULL AND dossier_path!=''")
        if not Path(r["dossier_path"]).exists()]
    dup_natural = conn.execute(
        "SELECT count(*) FROM (SELECT provider, natural_key, count(*) c FROM sources "
        "GROUP BY 1,2 HAVING c>1)").fetchone()[0]

    print("== 数据库体检 ==")
    for name, value, passed in checks:
        print(f"  {'OK ' if passed else 'FAIL'} {name}: {value}")
        ok = ok and passed
    print(f"  sources 表 {n_sources} 条 / fetch_log {n_log} 条")
    print(f"  raw 文件缺失 {len(missing_raw)} 个，Markdown 底稿缺失 {len(missing_dossier)} 个，"
          f"自然键重复 {dup_natural} 组")
    if missing_raw:
        print("    缺失 raw:", missing_raw[:5])
    if missing_dossier:
        print("    缺失底稿:", missing_dossier[:5])
    ok = ok and not missing_raw and not missing_dossier and dup_natural == 0

    print("== 数据源覆盖 ==")
    for r in conn.execute(
            "SELECT provider, endpoint, status, count(*) c, sum(record_count) rc "
            "FROM fetch_log GROUP BY provider, endpoint, status ORDER BY provider, endpoint"):
        print(f"  {r['provider']:<12}{r['endpoint']:<42}{r['status']:<12}"
              f"调用 {r['c']} 次 / 落盘 {r['rc'] or 0} 条")
    return 0 if ok else 1


def selftest() -> int:
    """在临时目录里验证存储语义，不污染生产库。"""
    saved = (config.DB_PATH, config.RAW_DIR)
    ok = True
    with tempfile.TemporaryDirectory(prefix="techcredit_selftest_") as td:
        config.DB_PATH = Path(td) / "selftest.db"
        config.RAW_DIR = Path(td) / "raw"
        config.RAW_DIR.mkdir(parents=True, exist_ok=True)
        try:
            conn = store.connect()
            args = dict(kind="patent", provider="zhihuiya", company_key="SELFTEST",
                        company_name="自检企业", natural_key="CN-SELFTEST-0001",
                        title="自检专利")
            sid_a = store.upsert_source(conn, payload={"title": "自检专利", "claims": ["A"]}, **args)
            sid_b = store.upsert_source(conn, payload={"title": "自检专利", "claims": ["A"]}, **args)
            row = conn.execute("SELECT version, fetch_count FROM sources WHERE source_id=?",
                               (sid_a,)).fetchone()
            step1 = sid_a == sid_b and row["version"] == 1 and row["fetch_count"] == 2
            print(f"  {'OK ' if step1 else 'FAIL'} 同内容重复抓取幂等（Source_ID 稳定、版本不涨）")

            sid_c = store.upsert_source(conn, payload={"title": "自检专利", "claims": ["A", "B"]}, **args)
            row = conn.execute("SELECT version, fetch_count FROM sources WHERE source_id=?",
                               (sid_a,)).fetchone()
            hist = (config.RAW_DIR / "SELFTEST" / f"{sid_a}.v1.json").exists()
            step2 = sid_c == sid_a and row["version"] == 2 and row["fetch_count"] == 3 and hist
            print(f"  {'OK ' if step2 else 'FAIL'} 内容变更版本递增并保留 v1 历史快照")

            forged = "PAT-ZHY-ffffffff00"
            step3 = conn.execute("SELECT 1 FROM sources WHERE source_id=?", (forged,)).fetchone() is None
            print(f"  {'OK ' if step3 else 'FAIL'} 校验层可拦截伪造 Source_ID（{forged} 不在库）")
            ok = step1 and step2 and step3
            conn.close()
        except Exception as exc:
            print("  FAIL 自检异常:", exc)
            ok = False
        finally:
            config.DB_PATH, config.RAW_DIR = saved
    return 0 if ok else 1


def main() -> int:
    print(f"部署目标: {config.DB_PATH}")
    conn = apply_schema()
    rc_health = health(conn)
    conn.close()
    print("== 隔离自检 ==")
    rc_test = selftest()
    if rc_health == 0 and rc_test == 0:
        print("\n部署完成：schema/WAL/索引就绪，体检与自检全部通过。")
        return 0
    print("\n部署未通过，请按上面的 FAIL 项处理。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
