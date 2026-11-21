#!/usr/bin/env python3
"""总分汇总层（Python功能实现层③收口）。

综合信用分 = 客观硬指标分（创新积分2.0核心分，权重55%）
           + 语义研判分（G-Eval加权档位归一到0-100，权重35%）
           - 风险扣分（红黄灯：稳定性+经济性，权重池10%）
           / 一票否决（触发则整案降为D级并标记veto）

一票否决（任一即触发）：
  V1 红灯数 ≥ 2
  V2 反包装核查 fact_score < 50%（对外声明过半无法被公开证据支撑）
  V3 核心专利全部处于无效/失效状态
"""
from __future__ import annotations

W_HARD = 0.55
W_SEMANTIC = 0.35
RISK_POOL = 0.10
RED_PENALTY = 15.0     # 每个红灯从风险池+总分中扣
YELLOW_PENALTY = 5.0

FACTSCORE_VETO = 0.5


def semantic_score_from_geval(geval_scores: dict, rubric_max: float = 4.0) -> float:
    """G-Eval 加权档位(0-4) → 百分制语义研判分。空集时返回 None（语义维度未参与）。"""
    if not geval_scores:
        return None
    avg = sum(geval_scores.values()) / len(geval_scores)
    return round(avg / rubric_max * 100.0, 2)


def fact_score_from_claims(claims) -> float:
    """FActScore：被公开证据支撑的原子声明占比（unverifiable 计入分母但不计入分子）。"""
    if not claims:
        return None
    supported = sum(1 for c in claims if getattr(c, "verdict", None) == "supported")
    return round(supported / len(claims), 4)


def compute_final(*, innovation_result: dict, semantic_score=None,
                  risk_flags=None, fact_score=None, all_core_invalid=False) -> dict:
    """汇总综合信用分与分级。"""
    risk_flags = risk_flags or []
    reds = [f for f in risk_flags if f.get("level") == "red"]
    yellows = [f for f in risk_flags if f.get("level") == "yellow"]

    hard = innovation_result["core_score"]

    # 风险扣分（稳定性：红黄灯；经济性：财务维度全缺或负增长由指标层体现）
    penalty = len(reds) * RED_PENALTY + len(yellows) * YELLOW_PENALTY
    penalty = min(penalty, 100.0 * RISK_POOL / 1.0 + len(reds) * RED_PENALTY)  # 池10%+红灯另扣

    veto_reasons = []
    if len(reds) >= 2:
        veto_reasons.append(f"V1 红灯数≥2（当前{len(reds)}）")
    if fact_score is not None and fact_score < FACTSCORE_VETO:
        veto_reasons.append(f"V2 反包装fact_score={fact_score:.0%}<{FACTSCORE_VETO:.0%}")
    if all_core_invalid:
        veto_reasons.append("V3 核心专利全部无效/失效")

    if semantic_score is None:
        # 语义维度未回填：硬指标口径（缺失维度权重再分配，仅作过渡展示）
        final = max(0.0, hard - penalty)
        coverage = ["hard"]
    else:
        final = hard * W_HARD + semantic_score * W_SEMANTIC
        final = max(0.0, final - penalty)
        coverage = ["hard", "semantic"]

    veto = bool(veto_reasons)
    grade = "D" if veto else _grade(final)
    return {
        "final_score": round(final, 2),
        "grade": grade,
        "weights_used": {"hard": W_HARD, "semantic": W_SEMANTIC,
                         "risk_pool": RISK_POOL,
                         "semantic_available": semantic_score is not None},
        "coverage": coverage,
        "risk_penalty": round(penalty, 2),
        "red_lights": [f["code"] for f in reds],
        "yellow_lights": [f["code"] for f in yellows],
        "fact_score": fact_score,
        "veto": veto,
        "veto_reasons": veto_reasons,
        "recommendation": _recommendation(grade, veto),
    }


def _grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def _recommendation(grade: str, veto: bool) -> str:
    if veto:
        return "触发一票否决：建议暂缓授信，先完成下户核查排除包装/造假风险"
    return {"A": "创新积分与技术信用优秀，建议作为优先授信白名单客户",
            "B": "技术与创新能力良好，可正常进入授信尽调流程",
            "C": "创新能力一般，建议关注行业基准线差距并压降授信敞口",
            "D": "创新能力薄弱或证据链不足，建议谨慎介入或要求补充材料"}[grade]
