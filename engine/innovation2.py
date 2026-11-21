#!/usr/bin/env python3
"""创新积分制2.0 打分引擎（核心）。

依据：《"创新积分制"工作指引（2.0版）（全国试行版）》2025年10月。
体系：9 个定量核心指标（100分）+ 3 个加分项（30分），总分 130 分。
  一级指标：创新投入(50)/创新产出(30)/创新发展(20) + 创新影响(加分30)
  权重按「8大技术领域 × 3成长阶段」差异化取值（附件2通用表 + 附件3领域表）。
  分级：A≥85 / B≥70 / C≥60 / D<60（按核心百分制口径，官方通行做法）。

本模块提供：
  1) 全部权重矩阵（9×8×3，self-test 校验每列和=100）
  2) 行业同期群分段归一化打分（p25/p50/p75/p90 分段线性）
  3) 缺失值动态调权（同组内按比例重分配；整组缺失时组间再分配）
  4) 加分项规则化打分（三项各封顶10分）
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------- 指标编码 ----------
RD_EXPENSE = "RD_EXPENSE"          # 创新投入：研发费用金额（万元）
RD_RATIO = "RD_RATIO"              # 创新投入：研发费用占营业收入比例（%）
RD_STAFF_RATIO = "RD_STAFF_RATIO"  # 创新投入：研发人员占比（%）
INVENTION_GRANTS = "INVENTION_GRANTS"      # 创新产出：发明专利授权量（件）
IP_PER_100_RD = "IP_PER_100_RD"    # 创新产出：每百人研发人员知识产权数量（件）
TECH_CONTRACT = "TECH_CONTRACT"     # 创新产出：企业技术合同成交额（万元）
MAIN_REVENUE = "MAIN_REVENUE"      # 创新发展：主营业务收入（万元）
REV_GROWTH = "REV_GROWTH"          # 创新发展：营业收入增长率（%）
ROE = "ROE"                        # 创新发展：净资产利润率（%）

CORE_INDICATORS = [RD_EXPENSE, RD_RATIO, RD_STAFF_RATIO,
                    INVENTION_GRANTS, IP_PER_100_RD, TECH_CONTRACT,
                    MAIN_REVENUE, REV_GROWTH, ROE]

CATEGORY_OF = {
    RD_EXPENSE: "创新投入", RD_RATIO: "创新投入", RD_STAFF_RATIO: "创新投入",
    INVENTION_GRANTS: "创新产出", IP_PER_100_RD: "创新产出", TECH_CONTRACT: "创新产出",
    MAIN_REVENUE: "创新发展", REV_GROWTH: "创新发展", ROE: "创新发展",
}
CATEGORY_ORDER = ["创新投入", "创新产出", "创新发展"]

DOMAINS = ["通用", "新一代信息技术", "生物医药", "航空航天", "新材料",
           "高技术服务", "新能源与节能", "资源与环境", "先进制造与自动化"]
STAGES = ["初创期", "成长期", "稳定期"]

# ---------- 权重矩阵（附件2通用 + 附件3八大领域，单位：%） ----------
_IND = CORE_INDICATORS
WEIGHTS = {
    "通用": {
        "初创期": dict(zip(_IND, [16, 15, 19, 10, 10, 10, 6, 10, 4])),
        "成长期": dict(zip(_IND, [15, 15, 20, 11, 10, 9, 7, 8, 5])),
        "稳定期": dict(zip(_IND, [14, 15, 21, 10, 11, 9, 8, 6, 6])),
    },
    "新一代信息技术": {
        "初创期": dict(zip(_IND, [19, 16, 15, 8, 10, 12, 6, 10, 4])),
        "成长期": dict(zip(_IND, [17, 16, 17, 11, 11, 8, 8, 7, 5])),
        "稳定期": dict(zip(_IND, [15, 16, 19, 10, 9, 11, 9, 5, 6])),
    },
    "生物医药": {
        "初创期": dict(zip(_IND, [18, 12, 20, 8, 14, 8, 4, 10, 6])),
        "成长期": dict(zip(_IND, [16, 16, 18, 12, 10, 8, 6, 8, 6])),
        "稳定期": dict(zip(_IND, [14, 16, 20, 10, 10, 10, 10, 6, 4])),
    },
    "航空航天": {
        "初创期": dict(zip(_IND, [20, 14, 16, 8, 11, 11, 10, 6, 4])),
        "成长期": dict(zip(_IND, [17, 17, 16, 13, 8, 9, 8, 8, 4])),
        "稳定期": dict(zip(_IND, [15, 14, 21, 10, 10, 10, 6, 6, 8])),
    },
    "新材料": {
        "初创期": dict(zip(_IND, [18, 14, 18, 10, 12, 8, 5, 10, 5])),
        "成长期": dict(zip(_IND, [16, 18, 16, 12, 10, 8, 7, 8, 5])),
        "稳定期": dict(zip(_IND, [16, 14, 20, 10, 8, 12, 9, 5, 6])),
    },
    "高技术服务": {
        "初创期": dict(zip(_IND, [18, 15, 17, 7, 11, 12, 6, 9, 5])),
        "成长期": dict(zip(_IND, [16, 15, 19, 12, 10, 8, 7, 8, 5])),
        "稳定期": dict(zip(_IND, [14, 15, 21, 11, 9, 10, 8, 5, 7])),
    },
    "新能源与节能": {
        "初创期": dict(zip(_IND, [19, 14, 17, 8, 14, 8, 6, 9, 5])),
        "成长期": dict(zip(_IND, [18, 16, 16, 12, 10, 8, 7, 10, 3])),
        "稳定期": dict(zip(_IND, [15, 15, 20, 8, 10, 12, 8, 8, 4])),
    },
    "资源与环境": {
        "初创期": dict(zip(_IND, [18, 12, 20, 8, 12, 10, 5, 10, 5])),
        "成长期": dict(zip(_IND, [15, 20, 15, 10, 10, 10, 7, 8, 5])),
        "稳定期": dict(zip(_IND, [15, 15, 20, 12, 8, 10, 9, 4, 7])),
    },
    "先进制造与自动化": {
        "初创期": dict(zip(_IND, [16, 14, 20, 9, 11, 10, 8, 7, 5])),
        "成长期": dict(zip(_IND, [18, 15, 17, 11, 9, 10, 9, 8, 3])),
        "稳定期": dict(zip(_IND, [16, 13, 21, 10, 8, 12, 10, 5, 5])),
    },
}

# ---------- 加分项规则（附件2细则） ----------
def bonus_science_program(*, provincial=0, municipal=0, standards=0) -> float:
    """科技计划项目/奖励/标准。省级及以上每项4分、地市级每项2分、标准制定每项5分，封顶10分。"""
    return min(10.0, provincial * 4 + municipal * 2 + standards * 5)


def bonus_funding(amount_wan: float) -> float:
    """融资金额（万元）。≥2000:10；[1000,2000):7；[500,1000):4；(0,500):1；≤0:0。"""
    if amount_wan is None or amount_wan <= 0:
        return 0.0
    if amount_wan >= 2000:
        return 10.0
    if amount_wan >= 1000:
        return 7.0
    if amount_wan >= 500:
        return 4.0
    return 1.0


def bonus_rank_qual(*, international_rank=False, national_rank=False,
                    provincial_rank=False, tech_qualification=False) -> float:
    """行业排名与科技资质。国际榜单10、国家榜单5、省级榜单3、有科技资质5，封顶10分。"""
    return min(10.0, international_rank * 10 + national_rank * 5
               + provincial_rank * 3 + tech_qualification * 5)


# ---------- 行业同期群分段归一化 ----------
# 基准线 {indicator: {p25,p50,p75,p90}}：成长期通用占位基准，可被 benchmarks.json 覆盖。
# 说明：官方口径为全国20余万家积分企业样本分位数；此处给出可运行的占位基准，
# 接入真实同期群数据后按行业×阶段重标定（load_benchmarks 支持外部文件）。
BASE_BENCH = {
    RD_EXPENSE:       {"p25": 300,  "p50": 1000, "p75": 4000,  "p90": 15000},
    RD_RATIO:         {"p25": 3.0,  "p50": 6.0,  "p75": 12.0,  "p90": 20.0},
    RD_STAFF_RATIO:   {"p25": 10.0, "p50": 20.0, "p75": 35.0,  "p90": 50.0},
    INVENTION_GRANTS: {"p25": 3,    "p50": 15,   "p75": 60,    "p90": 200},
    IP_PER_100_RD:    {"p25": 5,    "p50": 15,   "p75": 40,    "p90": 100},
    TECH_CONTRACT:    {"p25": 100,  "p50": 500,  "p75": 2000,  "p90": 8000},
    MAIN_REVENUE:     {"p25": 1000, "p50": 5000, "p75": 20000, "p90": 100000},
    REV_GROWTH:       {"p25": -5.0, "p50": 10.0, "p75": 30.0,  "p90": 60.0},
    ROE:              {"p25": 0.0,  "p50": 6.0,  "p75": 15.0,  "p90": 25.0},
}
_STAGE_BENCH_SCALE = {"初创期": 0.4, "成长期": 1.0, "稳定期": 2.5}
_MONEY_INDICATORS = {RD_EXPENSE, TECH_CONTRACT, MAIN_REVENUE}

BENCH_FILE = Path(__file__).resolve().parent / "benchmarks.json"


def load_benchmarks() -> dict:
    """占位基准 → benchmarks.json 覆盖（行业同期群标定后替换）。"""
    bench = {k: dict(v) for k, v in BASE_BENCH.items()}
    if BENCH_FILE.exists():
        over = json.loads(BENCH_FILE.read_text(encoding="utf-8"))
        for k, v in over.get("benchmarks", {}).items():
            if k in bench:
                bench[k].update(v)
        for dom, per_dom in over.get("by_domain", {}).items():
            for st, per_stage in per_dom.items():
                for ind, vals in per_stage.items():
                    bench[(dom, st, ind)] = dict(vals)
    return bench


def _bench_for(bench, domain, stage, indicator) -> dict:
    key = (domain, stage, indicator)
    if key in bench:
        return bench[key]
    b = dict(bench[indicator])
    scale = _STAGE_BENCH_SCALE.get(stage, 1.0)
    if indicator in _MONEY_INDICATORS:
        b = {k: v * scale for k, v in b.items()}
    return b


def score_indicator(value, bench: dict) -> float:
    """分段线性归一化：≤p25 → 0~40；p25~p50 → 40~70；p50~p75 → 70~90；p75~p90 → 90~100；≥p90 → 100。"""
    p25, p50, p75, p90 = (bench["p25"], bench["p50"], bench["p75"], bench["p90"])
    if value <= p25:
        if p25 == 0:
            return 0.0 if value < 0 else (20.0 if value == 0 else 40.0)
        return max(0.0, 40.0 * (0.5 + 0.5 * value / p25)) if value >= 0 else 0.0
    if value <= p50:
        return 40.0 + 30.0 * (value - p25) / (p50 - p25)
    if value <= p75:
        return 70.0 + 20.0 * (value - p50) / (p75 - p50)
    if value <= p90:
        return 90.0 + 10.0 * (value - p75) / (p90 - p75)
    return 100.0


def stage_from_years(founded_years: float) -> str:
    """初创期0-5年（不含5）、成长期5-10年（不含10）、稳定期10年以上。"""
    if founded_years is None:
        return "成长期"
    if founded_years < 5:
        return "初创期"
    if founded_years < 10:
        return "成长期"
    return "稳定期"


def _reweight_missing(weights: dict, present: set) -> dict:
    """缺失值动态调权：同组内按比例重分配；整组缺失则组间按比例重分配。"""
    present_w = {k: v for k, v in weights.items() if k in present}
    if not present_w:
        return {}
    out = {}
    groups = {}
    for ind in present_w:
        groups.setdefault(CATEGORY_OF[ind], {})[ind] = weights[ind]
    for cat, members in groups.items():
        cat_total = sum(weights[i] for i in CORE_INDICATORS if CATEGORY_OF[i] == cat)
        have = sum(members.values())
        if have > 0:
            for ind, w in members.items():
                out[ind] = w * (cat_total / have)
    total = sum(out.values())
    if total <= 0:
        return out
    return {k: v * 100.0 / total for k, v in out.items()}


def compute_innovation(inputs: dict, *, domain: str = "通用", stage: str = "成长期",
                       bonus: dict | None = None, bench: dict | None = None) -> dict:
    """计算创新积分2.0。

    inputs: {指标编码: 数值或 None}，None 表示该指标数据缺失。
    bonus:  {"science_program": {provincial,municipal,standards},
            "funding_amount_wan": 数值,
            "rank_qual": {international_rank,national_rank,provincial_rank,tech_qualification}}
    返回：核心得分（0-100，缺失调权后按有效权重合计）、加分（0-30）、总分、分级、逐指标明细。
    """
    if domain not in WEIGHTS:
        raise ValueError(f"未知技术领域: {domain}，可选: {DOMAINS}")
    if stage not in STAGES:
        raise ValueError(f"未知成长阶段: {stage}，可选: {STAGES}")
    bench = bench or load_benchmarks()
    weights = WEIGHTS[domain][stage]

    present = {k for k in CORE_INDICATORS if inputs.get(k) is not None}
    eff_weights = _reweight_missing(weights, present)

    metrics = []
    for ind in CORE_INDICATORS:
        raw = inputs.get(ind)
        b = _bench_for(bench, domain, stage, ind)
        if ind in eff_weights:
            s = score_indicator(float(raw), b)
            w = eff_weights[ind]
            metrics.append({"code": ind, "category": CATEGORY_OF[ind], "raw": raw,
                            "missing": False, "bench": b, "score": round(s, 2),
                            "weight": round(w, 4), "weighted": round(s * w / 100.0, 2)})
        else:
            metrics.append({"code": ind, "category": CATEGORY_OF[ind], "raw": None,
                            "missing": True, "bench": b, "score": None,
                            "weight": 0.0, "weighted": 0.0,
                            "note": "数据缺失，权重已动态再分配"})

    core = round(sum(m["weighted"] for m in metrics), 2)

    bonus = bonus or {}
    bonus_detail = {
        "science_program": {"score": bonus_science_program(
            **{k: bonus.get("science_program", {}).get(k, 0) for k in
               ("provincial", "municipal", "standards")})},
        "funding": {"score": bonus_funding(bonus.get("funding_amount_wan", 0))},
        "rank_qual": {"score": bonus_rank_qual(
            **{k: bool(bonus.get("rank_qual", {}).get(k, False)) for k in
               ("international_rank", "national_rank", "provincial_rank", "tech_qualification")})},
    }
    bonus_total = round(sum(v["score"] for v in bonus_detail.values()), 2)
    grade = grade_of(core)
    return {
        "engine": "innovation2.0", "domain": domain, "stage": stage,
        "core_score": core, "bonus_detail": bonus_detail, "bonus_score": bonus_total,
        "total_score": round(core + bonus_total, 2), "grade": grade,
        "metrics": metrics,
        "missing_reweighted": sorted(set(CORE_INDICATORS) - present),
    }


def grade_of(core_score: float) -> str:
    """A≥85 / B≥70 / C≥60 / D<60（核心百分制口径）。"""
    if core_score >= 85:
        return "A"
    if core_score >= 70:
        return "B"
    if core_score >= 60:
        return "C"
    return "D"
