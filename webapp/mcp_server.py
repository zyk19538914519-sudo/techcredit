#!/usr/bin/env python3
"""techcredit 详细分析版 MCP（Streamable HTTP，Bearer = FateBuddy 账户接入码）。

复用 FateBuddy 注册登录体系：
  - 用户在 https://fatebuddy.asia/signup 注册 / /signin 登录（邮箱验证或密码）；
  - 账户签发的接入码（user_keys ⋈ keys）即本 MCP 的 Bearer 凭证；
  - 本服务只读校验接入码有效性（active 且未到期），不扣减 FateBuddy 调用额度。

★ 数据落库边界（2026-11-21 确立，务必遵守）：
  前期筛选阶段（两段漏斗：screen_market 便宜海选 → deep_verify 贵数据复查）产生的
  企业数据，默认只进筛选暂存区（screening_staging/staging.db 独立库）：
  【不写正式 SQL 证据库 techcredit.db 的 sources 表】【不入 WeKnora 底稿 RAG】。
  只有客户明确表述要入库时才落正式库：
    ① deep_verify(persist=true) 发起贵复查时同步声明入库；
    ② 或先按默认暂存复查，客户确认后再调 persist_screening_result 提升落库。
  底稿认证阶段（upload_dossier / 4-Skill 闭环）数据是客户主动交付，按既有流程
  正常双落库，不受本边界影响。便宜海选 screen_market 纯本地计算，天然零落库。

工具（详细分析口径，默认零付费API调用，仅本地缓存+免费粗筛数据）：
  search_companies / get_company_profile / analyze_company / get_latest_analysis
  团队协同：list_my_projects / create_project / add_project_member / search_project_facts
           project_memory_add / project_memory_search
  两段漏斗（海选→选定→贵复查）：
    screen_market         ①便宜海选(上市)：全A股业绩报表底池(约1.15万家,免费缓存零网络)广筛硬科技潜客
                          纯计算不落任何库
    screen_sme            ①便宜海选(小微)：GGZY全国公共资源交易中标主体库(免费官方无Key)关键词扫池，
                          覆盖非上市科技小微，中标数/成立年限/法人信号，USCC直连贵复查，零落库
    screen_sme_web        ①便宜海选(小微Web)：Tavily搜索(廉价付费)发现资质公示/网络可见科技小微，
                          挖企业名/专精特新等资质/联系方式线索，verified=false，零落库
    screen_ipo            ①便宜海选(拟上市)：东财IPO申报/辅导备案池(免费缓存)优质成长科技中小微，
                          北交所=创新型中小，含招股书PDF(免费完整底稿)，零落库
  第一层质量过滤/确认（QCC免费积分主、Tavily廉价兜底，零落库）：
    risk_screen           负面风险筛(排除层)：失信/被执行/处罚/经营异常等红黄旗标，red≥1建议剔除
    qualify_company       资质核实+触达(确认层)：专精特新/小巨人/高新等资质+联系方式线索
    deep_verify           ②贵数据复查(成本闸门)：confirm=false只预估成本；confirm=true异步多源抓取
                          (智慧芽专利+天眼查工商/司法/风险+企查查+剑鱼中标+公共资源+财务+官网全文)
                          默认只落筛选暂存区(不入正式SQL/不入RAG)；persist=true(客户明确要入库)才
                          提升落库+入RAG；每账户≤10次
    persist_screening_result  客户明确表述要入库后的显式提升：暂存区→正式SQL+WeKnora RAG
    get_deep_verify       轮询贵复查作业→done后返回筛选预分析；persist=true时可直接接4-Skill闭环
    staging_preview       ★降级入口：暂存区数据直接跑引擎预分析（作业failed/未入库均可用），
                          零付费、只读暂存区、不入正式SQL/RAG；PQI+参考分+红黄灯+漏斗(含法律状态)
    get_skill_instructions ★量表阅读与正式提交解耦：无需run_id读5个Skill量表SKILL.md全文，
                          筛选/深挖阶段即可对暂存数据做量表研判（标注非正式闭环）
  功能一·潜客侦察（底稿前，公开数据）：
    discover_prospects    免费公开口径粗筛名单按优质度/潜力排序，逐指标证据+缺失标注
    get_prospect_pack     公开证据包+公开触达渠道(官网/注册地址/底稿线索，enrich可选在线补全)
                          +skill5_prospect_scout量表指令；输出经project_memory_add落项目
  功能二·服务闭环（对方Agent全流程，底稿后）：
    upload_dossier        MCP上传底稿 → SQLite+WeKnora双落库 → 引擎分析（任意企业USCC）
    get_context_pack      按run_id取4个Skill上下文包内容+SKILL.md量表指令（含WeKnora事实段）
    submit_skill_outputs  回传Skill输出JSON → 团队白名单+Pydantic校验+Source_ID伪造拦截
                          +持久熔断 → 综合信用分（硬55%+语义35%-风险扣分/一票否决）

部署（2026-11-21 双 MCP 拆分，同一代码按 TC_MCP_PROFILE 启动三个实例）：
  occult venv（含 mcp 库），uvicorn 仅本机监听，nginx 反代：
  - TC_MCP_PROFILE=research TC_MCP_PORT=8092 → /techcredit-research-mcp/
    公网 https://fatebuddy.asia/techcredit-research-mcp/mcp （一画·调研 MCP，18工具）
  - TC_MCP_PROFILE=collab   TC_MCP_PORT=8093 → /techcredit-collab-mcp/
    公网 https://fatebuddy.asia/techcredit-collab-mcp/mcp （一画·项目协作 MCP，13工具）
  - 默认 all（8091）→ /techcredit-mcp/ 旧端点保留全量26工具向后兼容。
  共用5工具：get_company_profile / analyze_company / get_latest_analysis / get_skill_instructions / get_context_pack。
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "data_layer"))

import config            # noqa: E402
import store             # noqa: E402
import innovation2       # noqa: E402
import run_engine        # noqa: E402
import projects          # noqa: E402
import facts_bridge      # noqa: E402
import dossier_pipeline  # noqa: E402
import schemas           # noqa: E402
import validate          # noqa: E402
import score             # noqa: E402

ACCOUNTS_DB = "file:/root/.hermes/agent_api.db?mode=ro"
ANALYSIS_DIR = config.DATA_DIR / "analysis_results"
ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
AUDIT = config.DATA_DIR / "mcp_audit.log"
# 双 MCP 拆分：research=调研 / collab=项目协作 / all=旧端点全量兼容（默认）
PROFILE = os.environ.get("TC_MCP_PROFILE", "all").strip().lower()
PORT = int(os.environ.get("TC_MCP_PORT", "8091"))

CUR_USER = contextvars.ContextVar("tc_mcp_user", default="")

mcp = FastMCP(
    "techcredit-analysis",
    instructions="企业技术信用详细分析（创新积分2.0 + OECD PQI + 硬排雷 + 漏斗）。"
                 "Bearer 凭证为 FateBuddy 账户接入码；数据为免费口径粗筛名单+本地缓存底稿。"
                 "★数据落库边界：前期筛选阶段（两段漏斗）产生的企业数据默认只进筛选暂存区，"
                 "不入正式SQL证据库、不入WeKnora底稿RAG；只有客户明确表述要入库，才用 "
                 "deep_verify(persist=true) 或 persist_screening_result 落库。"
                 "两段漏斗：①便宜海选 screen_market(上市/业绩报表)或 screen_sme(小微/GGZY中标库免费)或 screen_sme_web(小微/Tavily网络廉价)"
                 "或 screen_ipo(拟上市/IPO申报辅导池免费,含招股书底稿)"
                 "广筛（纯计算/搜索不落库） → 客户选定几家 → ②贵数据复查 deep_verify(confirm=true) 异步多源抓取"
                 "(智慧芽/天眼查/剑鱼等,成本闸门+配额)，默认落筛选暂存区(不入正式SQL/RAG)，"
                 "persist=true(客户明确要入库)才提升落库+入RAG → get_deep_verify 轮询拿结果。"
                 "★降级入口：作业failed或暂存未入库时 staging_preview(company_key) 零成本补跑引擎预分析"
                 "（只读暂存区，不入正式库/RAG）；get_skill_instructions 无需run_id读5个Skill量表，"
                 "筛选阶段即可对暂存数据做量表研判（标注非正式闭环）。"
                 "★零记忆冷启动：staging_preview 内联返回核心专利证据卡(patent_review_pack)，"
                 "get_context_pack 支持暂存run_id并内联返回包＋量表——任意Agent无需本地文件即可跑价值分析。"
                 "海选质量过滤：risk_screen(负面红黄旗标排除)+qualify_company(专精特新/高新资质核实+触达联系方式)，"
                 "QCC免费积分主、积分不足402降级Tavily廉价搜索。"
                 "潜客侦察：discover_prospects 精筛榜 → get_prospect_pack 公开证据+触达渠道+prospect_scout量表 → "
                 "create_project + project_memory_add 落触达方案。"
                 "底稿分析闭环：upload_dossier 上传(双落库+引擎) 或贵复查数据经客户确认入库后 → "
                 "get_context_pack 取Skill上下文包与量表 → "
                 "你的Agent按SKILL.md跑4个Skill → submit_skill_outputs 回传JSON拿校验后的综合信用分。"
                 "团队协同：项目=团队×企业，同一项目的底稿知识库对全体成员可见（跨项目隔离），"
                 "团队内多个成员各领接入码即可让不同 Agent 协同调用同一项目知识库；"
                 "项目共享记忆（project_memory_add/search）沉淀团队尽调结论与备忘。"
                 "铁律：Skill结论只能绑定上下文包 allowed_source_ids 内的真实Source_ID，伪造即拦截；"
                 "触达渠道只用企业公开信息，verified=false 须人工复核，不自动发送消息；"
                 "贵复查 confirm=false 只预估不扣费，confirm=true 才发起贵调用；"
                 "筛选阶段数据非客户明确要求不得主动入正式库或RAG。",
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key_identity(tok: str):
    """只读校验 FateBuddy 接入码 → 返回邮箱或 None。"""
    if not tok:
        return None
    try:
        conn = sqlite3.connect(ACCOUNTS_DB, uri=True, timeout=10)
        row = conn.execute(
            "SELECT u.email, k.status, k.expires_at FROM user_keys uk "
            "JOIN keys k ON k.key = uk.key JOIN users u ON u.id = uk.user_id "
            "WHERE uk.key = ?", (tok,)).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    email, status, exp = row
    if status != "active":
        return None
    if exp and exp < _now():
        return None
    return email


def _audit(tool: str, detail: str):
    try:
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now(), "user": CUR_USER.get(), "profile": PROFILE,
                                "tool": tool, "detail": detail[:200]}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _db():
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _dump(o):
    return json.dumps(o, ensure_ascii=False, default=str)


def _screened(conn, key):
    return conn.execute("SELECT * FROM screened_companies WHERE company_key=?", (key,)).fetchone()


def _finance(inputs):
    m = {"RD_EXPENSE": "rd_expense_wan", "RD_RATIO": "rd_ratio_pct",
         "RD_STAFF_RATIO": "rd_staff_ratio_pct", "INVENTION_GRANTS": "invention_grants",
         "IP_PER_100_RD": "ip_per_100_rd", "TECH_CONTRACT": "tech_contract_wan",
         "MAIN_REVENUE": "main_revenue_wan", "REV_GROWTH": "rev_growth_pct", "ROE": "roe_pct"}
    return {fin: inputs[i] for i, fin in m.items() if inputs.get(i) is not None}


@mcp.tool()
def search_companies(query: str = "", domain: str = "", grade: str = "", limit: int = 20) -> str:
    """搜索创新积分2.0免费粗筛的潜在科创企业名单（初步筛选口径）。
    query 匹配名称/城市/股票代码；domain 为8大技术领域之一；grade 为 A/B/C/D。"""
    conn = _db()
    sql = "SELECT company_key,name,full_name,code,city,domain,stage,core_score,grade,cached_patents FROM screened_companies WHERE 1=1"
    args = []
    if query:
        sql += " AND (name LIKE ? OR full_name LIKE ? OR city LIKE ? OR code LIKE ?)"
        args += [f"%{query}%"] * 4
    if domain:
        sql += " AND domain=?"; args.append(domain)
    if grade:
        sql += " AND grade=?"; args.append(grade)
    sql += " ORDER BY core_score DESC LIMIT ?"
    args.append(max(1, min(int(limit), 100)))
    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    _audit("search_companies", f"q={query} dom={domain} grade={grade} n={len(rows)}")
    return _dump({"ok": True, "count": len(rows), "companies": rows,
                  "note": "粗筛近似分（上市样本同期群归一+缺失调权），详细分析用 analyze_company"})


@mcp.tool()
def get_company_profile(company_key: str) -> str:
    """企业初步档案：粗筛指标明细、工商画像、缓存证据底稿种类（团队鉴权：只计本人可见的底稿）。
    company_key 见 search_companies。"""
    me = CUR_USER.get()
    conn = _db()
    r = _screened(conn, company_key)
    if not r:
        conn.close()
        return _dump({"ok": False, "error": "企业不在粗筛名单中"})
    d = dict(r)
    for k in ("missing_json", "inputs_json", "metrics_json", "profile_json"):
        d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
    projects.ensure_schema(conn)
    d["cached_sources"] = [dict(x) for x in conn.execute(
        "SELECT kind, COUNT(*) c FROM sources WHERE company_key=? AND " +
        projects.VISIBLE_SQL + " GROUP BY kind", (company_key, me))]
    d["viewer"] = me
    conn.close()
    _audit("get_company_profile", company_key)
    return _dump({"ok": True, "company": d})


SKILL_DIRS = {
    "patent_review": ROOT / "skills" / "skill1_patent_review",
    "commercialization_fit": ROOT / "skills" / "skill2_commercialization_fit",
    "anti_packaging_factcheck": ROOT / "skills" / "skill3_anti_packaging",
    "bank_report": ROOT / "skills" / "skill4_bank_report",
    "prospect_scout": ROOT / "skills" / "skill5_prospect_scout",
}
UPSTREAM_SKILLS = ("patent_review", "commercialization_fit", "anti_packaging_factcheck")


def _snapshot_packs(run_id: str, company_key: str) -> str:
    """把本次运行的4个Skill上下文包快照到 analysis_results/{run_id}/packs/，
    避免同企业后续运行覆盖公司级目录导致串包。返回快照目录。"""
    src = config.DATA_DIR / "context" / company_key
    dst = ANALYSIS_DIR / run_id / "packs"
    dst.mkdir(parents=True, exist_ok=True)
    for skill in schemas.OUTPUT_MODELS:
        fp = src / f"pack_{skill}.json"
        if fp.exists():
            shutil.copyfile(fp, dst / fp.name)
    return str(dst)


def _pack_dir_for(run_id: str, company_key: str):
    """优先按run快照取包；无快照（Web/CLI旧运行）回退公司级目录。"""
    snap = ANALYSIS_DIR / run_id / "packs"
    if snap.exists() and any(snap.glob("pack_*.json")):
        return snap, True
    return config.DATA_DIR / "context" / company_key, False


def _staging_context_pack(run_id: str, skill: str, me: str) -> str:
    """暂存运行的上下文包读取（冷启动友好）：与正式 get_context_pack 同接口，
    内联返回包内容＋量表，远程Agent无需访问服务器文件系统。暂存数据为平台抓取
    公共数据，任意已认证账户可读；结果为非正式闭环，不计入正式综合信用分。"""
    sconn = store.staging_connect()
    try:
        row = sconn.execute("SELECT * FROM evaluation_runs WHERE run_id=?", (run_id,)).fetchone()
    finally:
        sconn.close()
    if not row:
        return _dump({"ok": False, "error": "暂存运行不存在", "run_id": run_id,
                      "hint": "先 staging_preview(company_key) 生成暂存预分析运行"})
    company_key = row["company_key"]
    pack_dir = config.STAGING_CONTEXT_DIR / company_key
    names = [skill] if skill else list(schemas.OUTPUT_MODELS)
    if skill and skill not in schemas.OUTPUT_MODELS:
        return _dump({"ok": False, "error": f"skill 须为 {list(schemas.OUTPUT_MODELS)} 之一或留空"})
    packs, missing = {}, []
    for name in names:
        fp = pack_dir / f"pack_{name}.json"
        if fp.exists():
            packs[name] = json.loads(fp.read_text(encoding="utf-8"))
        else:
            missing.append(name)
    out = {"ok": True, "run_id": run_id, "company_key": company_key,
           "company_name": row["company_name"], "staging_run": True, "persisted": False,
           "packs_from": "staging_context", "packs": packs, "missing": missing, "viewer": me,
           "note": "筛选暂存口径·非正式闭环：可据量表研判，但结果不计入正式综合信用分；"
                   "客户明确要入库 → persist_screening_result → analyze_company → 正式4-Skill闭环"}
    if skill:
        md = SKILL_DIRS[skill] / "SKILL.md"
        out["skill_instructions"] = md.read_text(encoding="utf-8") if md.exists() else ""
        out["next_step"] = (f"按 skill_instructions 对 packs['{skill}'] 逐件研判，输出标注"
                            f"'非正式闭环·暂存量表应用'（暂存运行不调用 submit_skill_outputs）")
    else:
        out["next_step"] = "指定 skill 参数取该包＋SKILL.md量表；建议先跑三个上游Skill再取bank_report"
    _audit("get_context_pack", f"{run_id} STAGING skill={skill or 'ALL'} by={me}")
    return _dump(out)


def _visible_run(conn, run_id: str, me: str):
    row = conn.execute("SELECT * FROM evaluation_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None, "运行不存在"
    if not projects.run_visible(conn, row, me):
        return None, "无权访问该运行（他人团队项目作用域）"
    return row, None


@mcp.tool()
def analyze_company(company_key: str, domain: str = "", stage: str = "",
                    company_name: str = "") -> str:
    """运行详细分析引擎：OECD PQI 专利质量 + 创新积分2.0正式分 + 硬排雷红黄灯
    + 漏斗精选核心专利 + 4个Agent上下文包 + WeKnora事实桥。零付费API调用。
    名单内企业自动带粗筛财务口径；任意企业（18位USCC）只要有本人可见的底稿
    （平台缓存或 upload_dossier 上传）也可分析，domain/stage 可选（默认通用/成长期）。
    数据边界：本引擎读正式证据库与RAG；前期筛选阶段暂存数据默认不在此库中，
    须客户明确要求入库（persist_screening_result / deep_verify persist=true）后才可见。
    ★降级入口：暂存数据未入库（或贵复查作业failed但暂存已有数据）时，用 staging_preview
    零成本跑引擎预分析拿筛选参考分，不必重跑贵复查、不必入库。
    团队鉴权：数据供给与事实桥按调用者项目成员可见性收窄，跨项目底稿不进入评分。
    返回 run_id；用 get_context_pack(run_id) 取Skill上下文包内容。同步运行约数秒。"""
    me = CUR_USER.get()
    conn = _db()
    r = _screened(conn, company_key)
    if r:
        inputs = json.loads(r["inputs_json"] or "{}")
        fin = _finance(inputs)
        name = r["full_name"] or r["name"]
        dom = r["domain"] if r["domain"] in innovation2.DOMAINS else "通用"
        stg = r["stage"] if r["stage"] in innovation2.STAGES else "成长期"
        mode = "engine"
    else:
        projects.ensure_schema(conn)
        visible = projects.visible_source_ids(conn, company_key, me or None)
        if not visible:
            conn.close()
            return _dump({"ok": False,
                          "error": "企业不在粗筛名单且无本人可见底稿；请先 upload_dossier 上传底稿"})
        row = conn.execute("SELECT company_name FROM sources WHERE company_key=? "
                           "AND company_name IS NOT NULL AND company_name!='' LIMIT 1",
                           (company_key,)).fetchone()
        fin = {}
        name = company_name or (row[0] if row else company_key)
        dom = domain if domain in innovation2.DOMAINS else "通用"
        stg = stage if stage in innovation2.STAGES else "成长期"
        mode = "dossier"
    conn.close()
    if domain in innovation2.DOMAINS:
        dom = domain
    if stage in innovation2.STAGES:
        stg = stage
    try:
        base = run_engine.run(company_key, dom, stg, fin, {},
                              patent_top=5, viewer_email=me or None)
    except Exception as exc:
        _audit("analyze_company", f"{company_key} ERROR {exc!r}")
        return _dump({"ok": False, "error": repr(exc)[:300]})
    run_id = base["run_id"]
    conn = _db()
    conn.execute("UPDATE evaluation_runs SET company_name=?, mode=? WHERE run_id=?",
                 (company_name or name, mode, run_id))
    conn.commit()
    conn.close()
    _snapshot_packs(run_id, company_key)
    payload = {k: base[k] for k in ("run_id", "company", "pqi", "innovation",
                                    "risk_flags", "funnel", "packs", "facts_summary")}
    payload["company"]["company_name"] = company_name or name
    payload["viewer"] = me
    payload["mode"] = mode
    payload["next_step"] = f"get_context_pack(run_id='{run_id}') 取4个Skill上下文包内容与量表指令"
    (ANALYSIS_DIR / f"{run_id}.json").write_text(_dump(payload), encoding="utf-8")
    _audit("analyze_company", f"{company_key} -> {run_id} (viewer={me}, mode={mode})")
    return _dump({"ok": True, **payload})


@mcp.tool()
def get_latest_analysis(company_key: str) -> str:
    """读取企业最近一次「本人可见」的详细分析结果（不重跑）。含 PQI、创新积分明细、红黄灯、
    漏斗与上下文包路径。团队鉴权：他人团队项目作用域的运行结果不可见。"""
    me = CUR_USER.get()
    conn = _db()
    run = projects.latest_visible_run(conn, company_key, me)
    if not run:
        conn.close()
        return _dump({"ok": False, "error": "尚无本人可见的分析记录，请先调用 analyze_company"})
    fp = ANALYSIS_DIR / f"{run['run_id']}.json"
    result = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else None
    metrics = [dict(x) for x in conn.execute("SELECT * FROM metric_values WHERE run_id=?", (run["run_id"],))]
    conn.close()
    _audit("get_latest_analysis", f"{company_key} run={run['run_id']} viewer={me}")
    return _dump({"ok": True, "run": dict(run), "result": result, "metrics": metrics})


# ---------- 团队协同：项目知识库共享（多 Agent 凭各自接入码调用） ----------

@mcp.tool()
def list_my_projects() -> str:
    """团队协同：列出我（接入码所属账号）参与的项目、成员名单、角色与项目底稿数。
    团队内多个成员各自领接入码，即可让不同的 Agent 协同调用同一项目的知识库。"""
    me = CUR_USER.get()
    conn = _db()
    try:
        rows = projects.my_projects(conn, me)
    finally:
        conn.close()
    _audit("list_my_projects", f"n={len(rows)}")
    return _dump({"ok": True, "viewer": me, "count": len(rows), "projects": rows,
                  "note": "项目=团队×企业；同一项目的底稿知识库对全体成员可见，跨项目隔离"})


@mcp.tool()
def add_project_member(project_id: str, email: str) -> str:
    """团队协同：向项目添加成员（仅项目负责人可操作）。成员用邮箱注册 FateBuddy
    账户并领取接入码后，其 Agent 即可调用该项目知识库。"""
    me = CUR_USER.get()
    conn = _db()
    try:
        ok, msg = projects.add_member(conn, project_id, (email or "").strip().lower(), me)
    finally:
        conn.close()
    _audit("add_project_member", f"{project_id} {email} by {me} -> {ok}")
    return _dump({"ok": bool(ok), "message": msg})


@mcp.tool()
def search_project_facts(project_id: str, query: str, top_k: int = 6) -> str:
    """团队协同：在项目底稿知识库（WeKnora hybrid 检索）中查事实证据段。
    仅项目成员可调用；命中绑定本项目的 Source_ID，跨项目底稿不可见。
    返回 [{source_id, title, score, text}]，text 为底稿原文片段。"""
    me = CUR_USER.get()
    conn = _db()
    try:
        projects.ensure_schema(conn)
        p = projects.get_project(conn, project_id)
        if not p:
            return _dump({"ok": False, "error": "项目不存在"})
        if not projects.is_member(conn, project_id, me):
            _audit("search_project_facts", f"DENY {project_id} user={me}")
            return _dump({"ok": False, "error": "非项目成员，无权检索该项目知识库"})
        # 白名单 = 平台公共数据 + 本项目底稿（对该企业）
        allowed = projects.visible_source_ids(conn, p["company_key"], me)
        if not allowed:
            return _dump({"ok": True, "project_id": project_id, "query": query,
                          "facts": [], "note": "项目暂无可检索底稿"})
        # 项目伴随知识库：本项目专属库 + 平台公共库，合并检索
        kb_names = [projects.kb_name(project_id), config.WEKNORA_KB_NAME]
        raw_hits, kb_used = [], []
        for name in kb_names:
            kb_id = facts_bridge.resolve_kb_id(name)
            if not kb_id:
                continue
            kb_used.append(name)
            raw_hits.extend(facts_bridge.hybrid_search(
                query or "底稿", kb_id, top_k=max(1, min(int(top_k), 12))))
        # 去重（同 Source_ID+文本）+ 白名单过滤（公共库命中只留平台数据/本项目数据）
        facts, seen = [], set()
        for h in sorted(raw_hits, key=lambda x: -(x.get("score") or 0)):
            if not h.get("source_id") or h["source_id"] not in allowed:
                continue
            k = (h["source_id"], h.get("text", "")[:80])
            if k in seen:
                continue
            seen.add(k)
            facts.append(h)
        _audit("search_project_facts", f"{project_id} q={query} hits={len(facts)} kbs={kb_used}")
        return _dump({"ok": True, "project_id": project_id,
                      "company_key": p["company_key"], "query": query,
                      "knowledge_bases": kb_used,
                      "facts": facts,
                      "note": "一个项目一个知识库：命中来自项目专属库+平台公共库，已按成员可见性过滤"})
    finally:
        conn.close()


# ---------- 团队协同：项目共享记忆（Memory Core 直连，项目专属 service） ----------

MEMORY_CORE = "http://127.0.0.1:8420"
MEMORY_KEY = "local"
try:
    for _line in Path("/root/tdai-memory-mcp/mcp.env").read_text(encoding="utf-8").splitlines():
        if _line.startswith("TDAI_GATEWAY_API_KEY="):
            MEMORY_KEY = _line.split("=", 1)[1].strip() or MEMORY_KEY
except Exception:
    pass


def _mem_service(project_id: str) -> str:
    """项目专属记忆服务 ID（Memory Core service 级隔离：一个项目一个记忆服务）。"""
    return projects.memory_service(project_id)


def _core_call(path: str, body: dict, service_id: str) -> dict:
    """直连 Memory Core v3（内网 127.0.0.1:8420，按项目 service_id 隔离）。"""
    import urllib.request
    req = urllib.request.Request(MEMORY_CORE + path,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer " + MEMORY_KEY)
    req.add_header("x-tdai-service-id", service_id)
    with urllib.request.urlopen(req, timeout=40) as resp:
        result = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    if not isinstance(result, dict) or result.get("code") != 0:
        raise RuntimeError("Memory Core error: " + str(result.get("message", result))[:200])
    return result.get("data") or {}


def _require_member(project_id: str, me: str):
    conn = _db()
    try:
        projects.ensure_schema(conn)
        p = projects.get_project(conn, project_id)
        if not p:
            return None, "项目不存在"
        if not projects.is_member(conn, project_id, me):
            return None, "非项目成员，无权访问该项目记忆"
        return p, None
    finally:
        conn.close()


def _memory_search_project(project_id: str, query: str, limit: int) -> dict:
    """项目记忆检索：项目专属 Memory Core service（一个项目一个记忆服务），
    L0 会话层 + L1 提炼层均天然按 service 隔离，跨项目不可见。"""
    svc = _mem_service(project_id)
    data = _core_call("/v3/conversation/search",
                      {"team_id": "default", "agent_id": "default", "user_id": "default",
                       "session_id": "project", "query": query, "limit": limit}, svc)
    msgs = data.get("messages") or []
    try:
        atomic = _core_call("/v3/atomic/search",
                            {"team_id": "default", "agent_id": "default", "user_id": "default",
                             "session_id": "project", "query": query, "limit": limit}, svc)
    except Exception:
        atomic = {}
    items = atomic.get("items") or []
    return {"ok": True, "project_id": project_id, "query": query,
            "memory_service": svc,
            "count": len(msgs),
            "memories": [{"content": m.get("content", ""),
                          "role": m.get("role"),
                          "created_at": m.get("timestamp")}
                         for m in msgs],
            "insights": [s for it in items
                         for s in [it.get("content") or it.get("text")
                                   or it.get("summary") or ""]
                         if s]}


@mcp.tool()
def project_memory_add(project_id: str, content: str) -> str:
    """团队协同·项目共享记忆：向项目写入一条团队记忆（仅项目成员可写）。
    每个项目一个专属记忆服务（service 级隔离，跨项目不可见），写作者=接入码邮箱。
    团队内所有成员的 Agent 都能检索到——尽调结论、备忘、阶段判断等沉淀于此。"""
    me = CUR_USER.get()
    _, err = _require_member(project_id, me)
    if err:
        _audit("project_memory_add", f"DENY {project_id} user={me}")
        return _dump({"ok": False, "error": err})
    text = (content or "").strip()
    if not text:
        return _dump({"ok": False, "error": "content is required"})
    if len(text) > 8000:
        return _dump({"ok": False, "error": "content 超过 8000 字"})
    body = f"[{me}] {text}"
    try:
        data = _core_call("/v3/conversation/add",
                          {"team_id": "default", "agent_id": "default", "user_id": "default",
                           "session_id": "project",
                           "messages": [{"role": "user", "content": body}]},
                          _mem_service(project_id))
        _audit("project_memory_add", f"{project_id} by {me} len={len(text)}")
        return _dump({"ok": True, "project_id": project_id, "author": me,
                      "memory_service": _mem_service(project_id),
                      "note": "已写入项目专属记忆服务；刚写入即可被同项目成员检索",
                      "accepted": data.get("accepted_ids", [])[:2]})
    except Exception as exc:
        return _dump({"ok": False, "error": "记忆服务暂不可用: " + repr(exc)[:200]})


@mcp.tool()
def project_memory_search(project_id: str, query: str, limit: int = 5) -> str:
    """团队协同·项目共享记忆：检索项目团队记忆（仅项目成员）。
    每个项目一个专属记忆服务；返回队友与自己的 Agent 沉淀的
    尽调结论/备忘/工作记录（内容前缀 [邮箱] 为作者）及提炼洞察。"""
    me = CUR_USER.get()
    _, err = _require_member(project_id, me)
    if err:
        _audit("project_memory_search", f"DENY {project_id} user={me}")
        return _dump({"ok": False, "error": err})
    try:
        out = _memory_search_project(project_id, query or "记忆",
                                     max(1, min(int(limit), 20)))
        _audit("project_memory_search", f"{project_id} q={query} by={me} n={out.get('count')}")
        return _dump(out)
    except Exception as exc:
        return _dump({"ok": False, "error": "记忆服务暂不可用: " + repr(exc)[:200]})


# ---------- 服务闭环：对方 Agent 经 MCP 上传底稿 → 取包跑 Skill → 回传出分 ----------

@mcp.tool()
def upload_dossier(company_key: str, files_json: str, company_name: str = "",
                   domain: str = "通用", stage: str = "成长期",
                   project_id: str = "", analyze: bool = True) -> str:
    """服务闭环①：MCP上传底稿 → SQLite+WeKnora双落库 → 引擎三层分析（任意企业）。
    company_key=18位统一社会信用代码；files_json=JSON数组字符串：
      [{"filename":"专利.md","content":"<底稿全文>","kind":"patent"}]
    kind∈patent/financial/bid/judicial/company/risk/web/other；≤6份、单份≤2MB、
    扩展名.md/.txt/.json/.markdown。project_id给定→底稿归属该项目（须已是成员，
    团队共享、跨项目隔离）；不给→平台公共层（全员可见）。幂等：同项目同文件名
    重传=升版不重复。analyze=true(默认)立即跑完整管线返回run_id与硬指标摘要；
    false只落库，之后用analyze_company分析。零付费API调用。"""
    me = CUR_USER.get()
    key = dossier_pipeline._clean_company_key(company_key)
    if not dossier_pipeline._valid_key(key):
        return _dump({"ok": False, "error": "company_key 须为有效18位统一社会信用代码（USCC）"})
    try:
        files = json.loads(files_json)
        assert isinstance(files, list) and files
    except Exception:
        return _dump({"ok": False, "error": "files_json 须为非空JSON数组：[{filename,content,kind}]"})
    if len(files) > dossier_pipeline.MAX_FILES:
        return _dump({"ok": False, "error": f"一次最多上传{dossier_pipeline.MAX_FILES}份底稿"})
    blobs, kinds, rejected = [], [], []
    for f in files:
        fname = str(f.get("filename") or "")
        content = f.get("content")
        kind = str(f.get("kind") or "web")
        ext = Path(fname).suffix.lower()
        if not isinstance(content, str) or not content:
            rejected.append({"file": fname, "reason": "content 须为非空文本"})
            continue
        blob = content.encode("utf-8")
        if ext not in {".md", ".txt", ".json", ".markdown"} or len(blob) > dossier_pipeline.MAX_BYTES:
            rejected.append({"file": fname, "reason": "仅支持 ≤2MB 的 .md/.txt/.json"})
            continue
        blobs.append((fname, blob))
        kinds.append(kind if kind in dossier_pipeline.KIND_MAP else "web")
    if not blobs:
        return _dump({"ok": False, "error": "无有效底稿：" + "; ".join(r["reason"] for r in rejected)})
    pid = (project_id or "").strip()
    if pid:
        conn = _db()
        try:
            projects.ensure_schema(conn)
            if not projects.get_project(conn, pid):
                return _dump({"ok": False, "error": "项目不存在"})
            if not projects.is_member(conn, pid, me):
                _audit("upload_dossier", f"DENY project={pid} user={me}")
                return _dump({"ok": False, "error": "非项目成员，无法向该项目上传底稿"})
        finally:
            conn.close()
    if not analyze:
        conn = dossier_pipeline.store.connect()
        try:
            st = dossier_pipeline.store_uploads(conn, company_key=key, company_name=company_name,
                                                files=blobs, kinds=kinds, project_id=pid or None)
            ing = dossier_pipeline.ingest_uploads_weknora(conn, key)
        finally:
            conn.close()
        _audit("upload_dossier", f"{key} store-only n={len(st['saved'])} by={me}")
        return _dump({"ok": True, "company_key": key, "uploads": st, "weknora_ingest": ing,
                      "rejected": rejected, "viewer": me,
                      "next_step": f"analyze_company(company_key='{key}') 触发引擎分析"})
    try:
        result = dossier_pipeline.analyze_dossiers(
            company_key=key, company_name=company_name, domain=domain, stage=stage,
            files=blobs, kinds=kinds, actor_email=me or None, project_id=pid or None)
    except Exception as exc:
        _audit("upload_dossier", f"{key} ERROR {exc!r}")
        return _dump({"ok": False, "error": "分析管线异常: " + repr(exc)[:250]})
    if not result.get("ok"):
        return _dump(result)
    run_id = result["run_id"]
    _snapshot_packs(run_id, key)
    fp = ANALYSIS_DIR / f"{run_id}.json"
    try:
        saved = json.loads(fp.read_text(encoding="utf-8"))
        saved["next_step"] = f"get_context_pack(run_id='{run_id}') 取4个Skill上下文包内容与量表指令"
        fp.write_text(_dump(saved), encoding="utf-8")
    except Exception:
        pass
    inn = result.get("innovation") or {}
    summary = {
        "ok": True, "run_id": run_id, "company_key": key,
        "company_name": company_name or key, "viewer": me, "mode": "dossier",
        "uploads": result.get("uploads"), "weknora_ingest": result.get("weknora_ingest"),
        "rejected": rejected,
        "facts_summary": result.get("facts_summary"),
        "hard_metrics": {"core_score": inn.get("core_score"), "bonus_score": inn.get("bonus_score"),
                         "total_score": inn.get("total_score"), "grade": inn.get("grade"),
                         "pqi_composite": (result.get("pqi") or {}).get("composite"),
                         "risk_flags": [{"code": f["code"], "level": f["level"]}
                                        for f in result.get("risk_flags") or []]},
        "next_step": f"get_context_pack(run_id='{run_id}') 取4个Skill上下文包内容与量表指令",
    }
    _audit("upload_dossier", f"{key} -> {run_id} n={len(blobs)} by={me} project={pid or '-'}")
    return _dump(summary)


@mcp.tool()
def get_context_pack(run_id: str, skill: str = "") -> str:
    """服务闭环②：取Skill上下文包内容（含WeKnora事实段weknora_facts与allowed_source_ids白名单）。
    skill留空=返回4个包的全部内容；指定 patent_review / commercialization_fit /
    anti_packaging_factcheck / bank_report 之一=返回该包+对应SKILL.md完整量表指令，
    你的Agent严格按指令研判并输出符合契约的JSON，再经 submit_skill_outputs 回传。
    团队鉴权：仅本人可见的运行可取包。bank_report包在三个上游Skill通过校验后
    会回填语义研判分，届时再取用生成银行报告。
    ★暂存运行（run_id 以 run-staging- 开头，来自 staging_preview）同样支持：
    内联返回暂存上下文包＋量表，任意已认证账户可读，结果标注非正式闭环。"""
    me = CUR_USER.get()
    if run_id.startswith("run-staging-"):
        return _staging_context_pack(run_id, skill, me)
    conn = _db()
    try:
        run, err = _visible_run(conn, run_id, me)
        if err:
            _audit("get_context_pack", f"DENY {run_id} user={me}")
            return _dump({"ok": False, "error": err})
        company_key = run["company_key"]
    finally:
        conn.close()
    pack_dir, snapped = _pack_dir_for(run_id, company_key)
    names = [skill] if skill else list(schemas.OUTPUT_MODELS)
    if skill and skill not in schemas.OUTPUT_MODELS:
        return _dump({"ok": False, "error": f"skill 须为 {list(schemas.OUTPUT_MODELS)} 之一或留空"})
    packs, missing = {}, []
    for name in names:
        fp = pack_dir / f"pack_{name}.json"
        if fp.exists():
            packs[name] = json.loads(fp.read_text(encoding="utf-8"))
        else:
            missing.append(name)
    out = {"ok": True, "run_id": run_id, "company_key": company_key,
           "company_name": run["company_name"], "packs_from": "run_snapshot" if snapped else "company_latest",
           "packs": packs, "missing": missing, "viewer": me}
    if skill:
        md = SKILL_DIRS[skill] / "SKILL.md"
        out["skill_instructions"] = md.read_text(encoding="utf-8") if md.exists() else ""
        out["next_step"] = f"按 skill_instructions 研判后 submit_skill_outputs(run_id='{run_id}', outputs_json={{\"{skill}\": <输出JSON>}})"
    else:
        out["note"] = "指定 skill 参数可取该Skill的SKILL.md完整量表指令；建议先跑三个上游Skill再取bank_report"
    if not snapped:
        out["warning"] = "该运行无独立快照，包内容为该企业最近一次运行版本"
    _audit("get_context_pack", f"{run_id} skill={skill or 'ALL'} by={me}")
    return _dump(out)


def _loads_agent_outputs(outputs_json) -> dict:
    """容错解析 Agent 回传：接受 JSON字符串/JSON对象/```json围栏包裹/BOM前缀。
    外部Agent常见行为（markdown代码块包裹、直接传dict）不应导致校验层卡死。"""
    if isinstance(outputs_json, dict):
        return outputs_json
    s = str(outputs_json).strip()
    if s.startswith("\ufeff"):
        s = s[1:]
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", s)
    if m:
        s = m.group(1).strip()
    return json.loads(s)


@mcp.tool()
def submit_skill_outputs(run_id: str, outputs_json: "str | dict") -> str:
    """服务闭环③：回传Skill输出JSON → 团队白名单+Pydantic强校验+Source_ID伪造拦截
    +持久熔断 → 综合信用分。outputs_json=JSON对象字符串（兼容直接传JSON对象与
    ```json围栏包裹，内部自动剥除）：
      {"patent_review":{...}, "commercialization_fit":{...},
       "anti_packaging_factcheck":{...}, "bank_report":{...}}
    可一次提交1-4个（建议先交三个上游，通过后取bank_report包生成报告再交）。
    每个输出须严格符合对应SKILL.md输出契约（多字段/缺字段/量程外均拒）；
    结论绑定的Source_ID必须在上下文包allowed_source_ids内且本人可见，伪造即拦截；
    同一Skill连续3次校验失败→持久熔断，该Skill本次运行终止。
    ★暂存运行（run_id 以 run-staging- 开头）不支持本工具：暂存口径为非正式闭环；
    要拿正式综合信用分请先 persist_screening_result 入库 → analyze_company 重跑。
    返回逐Skill校验结果+综合信用分（硬55%+语义35%-风险扣分/一票否决）+分级+授信建议。"""
    me = CUR_USER.get()
    if (run_id or "").startswith("run-staging-"):
        _audit("submit_skill_outputs", f"DENY-STAGING {run_id} by={me}")
        return _dump({"ok": False, "run_id": run_id,
                      "error": "暂存运行不支持submit_skill_outputs（非正式闭环，暂存库不接收Skill回传）",
                      "hint": "量表研判结果以'非正式闭环·暂存量表应用'形式交付即可，无需提交；"
                              "要拿正式综合信用分：persist_screening_result(company_key=...) 提升入库 → "
                              "analyze_company(company_key=...) 正式重跑 → get_context_pack(run_id, skill) → "
                              "submit_skill_outputs"})
    conn = _db()
    try:
        run, err = _visible_run(conn, run_id, me)
        if err:
            _audit("submit_skill_outputs", f"DENY {run_id} user={me}")
            return _dump({"ok": False, "error": err})
        company_key = run["company_key"]
        projects.ensure_schema(conn)
        allowed = projects.visible_source_ids(conn, company_key, me or None)
        prior = {r["skill"]: dict(r) for r in conn.execute(
            "SELECT skill, ok, retries FROM agent_outputs WHERE run_id=?", (run_id,))}
    finally:
        conn.close()
    try:
        outputs = _loads_agent_outputs(outputs_json)
        assert isinstance(outputs, dict) and outputs
    except AssertionError:
        return _dump({"ok": False, "error": "outputs_json 须为非空JSON对象：{skill: 输出JSON}"})
    except Exception as exc:
        return _dump({"ok": False, "error": "outputs_json 解析失败（应为{skill: 输出JSON}对象）："
                                           + repr(exc)[:200]})
    bad = [s for s in outputs if s not in schemas.OUTPUT_MODELS]
    if bad:
        return _dump({"ok": False, "error": f"未注册的skill: {bad}，只允许 {list(schemas.OUTPUT_MODELS)}"})
    payload_fp = ANALYSIS_DIR / f"{run_id}.json"
    if not payload_fp.exists():
        return _dump({"ok": False, "error": "运行缺少结果底稿（CLI内部运行不支持MCP回传），请用 analyze_company/upload_dossier 重跑"})
    base_result = json.loads(payload_fp.read_text(encoding="utf-8"))
    pack_dir, snapped = _pack_dir_for(run_id, company_key)
    base_result["packs"] = dict(base_result.get("packs") or {})
    base_result["packs"]["patent_review"] = str(pack_dir / "pack_patent_review.json")
    agent_dir = ANALYSIS_DIR / run_id / "agent_outputs"
    agent_dir.mkdir(parents=True, exist_ok=True)

    statuses, broken = {}, []
    for skill, payload in outputs.items():
        p = prior.get(skill) or {}
        if p.get("ok") == 0 and (p.get("retries") or 0) >= validate.MAX_RETRIES:
            broken.append(skill)
            statuses[skill] = {"ok": False, "circuit_broken": True,
                               "errors": [f"已连续{validate.MAX_RETRIES}次校验失败熔断，本次运行不再接受该Skill输出"]}
            continue
        verdict = validate.validate_output(skill, payload, allowed)
        fp = agent_dir / f"{skill}.json"
        if verdict["ok"]:
            fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            statuses[skill] = {"ok": True, "errors": [], "retries": 0}
        else:
            retries = (p.get("retries") or 0) + 1
            fp.unlink(missing_ok=True)
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO agent_outputs VALUES(?,?,?,?,?,?,?)",
                         (run_id, skill, 0, retries,
                          json.dumps(verdict["errors"], ensure_ascii=False), None, _now()))
            conn.commit()
            conn.close()
            is_broken = retries >= validate.MAX_RETRIES
            if is_broken:
                broken.append(skill)
            statuses[skill] = {"ok": False, "errors": verdict["errors"],
                               "retries": retries, "circuit_broken": is_broken}
    # 有任一通过文件（含历史通过）→ 引擎校验熔断+总分汇总（唯一评分口径）
    final = semantic = fact_score = None
    if any(agent_dir.glob("*.json")):
        try:
            out = run_engine.run_with_agent_outputs(base_result, agent_dir)
            final, semantic, fact_score = out["final"], out["semantic_score"], out["fact_score"]
        except validate.CircuitBroken as exc:
            _audit("submit_skill_outputs", f"{run_id} BROKEN {exc!r}")
            return _dump({"ok": False, "circuit_broken": True, "error": str(exc)[:300],
                          "skills": statuses})
        except Exception as exc:
            _audit("submit_skill_outputs", f"{run_id} SCORE ERROR {exc!r}")
            return _dump({"ok": False, "error": "总分汇总异常: " + repr(exc)[:250],
                          "skills": statuses})
        # 上游通过后把语义分回填 bank_report 快照包（供Skill4消费）
        if snapped and semantic is not None:
            brp = pack_dir / "pack_bank_report.json"
            if brp.exists():
                try:
                    br = json.loads(brp.read_text(encoding="utf-8"))
                    br["semantic_score"] = semantic
                    br["fact_score"] = fact_score
                    br["upstream_validated"] = True
                    brp.write_text(json.dumps(br, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception:
                    pass
        # 综合分持久化到运行JSON（get_latest_analysis / get_prospect_pack 摘要可读）
        try:
            saved = json.loads(payload_fp.read_text(encoding="utf-8"))
            saved["final"] = final
            saved["semantic_score"] = semantic
            saved["fact_score"] = fact_score
            saved["skills_validated"] = sorted(p.stem for p in agent_dir.glob("*.json"))
            payload_fp.write_text(_dump(saved), encoding="utf-8")
        except Exception:
            pass
    all_ok = all(s["ok"] for s in statuses.values())
    upstream_done = all((agent_dir / f"{s}.json").exists() for s in UPSTREAM_SKILLS)
    bank_done = (agent_dir / "bank_report.json").exists()
    if not all_ok:
        next_step = "submit_skill_outputs 补齐未通过的Skill（errors逐条给出重试原因）"
    elif not upstream_done:
        next_step = ("上游三个Skill尚未全部通过（patent_review/commercialization_fit/"
                     "anti_packaging_factcheck），请按 get_context_pack(run_id, skill) 逐个研判补齐，"
                     "再取 bank_report 包生成报告")
    elif not bank_done:
        next_step = f"get_context_pack(run_id='{run_id}', skill='bank_report') 取回填语义分的报告包，生成报告后再回传"
    else:
        next_step = "闭环完成：综合信用分与银行报告已就绪"
    _audit("submit_skill_outputs", f"{run_id} skills={list(outputs)} ok={all_ok} by={me} "
           f"final={final.get('final_score') if final else None}")
    return _dump({"ok": all_ok, "run_id": run_id, "skills": statuses,
                  "circuit_broken": broken, "final": final,
                  "semantic_score": semantic, "fact_score": fact_score,
                  "viewer": me, "next_step": next_step})


# ---------- 功能一：公开数据潜客侦察 + 公开触达（底稿前阶段，默认零成本） ----------

_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8}|400[-\s]?\d{3}[-\s]?\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"(?:https?://|www\.)[A-Za-z0-9./\-_%?=&#:+~]+", re.I)
_NOISE_MAIL = ("example.com", "sentry", "wixpress", "schema.org", "w3.org", "email.com")
_AGG_HOSTS = ("tianyancha", "qcc.com", "qichacha", "aiqicha", "baidu", "zhihu", "sohu",
              "sina.", "163.com", "qq.com", "bilibili", "douyin", "xiaohongshu", "kanzhun",
              "maigoo", "cninfo", "eastmoney", "10jqka", "xueqiu", "wikipedia", "baike",
              "zhaopin", "51job", "liepin")
_RELIABILITY_ORDER = {"high": 0, "medium-high": 1, "medium": 2, "low": 3}

DOSSIER_CHECKLIST = [
    {"item": "专利清单（结构化JSON）", "kind": "patent",
     "fields": "pn/apno/title/inventor/apdt/pbdt/legal_status + 选填 grant_date/ipc4_list/family_offices/abstract/claim_1",
     "purpose": "OECD PQI 技术质量预分 + Skill1 专利质量研判；JSON数组或{patents:[...]}逐件落库，"
                "著录项越全维度越可算（缺法律状态/同族则对应维度缺失动态调权）"},
    {"item": "研发投入证明", "kind": "financial",
     "fields": "近3年研发费用/营收占比/研发人员占比（审计附注或高企申报材料）",
     "purpose": "创新积分2.0 创新投入维度"},
    {"item": "技术合同/商业化证明", "kind": "bid",
     "fields": "技术合同、中标通知、销售发票摘要",
     "purpose": "创新产出维度 + Skill2 商业化匹配研判"},
    {"item": "官方宣传稿/新闻稿", "kind": "web",
     "fields": "企业自称的技术亮点、市场地位、客户案例原文",
     "purpose": "Skill3 反包装事实核查的比对素材"},
    {"item": "工商/股权结构说明", "kind": "company",
     "fields": "股权沿革、实控人、关联公司、重大诉讼与处罚说明",
     "purpose": "硬排雷 + 团队白名单证据基线"},
]
COMPLIANCE_NOTES = [
    "只用企业公开披露的联系方式，不采集个人隐私信息",
    "触达时主动披露身份与目的（技术信用评估/银行审贷尽调合作），不得冒充银行、监管或政府",
    "每渠道尝试≤2次，对方明确拒绝即停止",
    "所有渠道 verified=false，正式触达前须人工复核（拨测/官网比对）",
    "本服务只产出触达方案，不自动发送任何消息；触达动作由调用方自行合规执行",
]


def _extract_contacts(text: str, origin: str, cap: int = 24) -> list:
    """从文本提取电话/邮箱/URL 线索（全部 verified=False，须人工复核）。"""
    out, seen = [], set()
    text = text or ""
    for m in _EMAIL_RE.finditer(text):
        v = m.group(0).rstrip(".")
        if any(n in v.lower() for n in _NOISE_MAIL):
            continue
        k = ("email", v.lower())
        if k not in seen:
            seen.add(k)
            out.append({"type": "email", "value": v, "source": origin, "verified": False})
    for m in _PHONE_RE.finditer(text):
        v = re.sub(r"[-\s]", "", m.group(1))
        k = ("phone", v)
        if k not in seen:
            seen.add(k)
            out.append({"type": "phone", "value": v, "source": origin, "verified": False})
    for m in _URL_RE.finditer(text):
        v = m.group(0).rstrip(".,;:）)")
        k = ("url", v.lower())
        if k not in seen:
            seen.add(k)
            out.append({"type": "url", "value": v, "source": origin, "verified": False})
    return out[:cap]


def _dedup_channels(channels: list, cap: int = 30) -> list:
    seen, out = set(), []
    for c in channels:
        k = (c["type"], str(c["value"]).lower().rstrip("/"))
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    out.sort(key=lambda c: (_RELIABILITY_ORDER.get(c.get("reliability", "low"), 3),
                            {"website": 0, "phone": 1, "email": 2}.get(c["type"], 3)))
    return out[:cap]


def _dossier_contact_clues(conn, company_key: str, me: str) -> list:
    """本人可见底稿中的联系方式线索（跨项目底稿不可见）。"""
    allowed = projects.visible_source_ids(conn, company_key, me or None)
    rows = conn.execute(
        "SELECT source_id, kind, dossier_path, raw_path FROM sources WHERE company_key=?",
        (company_key,)).fetchall()
    clues = []
    for r in rows:
        if r["source_id"] not in allowed:
            continue
        for field in ("dossier_path", "raw_path"):
            fp = r[field]
            if not fp:
                continue
            p = Path(fp)
            try:
                if p.exists() and p.stat().st_size < 2_000_000:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    batch = _extract_contacts(text, f"{r['source_id']}({r['kind']})", cap=8)
                    for c in batch:
                        c["reliability"] = "medium"
                    clues.extend(batch)
            except Exception:
                continue
    return clues


def _enrich_contacts(full_name: str) -> dict:
    """可选在线补全（既有网关凭证，严格预算：天眼查1次+Tavily搜索1次+全文1次）。
    任一源失败降级为 warning，不阻断本地渠道返回。总耗时上限约2分钟。"""
    import gateway
    channels, used, warns = [], [], []
    try:
        tyc = gateway.TianyanchaClient()
        tyc.timeout = 40
        prof = tyc.basic_profile(full_name)
        used.append("tianyancha:basic_profile")
        for c in _extract_contacts(json.dumps(prof, ensure_ascii=False),
                                   "tianyancha:basic_profile(工商/年报公示)", cap=12):
            c["reliability"] = "medium-high"
            channels.append(c)
    except Exception as exc:
        warns.append(f"tianyancha: {exc!r}"[:160])
    tav, official_url = None, None
    try:
        tav = gateway.TavilyClient()
        res = tav._post("/search", {"query": f'"{full_name}" 官网 联系方式 电话 邮箱',
                                    "max_results": 5, "search_depth": "basic",
                                    "include_answer": False}, timeout=40)
        used.append("tavily:search")
        for item in res.get("results", []):
            url = item.get("url", "") or ""
            host = urllib.parse.urlparse(url).netloc.lower()
            is_agg = any(a in host for a in _AGG_HOSTS)
            snippet = f"{item.get('title', '')} {item.get('content', '')}"
            for c in _extract_contacts(snippet, f"tavily-search:{url}", cap=8):
                c["reliability"] = "low" if is_agg else "medium"
                channels.append(c)
            if not is_agg and official_url is None and url.startswith("http"):
                official_url = url
    except Exception as exc:
        warns.append(f"tavily-search: {exc!r}"[:160])
    if official_url and tav is not None:
        try:
            ext = tav._post("/extract", {"urls": [official_url]}, timeout=45)
            used.append("tavily:extract")
            for r in ext.get("results", []):
                raw = r.get("raw_content") or r.get("content") or ""
                for c in _extract_contacts(raw[:200000], f"tavily-extract:{official_url}", cap=12):
                    c["reliability"] = "high" if c["type"] in ("phone", "email") else "medium"
                    channels.append(c)
        except Exception as exc:
            warns.append(f"tavily-extract: {exc!r}"[:160])
    return {"channels": _dedup_channels(channels), "providers_used": used, "warnings": warns}


def _prospect_metrics(row) -> dict:
    """粗筛行的公开指标证据摘要（全部可溯源到 screened_companies 免费口径）。"""
    inputs = json.loads(row["inputs_json"] or "{}")
    metrics = json.loads(row["metrics_json"] or "[]")
    top = sorted([m for m in metrics if not m.get("missing")],
                 key=lambda m: -(m.get("weighted") or 0))[:3]
    prof = json.loads(row["profile_json"] or "{}")
    return {
        "grade": row["grade"], "total_score": row["total_score"],
        "core_score": row["core_score"], "bonus_score": row["bonus_score"],
        "prescore_pqi": row["prescore"],
        "inputs": inputs,
        "top_metrics": [{"code": m["code"], "raw": m.get("raw"),
                         "score": m.get("score"), "weighted": m.get("weighted")} for m in top],
        "missing": json.loads(row["missing_json"] or "[]"),
        "report_period": row["report_period"], "source_note": row["source_note"],
        "website": prof.get("website"), "reg_addr": prof.get("reg_addr"),
        "founded": prof.get("founded"), "main_business": prof.get("main_business"),
    }


@mcp.tool()
def discover_prospects(domain: str = "", grade: str = "", min_score: float = 0.0,
                       limit: int = 10, sort_by: str = "total_score") -> str:
    """功能一①：从免费公开口径粗筛名单（62家，东财业绩报表+巨潮公司画像+新浪年报）
    发现优质潜客。domain 按赛道模糊过滤（如"新能源"/"半导体"）；grade 按分级过滤（A/B/C）；
    min_score 按创新积分总分下限；sort_by∈total_score(默认)/prescore(技术质量PQI)/rev_growth(营收增速)。
    返回逐企业：分级/总分/PQI预分/增速/缺失标注/官网/缓存专利数（>0可立即analyze_company硬分析），
    全部数字可溯源公开口径。零成本，不调任何外部API。
    下一步：get_prospect_pack(company_key) 取公开证据包+触达渠道+prospect_scout量表指令。"""
    me = CUR_USER.get()
    if sort_by not in ("total_score", "prescore", "rev_growth"):
        return _dump({"ok": False, "error": "sort_by 须为 total_score/prescore/rev_growth"})
    limit = max(1, min(int(limit), 30))
    conn = _db()
    try:
        sql = "SELECT * FROM screened_companies WHERE total_score >= ?"
        args = [float(min_score)]
        if domain:
            sql += " AND (domain LIKE ? OR industry LIKE ?)"
            args += [f"%{domain}%", f"%{domain}%"]
        if grade:
            sql += " AND grade = ?"
            args.append(grade.strip().upper())
        rows = conn.execute(sql, args).fetchall()
        cohort = conn.execute(
            "SELECT grade, COUNT(*) n FROM screened_companies GROUP BY grade").fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        m = _prospect_metrics(r)
        items.append({
            "company_key": r["company_key"], "name": r["name"], "full_name": r["full_name"],
            "domain": r["domain"], "stage": r["stage"], "industry": r["industry"],
            "province": r["province"], "city": r["city"],
            "grade": r["grade"], "total_score": r["total_score"], "prescore_pqi": r["prescore"],
            "rev_growth_pct": (m["inputs"] or {}).get("REV_GROWTH"),
            "rd_ratio_pct": (m["inputs"] or {}).get("RD_RATIO"),
            "roe_pct": (m["inputs"] or {}).get("ROE"),
            "missing_metrics": m["missing"],
            "cached_patents": r["cached_patents"],
            "deep_analysis_ready": bool(r["cached_patents"] or r["is_cached_entity"]),
            "website": m["website"],
        })
    if sort_by == "rev_growth":
        items.sort(key=lambda x: -(x["rev_growth_pct"] or -1e9))
    elif sort_by == "prescore":
        items.sort(key=lambda x: -(x["prescore_pqi"] or 0))
    else:
        items.sort(key=lambda x: -(x["total_score"] or 0))
    items = items[:limit]
    _audit("discover_prospects", f"domain={domain} grade={grade} sort={sort_by} n={len(items)} by={me}")
    return _dump({"ok": True, "count": len(items), "sort_by": sort_by,
                  "cohort": {"total": sum(c["n"] for c in cohort),
                             "grades": {c["grade"]: c["n"] for c in cohort}},
                  "prospects": items, "viewer": me,
                  "evidence_note": "全部指标来自免费公开口径粗筛（source_note 逐企业标注）；缺失≠差，须底稿核实",
                  "next_step": "get_prospect_pack(company_key=...) 取公开证据包+触达渠道+Skill指令；"
                               "deep_analysis_ready=true 的企业可先 analyze_company(company_key=...) 跑硬分析"})


@mcp.tool()
def get_prospect_pack(company_key: str, enrich: bool = False) -> str:
    """功能一②：取目标企业的公开证据包+公开触达渠道+prospect_scout量表指令（底稿前阶段）。
    返回：public_scores(粗筛指标证据)/cached_evidence(本人可见证据量)/
    outreach_channels(官网+注册地址+本人可见底稿线索，均verified=false)/
    dossier_request_checklist(标准底稿索取清单)/skill_instructions(Skill5完整量表)。
    enrich=false(默认)零成本纯本地；enrich=true 走既有网关在线补全联系方式
    （天眼查basic_profile 1次+Tavily搜索1次+官网全文1次封顶，约≤2分钟，失败自动降级）。
    你的Agent按 skill_instructions 产出触达方案JSON，经 create_project + project_memory_add 落项目。
    名单外企业：只要有本人可见底稿也可取包；两者皆无→提示先 upload_dossier。"""
    me = CUR_USER.get()
    key = (company_key or "").strip()
    if not key:
        return _dump({"ok": False, "error": "company_key is required"})
    conn = _db()
    try:
        row = _screened(conn, key)
        allowed = projects.visible_source_ids(conn, key, me or None)
        visible_kinds = {}
        for r in conn.execute("SELECT source_id, kind FROM sources WHERE company_key=?", (key,)):
            if r["source_id"] in allowed:
                visible_kinds[r["kind"]] = visible_kinds.get(r["kind"], 0) + 1
        latest = projects.latest_visible_run(conn, key, me)
        clues = _dossier_contact_clues(conn, key, me)
        name = (row["full_name"] or row["name"]) if row else None
        if not name:
            r0 = conn.execute("SELECT company_name FROM sources WHERE company_key=? "
                              "AND company_name IS NOT NULL LIMIT 1", (key,)).fetchone()
            name = r0["company_name"] if r0 else key
    finally:
        conn.close()
    if row is None and not allowed:
        return _dump({"ok": False,
                      "error": "该企业不在公开粗筛名单且无本人可见底稿。请先 upload_dossier 上传底稿，"
                               "或用 discover_prospects/search_companies 查找名单内企业"})
    channels = []
    if row:
        prof = json.loads(row["profile_json"] or "{}")
        if prof.get("website"):
            channels.append({"type": "website", "value": prof["website"],
                             "source": "screened:profile_json(巨潮公司画像)",
                             "reliability": "high", "verified": False})
        if prof.get("reg_addr"):
            channels.append({"type": "registered_address", "value": prof["reg_addr"],
                             "source": "screened:profile_json(工商注册地址)",
                             "reliability": "medium", "verified": False,
                             "usage": "公函/实地尽调地址，非即时通讯渠道"})
    for c in clues:
        c.setdefault("reliability", "medium")
    channels.extend(clues)
    enrichment = None
    if enrich:
        enrichment = _enrich_contacts(name)
        channels.extend(enrichment["channels"])
    channels = _dedup_channels(channels)
    latest_summary = None
    if latest:
        try:
            fp = ANALYSIS_DIR / f"{latest['run_id']}.json"
            if fp.exists():
                d = json.loads(fp.read_text(encoding="utf-8"))
                fin = d.get("final") or {}
                latest_summary = {"run_id": latest["run_id"],
                                  "final_score": fin.get("final_score"), "grade": fin.get("grade"),
                                  "created_at": latest["created_at"]}
        except Exception:
            pass
    md = SKILL_DIRS["prospect_scout"] / "SKILL.md"
    out = {
        "ok": True, "company_key": key, "company_name": name, "viewer": me,
        "company": {"domain": row["domain"] if row else None,
                    "stage": row["stage"] if row else None,
                    "industry": row["industry"] if row else None,
                    "province": row["province"] if row else None,
                    "city": row["city"] if row else None,
                    "in_public_screening": row is not None},
        "public_scores": _prospect_metrics(row) if row else None,
        "cached_evidence": {"cached_patents": row["cached_patents"] if row else 0,
                            "visible_sources_by_kind": visible_kinds,
                            "latest_visible_run": latest_summary},
        "outreach_channels": {"channels": channels, "enrichment": enrichment,
                              "compliance": COMPLIANCE_NOTES},
        "dossier_request_checklist": DOSSIER_CHECKLIST,
        "skill_instructions": md.read_text(encoding="utf-8") if md.exists() else "",
        "next_step": "按 skill_instructions 产出触达方案JSON → create_project(name, company_key) → "
                     "project_memory_add(project_id, 方案JSON) → 企业回复底稿后 "
                     "upload_dossier(company_key, files_json, project_id=...) 进入4-Skill闭环",
    }
    _audit("get_prospect_pack", f"{key} enrich={enrich} channels={len(channels)} by={me}")
    return _dump(out)


@mcp.tool()
def create_project(name: str, company_key: str, company_name: str = "") -> str:
    """团队协同：为潜客/尽调对象创建项目（项目=团队×企业，调用者自动成为owner）。
    项目是底稿归属、知识库隔离、共享记忆与触达方案沉淀的容器。
    company_key 建议18位USCC（名单内企业也可用粗筛key）；company_name 留空自动取名单全称。
    创建后：add_project_member 拉队友 → project_memory_add 沉淀触达方案/尽调结论 →
    upload_dossier(project_id=...) 上传底稿团队共享。每账户上限30个项目。"""
    me = CUR_USER.get()
    name = (name or "").strip()
    key = (company_key or "").strip()
    if not name or len(name) > 60:
        return _dump({"ok": False, "error": "name 须为1-60字"})
    if not key:
        return _dump({"ok": False, "error": "company_key is required"})
    conn = _db()
    try:
        projects.ensure_schema(conn)
        n = conn.execute("SELECT COUNT(*) n FROM projects WHERE owner_email=?", (me,)).fetchone()["n"]
        if n >= 30:
            return _dump({"ok": False, "error": "每账户项目数上限30，请先清理不再使用的项目"})
        if not company_name:
            row = _screened(conn, key)
            company_name = (row["full_name"] or row["name"]) if row else key
        info = projects.create_project(conn, name=name, company_key=key,
                                       company_name=company_name, owner_email=me)
    finally:
        conn.close()
    _audit("create_project", f"{info['project_id']} key={key} by={me}")
    return _dump({"ok": True, **info,
                  "next_step": f"project_memory_add(project_id='{info['project_id']}', content=触达方案JSON)；"
                               f"upload_dossier(company_key='{key}', files_json=..., project_id='{info['project_id']}')"})


# ---------- 两段漏斗：①便宜海选（全量业绩报表，零网络） ②贵数据复查（选定后，成本闸门） ----------

# 硬科技行业→技术领域映射（与 screening/screen_companies.py 一致；该模块依赖 akshare 不宜在 MCP 进程导入）
INDUSTRY_DOMAIN = {
    "半导体": "新一代信息技术", "消费电子": "新一代信息技术", "光学光电子": "新一代信息技术",
    "电子元件": "新一代信息技术", "电子化学品": "新一代信息技术", "计算机设备": "新一代信息技术",
    "软件开发": "新一代信息技术", "IT服务": "新一代信息技术", "通信设备": "新一代信息技术",
    "通信服务": "新一代信息技术", "互联网服务": "新一代信息技术",
    "生物制品": "生物医药", "化学制药": "生物医药", "医疗器械": "生物医药",
    "中药": "生物医药", "医疗服务": "生物医药",
    "航天航空": "航空航天",
    "电池": "新能源与节能", "光伏设备": "新能源与节能", "风电设备": "新能源与节能",
    "电网设备": "新能源与节能", "电源设备": "新能源与节能", "电机": "新能源与节能",
    "能源金属": "新能源与节能",
    "自动化设备": "先进制造与自动化", "专用设备": "先进制造与自动化", "通用设备": "先进制造与自动化",
    "仪器仪表": "先进制造与自动化", "工程机械": "先进制造与自动化", "轨交设备": "先进制造与自动化",
    "船舶制造": "先进制造与自动化", "工业母机": "先进制造与自动化",
    "化学制品": "新材料", "化学原料": "新材料", "化纤行业": "新材料",
    "非金属材料": "新材料", "小金属": "新材料", "塑料制品": "新材料",
    "环保行业": "资源与环境", "环境治理": "资源与环境",
}
DEEP_VERIFY_QUOTA = 10          # 每账户贵复查次数上限（evaluation_runs mode=deep_verify 计数）
DEEP_DIR = config.DATA_DIR / "deep_verify"
DEEP_DIR.mkdir(parents=True, exist_ok=True)


def _pct_ranks(vals):
    """纯Python分位数排名（0..1），None 记 0。"""
    idx = [i for i, v in enumerate(vals) if v is not None]
    out = [0.0] * len(vals)
    if len(idx) > 1:
        order = sorted(idx, key=lambda i: vals[i])
        for rank, i in enumerate(order):
            out[i] = rank / (len(idx) - 1)
    return out


def _num(v):
    try:
        f = float(v)
        return None if f != f else f   # NaN→None
    except (TypeError, ValueError):
        return None


@mcp.tool()
def screen_market(domain: str = "", industry: str = "", min_growth: float | None = None,
                  min_roe: float | None = None, limit: int = 30, date: str = "20251231") -> str:
    """两段漏斗①·便宜海选：在全A股业绩报表底池（约1.15万家，免费东财口径，本地缓存零网络）
    按需广筛硬科技潜客。行业→技术领域映射后剔除ST/退市/营收≤0，按 增速40%+ROE35%+规模25%
    分位数预排序。domain 按技术领域过滤（如"新能源与节能"/"新一代信息技术"），industry 按行业
    子串过滤（如"电池"/"半导体"），min_growth/min_roe 设增速/ROE下限（百分比），limit≤100。
    返回逐企业：代码/简称/行业/领域/营收/增速/ROE/预排序分 + 是否已在精筛名单(in_screened)。
    数据落库边界：本层为纯本地计算，结果不写任何 SQL 库、不入 WeKnora RAG；
    贵数据复查(deep_verify)抓取的数据同样默认只进筛选暂存区，非客户明确要求不入正式库。
    零成本，不调任何外部API。客户选定几家后 → deep_verify 上贵数据复查。"""
    me = CUR_USER.get()
    fp = config.DATA_DIR / "screening" / f"yjbb_{date}.json"
    if not fp.exists():
        avail = sorted(p.stem.replace("yjbb_", "") for p in (config.DATA_DIR / "screening").glob("yjbb_*.json"))
        return _dump({"ok": False, "error": f"无报告期 {date} 的缓存底池", "available_dates": avail})
    limit = max(1, min(int(limit), 100))
    raw = json.loads(fp.read_text(encoding="utf-8"))
    rows, n_hard = [], 0
    for r in raw:
        name = str(r.get("股票简称", ""))
        ind = str(r.get("所处行业", "") or "")
        if "ST" in name.upper() or "退" in name:
            continue
        dom = INDUSTRY_DOMAIN.get(ind)
        if not dom:
            continue
        n_hard += 1
        if domain and dom != domain:
            continue
        if industry and industry not in ind:
            continue
        rev = _num(r.get("营业总收入-营业总收入"))
        if rev is None or rev <= 0:
            continue
        growth = _num(r.get("营业总收入-同比增长"))
        roe = _num(r.get("净资产收益率"))
        if min_growth is not None and (growth is None or growth < min_growth):
            continue
        if min_roe is not None and (roe is None or roe < min_roe):
            continue
        rows.append({"code": str(r.get("股票代码", "")).zfill(6), "name": name,
                     "industry": ind, "domain": dom, "revenue_yuan": rev,
                     "rev_growth_pct": growth, "roe_pct": roe})
    g = _pct_ranks([r["rev_growth_pct"] for r in rows])
    ro = _pct_ranks([r["roe_pct"] for r in rows])
    import math
    sc = _pct_ranks([math.log(max(r["revenue_yuan"], 1)) for r in rows])
    for i, r in enumerate(rows):
        r["prescore"] = round(0.40 * g[i] + 0.35 * ro[i] + 0.25 * sc[i], 4)
    rows.sort(key=lambda r: -r["prescore"])
    short = rows[:limit]
    conn = _db()
    try:
        screened = {r["code"]: r for r in conn.execute("SELECT code, company_key, grade, total_score FROM screened_companies")}
    finally:
        conn.close()
    for r in short:
        s = screened.get(r["code"])
        r["in_screened"] = bool(s)
        r["screened_company_key"] = s["company_key"] if s else None
        r["screened_grade"] = s["grade"] if s else None
    _audit("screen_market", f"domain={domain} industry={industry} n={len(short)}/{len(rows)} by={me}")
    return _dump({"ok": True, "date": date, "universe_total": len(raw), "hard_tech": n_hard,
                  "filtered": len(rows), "count": len(short), "candidates": short, "viewer": me,
                  "cost": "零（本地缓存业绩报表，纯计算）",
                  "data_boundary": "便宜海选纯本地计算，本层结果不写任何SQL库、不入底稿RAG；"
                                   "后续贵复查数据同样默认只进筛选暂存区，非客户明确要求不入正式库",
                  "next_step": "客户选定后：已在名单(in_screened=true)→ analyze_company(screened_company_key) 或 "
                               "deep_verify(company_name, stock_code, confirm=true) 贵数据复查（默认仅暂存）；"
                               "未在名单 → deep_verify(company_name=简称或全称, stock_code=代码, confirm=true)。"
                               "仅当客户明确表述要入库时才传 persist=true 或事后调 persist_screening_result"})


@mcp.tool()
def screen_sme(keyword: str, min_bids: int = 1, limit: int = 30,
               max_pages: int = 4, region_code: str = "") -> str:
    """两段漏斗①·小微便宜海选（覆盖非上市小微企业，免费官方源，纯搜索零落库）。
    底池=全国公共资源交易平台(GGZY)中标主体库（官方免费无Key，2019至今全国中标成交）——
    上市业绩报表底池(screen_market)覆盖不到的科技小微在此，按企业名称关键词扫池。
    keyword 可逗号分隔多词扫池(如"电池,模具,精密,半导体")，每词≤max_pages页×50条，按USCC去重。
    信号模型（小微不公开财务，故改用）：中标数bid_count=商业化活跃度、成立年限=经营稳定性、
    法人=触达线索。min_bids 过滤中标数下限；region_code 按USCC行政区划码前缀过滤
    (如4403深圳/3302宁波/3205苏州/1101北京)；剔除机关事业单位(USCC非9开头)。
    返回逐企业：USCC/全称/法人/成立年份/中标数/区划码。零付费、不写任何库。
    选定后 deep_verify(uscc=..., company_name=..., confirm=true) 贵复查——USCC直传可绕过天眼查解析。"""
    me = CUR_USER.get()
    kws = [k.strip() for k in re.split(r"[,，]", keyword or "") if k.strip()]
    if not kws:
        return _dump({"ok": False, "error": "keyword is required（行业/技术词，可逗号分隔多词）"})
    max_pages = max(1, min(int(max_pages), 6))
    limit = max(1, min(int(limit), 100))
    min_bids = max(0, int(min_bids))
    import gateway
    import time
    gg = gateway.GgzyClient()
    seen, raw, warns = {}, [], []
    for kw in kws[:6]:
        for page in range(1, max_pages + 1):
            try:
                r = gg.search_company(kw, page=page, size=50)
                res = r.get("result") or {}
                for x in res.get("records") or []:
                    u = str(x.get("uniscid") or "")
                    if not u.startswith("9") or u in seen:
                        continue
                    seen[u] = True
                    raw.append(x)
                if page >= (res.get("pages") or 1):
                    break
            except Exception as exc:
                warns.append(f"{kw} p{page}: {exc!r}"[:120])
                break
            time.sleep(0.4)
    items = []
    for x in raw:
        bc = int(x.get("bidCount") or 0)
        if bc < min_bids:
            continue
        u = str(x.get("uniscid"))
        if region_code and not u[2:8].startswith(region_code):
            continue
        founded = None
        if x.get("estdate"):
            try:
                founded = datetime.fromtimestamp(float(x["estdate"]), tz=timezone.utc).year
            except Exception:
                pass
        items.append({"uscc": u, "company_name": x.get("entname"), "legal_rep": x.get("name"),
                      "founded_year": founded, "bid_count": bc, "region_code": u[2:8]})
    items.sort(key=lambda i: (-i["bid_count"], -(i["founded_year"] or 0)))
    items = items[:limit]
    _audit("screen_sme", f"kw={kws} n={len(items)}/{len(raw)} by={me}")
    return _dump({"ok": True, "keywords": kws, "pool": "GGZY全国公共资源交易平台中标主体库(免费官方)",
                  "scanned_enterprises": len(raw), "count": len(items), "candidates": items,
                  "warnings": warns, "viewer": me, "cost": "零（官方免费无Key）",
                  "data_boundary": "便宜海选纯在线免费搜索，本层结果不写任何SQL库、不入底稿RAG；"
                                   "后续贵复查数据默认只进筛选暂存区，非客户明确要求不入正式库",
                  "signal_note": "小微不公开财务：中标数=商业化活跃度，成立年限=稳定性，法人=触达线索；资质/专利须贵复查核实",
                  "next_step": "客户选定后 deep_verify(uscc=候选uscc, company_name=全称, confirm=true) 贵复查"
                               "（USCC直传绕过天眼查解析，默认仅暂存）→ get_deep_verify；"
                               "客户明确要入库时 persist=true 或 persist_screening_result"})


@mcp.tool()
def screen_sme_web(query: str, max_results: int = 8, extract: bool = True,
                   extract_top: int = 3) -> str:
    """两段漏斗①·小微便宜海选（Web发现口径，Tavily搜索API，廉价付费，零落库）。
    用 Tavily 搜索公开网络，发现 GGZY 中标库(screen_sme)覆盖不到的科技小微：
    专精特新/小巨人/科技型中小企业/高新技术 公示名单、行业目录、园区入驻企业、企业官网、新闻。
    query 自然语言(如"宁波 精密模具 专精特新 小巨人 企业"、"深圳 固态电池 科技型中小企业 名单")。
    max_results≤10 条搜索；extract=true 对前 extract_top(≤3) 条URL批量取全文挖掘企业名/资质/联系方式。
    返回逐条线索：标题/URL/摘要 + 企业名候选(正则) + 资质关键词 + 联系方式线索(电话/邮箱/官网)，
    全部 verified=false（须贵复查核实）。成本：Tavily search 1次 + extract≤1次(批量)，廉价远低于贵复查。
    与 screen_sme 互补：GGZY找有中标的小微，本工具找网络可见/资质公示的小微。
    选定后 deep_verify(company_name=企业全称, uscc=可选, confirm=true) 贵复查 → 4-Skill闭环。"""
    me = CUR_USER.get()
    query = (query or "").strip()
    if not query:
        return _dump({"ok": False, "error": "query is required（自然语言，行业+地区+资质词）"})
    max_results = max(1, min(int(max_results), 10))
    extract_top = max(0, min(int(extract_top), 3))
    import gateway
    tv = gateway.TavilyClient()
    warns = []
    try:
        sr = tv.search(query, max_results=max_results)
    except Exception as exc:
        return _dump({"ok": False, "error": f"Tavily search 失败: {exc!r}"[:200]})
    results = sr.get("results") or []
    fulltext = {}
    if extract and extract_top and results:
        urls = [r.get("url") for r in results[:extract_top] if r.get("url")]
        try:
            er = tv.extract(urls)
            for it in er.get("results") or []:
                fulltext[it.get("url")] = it.get("raw_content") or ""
        except Exception as exc:
            warns.append(f"extract: {exc!r}"[:120])
    qual_kw = ["专精特新", "小巨人", "科技型中小企业", "高新技术", "单项冠军", "隐形冠军", "瞪羚"]
    name_re = re.compile(r"[一-龥A-Za-z0-9（）()·]{4,28}?(?:有限公司|股份有限公司|有限责任公司|研究院|研究所)")
    name_noise = ("利用", "将", "在", "等", "创办", "成立", "开办", "乡镇", "村", "作坊", "工厂")
    leads = []
    for r in results:
        url = r.get("url") or ""
        content = r.get("content") or ""
        full = fulltext.get(url, "")
        blob = (r.get("title") or "") + "\n" + content + "\n" + full
        names = [n for n in dict.fromkeys(name_re.findall(blob))
                 if not any(w in n for w in name_noise)][:12]
        quals = [k for k in qual_kw if k in blob]
        contacts = _extract_contacts(full or content, url, cap=12)
        leads.append({"title": r.get("title"), "url": url, "snippet": content[:400],
                      "company_candidates": names, "qualification_signals": quals,
                      "contact_clues": contacts, "has_fulltext": bool(full), "verified": False})
    all_names = list(dict.fromkeys(n for l in leads for n in l["company_candidates"]))[:40]
    _audit("screen_sme_web", f"q={query[:40]} n={len(leads)} names={len(all_names)} by={me}")
    return _dump({"ok": True, "query": query, "pool": "Tavily公开网络搜索(廉价付费)",
                  "lead_count": len(leads), "leads": leads,
                  "company_candidates_aggregated": all_names, "warnings": warns, "viewer": me,
                  "cost": f"Tavily search 1次 + extract {1 if fulltext else 0}次(批量≤{extract_top}URL，廉价)",
                  "data_boundary": "便宜海选Web发现，本层结果不写任何SQL库、不入底稿RAG；贵复查数据默认仅暂存",
                  "signal_note": "企业名/资质/联系方式均为公开网络线索，verified=false，须 deep_verify 贵复查核实",
                  "next_step": "客户选定企业后 deep_verify(company_name=企业全称, uscc=可选, confirm=true) 贵复查"
                               "→ get_deep_verify；触达渠道补全可用 get_prospect_pack"})


@mcp.tool()
def screen_ipo(stage: str = "declare", board: str = "", province: str = "",
               status: str = "", name_kw: str = "", exclude_failed: bool = True,
               limit: int = 50) -> str:
    """两段漏斗①·便宜海选（拟上市/IPO管线企业，东财免费缓存，零落库）。
    底池=IPO申报(stage=declare,约3900家,含招股书PDF)或辅导备案(stage=tutor,约5400家,更早期)——
    已过券商/监管基本scrutiny的优质成长科技企业，多为中小微；**北交所拟上市=创新型中小企业精准目标**。
    board 拟上市地点过滤(北交所/科创板/创业板/主板,declare有效)；province 注册地(declare)/派出机构省份(tutor)；
    status 最新状态/辅导状态关键词；name_kw 企业名称关键词(行业代理,如"电池"/"半导体")；
    exclude_failed=true(默认)剔除终止/中止/暂缓(负面质量信号)。按更新日期/备案日期倒序。
    逐企业返回 名称/板块/省份/状态/保荐或辅导机构/律所/日期/招股书PDF URL。**零成本纯计算**。
    ★招股说明书=免费完整底稿(财务/专利/业务/风险)，可下载解析喂引擎，省贵数据复查。
    缓存刷新：scripts/fetch_ipo_pool.py（.venv含akshare）。选定后 deep_verify(company_name, confirm=true)
    贵复查，或解析招股书→upload_dossier走免费底稿分析路径。"""
    me = CUR_USER.get()
    stage = (stage or "declare").strip().lower()
    if stage not in ("declare", "tutor"):
        return _dump({"ok": False, "error": "stage 须为 declare(申报) 或 tutor(辅导备案)"})
    fp = config.DATA_DIR / "screening" / f"ipo_{stage}.json"
    if not fp.exists():
        return _dump({"ok": False, "error": f"IPO缓存缺失 {fp.name}，先跑 scripts/fetch_ipo_pool.py"})
    raw = json.loads(fp.read_text(encoding="utf-8"))
    limit = max(1, min(int(limit), 200))
    failed_kw = ("终止", "中止", "暂缓")
    rows = []
    for r in raw:
        name = str(r.get("企业名称") or "")
        if not name:
            continue
        if stage == "declare":
            bd = str(r.get("拟上市地点") or "")
            prov = str(r.get("注册地") or "")
            st = str(r.get("最新状态") or "")
            sponsor = str(r.get("保荐机构") or "")
            law = str(r.get("律师事务所") or "")
            date = str(r.get("更新日期") or "")
            pdf = str(r.get("招股说明书") or "")
        else:
            bd = ""
            prov = str(r.get("派出机构") or "")
            st = str(r.get("辅导状态") or "")
            sponsor = str(r.get("辅导机构") or "")
            law = ""
            date = str(r.get("备案日期") or "")
            pdf = ""
        if board and board not in bd:
            continue
        if province and province not in prov:
            continue
        if status and status not in st:
            continue
        if name_kw and name_kw not in name:
            continue
        if exclude_failed and any(k in st for k in failed_kw):
            continue
        rows.append({"company_name": name, "board": bd or None, "province": prov, "status": st,
                     "sponsor": sponsor, "law_firm": law or None, "date": date,
                     "prospectus_url": pdf if pdf.startswith("http") else None,
                     "has_prospectus": pdf.startswith("http")})
    rows.sort(key=lambda x: x["date"], reverse=True)
    import collections
    board_dist = dict(collections.Counter(x["board"] or "未知" for x in rows))
    short = rows[:limit]
    _audit("screen_ipo", f"stage={stage} board={board} prov={province} n={len(short)}/{len(rows)} by={me}")
    return _dump({"ok": True, "stage": stage, "universe_total": len(raw), "filtered": len(rows),
                  "count": len(short), "board_distribution": board_dist, "candidates": short,
                  "viewer": me, "cost": "零（东财IPO免费缓存，纯计算）",
                  "data_boundary": "便宜海选纯本地计算，不写任何SQL库、不入底稿RAG",
                  "signal_note": "IPO管线企业已过券商/监管基本scrutiny，优质成长科技中小微；北交所=创新型中小企业；"
                                 "招股说明书=免费完整底稿(财务/专利/业务/风险)，可解析喂引擎省贵复查",
                  "next_step": "选定后 deep_verify(company_name=..., confirm=true) 贵复查；"
                               "或下载招股书PDF解析→upload_dossier做底稿分析（免费底稿路径）"})


# ---------- 第一层海选·质量过滤/确认（负面风险筛 + 资质核实触达；QCC免费积分主、Tavily廉价兜底） ----------
_RISK_RED = ("失信", "被执行", "限制高消费", "限高", "破产", "清算", "严重违法", "吊销", "刑事", "拒不执行")
_RISK_YELLOW = ("行政处罚", "经营异常", "税收违法", "欠税", "股权冻结", "开庭", "立案", "裁判", "罚款", "警告", "环保处罚", "抽查不合格")
_QUAL_KW = ("专精特新", "小巨人", "高新技术企业", "科技型中小企业", "单项冠军", "隐形冠军", "瞪羚", "企业技术中心", "重点实验室", "技术创新示范")


def _flatten_qcc(r):
    if isinstance(r, str):
        return r
    try:
        return json.dumps(r, ensure_ascii=False)
    except Exception:
        return str(r)


@mcp.tool()
def risk_screen(company_name: str, uscc: str = "") -> str:
    """第一层海选·负面风险筛（排除层，提升潜客质量）。查企业负面旗标：
    红（失信/被执行/限高/破产/清算/严重违法/吊销/刑事）、黄（行政处罚/经营异常/税收违法/欠税/股权冻结/涉诉）。
    优先企查查risk服务(get_company_risk_scan,免费积分,结构化权威)；积分不足402时降级Tavily搜索
    信用中国/执行信息/裁判文书公开记录(廉价,verified=false须复核)。
    返回 red_flags/yellow_flags/clean + 来源 + source_mode(qcc/tavily/none)。red≥1建议海选剔除。零落库。
    用于 screen_market/screen_sme/screen_sme_web/screen_ipo 候选选定前的质量过滤。"""
    me = CUR_USER.get()
    name = (company_name or "").strip()
    key = (uscc or "").strip() or name
    if not key:
        return _dump({"ok": False, "error": "company_name 或 uscc 至少一个"})
    import gateway
    red, yellow, sources, mode, warns = [], [], [], "none", []
    try:
        q = gateway.QccClient()
        txt = _flatten_qcc(q.call("risk", "get_company_risk_scan", searchKey=key))
        red = [k for k in _RISK_RED if k in txt]
        yellow = [k for k in _RISK_YELLOW if k in txt]
        sources = ["qcc:risk/get_company_risk_scan"]
        mode = "qcc"
    except Exception as exc:
        warns.append(f"qcc: {exc!r}"[:120])
    if mode != "qcc":
        try:
            tv = gateway.TavilyClient()
            sr = tv.search(f"{name or key} 失信被执行人 行政处罚 经营异常 严重违法", max_results=6)
            mode = "tavily"
            for it in sr.get("results") or []:
                blob = (it.get("title") or "") + " " + (it.get("content") or "")
                if name and name not in blob and key not in blob:
                    continue
                url = it.get("url") or ""
                for k in _RISK_RED:
                    if k in blob and k not in red:
                        red.append(k)
                        sources.append(url)
                for k in _RISK_YELLOW:
                    if k in blob and k not in yellow:
                        yellow.append(k)
                        sources.append(url)
        except Exception as exc:
            warns.append(f"tavily: {exc!r}"[:120])
    sources = list(dict.fromkeys(s for s in sources if s))[:12]
    _audit("risk_screen", f"name={name[:30]} mode={mode} red={len(red)} yellow={len(yellow)} by={me}")
    return _dump({"ok": True, "company_name": name or None, "uscc": uscc or None,
                  "source_mode": mode, "red_flags": red, "yellow_flags": yellow,
                  "clean": not red and not yellow, "sources": sources, "warnings": warns,
                  "viewer": me, "verified": mode == "qcc",
                  "recommendation": "red_flags≥1 建议海选剔除" if red else ("yellow_flags 需复核" if yellow else "未见负面旗标"),
                  "cost": "企查查免费积分1次（402时降级Tavily search 1次廉价）",
                  "data_boundary": "海选排除层纯查询，不写任何SQL库、不入底稿RAG",
                  "signal_note": "tavily口径为公开网络线索verified=false须复核；qcc口径结构化权威"})


@mcp.tool()
def qualify_company(company_name: str, uscc: str = "", with_contact: bool = True) -> str:
    """第一层海选·资质核实+触达补全（质量确认层）。核实科技资质：
    专精特新/小巨人/高新技术企业/科技型中小企业/单项冠军/隐形冠军/瞪羚/企业技术中心/荣誉榜单。
    优先企查查operation(get_qualifications+get_honor_info,免费积分)+company(get_contact_info联系方式)，
    积分不足402降级Tavily搜官方公示名单(廉价,verified=false)。
    返回 qualification_signals + is_qualified + contact_clues(电话/邮箱/官网,触达用) + 来源 + source_mode。零落库。
    用于 screen_sme/screen_sme_web 小微候选的优质度确认与触达（接功能一）。"""
    me = CUR_USER.get()
    name = (company_name or "").strip()
    key = (uscc or "").strip() or name
    if not key:
        return _dump({"ok": False, "error": "company_name 或 uscc 至少一个"})
    import gateway
    quals, contacts, sources, mode, warns = [], [], [], "none", []
    try:
        q = gateway.QccClient()
        txt = ""
        for tool in ("get_qualifications", "get_honor_info"):
            try:
                txt += _flatten_qcc(q.call("operation", tool, searchKey=key)) + "\n"
                sources.append(f"qcc:operation/{tool}")
            except Exception as exc:
                warns.append(f"qcc {tool}: {exc!r}"[:100])
        if txt.strip():
            mode = "qcc"
            quals = [k for k in _QUAL_KW if k in txt]
        if with_contact:
            try:
                cr = q.call("company", "get_contact_info", searchKey=key)
                contacts = _extract_contacts(_flatten_qcc(cr), "qcc:contact_info", cap=12)
                sources.append("qcc:company/get_contact_info")
            except Exception as exc:
                warns.append(f"qcc contact: {exc!r}"[:100])
    except Exception as exc:
        warns.append(f"qcc: {exc!r}"[:120])
    if mode != "qcc":
        try:
            tv = gateway.TavilyClient()
            sr = tv.search(f"{name or key} 专精特新 小巨人 高新技术企业 科技型中小企业", max_results=6)
            mode = "tavily"
            for it in sr.get("results") or []:
                blob = (it.get("title") or "") + " " + (it.get("content") or "")
                if name and name not in blob and key not in blob:
                    continue
                url = it.get("url") or ""
                for k in _QUAL_KW:
                    if k in blob and k not in quals:
                        quals.append(k)
                        sources.append(url)
                contacts.extend(_extract_contacts(blob, url, cap=6))
        except Exception as exc:
            warns.append(f"tavily: {exc!r}"[:120])
    contacts = _dedup_channels(contacts, cap=16)
    sources = list(dict.fromkeys(s for s in sources if s))[:12]
    _audit("qualify_company", f"name={name[:30]} mode={mode} quals={quals} by={me}")
    return _dump({"ok": True, "company_name": name or None, "uscc": uscc or None,
                  "source_mode": mode, "qualification_signals": quals, "is_qualified": bool(quals),
                  "contact_clues": contacts, "sources": sources, "warnings": warns,
                  "viewer": me, "verified": mode == "qcc",
                  "cost": "企查查免费积分≤3次（402时降级Tavily search 1次廉价）",
                  "data_boundary": "海选质量确认层纯查询，不写任何SQL库、不入底稿RAG",
                  "signal_note": "资质/联系方式：qcc口径结构化；tavily口径公开线索verified=false须复核",
                  "next_step": "优质候选→deep_verify(confirm=true)贵复查；触达用contact_clues或get_prospect_pack"})


def _dv_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS deep_verify_jobs(
        job_id TEXT PRIMARY KEY, email TEXT, company_name TEXT, stock_code TEXT,
        project_id TEXT, domain TEXT, stage TEXT, status TEXT, pid INTEGER,
        log_path TEXT, exit_path TEXT, company_key TEXT, run_id TEXT, error TEXT,
        created_at TEXT, finished_at TEXT)""")
    conn.commit()


