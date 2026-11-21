#!/usr/bin/env python3
"""techcredit 引擎 CLI（Python功能实现层入口）。

用法（在云端服务器的项目 engine/ 目录下）：
  python3 run_engine.py --company 91350900587527783P --domain 新能源与节能 --stage 稳定期
      # 跑：OECD PQI + 创新积分2.0 + 硬排雷 + 漏斗抽样 → 生成4个Skill上下文包
  python3 run_engine.py ... --finance-file finance.json --bonus-file bonus.json
      # 带财务口径输入（研发费用/收入/ROE等，接税务或天眼查后自动回填）
  python3 run_engine.py ... --agent-out dir/
      # 校验熔断模式：消费4个Skill的输出JSON，跑Pydantic+Source_ID核查，出综合信用分
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "data_layer"))

import store                      # noqa: E402
import config                     # noqa: E402
import data_provider              # noqa: E402
import innovation2                # noqa: E402
import patent_quality             # noqa: E402
import funnel                     # noqa: E402
import schemas                    # noqa: E402
import validate                   # noqa: E402
import score                      # noqa: E402
import facts_bridge               # noqa: E402

ENGINE_VERSION = "1.0.0"

EVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_runs(
  run_id         TEXT PRIMARY KEY,
  company_key    TEXT NOT NULL,
  company_name   TEXT,
  domain         TEXT,
  stage          TEXT,
  engine_version TEXT,
  created_at     TEXT NOT NULL,
  mode           TEXT,
  core_score     REAL,
  bonus_score    REAL,
  total_score    REAL,
  pqi_composite  REAL,
  final_score    REAL,
  grade          TEXT,
  veto           INTEGER DEFAULT 0,
  result_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_company ON evaluation_runs(company_key, created_at);
CREATE TABLE IF NOT EXISTS metric_values(
  run_id  TEXT NOT NULL,
  code    TEXT NOT NULL,
  category TEXT,
  raw_value REAL,
  missing INTEGER,
  score   REAL,
  weight  REAL,
  weighted REAL,
  PRIMARY KEY(run_id, code)
);
CREATE TABLE IF NOT EXISTS risk_flags(
  run_id TEXT NOT NULL,
  code TEXT, level TEXT, title TEXT, detail TEXT, source_ids_json TEXT
);
CREATE TABLE IF NOT EXISTS agent_outputs(
  run_id TEXT NOT NULL,
  skill TEXT NOT NULL,
  ok INTEGER, retries INTEGER, errors_json TEXT, payload_json TEXT,
  created_at TEXT,
  PRIMARY KEY(run_id, skill)
);
"""

# v3：团队协同——运行归属与项目作用域（跨项目运行结果按成员可见性过滤）
EVAL_V3 = """
ALTER TABLE evaluation_runs ADD COLUMN viewer_email TEXT;
ALTER TABLE evaluation_runs ADD COLUMN project_ids_json TEXT;
"""


