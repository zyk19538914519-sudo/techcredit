#!/usr/bin/env python3
"""引擎自检（可反复执行，零外部依赖，全部通过退出码0）。

覆盖：
  T1 权重矩阵：9领域×3阶段每列和=100%
  T2 分段归一化：单调不减、上下界、p90以上封顶
  T3 缺失调权：组内再分配保一级结构、整组缺失组间再分配、全缺失不崩
  T4 加分项：三档封顶、融资分档边界
  T5 分级阈值：85/70/60边界
  T6 PQI：发明/实用新型双信号识别、同族归一化+winsorize、IPC广度、授权周期公式、留存率
  T7 排雷规则：突击申请/发明人断层触发、黄灯IPC集中
  T8 漏斗：价值分排序Top-K、Source_ID与原始库一致
  T9 校验熔断：Pydantic拒非法字段、伪造Source_ID拦截、置信度归一、熔断器3次熔断
  T10 总分汇总：G-Eval加权→语义分、红黄灯扣分、一票否决降D
  T11 授权发明口径：A公开不计授权、组合全不明→缺失调权、实用新型不计入
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "data_layer"))

import innovation2 as I
import patent_quality as PQ
import funnel as FN
import schemas as SC
import validate as VD
import score as SCOR

PASS, FAIL = "OK ", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL} {name}{(' | ' + detail) if detail and not cond else ''}")


def t1_weights():
    print("[T1] 权重矩阵")
    for dom in I.WEIGHTS:
        for st in I.STAGES:
            w = I.WEIGHTS[dom][st]
            check(f"{dom}/{st} 权重和=100", abs(sum(w.values()) - 100) < 1e-9,
                  f"实际={sum(w.values())}")
    check("指标数=9", len(I.CORE_INDICATORS) == 9)


def t2_score_indicator():
    print("[T2] 分段归一化")
    b = {"p25": 10, "p50": 20, "p75": 40, "p90": 80}
    seq = [I.score_indicator(v, b) for v in [-5, 0, 5, 10, 15, 20, 30, 40, 60, 80, 999]]
    check("单调不减", all(x <= y + 1e-9 for x, y in zip(seq, seq[1:])), str(seq))
    check("p90封顶100", seq[-1] == 100.0)
    check("负值不给分", seq[0] == 0.0)
    check("p50中段=70", abs(I.score_indicator(20, b) - 70) < 1e-9)


def t3_missing_reweight():
    print("[T3] 缺失调权")
    w = I.WEIGHTS["通用"]["成长期"]
    eff = I._reweight_missing(w, {I.RD_EXPENSE, I.RD_RATIO, I.RD_STAFF_RATIO})
    check("仅投入组在场→组内权重保持原比例并归一到100",
          abs(sum(eff.values()) - 100) < 1e-9 and
          abs(eff[I.RD_EXPENSE] - 30) < 1e-9 and abs(eff[I.RD_STAFF_RATIO] - 40) < 1e-9,
          str(eff))
    eff2 = I._reweight_missing(w, {I.RD_STAFF_RATIO, I.INVENTION_GRANTS})
    cat_in = sum(v for k, v in eff2.items() if I.CATEGORY_OF[k] == "创新投入")
    cat_out = sum(v for k, v in eff2.items() if I.CATEGORY_OF[k] == "创新产出")
    check("一级结构比例保持(投入:产出=50:30)",
          abs(cat_in / cat_out - 50 / 30) < 1e-9, f"{cat_in}/{cat_out}")
    check("调权后总和=100", abs(sum(eff2.values()) - 100) < 1e-9)
    check("全缺失不崩", I._reweight_missing(w, set()) == {})
    r = I.compute_innovation({I.RD_EXPENSE: None, I.RD_RATIO: None, I.RD_STAFF_RATIO: None,
                              I.INVENTION_GRANTS: 10, I.IP_PER_100_RD: None,
                              I.TECH_CONTRACT: None, I.MAIN_REVENUE: None,
                              I.REV_GROWTH: None, I.ROE: None}, stage="成长期")
    check("缺失指标在明细中标记", all(m["missing"] for m in r["metrics"]
                                   if m["code"] != I.INVENTION_GRANTS))
    check("仅1项时权重集中该项", abs(next(m["weight"] for m in r["metrics"]
                                      if m["code"] == I.INVENTION_GRANTS) - 100) < 1e-6)


def t4_bonus():
    print("[T4] 加分项")
    check("标准封顶10", I.bonus_science_program(provincial=3) == 10)
    check("省级4分/地市2分/标准5分",
          I.bonus_science_program(provincial=1, municipal=1, standards=1) == 10
          and I.bonus_science_program(municipal=1) == 2)
    check("融资分档", I.bonus_funding(2000) == 10 and I.bonus_funding(1999) == 7
          and I.bonus_funding(1000) == 7 and I.bonus_funding(500) == 4
          and I.bonus_funding(1) == 1 and I.bonus_funding(0) == 0)
    check("排名/资质封顶10", I.bonus_rank_qual(international_rank=True,
                                             tech_qualification=True) == 10)


def t5_grade():
    print("[T5] 分级阈值")
    check("A边界85", I.grade_of(85) == "A" and I.grade_of(84.99) == "B")
    check("B边界70", I.grade_of(70) == "B" and I.grade_of(69.99) == "C")
    check("C边界60", I.grade_of(60) == "C" and I.grade_of(59.99) == "D")


def _mk_patent(sid, pn="CN100000001B", apno="CN20201000001.1", apdt="20200110",
              pbdt="20211201", inventors=("张三", "李四"), ipc=None, family=None,
              legal=None):
    return {"source_id": sid, "pn": pn, "apno": apno, "title": "测试专利",
            "type": PQ.patent_type({"pn": pn, "apno": apno}), "apdt": apdt, "pbdt": pbdt,
            "grant_days": (date(int(pbdt[:4]), int(pbdt[4:6]), int(pbdt[6:8]))
                           - date(int(apdt[:4]), int(apdt[4:6]), int(apdt[6:8]))).days,
            "inventors": list(inventors), "ipc4_list": ipc or [], "family_offices": family or [],
            "legal_status": legal, "abstract": None, "claim_1": None}

def t6_pqi():
    print("[T6] PQI")
    check("实用新型识别(U尾缀)", PQ.patent_type({"pn": "CN206470136U", "apno": "CN201621406583.9"}) == "utility")
    check("发明识别(申请号类型位=1)", PQ.patent_type({"pn": "CN111111111", "apno": "CN20201100000.3"}) == "invention")
    pats = [_mk_patent("PAT-ZHY-a1", "CN100000001B", legal="有效",
                        ipc=["H01M10/20", "H02J7/00"], family=["CN", "US"]),
            _mk_patent("PAT-ZHY-a2", "CN200000002U", apdt="20200310", pbdt="20210101",
                       legal="有效", ipc=["H01M10/20"]),
            _mk_patent("PAT-ZHY-a3", "CN200000003U", apdt="20200510", pbdt="20210201",
                       legal="有效")]
    pqi = PQ.compute_pqi(pats, today=date(2021, 6, 1))
    d = pqi["dims"]
    check("发明占比=1/3", abs(d["invention_share"]["value"] - 1 / 3) < 1e-3)
    check("同族归一化（有数据1.0，缺失为None）",
          pqi["per_patent"][0]["family_index"] == 1.0
          and pqi["per_patent"][1]["family_index"] is None)
    check("IPC广度归一化（2/1/0 → 1.0/0.5/0）",
          pqi["per_patent"][0]["scope_index"] == 1.0
          and abs(pqi["per_patent"][1]["scope_index"] - 0.5) < 1e-9)
    check("授权周期公式（1-Δt/MaxΔt）",
          abs(pqi["per_patent"][0]["grant_index"] - 0.0) < 1e-9, str(pqi["per_patent"]))
    check("组合分=维度动态调权加权和", pqi["composite"] is not None)
    check("缺失维度降权后有效权重和=1",
          abs(sum(x["effective_weight"] for x in d.values()) - 1) < 1e-9)
    pqi2 = PQ.compute_pqi(pats, today=date(2026, 11, 21))
    check("留存率窗口外标不可算（非机械归0）",
          pqi2["dims"]["inventor_retention"]["available"] is False
          and pqi2["dims"]["inventor_retention"]["value"] is None
          and pqi2["recent_applications_24m"] == 0
          and "数据窗口" in (pqi2["dims"]["inventor_retention"]["note"] or ""))
    check("窗口外留存维度有效权重=0且其余调权和=1",
          pqi2["dims"]["inventor_retention"]["effective_weight"] == 0.0
          and abs(sum(x["effective_weight"] for x in pqi2["dims"].values()) - 1) < 1e-9)
    same_day = [_mk_patent("PAT-ZHY-s1", apdt="20200110", pbdt="20200110"),
                _mk_patent("PAT-ZHY-s2", apdt="20200210", pbdt="20200210")]
    check("同年同日授权不除零", PQ.compute_pqi(same_day, today=date(2020, 12, 1))
          ["per_patent"][0]["grant_index"] == 1.0)
    # 授权周期口径：A文献pbdt=公开日不得当授权日；显式grant_date才计
    a_rec = PQ.normalize_payload({"pn": "CN114000001A", "apno": "CN20211000001.1",
                                  "apdt": "20210110", "pbdt": "20210401",
                                  "inventor": "张三"}, "PAT-ZHY-a")
    check("A文献无grant_date→授权周期缺失", a_rec["grant_days"] is None
          and a_rec["type"] == "invention_pending")
    a_rec2 = PQ.normalize_payload({"pn": "CN114000001A", "apdt": "20210110",
                                   "pbdt": "20210401", "grant_date": "20240531",
                                   "inventor": "张三"}, "PAT-ZHY-a2")
    check("A文献有grant_date→按授权日计周期", a_rec2["grant_days"] == (date(2024, 5, 31) - date(2021, 1, 10)).days)
    u_rec = PQ.normalize_payload({"pn": "CN216000001U", "apno": "CN20212000001.1",
                                  "apdt": "20210110", "pbdt": "20210601",
                                  "inventor": "张三"}, "PAT-ZHY-u")
    check("U文献pbdt即授权日且类型=实用新型", u_rec["type"] == "utility"
          and u_rec["grant_days"] == (date(2021, 6, 1) - date(2021, 1, 10)).days)
    # 法律状态折减：失效×0.35、审中×0.85、缺失不惩罚
    base = _mk_patent("PAT-ZHY-l0", family=["CN", "US"], ipc=["H01M10/20", "H02J7/00"])
    dead = dict(base, source_id="PAT-ZHY-l1", legal_status="失效")
    pend = dict(base, source_id="PAT-ZHY-l2", legal_status="审中")
    pqi_l = PQ.compute_pqi([base, dead, pend], today=date(2024, 1, 1))
    vs = {x["source_id"]: x["value_score"] for x in pqi_l["per_patent"]}
    check("失效件价值分折减0.35", abs(vs["PAT-ZHY-l1"] - round(vs["PAT-ZHY-l0"] * 0.35, 4)) < 1e-3, str(vs))
    check("审中件价值分折减0.85", abs(vs["PAT-ZHY-l2"] - round(vs["PAT-ZHY-l0"] * 0.85, 4)) < 1e-3, str(vs))
    check("法律状态缺失不惩罚", PQ._legal_factor({"legal_status": None}) == 1.0
          and PQ._legal_factor({}) == 1.0)
    check("per_patent回传legal_status供语义层交叉",
          all("legal_status" in x and "legal_factor" in x for x in pqi_l["per_patent"]))
    # 外来日期格式容错（第三轮修复固化）：ISO/缺零/中文/带时间均可解析
    iso_rec = PQ.normalize_payload({"pn": "CN115000001B", "apdt": "2023-01-10",
                                    "pbdt": "2024.5.31", "inventor": "张三"}, "PAT-ZHY-iso")
    check("ISO/缺零日期容错解析", iso_rec["grant_days"] == (date(2024, 5, 31) - date(2023, 1, 10)).days)
    cn_rec = PQ.normalize_payload({"pn": "CN115000001B", "apdt": "2023年01月10日",
                                   "pbdt": "2024/05/31 00:00:00", "inventor": "张三"}, "PAT-ZHY-cn")
    check("中文/斜杠/带时间日期容错", cn_rec["grant_days"] == (date(2024, 5, 31) - date(2023, 1, 10)).days)
    bad_rec = PQ.normalize_payload({"pn": "CN115000001B", "apdt": "202301",
                                    "pbdt": "20240531", "inventor": "张三"}, "PAT-ZHY-baddt")
    check("歧义日期宁缺不猜", bad_rec["grant_days"] is None)
    # 法律状态外来值域容错（第三轮修复固化）
    check("外来失效表述折减0.35", PQ._legal_factor({"legal_status": "驳回"}) == 0.35
          and PQ._legal_factor({"legal_status": "Revoked"}) == 0.35
          and PQ._legal_factor({"legal_status": "视撤"}) == 0.35)
    check("外来审中表述折减0.85", PQ._legal_factor({"legal_status": "实质审查"}) == 0.85
          and PQ._legal_factor({"legal_status": "Pending"}) == 0.85)
    check("外来有效/未识别表述不惩罚", PQ._legal_factor({"legal_status": "授权"}) == 1.0
          and PQ._legal_factor({"legal_status": "Active"}) == 1.0
          and PQ._legal_factor({"legal_status": "未知状态"}) == 1.0)


def t7_risk():
    print("[T7] 排雷规则")
    today = date(2026, 11, 21)
    # 场景A：突击申请（近12月4件 vs 前两年2件：4 ≥ 3×1，占比4/6>50%）
    d1 = date(2026, 10, 1); d2 = date(2026, 9, 1); d3 = date(2026, 8, 1); d4 = date(2026, 7, 1)
    d5 = date(2024, 10, 1); d6 = date(2024, 11, 1)
    burst = [_mk_patent(f"PAT-ZHY-b{i}", apdt=d.strftime("%Y%m%d"),
                        pbdt=d.strftime("%Y%m%d")) for i, d in enumerate([d1, d2, d3, d4])]
    history = [_mk_patent("PAT-ZHY-h1", apdt="20241001", pbdt="20250101"),
               _mk_patent("PAT-ZHY-h2", apdt="20241101", pbdt="20250201")]
    pqi = PQ.compute_pqi(burst + history, today=today)
    flags = FN.evaluate_risks(burst + history, pqi, today=today)
    codes = [f["code"] for f in flags]
    check("突击申请红灯触发", "R1_BURST" in codes, str(codes))
    check("核心发明人活跃时不误报断层", "R2_INVENTOR_GAP" not in codes, str(codes))
    check("近24月有申请不触发窗口黄灯", "Y4_STALE_WINDOW" not in codes, str(codes))
    check("红黄灯级别合法", all(f["level"] in ("red", "yellow") for f in flags))
    check("每个灯绑定真实Source_ID", all(isinstance(f["source_ids"], list) for f in flags))

    # 场景B：全部申请在24个月前且发明人不同→R2断层；同期群仅3件仍可计算
    stale = [_mk_patent("PAT-ZHY-g1", apdt="20200110", pbdt="20201201", inventors=("赵六",)),
             _mk_patent("PAT-ZHY-g2", apdt="20200310", pbdt="20201201", inventors=("钱七",)),
             _mk_patent("PAT-ZHY-g3", apdt="20200510", pbdt="20210201", inventors=("孙八",))]
    pqi_b = PQ.compute_pqi(stale, today=today)
    flags_b = FN.evaluate_risks(stale, pqi_b, today=today)
    codes_b = [f["code"] for f in flags_b]
    check("发明人断层红灯触发", "R2_INVENTOR_GAP" in codes_b, str(codes_b))
    check("数据窗口黄灯触发", "Y4_STALE_WINDOW" in codes_b, str(codes_b))

    # 场景C：单一IPC过度集中→黄灯
    conc = [_mk_patent(f"PAT-ZHY-c{i}", ipc=["H01M10/20"], apdt="20220110", pbdt="20221201")
            for i in range(4)]
    flags2 = FN.evaluate_risks(conc, PQ.compute_pqi(conc, today=today), today=today)
    check("IPC过度集中黄灯", "Y1_IPC_CONCENTRATION" in [f["code"] for f in flags2])

    # 场景D：外来失效表述（驳回/Revoked）→R3必须与价值折减同口径识别（防分叉）
    mix = [_mk_patent("PAT-ZHY-m1", legal="有效"),
           _mk_patent("PAT-ZHY-m2", legal="驳回"),
           _mk_patent("PAT-ZHY-m3", legal="Revoked"),
           _mk_patent("PAT-ZHY-m4", legal="有效")]
    flags_d = FN.evaluate_risks(mix, PQ.compute_pqi(mix, today=today), today=today)
    check("外来失效表述触发R3（与价值折减同口径）",
          "R3_INVALID_HEAVY" in [f["code"] for f in flags_d],
          str([f["code"] for f in flags_d]))

    # 场景E：ISO/中文日期不哑灯——风险灯日期解析须与引擎同容错
    def _iso_pat(sid, apdt, inv):
        return {"source_id": sid, "pn": "CN114000009A", "title": "t", "type": "invention_pending",
                "apdt": apdt, "pbdt": apdt, "grant_days": None, "inventors": [inv],
                "ipc4_list": [], "family_offices": [], "legal_status": None,
                "abstract": None, "claim_1": None}
    iso_pats = [_iso_pat("PAT-ZHY-i1", "2020-01-10", "赵六"),
                _iso_pat("PAT-ZHY-i2", "2020年03月10日", "钱七"),
                _iso_pat("PAT-ZHY-i3", "2020/05/10", "孙八")]
    flags_i = FN.evaluate_risks(iso_pats, PQ.compute_pqi(iso_pats, today=today), today=today)
    check("ISO/中文日期下Y4窗口黄灯不哑",
          "Y4_STALE_WINDOW" in [f["code"] for f in flags_i],
          str([f["code"] for f in flags_i]))


def t8_funnel():
    print("[T8] 漏斗")
    pats = [_mk_patent(f"PAT-ZHY-f{i}", pn=f"CN10000000{i}B",
                       ipc=(["H01M10/20", "H02J7/00"] if i % 2 else ["H01M10/20"]),
                       family=(["CN", "US"] if i % 2 else []))
            for i in range(8)]
    pqi = PQ.compute_pqi(pats, today=date(2024, 1, 1))
    sel = FN.funnel_select(pats, pqi, top_patents=5)
    check("Top-K数量正确", len(sel["core_patents"]) == 5)
    vs = [c.get("value_score") or 0 for c in sel["core_patents"]]
    check("价值分降序", all(x >= y for x, y in zip(vs, vs[1:])), str(vs))
    check("中标源占位显式声明", sel["bid_source_pending"] is True and sel["bids"] == [])
    all_ids = {c["source_id"] for c in sel["core_patents"]}
    check("核心专利ID来自原始组合", all_ids <= {p["source_id"] for p in pats})


def t9_validation():
    print("[T9] 校验熔断")
    allowed = {"PAT-ZHY-a1", "PAT-ZHY-a2", "PAT-ZHY-a3", "BID-TYC-x1"}
    good = {"skill": "patent_review", "patents": [
        {"source_id": "PAT-ZHY-a1", "rubric_level": 4, "conf_1": 0.1, "conf_2": 0.2,
         "conf_3": 0.3, "conf_4": 0.4, "barrier_score": 90, "innovation_score": 85,
         "claim_quality_score": 80,
         "rationale": "独立权利要求技术特征链完整，多法域同族布局，检索未见近邻现有技术，"
                      "创新高度与壁垒均较高。",
         "evidence_quote": "独权1", "red_flags": []}],
        "portfolio_rationale": "整体技术路线聚焦电池组结构设计，核心专利质量较高"}
    v = VD.validate_output("patent_review", good, allowed)
    check("合法输出通过", v["ok"], str(v["errors"]))
    check("G-Eval加权分在0-4", v["ok"] and 0 <= v["geval_scores"]["PAT-ZHY-a1"] <= 4)

    forged = json.loads(json.dumps(good))
    forged["patents"][0]["source_id"] = "PAT-ZHY-forged999"
    v2 = VD.validate_output("patent_review", forged, allowed)
    check("伪造Source_ID拦截", not v2["ok"] and any("伪造" in e for e in v2["errors"]),
          str(v2["errors"]))

    junk = json.loads(json.dumps(good))
    junk["patents"][0]["extra_field"] = 1
    v3 = VD.validate_output("patent_review", junk, allowed)
    check("多余字段被Pydantic拒绝", not v3["ok"], str(v3["errors"]))

    bad_range = json.loads(json.dumps(good))
    bad_range["patents"][0]["rubric_level"] = 9
    v4 = VD.validate_output("patent_review", bad_range, allowed)
    check("量程外数值被拒", not v4["ok"])

    unnormalized = json.loads(json.dumps(good))
    unnormalized["patents"][0].update({"conf_1": 0.2, "conf_2": 0.4, "conf_3": 0.4, "conf_4": 0.8})
    v5 = VD.validate_output("patent_review", unnormalized, allowed)
    check("置信度和≠1被归一加权后仍通过", v5["ok"] and v5["geval_scores"]["PAT-ZHY-a1"] > 2,
          str(v5["errors"]))

    br = VD.CircuitBreaker(max_retries=2)
    broke = False
    for _ in range(3):
        try:
            br.attempt("patent_review", forged, allowed)
        except VD.CircuitBroken:
            broke = True
            break
    check("熔断器连续失败触发CircuitBroken", broke)

    factcheck = {"skill": "anti_packaging_factcheck", "claims": [
        {"claim_id": "c1", "atomic_fact": "公司拥有电池组连接片核心技术", "source_ids": ["PAT-ZHY-a1"],
         "verdict": "supported", "evidence_quote": "独权1记载"},
        {"claim_id": "c2", "atomic_fact": "公司产品市占率全球第一", "source_ids": [],
         "verdict": "unverifiable"}],
        "overall_note": "官网声明2条中1条可由专利支撑"}
    v6 = VD.validate_output("anti_packaging_factcheck", factcheck, allowed)
    check("FActScore输出契约通过", v6["ok"], str(v6["errors"]))


def t10_final():
    print("[T10] 总分汇总")
    inn = I.compute_innovation({k: None for k in I.CORE_INDICATORS}, stage="成长期")
    inn["core_score"] = 80.0
    s = SCOR.semantic_score_from_geval({"a": 3.2, "b": 2.8})
    check("G-Eval档位→百分制语义分", s == round((3.0 / 4) * 100, 2), str(s))
    f = SCOR.compute_final(innovation_result=inn, semantic_score=s, risk_flags=[
        {"code": "R1_BURST", "level": "red"}, {"code": "Y1", "level": "yellow"}])
    check("红黄灯扣分生效", f["final_score"] < 80 * SCOR.W_HARD + s * SCOR.W_SEMANTIC)
    check("单红灯不否决", f["veto"] is False)
    f2 = SCOR.compute_final(innovation_result=inn, semantic_score=s, risk_flags=[
        {"code": "R1", "level": "red"}, {"code": "R2", "level": "red"}])
    check("双红灯一票否决降D", f2["veto"] is True and f2["grade"] == "D")
    f3 = SCOR.compute_final(innovation_result=inn, semantic_score=s, risk_flags=[],
                            fact_score=0.4)
    check("fact_score<50%触发否决", f3["veto"] is True)
    fs = SCOR.fact_score_from_claims(None)
    check("无声明时fact_score为None", fs is None)


def t11_grants_caliber():
    print("[T11] 授权发明口径（创新积分2.0 INVENTION_GRANTS=授权量，非申请量）")
    import run_engine as RE
    a_pats = [dict(_mk_patent("PAT-ZHY-ga1", pn="CN114000001A"), grant_days=None),
              dict(_mk_patent("PAT-ZHY-ga2", pn="CN114000002A"), grant_days=None)]
    check("A公开无授权日→指标缺失调权", RE.derive_patent_inputs(a_pats) == {})
    b_pats = [_mk_patent("PAT-ZHY-gb1", pn="CN114000003B"),
              dict(_mk_patent("PAT-ZHY-gb2", pn="CN114000004A"), grant_days=900)]
    check("B授权件+显式grant_date的A件各计1（共2）",
          RE.derive_patent_inputs(b_pats) == {I.INVENTION_GRANTS: 2.0})
    mixed = [dict(_mk_patent("PAT-ZHY-gm1", pn="CN114000005A"), grant_days=None),
             _mk_patent("PAT-ZHY-gm2", pn="CN114000006B")]
    check("A无授权日+B授权→只计B共1件",
          RE.derive_patent_inputs(mixed) == {I.INVENTION_GRANTS: 1.0})
    u_pats = [_mk_patent("PAT-ZHY-gu1", pn="CN216000005U")]
    check("实用新型不计授权发明", RE.derive_patent_inputs(u_pats) == {I.INVENTION_GRANTS: 0.0})
    check("空组合不虚报", RE.derive_patent_inputs([]) == {})


def main():
    for t in (t1_weights, t2_score_indicator, t3_missing_reweight, t4_bonus, t5_grade,
              t6_pqi, t7_risk, t8_funnel, t9_validation, t10_final, t11_grants_caliber):
        t()
    n_fail = sum(1 for _, c, _ in results if not c)
    print(f"\n自检完成：{len(results)}项，通过{len(results) - n_fail}项，失败{n_fail}项")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