def _dv_estimate(patent_limit, bid_limit, web_limit, patent_enrich=True):
    ps = ("智慧芽检索1次+著录项≤%d条；★价值增强(默认开)：每件再取著录IPC/同族/法律状态/独权"
          "≈4次调用，合计≈%d次（跑PQI五维+Skill1价值研判所需）；--patent-enrich 0 可关（仅著录项，无价值维度）"
          % (patent_limit, 1 + 4 * patent_limit)) if patent_enrich else (
        f"智慧芽专利检索1次+著录项≤{patent_limit}条（廉价模式，无同族/法律状态/独权，价值维度缺失）")
    return {
        "zhihuiya_patents": ps,
        "tianyancha": "天眼查解析1+工商/股东/司法/风险/招投标摘要≈6次（限流10次/分，贵）",
        "qcc": "企查查工商/年报/处罚/失信/裁判/中标等≈12次（免费积分）",
        "jianyu_bidding": f"剑鱼模糊召回扫描≤12块+中标正文≤{bid_limit}条（消耗剑鱼币，贵）",
        "ggzy": "全国公共资源交易平台中标/黑名单/资质（官方免费）",
        "akshare_fin": "AKShare财务年报（免费）",
        "web_fulltext": f"Tavily/Firecrawl官网/新闻稿全文≤{web_limit}页（反包装素材）",
        "storage": "落筛选暂存区 staging.db（默认不入正式SQL、不入WeKnora RAG；"
                   "persist=true 客户明确要入库时才提升）",
        "typical_elapsed": "约2-5分钟（异步作业，get_deep_verify轮询）",
    }