def _ensure_eval_v3(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(evaluation_runs)")}
    if "viewer_email" not in cols:
        conn.execute("ALTER TABLE evaluation_runs ADD COLUMN viewer_email TEXT")
    if "project_ids_json" not in cols:
        conn.execute("ALTER TABLE evaluation_runs ADD COLUMN project_ids_json TEXT")
    conn.commit()

CORE_FROM_FINANCE = {
    innovation2.RD_EXPENSE: "rd_expense_wan",
    innovation2.RD_RATIO: "rd_ratio_pct",
    innovation2.RD_STAFF_RATIO: "rd_staff_ratio_pct",
    innovation2.INVENTION_GRANTS: "invention_grants",
    innovation2.IP_PER_100_RD: "ip_per_100_rd",
    innovation2.TECH_CONTRACT: "tech_contract_wan",
    innovation2.MAIN_REVENUE: "main_revenue_wan",
    innovation2.REV_GROWTH: "rev_growth_pct",
    innovation2.ROE: "roe_pct",
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_patents_and_company(company_key: str, viewer_email: str | None = None):
    """SQLite 供给：专利记录从 sources 表读取（v2 起 payload 入库）。
    viewer_email 给定时按团队鉴权过滤上传底稿。"""
    data_provider.migrate_v2(store.connect())
    conn = store.connect()
    patents = data_provider.load_patents(company_key, conn, viewer_email=viewer_email)
    row = conn.execute("SELECT company_name FROM sources WHERE company_key=? LIMIT 1",
                       (company_key,)).fetchone()
    company = {"company_key": company_key, "company_name": row[0] if row else company_key}
    return patents, company, conn


def load_staging_patents_and_company(company_key: str):
    """前期筛选阶段专用供给：只读筛选暂存区（staging.db），不触碰正式库。
    用于两段漏斗②贵数据复查后的预分析——数据尚未（也不应）进入正式 SQL
    证据库与 WeKnora RAG，引擎仅基于暂存数据给出筛选参考分。
    标准化统一走 patent_quality.normalize_payload（与 SQLite/文件路径同构）：
    类型按公告号/申请号双信号重判（实用新型不再被误计为发明），
    apdt/pbdt/inventor/ipc4_list/family_offices/legal_status/abstract/claim_1 全量透传。"""
    conn = store.staging_connect()
    rows = conn.execute("SELECT * FROM staging_sources WHERE company_key=? AND kind='patent'",
                        (company_key,)).fetchall()
    patents = []
    for r in rows:
        try:
            envelope = json.loads(Path(r["raw_path"]).read_text(encoding="utf-8"))
        except Exception:
            continue
        payload = envelope.get("payload") or {}
        rec = patent_quality.normalize_payload(payload, r["source_id"])
        rec["staging"] = True
        if not rec.get("title"):
            rec["title"] = r["title"]
        if not rec.get("pn"):
            rec["pn"] = payload.get("patent_number")
        patents.append(rec)
    row = conn.execute("SELECT company_name FROM staging_sources WHERE company_key=? "
                       "AND company_name IS NOT NULL LIMIT 1", (company_key,)).fetchone()
    company = {"company_key": company_key, "company_name": row[0] if row else company_key}
    return patents, company, conn


def derive_patent_inputs(patents) -> dict:
    """专利口径可直接回填的创新积分数值。
    INVENTION_GRANTS=「发明专利授权量」口径（创新积分2.0官方定义）：
      - type=invention（B/Y授权公告号）→ 计1件授权发明；
      - type=invention_pending（A公开号）且有显式 grant_date → 已授权，计1件；
      - A公开号且无 grant_date → 授权状态未知：若组合内无任何可确认授权，
        该指标视为数据缺口返回空（缺失调权），不虚报0也不虚计授权。
    无专利样本时同样返回空——数据缺口≠0分，不得虚增惩罚。"""
    if not patents:
        return {}
    granted = sum(1 for p in patents
                  if p["type"] == "invention"
                  or (p["type"] == "invention_pending" and p.get("grant_days") is not None))
    unknown_pending = sum(1 for p in patents
                          if p["type"] == "invention_pending" and p.get("grant_days") is None)
    if granted == 0 and unknown_pending > 0:
        return {}   # 全部为授权状态不明的申请公开件：指标缺失走动态调权
    return {innovation2.INVENTION_GRANTS: float(granted)}


def run(company_key: str, domain: str, stage: str, finance: dict, bonus: dict,
        patent_top: int, viewer_email: str | None = None,
        staging: bool = False) -> dict:
    """运行引擎。viewer_email 给定时启用团队鉴权：
    SQLite 数据供给与 WeKnora 事实桥的白名单都按项目成员可见性收窄，
    评估过程只见平台抓取数据 + 本团队项目上传的底稿。

    staging=True 为前期筛选阶段专用模式（数据落库边界）：
      - 数据供给只读筛选暂存区 staging.db（贵数据复查抓取结果）；
      - 跳过 WeKnora 事实桥（暂存数据绝不在 RAG 检索中出现）；
      - 上下文包写入 STAGING_CONTEXT_DIR，与正式包目录隔离；
      - 评估记录（evaluation_runs/metric_values/risk_flags）落 staging.db，
        不写正式 techcredit.db。
    该模式只产出筛选参考分，不代表底稿认证阶段的正式信用评估。"""
    if staging:
        patents, company, conn = load_staging_patents_and_company(company_key)
        auto_fin = {}          # 财务自动合成只对正式库有意义，筛选模式用传入口径
        merged = dict(auto_fin)
        merged.update({k: v for k, v in finance.items() if v is not None})
        finance = merged
        viewer_email = None    # 团队鉴权只约束正式库，筛选阶段不适用
    else:
        patents, company, conn = load_patents_and_company(company_key, viewer_email)
        # SQLite 财务口径自动合成（研发费用/营收/ROE/增速），显式输入优先
        auto_fin = data_provider.load_finance(company_key, conn, viewer_email=viewer_email)
        merged = dict(auto_fin)
        merged.update({k: v for k, v in finance.items() if v is not None})
        finance = merged

    # ① OECD PQI 专利质量
    pqi = patent_quality.compute_pqi(patents)

    # ② 创新积分2.0（专利口径自动回填 + 财务口径覆盖 + 缺失动态调权）
    inputs = {}
    for ind, key in CORE_FROM_FINANCE.items():
        if key in finance and finance[key] is not None:
            inputs[ind] = float(finance[key])
    inputs.update(derive_patent_inputs(patents))
    inputs.update({k: v for k, v in finance.items()
                   if k in innovation2.CORE_INDICATORS and v is not None})
    innovation_result = innovation2.compute_innovation(inputs, domain=domain,
                                                        stage=stage, bonus=bonus)

    # ③ 硬排雷 + 漏斗抽样（筛选阶段：上下文包写暂存目录，与正式包隔离）
    risk_flags = funnel.evaluate_risks(patents, pqi)
    funnel_result = funnel.funnel_select(patents, pqi, top_patents=patent_top)
    pack_dir = (config.STAGING_CONTEXT_DIR if staging
                else config.DATA_DIR / "context") / company_key
    packs = funnel.build_context_packs(company, patents, pqi, risk_flags,
                                       funnel_result, innovation_result, pack_dir)

    if staging:
        # 数据落库边界：筛选阶段数据不入 WeKnora 底稿 RAG，因此跳过事实桥，
        # 包内 weknora_facts 留空并在评估结果中显式标注 degraded 原因。
        core_ids = [p.get("source_id") for p in funnel_result.get("core_patents", [])
                    if p.get("source_id")]
        facts = {skill: {"facts": [], "degraded": True,
                         "note": "筛选暂存阶段：数据未入正式SQL与底稿RAG，无WeKnora事实段"}
                for skill in schemas.OUTPUT_MODELS}
        fact_packs = facts_bridge.attach_facts_to_packs(pack_dir, facts)
    else:
        # ④ WeKnora 事实桥：SQLite 圈白名单 → WeKnora hybrid 检索出事实段 → 注入包
        core_ids = [p.get("source_id") for p in funnel_result.get("core_patents", [])
                    if p.get("source_id")]
        facts = facts_bridge.build_fact_bridges(
            conn, company_key, core_patent_ids=core_ids, viewer_email=viewer_email)
        fact_packs = facts_bridge.attach_facts_to_packs(pack_dir, facts)

    # ④ 落库（评估主表+指标明细+风险灯）——staging 模式落 staging.db，不写正式库
    run_id = ("run-staging-" if staging else "run-") + \
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    conn.executescript(EVAL_SCHEMA)
    _ensure_eval_v3(conn)
    # 团队协同：记录运行归属与项目作用域（供 get_latest_analysis 按成员可见性过滤）
    project_ids_json = None
    if viewer_email:
        import projects as _proj
        _proj.ensure_schema(conn)
        pids = [r[0] for r in conn.execute(
            "SELECT DISTINCT s.project_id FROM sources s "
            "WHERE s.company_key=? AND s.project_id IS NOT NULL "
            "AND s.project_id IN (SELECT project_id FROM project_members WHERE email=?)",
            (company_key, viewer_email))]
        project_ids_json = json.dumps(pids, ensure_ascii=False)
    conn.execute("INSERT INTO evaluation_runs(run_id,company_key,company_name,domain,stage,"
                 "engine_version,created_at,mode,core_score,bonus_score,total_score,pqi_composite,"
                 "viewer_email,project_ids_json)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, company_key, company["company_name"], domain, stage,
                  ENGINE_VERSION, _now(),
                  "screening_staging" if staging else "engine",
                  innovation_result["core_score"], innovation_result["bonus_score"],
                  innovation_result["total_score"], pqi.get("composite"),
                  viewer_email, project_ids_json))
    for m in innovation_result["metrics"]:
        conn.execute("INSERT OR REPLACE INTO metric_values VALUES(?,?,?,?,?,?,?,?)",
                     (run_id, m["code"], m["category"], m["raw"],
                      1 if m["missing"] else 0, m["score"], m["weight"], m["weighted"]))
    for f in risk_flags:
        conn.execute("INSERT INTO risk_flags VALUES(?,?,?,?,?,?)",
                     (run_id, f["code"], f["level"], f["title"], f["detail"],
                      json.dumps(f["source_ids"], ensure_ascii=False)))
    conn.commit()

    return {"run_id": run_id, "company": company, "pqi": pqi,
            "innovation": innovation_result, "risk_flags": risk_flags,
            "funnel": funnel_result, "packs": packs, "fact_packs": fact_packs,
            "staging": staging,
            "data_boundary_note": ("前期筛选暂存口径：数据未入正式SQL证据库、未入WeKnora底稿RAG；"
                                   "客户明确要求入库后方可提升（persist）并进入底稿认证闭环"
                                   if staging else ""),
            "facts_summary": {k: {"degraded": v["degraded"],
                                  "n": len(v["facts"])} for k, v in facts.items()}}


def run_with_agent_outputs(base_result: dict, agent_dir: Path) -> dict:
    """校验熔断 + 总分汇总。agent_dir 下放 4 个文件：patent_review.json 等。"""
    conn = store.connect()
    company_key = base_result["company"]["company_key"]
    allowed = validate.load_allowed_source_ids(conn, company_key)
    pack = json.loads(Path(base_result["packs"]["patent_review"]).read_text(encoding="utf-8"))
    allowed = {s for s in (allowed | set(pack.get("allowed_source_ids", [])))}

    breaker = validate.CircuitBreaker()
    results = {}
    for skill in schemas.OUTPUT_MODELS:
        fp = agent_dir / f"{skill}.json"
        if not fp.exists():
            continue
        verdict = breaker.attempt(skill, json.loads(fp.read_text(encoding="utf-8")), allowed)
        conn.execute("INSERT OR REPLACE INTO agent_outputs VALUES(?,?,?,?,?,?,?)",
                     (base_result["run_id"], skill, 1 if verdict["ok"] else 0,
                      breaker.fails.get(skill, 0),
                      json.dumps(verdict["errors"], ensure_ascii=False),
                      None, _now()))
        conn.commit()
        if verdict["ok"]:
            results[skill] = verdict

    geval_scores = results.get("patent_review", {}).get("geval_scores", {})
    semantic = score.semantic_score_from_geval(geval_scores)
    fact_score = None
    if "anti_packaging_factcheck" in results:
        fact_score = score.fact_score_from_claims(results["anti_packaging_factcheck"]
                                                  ["result"].claims)
    all_core_invalid = False
    flags = base_result["risk_flags"]
    if flags:
        invalid_codes = {"R3_INVALID_HEAVY"}
        all_core_invalid = any(f["code"] in invalid_codes and f["level"] == "red"
                               for f in flags) and bool(geval_scores) and \
            all(r.rubric_level == 1 for r in results["patent_review"]["result"].patents)
    final = score.compute_final(innovation_result=base_result["innovation"],
                                semantic_score=semantic,
                                risk_flags=flags,
                                fact_score=fact_score,
                                all_core_invalid=all_core_invalid)
    conn.execute("UPDATE evaluation_runs SET final_score=?, grade=?, veto=?, result_json=? "
                 "WHERE run_id=?",
                 (final["final_score"], final["grade"], 1 if final["veto"] else 0,
                  json.dumps(final, ensure_ascii=False), base_result["run_id"]))
    conn.commit()
    return {"final": final, "semantic_score": semantic, "fact_score": fact_score,
            "agent_results": {k: v["geval_scores"] if k == "patent_review" else "ok"
                              for k, v in results.items()}}


def main():
    ap = argparse.ArgumentParser(description="企业技术信用评估引擎")
    ap.add_argument("--company", required=True, help="企业统一社会信用代码(company_key)")
    ap.add_argument("--domain", default="通用", choices=innovation2.DOMAINS)
    ap.add_argument("--stage", default="成长期", choices=innovation2.STAGES)
    ap.add_argument("--finance-file", default=None, help="财务口径JSON")
    ap.add_argument("--bonus-file", default=None, help="加分项JSON")
    ap.add_argument("--patent-top", type=int, default=5, help="漏斗精选核心专利数(5-8)")
    ap.add_argument("--agent-out", default=None, help="4个Skill输出目录（校验熔断模式）")
    ap.add_argument("--viewer-email", default=None,
                    help="团队鉴权：按该邮箱的项目成员可见性过滤数据（不传=内部全量）")
    ap.add_argument("--staging", action="store_true",
                    help="前期筛选模式：只读筛选暂存区staging.db，跳过WeKnora事实桥，"
                         "评估记录落暂存库（数据默认不入正式SQL/RAG，仅供筛选参考分）")
    a = ap.parse_args()

    finance = json.loads(Path(a.finance_file).read_text(encoding="utf-8")) if a.finance_file else {}
    bonus = json.loads(Path(a.bonus_file).read_text(encoding="utf-8")) if a.bonus_file else {}

    base = run(a.company, a.domain, a.stage, finance, bonus, a.patent_top,
               viewer_email=a.viewer_email, staging=a.staging)

    inn = base["innovation"]
    print(f"== 引擎运行 {base['run_id']} ==")
    print(f"企业: {base['company']['company_name']} | 领域: {a.domain} | 阶段: {a.stage}")
    print(f"创新积分2.0 核心{inn['core_score']} + 加分{inn['bonus_score']} "
          f"= 总分{inn['total_score']}（{inn['grade']}级）")
    print(f"缺失调权指标: {inn['missing_reweighted'] or '无'}")
    pqi = base["pqi"]
    print(f"PQI composite: {pqi.get('composite')} "
          f"(发明占比{pqi.get('dims', {}).get('invention_share', {}).get('value')})")
    print(f"风险灯: 红{[f['code'] for f in base['risk_flags'] if f['level']=='red']} "
          f"黄{[f['code'] for f in base['risk_flags'] if f['level']=='yellow']}")
    print(f"漏斗: 专利{pqi.get('n_patents')}件 → Top{len(base['funnel']['core_patents'])}")
    for name, path in base["packs"].items():
        print(f"  上下文包: {path}")
    fs = base.get("facts_summary") or {}
    if fs:
        print("WeKnora事实桥: " + " | ".join(
            f"{k}={'降级' if v['degraded'] else '在线'}×{v['n']}" for k, v in fs.items()))

    if a.agent_out:
        out = run_with_agent_outputs(base, Path(a.agent_out))
        print("\n== 校验熔断+总分汇总 ==")
        print(json.dumps(out["final"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
