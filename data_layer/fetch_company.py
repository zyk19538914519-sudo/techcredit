#!/usr/bin/env python3
"""数据层入口：企业解析 -> 多源抓取 -> 筛选暂存区落盘编号 -> 暂存底稿预览。

数据落库边界（2026-11-21 确立）：
  本脚本服务"前期筛选阶段·贵数据复查"（两段漏斗②）。抓取结果默认写入
  筛选暂存区（screening_staging/staging.db + staging raw 目录），即：
    - 不写正式 SQL 证据库（techcredit.db sources 表）；
    - 不渲染正式底稿、不入 WeKnora 底稿 RAG。
  只有当【客户明确表述要把数据入库】时，才传 --ingest：此时把暂存数据
  提升落库（正式 sources 表 + 正式底稿），并立即入 WeKnora RAG。
  --dry-run 可只看会动用哪些数据源，不发起任何调用。

数据源（2026-11-21 整合）：
  - 智慧芽 MCP：专利全维度（PAT）
  - 天眼查 MCP：工商/股东（COR）、司法（JUD）、风险总览（RSK）、招投标摘要交叉校验（BID）
  - 剑鱼标讯 MCP：中标/招标正文与详情（BID，按中标单位精确过滤）
  - Tavily/Firecrawl：官网/新闻稿全文（WEB，反包装核查素材）
  - 元典：默认停用；企查查：占位（无凭证）

用法（在云端服务器上）：
  # 前期筛选·贵数据复查（默认：只进暂存区，不入正式 SQL/RAG）
  python3 fetch_company.py --name "北京北方华创微电子装备有限公司" --bid-limit 5
  # 客户明确要求入库后，才加 --ingest（暂存数据提升落库 + 入 WeKnora RAG）
  python3 fetch_company.py --name "北京北方华创微电子装备有限公司" --bid-limit 5 --ingest
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time

import config
import dossier
import gateway
import store


def _tyc_first_row(text: str):
    """从天眼查 search_companies 的 Markdown 候选表取首行 (name, uscc)。"""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and ("公司" in cells[1] or "合伙" in cells[1] or "事务所" in cells[1]):
            return cells[1], cells[2]
    return None, None


def _jy_blocks(text: str):
    """解析剑鱼搜索结果的 【n】 块 -> [(title, id)]。"""
    out = []
    for block in re.split(r"【\d+】", text)[1:]:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        m = re.search(r"ID:\s*(\S+)", block)
        if m:
            out.append((lines[0], m.group(1)))
    return out


def _jy_winner_section(text: str) -> str:
    m = re.search(r"【中标单位】(.*?)【公告内容】", text, re.S)
    return m.group(1) if m else ""


def _text(res: dict) -> str:
    if isinstance(res, dict):
        if "text" in res and isinstance(res["text"], str):
            return res["text"]
        return json.dumps(res, ensure_ascii=False)
    return str(res)


def _first_data(resp):
    d = (resp or {}).get("data") or []
    if isinstance(d, list):
        return d[0] if d else {}
    return d if isinstance(d, dict) else {}


def _ipc4_from_bib(bib):
    """IPC4 = 部+大类+小类（如 H02J/H02M），取 main+further classification。"""
    ipcr = ((bib or {}).get("classification_data") or {}).get("classification_ipcr") or {}
    syms = []
    if ipcr.get("main_classification"):
        syms.append(ipcr["main_classification"])
    syms += ipcr.get("further_classification") or []
    out = []
    for s in syms:
        sec, cls, sub = s.get("section"), s.get("class"), s.get("sub_class")
        if sec and cls and sub:
            code = f"{sec}{cls}{sub}"[:4]
            if code not in out:
                out.append(code)
    return out


def _abstract_from_bib(bib):
    arr = (bib or {}).get("abstracts") or []
    for a in arr:
        if a.get("lang") == "CN" and a.get("text"):
            return a["text"]
    return arr[0].get("text") if arr else None


def _family_offices(fam):
    pf = (fam or {}).get("patent_family") or {}
    members = pf.get("inpadoc_family") or pf.get("simple_family") or []
    offices = []
    for m in members:
        c = m.get("country")
        if c and c not in offices:
            offices.append(c)
    return offices


def _claim1_from_claims(cl):
    """取第一件独立权利要求纯文本 + 独权项数。"""
    import re as _re
    arr = (cl or {}).get("claims") or []
    pick = next((c for c in arr if c.get("lang") == "CN"), arr[0] if arr else {})
    txt, indep = pick.get("claim_text"), pick.get("claim_independent_count")
    if not txt:
        return None, indep
    blocks = _re.findall(r'<div class="indep-clm" num="1">(.*?)</div>', txt, _re.S)
    if blocks:
        c1 = " ".join(_re.sub(r"<[^>]+>", "", b) for b in blocks)
    else:
        c1 = _re.sub(r"<[^>]+>", "", txt)
    c1 = _re.sub(r"\s+", " ", c1).strip()
    return (c1[:1500] or None), indep


def _enrich_patent(ps, pid, payload, want_claims=True):
    """价值增强：补抓 IPC4/摘要/授权日/同族局/法律状态/独权，扁平化进 payload
    （键名与 patent_quality.normalize_payload 对齐）。单源失败降级留 _error，不中断。"""
    if not pid:
        return payload
    try:
        bib = _first_data(ps.bibliography(pid)).get("bibliographic_data") or {}
        payload["ipc4_list"] = _ipc4_from_bib(bib)
        payload["abstract"] = _abstract_from_bib(bib)
        gd = ((bib.get("dates_of_public_availability") or {}).get("printed_with_granted"))
        if gd:
            payload["grant_date"] = gd
        payload["bibliography"] = bib
    except gateway.GatewayError as e:
        payload["_enrich_bibliography_error"] = str(e)[:200]
    try:
        fam = _first_data(ps.family(pid))
        payload["family_offices"] = _family_offices(fam)
        payload["family"] = fam.get("patent_family") or {}
    except gateway.GatewayError as e:
        payload["_enrich_family_error"] = str(e)[:200]
    try:
        ls = _first_data(ps.legal_status(pid))
        st = ls.get("simple_legal_status") or []
        payload["legal_status"] = st[0] if st else None
        payload["simple_legal_status"] = st
    except gateway.GatewayError as e:
        payload["_enrich_legal_error"] = str(e)[:200]
    if want_claims:
        try:
            cl = _first_data(ps.claims(pid))
            c1, indep = _claim1_from_claims(cl)
            if c1:
                payload["claim_1"] = c1
            if indep is not None:
                payload["claim_independent_count"] = indep
            payload["claims"] = cl
        except gateway.GatewayError as e:
            payload["_enrich_claims_error"] = str(e)[:200]
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="企业全称")
    ap.add_argument("--uscc", help="统一社会信用代码")
    ap.add_argument("--patent-limit", type=int, default=8)
    ap.add_argument("--claims", type=int, default=0,
                    help="权利要求抓取件数上限；0=增强模式下全部抓（取独权claim_1），>0=仅前N件")
    ap.add_argument("--patent-enrich", type=int, default=1,
                    help="专利价值增强（默认开）：逐件补抓bibliography(IPC4/摘要/授权日)+family(同族局)"
                         "+legal_status(法律状态)+claims(独权)，扁平化进payload供PQI/skill1价值分析；"
                         "0=仅著录项廉价模式（无价值维度）。每件约4次智慧芽调用")
    ap.add_argument("--bid-limit", type=int, default=5, help="剑鱼中标正文保留条数")
    ap.add_argument("--bid-scan", type=int, default=12, help="剑鱼模糊召回扫描块数上限")
    ap.add_argument("--bid-filter", help="中标单位过滤子串（默认用企业名去后缀）")
    ap.add_argument("--web-limit", type=int, default=3, help="官网/新闻稿全文抓取条数")
    ap.add_argument("--stock-code", help="A股代码（如 300750）；不填则按企业名自动匹配")
    ap.add_argument("--fin-years", type=int, default=4, help="财务年报保留年数")
    ap.add_argument("--skip-fin", action="store_true", help="跳过 AKShare 财务")
    ap.add_argument("--skip-patsnap", action="store_true")
    ap.add_argument("--skip-tyc", action="store_true")
    ap.add_argument("--skip-qcc", action="store_true", help="跳过企查查MCP")
    ap.add_argument("--skip-ggzy", action="store_true", help="跳过全国公共资源交易平台")
    ap.add_argument("--skip-rd", action="store_true", help="跳过巨潮年报研发人员解析")
    ap.add_argument("--skip-jianyu", action="store_true")
    ap.add_argument("--skip-web", action="store_true")
    ap.add_argument("--ingest", action="store_true",
                    help="客户明确要求入库时才使用：把暂存数据提升落正式SQL并立即入WeKnora RAG；"
                         "默认（不带本参数）只进筛选暂存区，不入正式库/RAG")
    ap.add_argument("--dry-run", action="store_true", help="只打印将调用的数据源，不发起任何抓取/落盘")
    args = ap.parse_args()
    if not args.name and not args.uscc:
        ap.error("--name 与 --uscc 至少提供一个")

    if args.dry_run:
        print("dry-run：将调用的数据源 = 天眼查(解析+5端点) 企查查(≈19端点) 公共资源交易 "
              "巨潮年报研发解析 剑鱼中标(模糊召回≤scan,正文≤bid_limit) Tavily全文 "
              "AKShare财务 智慧芽专利(≤patent_limit)")
        print(f"落库模式 = {'正式SQL+WeKnora RAG（客户已明确要求入库）' if args.ingest else '筛选暂存区（不入正式SQL/不进RAG）'}")
        return

    # 前期筛选阶段：默认全部写筛选暂存区（staging.db），与正式证据库物理隔离。
    # 只有客户明确表述要入库（--ingest）时，末尾才把暂存数据提升落库。
    conn = store.staging_connect()
    cname = args.name or args.uscc
    company_key = args.uscc or args.name

    # 0) 天眼查解析精确主体（名称 + USCC）——解析记录同样只落暂存日志
    tyc = None
    if not args.skip_tyc:
        try:
            tyc = gateway.TianyanchaClient()
            res = tyc.search_companies(args.uscc or args.name, page_size=3)
            n, u = _tyc_first_row(_text(res))
            if n:
                cname = n
            if u and re.fullmatch(r"[0-9A-Z]{18}", u):
                company_key = u
            store.staging_log_fetch(conn, company_key=company_key, provider="tianyancha",
                                    endpoint="search_companies", params={"query": args.uscc or args.name},
                                    started_at=store.now(), status="ok", record_count=1 if n else 0)
            print(f"企业: {cname} | company_key: {company_key}")
        except gateway.GatewayError as exc:
            print(f"  [失败] 天眼查解析: {exc}")
            tyc = None
    else:
        print(f"企业: {cname} | company_key: {company_key}")

    def save(kind, provider, natural_key, title, payload, meta=None):
        # 落筛选暂存区（staging.db），不写正式 sources 表，不进底稿 RAG
        return store.staging_upsert_source(conn, kind=kind, provider=provider,
                                          company_key=company_key, company_name=cname,
                                          natural_key=natural_key, title=title,
                                          payload=payload, meta=meta)

    # 1) 天眼查：工商/股东/司法/风险/招投标摘要
    if tyc is not None:
        tyc_jobs = [
            ("company", "basic_profile", lambda: tyc.basic_profile(cname), "工商基础画像"),
            ("company", "get_shareholder_info", lambda: tyc.call_tool("get_shareholder_info", cname), "股东出资"),
            ("judicial", "get_judicial_case", lambda: tyc.call_tool("get_judicial_case", cname), "涉诉案件"),
            ("risk", "get_risk_overview", lambda: tyc.call_tool("get_risk_overview", cname), "风险总览(处罚/失信/被执行)"),
            ("bid", "get_bidding_info", lambda: tyc.call_tool("get_bidding_info", cname), "招投标摘要(交叉校验)"),
        ]
        for kind, tool, fn, desc in tyc_jobs:
            t0 = store.now()
            try:
                payload = fn()
                save(kind, "tianyancha", f"{company_key}|{tool}", f"{cname}-{desc}", payload,
                     {"tool": tool})
                store.staging_log_fetch(conn, company_key=company_key, provider="tianyancha", endpoint=tool,
                                params={"company": cname}, started_at=t0, status="ok", record_count=1)
                print(f"  [天眼查] {desc}: 1 条")
            except gateway.GatewayError as exc:
                store.staging_log_fetch(conn, company_key=company_key, provider="tianyancha", endpoint=tool,
                                params={"company": cname}, started_at=t0, status="failed", note=str(exc)[:300])
                print(f"  [失败] 天眼查 {desc}: {exc}")
            time.sleep(1)  # 尊重 ~10 次/分钟限流

    # 1.5) 企查查 MCP（免费积分；补司法/处罚/中标/股东/年报/布图/软著/标准/资质/融资等）
    if not args.skip_qcc:
        try:
            qcc = gateway.QccClient()
            jobs = [
                ("company", "company", "get_company_profile", {"searchKey": cname}, "工商画像"),
                ("company", "company", "get_shareholder_info", {"searchKey": cname}, "股东出资"),
                ("company", "company", "get_external_investments", {"searchKey": cname}, "对外投资"),
                ("financial", "company", "get_annual_reports", {"searchKey": cname}, "工商年报"),
                ("financial", "company", "get_financial_data", {"searchKey": cname}, "财务数据"),
                ("penalty", "risk", "get_administrative_penalty", {"searchKey": cname}, "行政处罚"),
                ("risk", "risk", "get_dishonest_info", {"searchKey": cname}, "失信被执行"),
                ("risk", "risk", "get_judgment_debtor_info", {"searchKey": cname}, "被执行人"),
                ("judicial", "risk", "get_judicial_documents", {"searchKey": cname}, "裁判文书"),
                ("judicial", "case", "get_judicial_case_search", {"party": cname}, "涉诉案件"),
                ("ipx", "ipr", "get_integrated_circuit_layout", {"searchKey": cname}, "集成电路布图"),
                ("ipx", "ipr", "get_software_copyright_info", {"searchKey": cname}, "软件著作权"),
                ("standard", "ipr", "get_standard_info", {"searchKey": cname}, "标准制定"),
                ("cert", "operation", "get_qualifications", {"searchKey": cname}, "科技资质"),
                ("award", "operation", "get_honor_info", {"searchKey": cname}, "荣誉奖励"),
                ("rank", "operation", "get_ranking_list_info", {"searchKey": cname}, "行业榜单"),
                ("fund", "operation", "get_financing_records", {"searchKey": cname}, "融资记录"),
                ("hr", "operation", "get_recruitment_info", {"searchKey": cname}, "招聘结构"),
            ]
            for kind, svc, tool, targs, desc in jobs:
                t0 = store.now()
                try:
                    payload = qcc.call(svc, tool, **targs)
                    save(kind, "qcc", f"{company_key}|{tool}", f"{cname}-{desc}", payload,
                         {"query": cname, "tool": tool})
                    store.staging_log_fetch(conn, company_key=company_key, provider="qcc", endpoint=f"{svc}/{tool}",
                                    params=targs, started_at=t0, status="ok", record_count=1)
                    print(f"  [企查查] {desc}: 已入库")
                except gateway.GatewayError as exc:
                    store.staging_log_fetch(conn, company_key=company_key, provider="qcc", endpoint=f"{svc}/{tool}",
                                    params=targs, started_at=t0, status="failed", note=str(exc)[:300])
                    print(f"  [失败] 企查查 {desc}: {exc}")
                time.sleep(0.4)
            t0 = store.now()
            try:
                bids = qcc.call("operation", "get_bidding_info", searchKey=cname, role="中标方")
                save("bid", "qcc", f"{company_key}|qcc_bidding", f"{cname}-中标记录(企查查)", bids,
                     {"query": cname, "role": "中标方"})
                store.staging_log_fetch(conn, company_key=company_key, provider="qcc", endpoint="operation/get_bidding_info",
                                params={"searchKey": cname, "role": "中标方"}, started_at=t0, status="ok", record_count=1)
                print("  [企查查] 中标记录: 已入库")
            except gateway.GatewayError as exc:
                store.staging_log_fetch(conn, company_key=company_key, provider="qcc", endpoint="operation/get_bidding_info",
                                params={"name": cname}, started_at=t0, status="failed", note=str(exc)[:300])
                print(f"  [失败] 企查查 中标: {exc}")
        except gateway.GatewayError as exc:
            print(f"  [跳过] 企查查: {exc}")

    # 1.6) 全国公共资源交易平台·数据服务（免费无Key：中标/交易黑名单/资质）
    if not args.skip_ggzy:
        t0 = store.now()
        try:
            g = gateway.GgzyClient()
            res = g.search_company(args.uscc or cname)
            recs = ((res.get("result") or {}).get("records")) or []
            match = next((r for r in recs if r.get("uniscid") == company_key), None) or (recs[0] if recs else None)
            if match:
                uid = match.get("uniscid")
                bids = g.bid_list(uid, size=10)
                save("bid", "ggzy", f"{company_key}|ggzy_bid_list", f"{cname}-中标记录(公共资源交易)", bids,
                     {"uniscid": uid})
                bl = g.black_list(uid)
                save("risk", "ggzy", f"{company_key}|ggzy_black", f"{cname}-交易黑名单(公共资源交易)", bl,
                     {"uniscid": uid})
                qu = g.qualient(uid)
                if qu.get("result"):
                    save("cert", "ggzy", f"{company_key}|ggzy_qual", f"{cname}-交易资质(公共资源交易)", qu,
                         {"uniscid": uid})
                store.staging_log_fetch(conn, company_key=company_key, provider="ggzy", endpoint="search+bid_list+black_list",
                                params={"uniscid": uid}, started_at=t0, status="ok", record_count=1)
                print(f"  [公共资源] 中标/黑名单/资质: 已入库 (bidCount={match.get('bidCount')})")
            else:
                store.staging_log_fetch(conn, company_key=company_key, provider="ggzy", endpoint="search",
                                params={"keyword": args.uscc or cname}, started_at=t0, status="empty")
                print("  [公共资源] 未匹配到交易主体")
        except gateway.GatewayError as exc:
            store.staging_log_fetch(conn, company_key=company_key, provider="ggzy", endpoint="search",
                            params={"keyword": args.uscc or cname}, started_at=t0, status="failed", note=str(exc)[:300])
            print(f"  [失败] 公共资源: {exc}")

    # 1.7) 巨潮年报PDF：研发人员数/占比/研发费用（上市；指标3/5分母/1/2交叉）
    if not args.skip_rd:
        t0 = store.now()
        try:
            cmd = [config.AKSHARE_PYTHON, str(config.FIN_HELPER.parent / "fin_rd.py")]
            if args.stock_code:
                cmd += ["--code", args.stock_code]
            cmd += ["--name", cname]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise gateway.GatewayError(f"fin_rd 子进程失败: {proc.stderr[-200:]}")
            res = json.loads(proc.stdout)
            n = 0
            for rec in res.get("records", []):
                save("hr", "cninfo", rec["natural_key"], rec["title"], rec["payload"],
                     {"code": res.get("code")})
                n += 1
            store.staging_log_fetch(conn, company_key=company_key, provider="cninfo", endpoint="annual_report_pdf",
                            params={"code": res.get("code")}, started_at=t0, status="ok" if n else "empty",
                            record_count=n, note=res.get("error", ""))
            print(f"  [巨潮年报] 研发人员/研发投入: {n} 条 {res.get('error','')}")
        except (gateway.GatewayError, ValueError, subprocess.TimeoutExpired) as exc:
            store.staging_log_fetch(conn, company_key=company_key, provider="cninfo", endpoint="annual_report_pdf",
                            params={"name": cname}, started_at=t0, status="failed", note=str(exc)[:300])
            print(f"  [失败] 巨潮年报: {exc}")

    # 2) 剑鱼标讯：中标正文（按中标单位精确过滤模糊召回）
    if not args.skip_jianyu:
        t0 = store.now()
        try:
            jy = gateway.JianYuClient()
            bal = _text(jy.balance())
            print(f"  [剑鱼] 余额: {' / '.join(l.strip() for l in bal.splitlines() if '余额' in l or '今日' in l)}")
            core = args.bid_filter or re.sub(r"(股份有限公司|有限责任公司|有限公司|公司)$", "", cname)
            res = jy.search_bidding(keyword=cname, search_field="中标企业", info_type="中标", page=1)
            blocks = _jy_blocks(_text(res))[: args.bid_scan]
            kept = 0
            for title, bid_id in blocks:
                if kept >= args.bid_limit:
                    break
                try:
                    detail = jy.bidding_detail(bid_id)
                except gateway.GatewayError:
                    continue
                dtext = _text(detail)
                if core and core not in _jy_winner_section(dtext):
                    continue  # 模糊召回的无关中标，丢弃
                save("bid", "jianyu", bid_id, title[:120], detail, {"core_filter": core})
                kept += 1
                print(f"  [剑鱼] 中标正文: {title[:40]}")
            store.staging_log_fetch(conn, company_key=company_key, provider="jianyu", endpoint="search_bidding+detail",
                            params={"keyword": cname, "search_field": "中标企业"}, started_at=t0,
                            status="ok", record_count=kept, note=f"召回{len(blocks)}条,精确保留{kept}条")
        except gateway.GatewayError as exc:
            store.staging_log_fetch(conn, company_key=company_key, provider="jianyu", endpoint="search_bidding",
                            params={"keyword": cname}, started_at=t0, status="failed", note=str(exc)[:300])
            print(f"  [失败] 剑鱼: {exc}")

    # 3) 网页搜索 + 全文抓取（反包装核查素材）
    if not args.skip_web:
        t0 = store.now()
        try:
            tv = gateway.TavilyClient()
            sr = tv.search(f"{cname} 官网 核心技术 产品 新闻稿", max_results=args.web_limit + 2)
            urls = [r.get("url") for r in (sr.get("results") or []) if r.get("url")][: args.web_limit]
            n = 0
            if urls:
                ex = tv.extract(urls)
                for item in (ex.get("results") or []):
                    u = item.get("url")
                    if not u:
                        continue
                    save("web", "tavily", u, (item.get("title") or u)[:120], item, {"query": cname})
                    n += 1
            store.staging_log_fetch(conn, company_key=company_key, provider="tavily", endpoint="search+extract",
                            params={"query": cname, "urls": urls}, started_at=t0, status="ok", record_count=n)
            print(f"  [Tavily] 官网/新闻稿全文: {n} 条")
        except gateway.GatewayError as exc:
            store.staging_log_fetch(conn, company_key=company_key, provider="tavily", endpoint="search+extract",
                            params={"query": cname}, started_at=t0, status="failed", note=str(exc)[:300])
            print(f"  [失败] Tavily: {exc}")

    # 3.5) AKShare 财务（创新积分2.0 投入/发展分；仅上市/挂牌有公开财报）
    if not args.skip_fin:
        t0 = store.now()
        try:
            akc = gateway.AkshareClient()
            res = akc.financial(code=args.stock_code, name=cname, years=args.fin_years)
            n = 0
            for rec in res.get("records", []):
                if rec["payload"].get("_error"):
                    continue
                save("financial", "akshare", rec["natural_key"], rec["title"], rec["payload"],
                     {"code": res.get("code"), "statement": rec["payload"].get("statement")})
                n += 1
            store.staging_log_fetch(conn, company_key=company_key, provider="akshare",
                            endpoint="stock_financial_report_sina+analysis_indicator",
                            params={"code": res.get("code"), "name": cname}, started_at=t0,
                            status="ok" if n else "empty", record_count=n,
                            note=res.get("error", ""))
            print(f"  [AKShare] 财务: {n} 条 (code={res.get('code')}) {res.get('error','')}")
        except gateway.GatewayError as exc:
            store.staging_log_fetch(conn, company_key=company_key, provider="akshare", endpoint="financial",
                            params={"name": cname}, started_at=t0, status="failed", note=str(exc)[:300])
            print(f"  [失败] AKShare: {exc}")

    # 4) 智慧芽专利
    if not args.skip_patsnap:
        t0 = store.now()
        query = f'ANC:("{cname}")'
        try:
            ps = gateway.PatsnapClient()
            res = ps.search_patents(query, limit=args.patent_limit)
            hits = (res.get("data") or {}).get("results") or []
            n = 0
            for i, hit in enumerate(hits):
                pn = hit.get("pn") or hit.get("patent_number")
                if not pn:
                    continue
                payload = dict(hit)
                if args.patent_enrich and hit.get("patent_id"):
                    want_claims = (args.claims <= 0) or (i < args.claims)
                    payload = _enrich_patent(ps, hit["patent_id"], payload, want_claims=want_claims)
                elif i < args.claims and hit.get("patent_id"):
                    for extra, fn in (("bibliography", ps.bibliography), ("claims", ps.claims)):
                        try:
                            payload[extra] = (fn(hit["patent_id"]).get("data") or {})
                        except gateway.GatewayError as e:
                            payload[extra] = {"_error": str(e)[:200]}
                save("patent", "zhihuiya", pn, (hit.get("title") or pn)[:120], payload,
                     {"query": query, "patent_id": hit.get("patent_id"),
                      "enriched": bool(args.patent_enrich and hit.get("patent_id"))})
                n += 1
            store.staging_log_fetch(conn, company_key=company_key, provider="zhihuiya", endpoint="search_patents",
                            params={"query": query, "limit": args.patent_limit}, started_at=t0,
                            status="ok", record_count=n,
                            note=f"total={(res.get('data') or {}).get('total_search_result_count')}; "
                                 f"enrich={'on(IPC4/同族/法律状态/独权)' if args.patent_enrich else 'off(仅著录项)'}")
            print(f"  [智慧芽] 专利: {n} 条（价值增强={'开' if args.patent_enrich else '关'}）")
        except gateway.GatewayError as exc:
            store.staging_log_fetch(conn, company_key=company_key, provider="zhihuiya", endpoint="search_patents",
                            params={"query": query}, started_at=t0, status="failed", note=str(exc)[:300])
            print(f"  [失败] 智慧芽: {exc}")

    # 5) 企查查占位登记
    if not gateway.placeholder_status("qcc"):
        store.staging_log_fetch(conn, company_key=company_key, provider="qcc", endpoint="corp_profile",
                                params={}, started_at=store.now(), status="placeholder", note="占位：需配置 QCC_TOKEN")

    # 6) 暂存底稿预览 + 汇总（暂存区口径：不写正式 sources 表，不入 RAG）
    n = store.staging_render_dossier(conn, company_key)
    rows = conn.execute("SELECT * FROM staging_sources WHERE company_key=?",
                        (company_key,)).fetchall()
    by = {}
    for r in rows:
        k = f"{r['kind']}/{r['provider']}"
        by[k] = by.get(k, 0) + 1
    print(f"暂存底稿预览: {n} 份 -> {config.STAGING_DOSSIER_DIR / company_key}")
    print(f"筛选暂存区入库: 共 {len(rows)} 条 | {json.dumps(by, ensure_ascii=False)}")
    print(f"暂存库: {config.STAGING_DB_PATH}")
    if not args.ingest:
        print("落库边界: 本次数据仅存于筛选暂存区——未写正式SQL sources 表，未入 WeKnora 底稿 RAG；"
              "客户明确要求入库后，重跑加 --ingest 或调用 MCP persist_screening_result 提升落库")

    # 7) 仅当客户明确表述要入库（--ingest）时：暂存数据提升落库 + 入 WeKnora RAG
    if args.ingest:
        print("---- 客户已明确要求入库：提升落库（正式SQL + WeKnora RAG） ----")
        res = store.promote_staging_to_main(company_key)
        print(f"提升落库: {res['promoted']} 条 -> {config.DB_PATH}（正式 sources 表）")
        print("---- WeKnora 入库 ----")
        subprocess.run([sys.executable, str(config.PROJECT_ROOT / "data_layer" / "ingest_weknora.py"),
                        "--company", company_key], check=False)
    else:
        print("未传 --ingest：按默认边界保持暂存，不执行任何正式落库/RAG操作")


if __name__ == "__main__":
    main()