@mcp.tool()
def deep_verify(company_name: str, stock_code: str = "", uscc: str = "", project_id: str = "",
                confirm: bool = False, patent_limit: int = 8, bid_limit: int = 5,
                web_limit: int = 3, domain: str = "", stage: str = "",
                persist: bool = False, patent_enrich: bool = True) -> str:
    """两段漏斗②·贵数据复查（客户选定企业后启用，成本闸门）。
    confirm=false(默认)：只返回成本预估+剑鱼币余额+剩余配额，不发起任何贵调用。
    confirm=true：启动异步多源抓取作业（智慧芽专利+天眼查工商/司法/风险+企查查+剑鱼中标
    +公共资源平台+AKShare财务+官网全文）→ 落筛选暂存区（staging.db）→ 引擎筛选预分析。
    patent_enrich=true(默认)：每件专利追加IPC4/摘要/授权日/同族局/法律状态/独权（每件≈4次
    智慧芽调用）——PQI五维、法律状态折减与Skill1价值研判所必需；false=仅著录项廉价模式
    （成本≈1/4，但无价值分析维度）。
    ★数据落库边界：抓取结果默认【不写正式SQL证据库】【不入WeKnora底稿RAG】，只在
    筛选暂存区供预分析；persist=true 仅在客户明确表述要入库时使用——此时作业完成后
    自动把暂存数据提升落库（正式sources表+底稿渲染）并入WeKnora RAG，可直接接
    get_context_pack/submit_skill_outputs 4-Skill闭环。
    project_id给定→底稿与运行归属该项目（须成员，团队共享跨项目隔离）；不给→平台公共层。
    uscc给定（如 screen_sme 小微海选返回的统一社会信用代码）→直传 fetch_company --uscc，
    company_key 直接取 USCC，绕过天眼查名称解析（小微无股票代码、或天眼查配额受限时的关键通道）。
    每账户贵复查上限10次。patent_limit≤20/bid_limit≤10/web_limit≤5。零确认不扣费。"""
    me = CUR_USER.get()
    company_name = (company_name or "").strip()
    if not company_name:
        return _dump({"ok": False, "error": "company_name is required（企业全称或股票简称）"})
    patent_limit = max(1, min(int(patent_limit), 20))
    bid_limit = max(0, min(int(bid_limit), 10))
    web_limit = max(0, min(int(web_limit), 5))
    conn = _db()
    try:
        _dv_schema(conn)
        used = conn.execute("SELECT COUNT(*) n FROM evaluation_runs WHERE viewer_email=? AND mode='deep_verify'",
                            (me,)).fetchone()["n"]
        running = conn.execute("SELECT COUNT(*) n FROM deep_verify_jobs WHERE email=? AND status='running'",
                               (me,)).fetchone()["n"]
        if project_id:
            if not projects.is_member(conn, project_id, me):
                return _dump({"ok": False, "error": "非项目成员，无法向该项目发起贵复查"})
    finally:
        conn.close()
    if used + running >= DEEP_VERIFY_QUOTA:
        return _dump({"ok": False, "error": f"贵复查配额已用尽（{used}次完成+{running}次进行中，上限{DEEP_VERIFY_QUOTA}）"})
    balance = None
    try:
        import gateway
        balance = gateway.JianYuClient().balance()
    except Exception:
        balance = {"note": "剑鱼余额不可用（凭证缺失或网络），不影响其他源"}
    est = _dv_estimate(patent_limit, bid_limit, web_limit, patent_enrich=bool(patent_enrich))
    if not confirm:
        _audit("deep_verify", f"ESTIMATE name={company_name} by={me}")
        return _dump({"ok": True, "phase": "estimate", "company_name": company_name,
                      "stock_code": stock_code, "uscc": uscc or None, "project_id": project_id or None,
                      "cost_estimate": est, "jianyu_balance": balance,
                      "quota": {"used": used, "running": running, "limit": DEEP_VERIFY_QUOTA},
                      "viewer": me,
                      "data_boundary": "复查结果默认仅进筛选暂存区（不入正式SQL/不入RAG）；"
                                       "客户明确要入库才传 persist=true",
                      "next_step": "确认成本后 deep_verify(company_name=..., confirm=true) 启动贵复查作业"
                                   "（persist 仅在客户明确表述要入库时传 true）"})
    job_id = "DV-" + secrets.token_hex(5)
    log_fp = DEEP_DIR / f"{job_id}.log"
    exit_fp = DEEP_DIR / f"{job_id}.exit"
    py = shlex.quote(sys.executable)
    # persist=false（默认）：抓取只落筛选暂存区，不带 --ingest，不入正式SQL/RAG。
    # persist=true（客户明确要入库）：抓取脚本末尾自动把暂存数据提升落库并入 WeKnora RAG。
    cmd = (f"cd {shlex.quote(str(ROOT / 'data_layer'))} && {py} fetch_company.py "
           f"--name {shlex.quote(company_name)} "
           f"--patent-limit {patent_limit} --bid-limit {bid_limit} --web-limit {web_limit}"
           + (" --patent-enrich 1" if patent_enrich else " --patent-enrich 0")
           + (" --ingest" if persist else ""))
    if stock_code:
        cmd += f" --stock-code {shlex.quote(stock_code)}"
    if uscc:
        cmd += f" --uscc {shlex.quote(uscc.strip())}"
    full = f"{cmd} > {shlex.quote(str(log_fp))} 2>&1; echo $? > {shlex.quote(str(exit_fp))}"
    proc = subprocess.Popen(full, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    conn = _db()
    try:
        _dv_schema(conn)
        conn.execute("INSERT INTO deep_verify_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (job_id, me, company_name, stock_code, project_id or None, domain, stage,
                      "running", proc.pid, str(log_fp), str(exit_fp), None, None, None, _now(), None))
        conn.commit()
    finally:
        conn.close()
    _audit("deep_verify", f"START {job_id} name={company_name} pid={proc.pid} by={me} "
                         f"project={project_id or '-'} persist={persist}")
    return _dump({"ok": True, "phase": "running", "job_id": job_id, "company_name": company_name,
                  "project_id": project_id or None, "persist": persist,
                  "data_boundary": ("客户已明确要求入库：作业完成后暂存数据自动提升落正式SQL并入RAG"
                                    if persist else
                                    "默认边界：结果仅进筛选暂存区，不入正式SQL证据库、不入WeKnora底稿RAG；"
                                    "客户明确要入库后可调 persist_screening_result 提升"),
                  "cost_estimate": est, "jianyu_balance": balance,
                  "quota": {"used": used, "running": running + 1, "limit": DEEP_VERIFY_QUOTA},
                  "viewer": me, "next_step": f"get_deep_verify(job_id='{job_id}') 轮询（约2-5分钟）"})


