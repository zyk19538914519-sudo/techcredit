#!/usr/bin/env python3
"""漏斗式抽样与硬排雷（Python功能实现层②）。

硬排雷（红灯）——统计规则：
  R1 突击申请：近12个月申请量 ≥ 3×前两年年均 且 近12月占比 > 50%
  R2 发明人断层：Top3核心发明人近24个月全部未再出现且无新发明人承接
  R3 专利无效雷：失效/无效宣告占比 > 30%（法律状态可用时）
黄灯（提示）：
  Y1 单一IPC过度集中：Top1 4位IPC集中度 > 70%
  Y2 授权周期异常：平均授权周期 > 同期群P90（无同期群时按36个月固定线）
  Y3 单一发明人依赖：单一发明人参与专利占比 > 60%
  Y4 数据窗口不足：全部申请早于24个月前（留存率0可能为数据窗口问题）

漏斗抽样：按 patent_quality.value_score 取 Top5-8 核心专利 + Top10 中标公告，
只把精简上下文喂给下游 Agent（省 80% Token 的第一道闸门）。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import patent_quality as PQ


def _apdt_date(p):
    """申请日解析：与 patent_quality._to_date 同一容错实现（YYYYMMDD 原生格式
    及 ISO/中文/缺零等外来格式均可），防风险灯与引擎日期口径分叉。"""
    return PQ._to_date(p.get("apdt"))


def evaluate_risks(patents, pqi, today=None) -> list:
    """返回 [{code, level(red|yellow), title, detail, source_ids}]。"""
    today = today or date.today()
    flags = []

    # R1 突击申请
    dates = [(p, _apdt_date(p)) for p in patents]
    dated = [(p, d) for p, d in dates if d]
    if dated:
        y1 = date(today.year - 1, today.month, today.day)
        y3_ago = date(today.year - 3, today.month, today.day)
        recent = [p for p, d in dated if d >= y1]
        prior = [p for p, d in dated if y3_ago <= d < y1]
        prior_per_year = len(prior) / 2
        # 突击申请=相对自身历史突变；无历史基数（前两年0件）时无法判定突变，不报红灯
        # （真实原因往往是数据源只回溯了近期，属数据缺口，由Y4与来源说明覆盖）
        if recent and prior_per_year >= 1 and len(recent) >= 3 * prior_per_year \
                and len(recent) / len(dated) > 0.5:
            flags.append({"code": "R1_BURST", "level": "red",
                          "title": "突击申请",
                          "detail": f"近12个月申请{len(recent)}件≥3×前两年年均{prior_per_year:.1f}件，"
                                   f"且占全部{len(dated)}件的{len(recent)/len(dated):.0%}",
                          "source_ids": [p["source_id"] for p in recent]})
        # Y4 数据窗口：近24个月（而非12个月）无新申请时提示
        y2 = date(today.year - 2, today.month, today.day)
        if not any(d >= y2 for _, d in dated):
            flags.append({"code": "Y4_STALE_WINDOW", "level": "yellow",
                          "title": "数据窗口不足",
                          "detail": f"近24个月内无新申请记录（共{len(dated)}件，最早"
                                   f"{min(d for _, d in dated)}、最晚{max(d for _, d in dated)}），"
                                   "留存率=0可能是数据窗口问题而非团队断层，请核实抓取范围",
                          "source_ids": [t[0]["source_id"] for t in
                                         sorted(dated, key=lambda x: x[1])[:3]]})

    # R2 发明人断层
    from collections import Counter
    cnt = Counter(i for p in patents for i in p["inventors"])
    core = [n for n, _ in cnt.most_common(3)]
    if core:
        cutoff = date(today.year - 2, today.month, today.day)
        recent_inv = {i for p, d in dated if d and d >= cutoff for i in p["inventors"]}
        new_inv = recent_inv - set(i for p in patents for i in p["inventors"])
        if not (set(core) & recent_inv) and not new_inv:
            flags.append({"code": "R2_INVENTOR_GAP", "level": "red",
                          "title": "核心发明人断层",
                          "detail": f"Top3核心发明人({('、'.join(core))})近24个月均未出现在新申请中，"
                                   "且无新发明人承接",
                          "source_ids": [p["source_id"] for p in
                                         [q for q in patents if set(q["inventors"]) & set(core)][:3]]})

    # R3 无效/失效雷（法律状态可用时）——失效判定与 patent_quality 价值折减
    # 共用 is_lapsed_status（驳回/视撤/Revoked 等外来措辞同口径），防两处分叉
    statused = [p for p in patents if p.get("legal_status")]
    if statused:
        bad = [p for p in statused if PQ.is_lapsed_status(p["legal_status"])]
        if len(bad) / len(statused) > 0.3:
            flags.append({"code": "R3_INVALID_HEAVY", "level": "red",
                          "title": "专利无效/失效占比过高",
                          "detail": f"{len(bad)}/{len(statused)} 件处于无效或失效状态"
                                   f"（{len(bad)/len(statused):.0%}>30%）",
                          "source_ids": [p["source_id"] for p in bad[:5]]})

    # Y1 IPC集中度
    if any(p["ipc4_list"] for p in patents):
        ipc_cnt = Counter(ipc for p in patents for ipc in set(p["ipc4_list"]))
        top_ipc, top_n = ipc_cnt.most_common(1)[0]
        total = sum(ipc_cnt.values())
        if top_n / total > 0.7:
            flags.append({"code": "Y1_IPC_CONCENTRATION", "level": "yellow",
                          "title": "技术领域过度集中",
                          "detail": f"Top1 IPC4={top_ipc} 占比{top_n/total:.0%}>70%",
                          "source_ids": [p["source_id"] for p in patents if top_ipc in p["ipc4_list"]][:5]})

    # Y2 授权周期异常
    grants = [p for p in patents if p["grant_days"] is not None]
    if grants and len(grants) >= 3:
        days = sorted(p["grant_days"] for p in grants)
        import math
        p90 = days[min(len(days) - 1, math.floor(0.9 * (len(days) - 1)))]
        avg_days = sum(days) / len(days)
        if avg_days > max(p90, 36 * 30):
            flags.append({"code": "Y2_GRANT_LAG", "level": "yellow",
                          "title": "平均授权周期偏长",
                          "detail": f"平均授权周期{avg_days/30:.1f}个月",
                          "source_ids": [p["source_id"] for p in grants[:3]]})

    # Y3 单一发明人依赖
    if cnt:
        solo, solo_n = cnt.most_common(1)[0]
        involved = sum(1 for p in patents if solo in p["inventors"])
        if involved / len(patents) > 0.6:
            flags.append({"code": "Y3_SOLO_INVENTOR", "level": "yellow",
                          "title": "单一发明人依赖",
                          "detail": f"发明人{solo}参与了{involved}/{len(patents)}件专利（>60%）",
                          "source_ids": [p["source_id"] for p in patents if solo in p["inventors"]][:5]})

    return flags


def funnel_select(patents, pqi, *, top_patents=5, top_bids=10):
    """按价值分选 Top-K 核心专利与 Top-K 中标公告（中标源当前占位则显式声明缺口）。"""
    scored = sorted(pqi.get("per_patent", []),
                    key=lambda x: -(x.get("value_score") or 0))
    core_patents = scored[:top_patents]
    bids = []  # 中标公告：TYC_TOKEN 配置后由 gateway 落盘 BID-* 记录
    return {
        "core_patents": core_patents,
        "bids": bids,
        "bid_source_pending": True,
        "funnel_note": f"专利{len(patents)}件 → 精选{len(core_patents)}件；"
                       f"中标公告数据源占位（TYC_TOKEN 待配置），本批为空",
    }


def build_context_packs(company, patents, pqi, risk_flags, funnel_result,
                        innovation_result, out_dir: Path) -> dict:
    """为4个Agent Skill生成精简上下文包（每个Skill只拿它需要的字段）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    core_ids = [c["source_id"] for c in funnel_result["core_patents"]]

    patent_cards = []
    for c in funnel_result["core_patents"]:
        p = next(x for x in patents if x["source_id"] == c["source_id"])
        patent_cards.append({
            "source_id": c["source_id"], "pn": p["pn"], "title": p["title"],
            "type": p["type"], "apdt": p["apdt"], "pbdt": p["pbdt"],
            "inventors": p["inventors"], "ipc4_list": p["ipc4_list"],
            "abstract": p["abstract"], "claim_1": p["claim_1"],
            "family_offices": p["family_offices"], "legal_status": p["legal_status"],
            "value_score": c.get("value_score"),
        })

    company_meta = {"company_key": company["company_key"], "company_name": company.get("company_name"),
                    "domain": innovation_result["domain"], "stage": innovation_result["stage"]}

    packs = {
        "patent_review": {
            "meta": company_meta,
            "task": "对以下Top-K核心专利逐件研判技术路线壁垒与创新高度（G-Eval 4档量表）"
                   if patent_cards else
                   "本企业暂无专利原始数据（no_patents=true）：不要伪造研判、不要提交空patents，"
                   "向调用方声明数据缺口并跳过本Skill",
            "no_patents": not patent_cards,
            "patents": patent_cards,
            "allowed_source_ids": core_ids,
            "innovation_output_summary": {"core_score": innovation_result["core_score"],
                                          "grade": innovation_result["grade"],
                                          "invention_share": pqi.get("dims", {}).get("invention_share", {}).get("value")},
        },
        "commercialization_fit": {
            "meta": company_meta,
            "task": "对比核心专利技术领域与中标公告正文，判断真实依赖/贸易型订单",
            "patents": [{k: c[k] for k in ("source_id", "pn", "title", "ipc4_list", "type")} for c in patent_cards],
            "bids": funnel_result["bids"],
            "bid_source_pending": funnel_result["bid_source_pending"],
            "allowed_source_ids": core_ids,
        },
        "anti_packaging_factcheck": {
            "meta": company_meta,
            "task": "把官网/新闻稿声明拆成原子事实，逐条比对专利与中标证据（FActScore范式）",
            "publicity_claims": [],   # Tavily 接入后填入
            "evidence": {"patents": [{k: c[k] for k in ("source_id", "pn", "title", "ipc4_list",
                                                        "abstract", "apdt", "pbdt")} for c in patent_cards],
                          "bids": funnel_result["bids"]},
            "allowed_source_ids": core_ids,
            "publicity_source_pending": True,
        },
        "bank_report": {
            "meta": company_meta,
            "task": "消费校验通过的硬指标、语义研判与红黄灯，生成《技术信用画像摘要》+《下户访谈问题清单》",
            "innovation_score": {"core_score": innovation_result["core_score"],
                                 "bonus_score": innovation_result["bonus_score"],
                                 "total_score": innovation_result["total_score"],
                                 "grade": innovation_result["grade"],
                                 "metrics": innovation_result["metrics"]},
            "pqi": {k: v for k, v in pqi.items() if k != "per_patent"},
            "risk_flags": risk_flags,
            "allowed_source_ids": core_ids,
            "note": "本包在四个上游Skill输出全部通过校验熔断后由score.py填充语义研判结果再投喂",
        },
    }
    paths = {}
    for name, body in packs.items():
        fp = out_dir / f"pack_{name}.json"
        fp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        paths[name] = str(fp)
    return paths