@mcp.tool()
def get_deep_verify(job_id: str) -> str:
    """轮询贵复查作业（仅发起者本人）。running→返回进度与日志尾；failed→错误与日志尾。
    done→分两种口径（数据落库边界）：
      · 默认（persist=false）：返回筛选预分析参考分——数据仅存筛选暂存区（staging.db），
        未入正式SQL、未入WeKnora底稿RAG；客户明确要入库后须先 persist_screening_result。
      · persist=true（客户明确要求入库）：数据已提升落正式SQL并入RAG，返回的 run_id 可直接
        get_context_pack 进入4-Skill闭环。
    作业完成后自动：底稿归属项目（若指定）→引擎分析→run快照→综合分待Skill回传。"""
    me = CUR_USER.get()
    conn = _db()
    try:
        _dv_schema(conn)
        job = conn.execute("SELECT * FROM deep_verify_jobs WHERE job_id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not job:
        return _dump({"ok": False, "error": "作业不存在"})
    if job["email"] != me:
        _audit("get_deep_verify", f"DENY {job_id} user={me}")
        return _dump({"ok": False, "error": "无权访问该作业（仅发起者本人）"})
    log_tail = ""
    try:
        log_tail = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")[-1200:]
    except Exception:
        pass
    if job["status"] == "done":
        summary = {}
        try:
            fp = ANALYSIS_DIR / f"{job['run_id']}.json"
            if fp.exists():
                d = json.loads(fp.read_text(encoding="utf-8"))
                inn = d.get("innovation") or {}
                summary = {"hard_metrics": {"core_score": inn.get("core_score"), "total_score": inn.get("total_score"),
                                            "grade": inn.get("grade"), "pqi_composite": (d.get("pqi") or {}).get("composite")},
                           "risk_flags": [{"code": f["code"], "level": f["level"]} for f in d.get("risk_flags") or []],
                           "facts_summary": d.get("facts_summary")}
        except Exception:
            pass
        if (job["run_id"] or "").startswith("run-staging-"):
            # 数据落库边界：筛选暂存预分析，数据未入正式SQL/RAG
            return _dump({"ok": True, "job_id": job_id, "status": "done", "company_key": job["company_key"],
                          "run_id": job["run_id"], "project_id": job["project_id"], **summary,
                          "persisted": False, "staging_run": True,
                          "data_boundary": "筛选暂存口径：数据未入正式SQL证据库、未入WeKnora底稿RAG，"
                                           "结果为筛选参考分，非底稿认证阶段的正式信用评估",
                          "persist_hint": "客户明确表述要入库后：persist_screening_result(company_key=...) "
                                          "提升落库+入RAG，再 analyze_company 重跑正式分析",
                          "next_step": "客户明确要入库 → "
                                      f"persist_screening_result(company_key='{job['company_key']}', confirm=true)"})
        return _dump({"ok": True, "job_id": job_id, "status": "done", "company_key": job["company_key"],
                      "run_id": job["run_id"], "project_id": job["project_id"], **summary,
                      "next_step": f"get_context_pack(run_id='{job['run_id']}') 取4个Skill上下文包 → submit_skill_outputs 回传出综合分"})
    if job["status"] == "failed":
        return _dump({"ok": False, "job_id": job_id, "status": "failed", "error": job["error"], "log_tail": log_tail})
    if job["status"] == "finishing":
        return _dump({"ok": True, "job_id": job_id, "status": "finishing",
                      "note": "作业正在收尾（引擎分析），稍后再轮询"})
    # running：检查退出码
    exit_fp = Path(job["exit_path"])
    if not exit_fp.exists():
        return _dump({"ok": True, "job_id": job_id, "status": "running", "log_tail": log_tail,
                      "next_step": "继续 get_deep_verify 轮询"})
    code = exit_fp.read_text().strip()
    conn = _db()
    claimed = conn.execute("UPDATE deep_verify_jobs SET status='finishing' WHERE job_id=? AND status='running'",
                           (job_id,)).rowcount
    conn.commit(); conn.close()
    if not claimed:
        return _dump({"ok": True, "job_id": job_id, "status": "finishing",
                      "note": "作业正在收尾（引擎分析），稍后再轮询"})
    m = re.search(r"企业:\s*(.+?)\s*\|\s*company_key:\s*([0-9A-Z]{18}|\S+)", log_tail)
    if not m:
        try:
            full_log = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")
            m = re.search(r"企业:\s*(.+?)\s*\|\s*company_key:\s*([0-9A-Z]{18}|\S+)", full_log)
        except Exception:
            pass
    cname = m.group(1).strip() if m else job["company_name"]
    key = m.group(2).strip() if m else None
    if code != "0" or not key:
        err = f"抓取退出码={code}" + ("" if key else "；未能解析企业USCC")
        conn = _db()
        conn.execute("UPDATE deep_verify_jobs SET status='failed', error=?, finished_at=? WHERE job_id=?",
                     (err[:300], _now(), job_id))
        conn.commit(); conn.close()
        _audit("get_deep_verify", f"{job_id} FAILED {err}")
        return _dump({"ok": False, "job_id": job_id, "status": "failed", "error": err, "log_tail": log_tail})
    # 抓取成功。persist 判定：fetch_company.py --ingest 模式会在日志中打印
    # "客户已明确要求入库"；据此选择正式管线或筛选暂存预分析。
    try:
        persisted = "客户已明确要求入库" in Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        persisted = False
    pid = job["project_id"]
    if persisted:
        # 客户明确要求入库：数据已提升落正式SQL并已入WeKnora RAG，走正式管线。
        conn = _db()
        try:
            if pid:
                conn.execute("UPDATE sources SET project_id=? WHERE company_key=? AND project_id IS NULL "
                             "AND first_fetched_at>=?", (pid, key, job["created_at"]))
                conn.commit()
            r = _screened(conn, key)
            if r:
                fin = _finance(json.loads(r["inputs_json"] or "{}"))
                dom = r["domain"] if r["domain"] in innovation2.DOMAINS else "通用"
                stg = r["stage"] if r["stage"] in innovation2.STAGES else "成长期"
            else:
                fin, dom, stg = {}, "通用", "成长期"
            if job["domain"] in innovation2.DOMAINS:
                dom = job["domain"]
            if job["stage"] in innovation2.STAGES:
                stg = job["stage"]
        finally:
            conn.close()
        try:
            base = run_engine.run(key, dom, stg, fin, {}, patent_top=5, viewer_email=me or None)
        except Exception as exc:
            conn = _db()
            conn.execute("UPDATE deep_verify_jobs SET status='failed', error=?, finished_at=? WHERE job_id=?",
                         (repr(exc)[:300], _now(), job_id))
            conn.commit(); conn.close()
            _audit("get_deep_verify", f"{job_id} ENGINE ERROR {exc!r}")
            return _dump({"ok": False, "job_id": job_id, "status": "failed", "error": "引擎分析异常: " + repr(exc)[:250],
                          "hint": "暂存区若已有数据，可 staging_preview(company_key=...) 零成本补跑预分析"})
        run_id = base["run_id"]
        conn = _db()
        conn.execute("UPDATE evaluation_runs SET company_name=?, mode='deep_verify', project_ids_json=? WHERE run_id=?",
                     (cname, json.dumps([pid]) if pid else "[]", run_id))
        conn.commit()
        by_kind = {}
        for row in conn.execute("SELECT kind, provider, COUNT(*) n FROM sources WHERE company_key=? GROUP BY kind, provider", (key,)):
            by_kind[f"{row['kind']}/{row['provider']}"] = row["n"]
        conn.close()
        _snapshot_packs(run_id, key)
        payload = {k: base[k] for k in ("run_id", "company", "pqi", "innovation", "risk_flags", "funnel", "packs", "facts_summary")}
        payload["company"]["company_name"] = cname
        payload["viewer"] = me
        payload["mode"] = "deep_verify"
        payload["persisted"] = True
        payload["sources_by_kind"] = by_kind
        payload["data_boundary"] = "客户已明确要求入库：数据已提升落正式SQL并入WeKnora底稿RAG，可进入4-Skill底稿认证闭环"
        payload["next_step"] = f"get_context_pack(run_id='{run_id}') 取4个Skill上下文包内容与量表指令"
        (ANALYSIS_DIR / f"{run_id}.json").write_text(_dump(payload), encoding="utf-8")
        conn = _db()
        conn.execute("UPDATE deep_verify_jobs SET status='done', company_key=?, run_id=?, finished_at=? WHERE job_id=?",
                     (key, run_id, _now(), job_id))
        conn.commit(); conn.close()
        inn = base.get("innovation") or {}
        _audit("get_deep_verify", f"{job_id} DONE persisted key={key} run={run_id} by={me}")
        return _dump({"ok": True, "job_id": job_id, "status": "done", "company_key": key, "company_name": cname,
                      "run_id": run_id, "project_id": pid, "persisted": True,
                      "sources_by_kind": by_kind,
                      "hard_metrics": {"core_score": inn.get("core_score"), "total_score": inn.get("total_score"),
                                   "grade": inn.get("grade"), "pqi_composite": (base.get("pqi") or {}).get("composite")},
                      "risk_flags": [{"code": f["code"], "level": f["level"]} for f in base.get("risk_flags") or []],
                      "facts_summary": base.get("facts_summary"),
                      "data_boundary": "客户已明确要求入库：数据已提升落正式SQL并入WeKnora底稿RAG",
                      "next_step": f"get_context_pack(run_id='{run_id}') 取4个Skill上下文包 → submit_skill_outputs 回传出综合分"})

    # ---- 默认路径：筛选暂存预分析（staging 模式）——不入正式SQL/RAG ----
    conn = _db()
    try:
        r = _screened(conn, key)
        if r:
            fin = _finance(json.loads(r["inputs_json"] or "{}"))
            dom = r["domain"] if r["domain"] in innovation2.DOMAINS else "通用"
            stg = r["stage"] if r["stage"] in innovation2.STAGES else "成长期"
        else:
            fin, dom, stg = {}, "通用", "成长期"
        if job["domain"] in innovation2.DOMAINS:
            dom = job["domain"]
        if job["stage"] in innovation2.STAGES:
            stg = job["stage"]
    finally:
        conn.close()
    try:
        base = run_engine.run(key, dom, stg, fin, {}, patent_top=5,
                              viewer_email=me or None, staging=True)
    except Exception as exc:
        conn = _db()
        conn.execute("UPDATE deep_verify_jobs SET status='failed', error=?, finished_at=? WHERE job_id=?",
                     (repr(exc)[:300], _now(), job_id))
        conn.commit(); conn.close()
        _audit("get_deep_verify", f"{job_id} STAGING ENGINE ERROR {exc!r}")
        return _dump({"ok": False, "job_id": job_id, "status": "failed", "error": "筛选预分析异常: " + repr(exc)[:250],
                      "hint": "可 staging_preview(company_key=...) 重试暂存预分析（零成本、不入正式库）"})
    run_id = base["run_id"]
    by_kind = {}
    sconn = store.staging_connect()
    try:
        for row in sconn.execute("SELECT kind, provider, COUNT(*) n FROM staging_sources WHERE company_key=? GROUP BY kind, provider", (key,)):
            by_kind[f"{row['kind']}/{row['provider']}"] = row["n"]
    finally:
        sconn.close()
    inn = base.get("innovation") or {}
    conn = _db()
    conn.execute("UPDATE deep_verify_jobs SET status='done', company_key=?, run_id=?, finished_at=? WHERE job_id=?",
                 (key, run_id, _now(), job_id))
    conn.commit(); conn.close()
    _audit("get_deep_verify", f"{job_id} DONE(staging) key={key} run={run_id} by={me}")
    return _dump({"ok": True, "job_id": job_id, "status": "done",
                  "company_key": key, "company_name": cname, "run_id": run_id,
                  "project_id": pid, "persisted": False, "staging_run": True,
                  "sources_by_kind_staging": by_kind,
                  "hard_metrics": {"core_score": inn.get("core_score"), "total_score": inn.get("total_score"),
                               "grade": inn.get("grade"), "pqi_composite": (base.get("pqi") or {}).get("composite")},
                  "risk_flags": [{"code": f["code"], "level": f["level"]} for f in base.get("risk_flags") or []],
                  "facts_summary": base.get("facts_summary"),
                  "data_boundary": "筛选暂存口径：全部数据仅存于筛选暂存区（staging.db），"
                                   "未写正式SQL证据库、未入WeKnora底稿RAG；本次结果为筛选参考分，"
                                   "非底稿认证阶段的正式信用评估",
                  "persist_hint": "客户明确表述要入库后：persist_screening_result(company_key=...) "
                                  "把暂存数据提升落正式SQL并入RAG，即可进入4-Skill闭环拿正式综合分",
                  "next_step": "向客户呈现筛选预分析结果；客户明确要入库 → "
                              f"persist_screening_result(company_key='{key}')；"
                              f"客户另交付底稿 → upload_dossier(company_key='{key}', files_json=...)"})


@mcp.tool()
def persist_screening_result(company_key: str, project_id: str = "",
                              ingest_weknora: bool = True,
                              confirm: bool = False) -> str:
    """前期筛选阶段数据落库边界的显式入口：客户明确表述要把筛选暂存数据入库后调用。
    把该企业在筛选暂存区（staging.db）的复查数据提升落库：
      1) 暂存 Source 逐条提升到正式 SQL 证据库（techcredit.db sources 表）；
      2) 渲染正式 Markdown 底稿；ingest_weknora=true(默认)时同步入 WeKnora 底稿 RAG；
      3) 提升后可 analyze_company(company_key) 重跑正式分析，进入4-Skill底稿认证闭环。
    ★边界铁律：本工具是筛选数据进入正式库/RAG的唯一通道，confirm=false(默认)只返回
    待入库清单（暂存条数/类型）不执行任何落库；confirm=true 且客户已明确表述要入库，
    才执行提升。project_id 给定时把提升后的底稿挂到项目（须已是成员）。
    幂等：已提升的条目(promoted=1)不重复提升。"""
    me = CUR_USER.get()
    key = (company_key or "").strip()
    if not key:
        return _dump({"ok": False, "error": "company_key is required"})
    pid = (project_id or "").strip()
    if pid:
        conn = _db()
        try:
            projects.ensure_schema(conn)
            if not projects.get_project(conn, pid):
                return _dump({"ok": False, "error": "项目不存在"})
            if not projects.is_member(conn, pid, me):
                return _dump({"ok": False, "error": "非项目成员，无法把数据挂到该项目"})
        finally:
            conn.close()
    sconn = store.staging_connect()
    try:
        pending = sconn.execute(
            "SELECT kind, provider, COUNT(*) n FROM staging_sources "
            "WHERE company_key=? AND promoted=0 GROUP BY kind, provider", (key,)).fetchall()
        promoted_n = sconn.execute(
            "SELECT COUNT(*) n FROM staging_sources WHERE company_key=? AND promoted=1", (key,)).fetchone()["n"]
    finally:
        sconn.close()
    if not pending and promoted_n == 0:
        return _dump({"ok": False, "error": "该企业在筛选暂存区无数据（可能尚未做贵数据复查 deep_verify，"
                                            "或数据已全部提升）"})
    summary = {f"{r['kind']}/{r['provider']}": r["n"] for r in pending}
    if not confirm:
        _audit("persist_screening_result", f"ESTIMATE key={key} pending={sum(summary.values())} by={me}")
        return _dump({"ok": True, "phase": "estimate", "company_key": key,
                      "pending_promotion": summary, "already_promoted": promoted_n,
                      "viewer": me,
                      "data_boundary": "确认前不做任何落库；暂存数据当前未入正式SQL、未入RAG",
                      "next_step": "客户明确表述要入库后 persist_screening_result(company_key=..., "
                                  "confirm=true) 执行提升（正式sources表+底稿+WeKnora RAG）"})
    # 客户已明确要求入库 → 执行提升
    res = store.promote_staging_to_main(key)
    ing = {"uploaded": 0, "note": "跳过 WeKnora 入库（ingest_weknora=false）"}
    if ingest_weknora and res.get("promoted"):
        # 与 fetch_company.py --ingest 完全同路径：ingest_weknora.py 幂等上传
        # weknora_status!=uploaded 的正式底稿，入 techcredit-dossiers RAG。
        try:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "data_layer" / "ingest_weknora.py"),
                 "--company", key],
                capture_output=True, text=True, timeout=300)
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()
            ing = {"exit_code": proc.returncode,
                   "note": tail[-1] if tail else "ingest_weknora 完成"}
        except Exception as exc:
            ing = {"uploaded": 0, "note": "WeKnora 入库暂不可用：" + repr(exc)[:180] +
                                          "；底稿保持 pending，可重跑 ingest_weknora.py --company"}
    if pid:
        mconn = store.connect()
        try:
            mconn.execute("UPDATE sources SET project_id=? WHERE company_key=? AND project_id IS NULL",
                          (pid, key))
            mconn.commit()
        finally:
            mconn.close()
    _audit("persist_screening_result", f"DONE key={key} promoted={res.get('promoted')} by={me} project={pid or '-'}")
    return _dump({"ok": True, "company_key": key, "project_id": pid or None,
                  "promoted": res.get("promoted"), "weknora": ing,
                  "data_boundary": "客户已明确要求入库：暂存数据已提升落正式SQL证据库"
                                   + ("，并已提交WeKnora底稿RAG" if ingest_weknora else "（RAG入库被跳过，可稍后统一重跑）"),
                  "viewer": me,
                  "next_step": f"analyze_company(company_key='{key}') 重跑正式分析（含RAG事实桥）→ "
                               f"get_context_pack(run_id=...) → submit_skill_outputs 进入4-Skill底稿认证闭环"})


@mcp.tool()
def staging_preview(company_key: str, domain: str = "", stage: str = "",
                    patent_top: int = 5) -> str:
    """对筛选暂存区数据直接跑引擎预分析（零付费、不入正式SQL/RAG）。
    ★降级入口：deep_verify 作业 failed 但暂存区已有数据时，用本工具补跑预分析，
    不必重跑贵复查、不必入库；也适用于作业 done 后换 domain/stage 重算筛选参考分。
    只读 staging.db＋暂存原始JSON，经 patent_quality.normalize_payload 标准化
    （类型双信号重判、法律状态/同族/IPC/独权全量参与），评估记录落 staging.db
    （mode=screening_staging），上下文包写 STAGING_CONTEXT_DIR，跳过 WeKnora 事实桥。
    返回 PQI 五维＋组合分（法律状态折减/授权周期口径/留存窗口纪律见引擎说明）、
    创新积分参考分、红黄灯、漏斗核心专利（含 legal_status）。
    结果为筛选参考分，非底稿认证阶段的正式信用评估；客户明确要入库 →
    persist_screening_result。company_key=18位USCC或暂存区企业键；
    domain/stage 可选（默认通用/成长期）；patent_top 5-8。"""
    me = CUR_USER.get()
    key = (company_key or "").strip()
    if not key:
        return _dump({"ok": False, "error": "company_key is required"})
    sconn = store.staging_connect()
    try:
        # 键解析：暂存区主键可能是USCC或企业全称（USCC解析失败时回退用名称抓取）
        r = sconn.execute("SELECT company_key FROM staging_sources WHERE company_key=? LIMIT 1",
                          (key,)).fetchone()
        if not r:
            r = sconn.execute("SELECT company_key FROM staging_sources WHERE company_name=? LIMIT 1",
                              (key,)).fetchone()
        if not r:
            r = sconn.execute("SELECT company_key FROM staging_sources WHERE company_name LIKE ? LIMIT 1",
                              (f"%{key}%",)).fetchone()
        if not r and len(key) == 18:
            mc = _db()
            sr = mc.execute("SELECT full_name,name FROM screened_companies WHERE code=? LIMIT 1",
                            (key,)).fetchone()
            mc.close()
            if sr:
                nm = sr["full_name"] or sr["name"]
                r = sconn.execute("SELECT company_key FROM staging_sources WHERE company_name=? LIMIT 1",
                                  (nm,)).fetchone()
        resolved = r["company_key"] if r else key
        n = sconn.execute("SELECT COUNT(*) n FROM staging_sources WHERE company_key=?",
                          (resolved,)).fetchone()["n"]
        npat = sconn.execute("SELECT COUNT(*) n FROM staging_sources WHERE company_key=? "
                             "AND kind='patent'", (resolved,)).fetchone()["n"]
        row = sconn.execute("SELECT company_name FROM staging_sources WHERE company_key=? "
                            "AND company_name IS NOT NULL LIMIT 1", (resolved,)).fetchone()
        cname = row["company_name"] if row else resolved
    finally:
        sconn.close()
    if n == 0:
        return _dump({"ok": False, "error": "暂存区无该企业数据", "company_key": key,
                      "hint": "先 deep_verify(confirm=true) 抓取，或 upload_dossier 交付底稿"})
    dom = domain if domain in innovation2.DOMAINS else "通用"
    stg = stage if stage in innovation2.STAGES else "成长期"
    try:
        base = run_engine.run(resolved, dom, stg, {}, {},
                              patent_top=max(5, min(8, patent_top)),
                              viewer_email=None, staging=True)
    except Exception as exc:
        _audit("staging_preview", f"{key} ENGINE ERROR {exc!r}")
        return _dump({"ok": False, "error": "引擎预分析异常: " + repr(exc)[:250]})
    _audit("staging_preview", f"{resolved} OK run={base['run_id']} staged={n} patents={npat} by={me}")
    inn = base.get("innovation") or {}
    # 冷启动友好：内联返回核心专利证据卡（含摘要/独权/IPC/同族/法律状态/allowed_source_ids），
    # 远程零记忆Agent无需访问服务器文件即可据量表研判。
    pr_pack = None
    pr_fp = config.STAGING_CONTEXT_DIR / resolved / "pack_patent_review.json"
    if pr_fp.exists():
        try:
            pr_pack = json.loads(pr_fp.read_text(encoding="utf-8"))
        except Exception:
            pr_pack = None
    return _dump({"ok": True, "company_key": resolved, "input_key": key, "company_name": cname,
                  "run_id": base["run_id"], "staging_run": True, "persisted": False,
                  "n_staged": n, "n_patents": npat,
                  "pqi": base.get("pqi"),
                  "hard_metrics": {"core_score": inn.get("core_score"),
                                   "total_score": inn.get("total_score"), "grade": inn.get("grade")},
                  "risk_flags": base.get("risk_flags"),
                  "funnel": base.get("funnel"),
                  "patent_review_pack": pr_pack,
                  "allowed_source_ids": (pr_pack or {}).get("allowed_source_ids"),
                  "packs": base.get("packs"),
                  "context_pack_tool": f"get_context_pack(run_id='{base['run_id']}', skill='patent_review') "
                                       f"取全部上下文包＋SKILL.md量表（已支持暂存run_id，内联返回）",
                  "data_boundary": base.get("data_boundary_note"),
                  "next_step": f"零记忆价值分析：本返回已含 patent_review_pack 证据卡 → "
                               f"get_skill_instructions('patent_review') 取量表 → 逐件研判标注'非正式闭环'；"
                               f"客户明确要入库 → persist_screening_result(company_key='{resolved}') → 正式闭环"})


@mcp.tool()
def get_skill_instructions(skill: str = "") -> str:
    """读取Skill量表指令（SKILL.md全文），无需run_id、零成本、零落库。
    ★量表阅读与正式提交解耦：筛选/深挖阶段即可取量表对暂存数据做研判
    （输出须标注"非正式闭环·量表人工应用"）；正式 submit_skill_outputs 校验
    仍需 analyze_company/upload_dossier 产生的 run_id 与上下文包 allowed_source_ids。
    skill∈patent_review/commercialization_fit/anti_packaging_factcheck/bank_report/
    prospect_scout；留空返回全部5个量表。量表自包含输出契约，任意Agent可直接据此研判。"""
    names = [skill.strip()] if skill.strip() else list(SKILL_DIRS)
    out = {}
    for nm in names:
        d = SKILL_DIRS.get(nm)
        if not d:
            return _dump({"ok": False, "error": f"未知skill: {nm}",
                          "valid": list(SKILL_DIRS)})
        fp = d / "SKILL.md"
        if not fp.exists():
            return _dump({"ok": False, "error": f"量表文件缺失: {fp}"})
        out[nm] = fp.read_text(encoding="utf-8")
    _audit("get_skill_instructions", ",".join(names))
    return _dump({"ok": True, "skills": out,
                  "note": "量表可应用于暂存数据（标注非正式闭环）；证据卡由 staging_preview/"
                          "get_context_pack 内联返回，无需本地文件；正式提交校验需run_id＋"
                          "allowed_source_ids白名单。"})


# ── 双 MCP 拆分（2026-11-21）──────────────────────────────────────────────
# 调研 MCP（research）：筛选漏斗 + 潜客侦察 + 贵复查 + 分析评分 + 显式入库提升。
# 项目协作 MCP（collab）：项目/成员/共享记忆/知识库事实 + 底稿4-Skill闭环；
#   与调研侧共用3个只读分析工具，协作 Agent 不切换端点即可完成底稿闭环。
# all = 全量26工具，仅供旧端点 /techcredit-mcp/ 向后兼容，不建议新客户使用。
RESEARCH_TOOLS = {
    "search_companies", "get_company_profile", "analyze_company", "get_latest_analysis",
    "discover_prospects", "get_prospect_pack",
    "screen_market", "screen_sme", "screen_sme_web", "screen_ipo",
    "risk_screen", "qualify_company", "deep_verify", "get_deep_verify",
    "persist_screening_result", "staging_preview", "get_skill_instructions",
    "get_context_pack",
}
COLLAB_TOOLS = {
    "list_my_projects", "create_project", "add_project_member", "search_project_facts",
    "project_memory_add", "project_memory_search",
    "upload_dossier", "get_context_pack", "submit_skill_outputs",
    "get_company_profile", "analyze_company", "get_latest_analysis",
    "get_skill_instructions",
}
_RESEARCH_INSTRUCTIONS = (
    "一画·调研 MCP（科创企业调研与筛选）。Bearer=一画账户接入码，只读校验不扣额度。"
    "两段漏斗：①便宜海选 screen_market(上市/业绩报表免费缓存)或 screen_sme(小微/GGZY中标库免费)"
    "或 screen_sme_web(小微/Tavily网络廉价)或 screen_ipo(拟上市/IPO申报辅导池,含招股书底稿)"
    "广筛纯计算不落库 → 客户选定几家 → ②deep_verify(confirm=false只预估成本；confirm=true才发起"
    "贵多源抓取:智慧芽/天眼查/剑鱼等,每账户≤10次)默认只落筛选暂存区 → get_deep_verify 轮询拿结果。"
    "★降级入口：作业failed或暂存未入库时 staging_preview(company_key) 零成本补跑引擎预分析"
    "（PQI+创新积分参考分+红黄灯+漏斗核心专利含法律状态，只读暂存区不入正式库/RAG）；"
    "get_skill_instructions 无需run_id读Skill量表，筛选阶段即可研判（标注非正式闭环）。"
    "★零记忆冷启动价值分析全链路（任意Agent无需本地文件/历史记忆即可用）：staging_preview(company_key)"
    "→内联返回 patent_review_pack 证据卡(摘要/独权/IPC4/同族局/法律状态/allowed_source_ids)＋PQI＋红黄灯"
    "→get_skill_instructions('patent_review')或get_context_pack(run_id,skill)取G-Eval量表(已支持暂存run_id)"
    "→按量表逐件研判、输出标注'非正式闭环·暂存量表应用'。正式综合信用分须客户明确点头入库："
    "persist_screening_result→analyze_company→get_context_pack→submit_skill_outputs(白名单+Pydantic校验)。"
    "质量过滤：risk_screen 负面红黄旗标排除 + qualify_company 专精特新/高新资质核实与触达线索。"
    "潜客侦察：discover_prospects 精筛榜 → get_prospect_pack 公开证据+触达渠道+prospect_scout量表。"
    "分析评分：search_companies / get_company_profile / analyze_company / get_latest_analysis。"
    "★数据落库边界：筛选阶段数据默认不入正式SQL证据库、不入WeKnora底稿RAG；"
    "只有客户明确表述要入库，才用 deep_verify(persist=true) 或 persist_screening_result 提升落库。"
    "跨MCP衔接：客户选定标的后，用一画·项目协作 MCP（/techcredit-collab-mcp/mcp）create_project "
    "建项目、project_memory_add 落触达方案、upload_dossier 进入底稿认证闭环。"
    "铁律：贵复查 confirm=false 不扣费；触达渠道只用企业公开信息，verified=false 须人工复核，"
    "不自动发送消息；筛选数据非客户明确要求不得主动入正式库或RAG。"
)
_COLLAB_INSTRUCTIONS = (
    "一画·项目协作 MCP（团队×企业项目空间，跨Agent协作）。Bearer=一画账户接入码，只读校验不扣额度。"
    "项目=团队×企业：create_project 创建 → add_project_member 邀请成员 → list_my_projects 查看；"
    "同一项目内不同成员各领接入码，即可让不同 Agent 协同同一项目空间。"
    "共享层：search_project_facts 检索项目专属知识库事实（WeKnora）；"
    "project_memory_add / project_memory_search 沉淀与检索团队尽调结论（项目专属记忆服务器，跨项目隔离）。"
    "底稿4-Skill闭环：upload_dossier 上传底稿(SQLite+WeKnora双落库+引擎分析) → "
    "get_context_pack 按run_id取Skill上下文包与SKILL.md量表 → 你的Agent按量表跑4个Skill → "
    "submit_skill_outputs 回传JSON(Pydantic校验+Source_ID伪造拦截+持久熔断) → "
    "综合信用分(硬55%+语义35%-风险扣分/一票否决)。"
    "get_skill_instructions 无需run_id即可读5个Skill量表（暂存数据研判可用，标注非正式闭环）。"
    "get_context_pack 亦支持暂存run_id（run-staging-前缀，来自调研侧staging_preview），内联返回包＋量表。"
    "只读分析工具（与调研MCP共用）：get_company_profile / analyze_company / get_latest_analysis；"
    "调研侧 persist_screening_result 提升入库的数据可直接 analyze_company 进入闭环。"
    "跨MCP衔接：海选/贵复查/潜客侦察用一画·调研 MCP（/techcredit-research-mcp/mcp）。"
    "铁律：Skill结论只能绑定上下文包 allowed_source_ids 内的真实Source_ID，伪造即拦截；"
    "upload底稿仅项目成员可见，平台抓取公共数据全员可见；团队隔离的是运行、上下文包、作业与记忆。"
)
if PROFILE in ("research", "collab"):
    _keep = RESEARCH_TOOLS if PROFILE == "research" else COLLAB_TOOLS
    for _name in list(mcp._tool_manager._tools):
        if _name not in _keep:
            mcp.remove_tool(_name)
    mcp._mcp_server.name = "yihua-research" if PROFILE == "research" else "yihua-collab"
    mcp._mcp_server.instructions = _RESEARCH_INSTRUCTIONS if PROFILE == "research" else _COLLAB_INSTRUCTIONS


class _Auth:
    """ASGI Bearer 中间件：凭证=FateBuddy 接入码（只读校验，不扣额度）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in (scope.get("headers") or [])}
        auth = headers.get("authorization", "")
        supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not supplied:
            q = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
            supplied = (q.get("token") or [""])[0]
        ident = _key_identity(supplied)
        if not ident:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"unauthorized","hint":"Bearer \xe9\x9c\x80 FateBuddy \xe8\xb4\xa6\xe6\x88\xb7\xe6\x8e\xa5\xe5\x85\xa5\xe7\xa0\x81\xef\xbc\x9ahttps://fatebuddy.asia/signup"}'})
            return
        CUR_USER.set(ident)
        return await self.app(scope, receive, send)


class _Charset:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def wrapped_send(message):
            if message.get("type") == "http.response.start":
                headers = []
                for key, value in message.get("headers") or []:
                    if key.lower() == b"content-type" and b"text/event-stream" in value and b"charset" not in value.lower():
                        value += b"; charset=utf-8"
                    headers.append((key, value))
                message["headers"] = headers
            await send(message)
        return await self.app(scope, receive, wrapped_send)


app = mcp.streamable_http_app()
app.add_middleware(_Auth)
app.add_middleware(_Charset)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")