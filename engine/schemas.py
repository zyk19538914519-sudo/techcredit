#!/usr/bin/env python3
"""Agent Skill 输出契约（Pydantic v2 强校验）。

四个 Skill 各自的输出必须能被这里的模型解析通过，任何多余字段、
缺字段、类型错误、量程外数值都会在校验熔断层被拦截并触发重试。

G-Eval 加权评分的适配说明：
  论文口径 final = Σ p(level_i)×level_i（p 来自模型输出token概率）。
  Agent 运行时通常拿不到 token 概率，故契约要求 Skill 对 1-4 档
  逐档自报置信度 conf_1..conf_4（和≈1），由 validate 层做加权。
  这保留了 G-Eval "概率加权和" 的核心思想并可在无 logits 的运行时落地。
"""
from __future__ import annotations

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator

# 创新积分2.0 九个核心指标编码（与 innovation2.CORE_INDICATORS 保持一致）
METRIC_CODES = {
    "RD_EXPENSE", "RD_RATIO", "RD_STAFF_RATIO",
    "INVENTION_GRANTS", "IP_PER_100_RD", "TECH_CONTRACT",
    "MAIN_REVENUE", "REV_GROWTH", "ROE",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- Skill 1：专利技术研判 ----------
class PatentJudgement(_Strict):
    source_id: str
    rubric_level: int = Field(ge=1, le=4, description="G-Eval综合档位1-4")
    conf_1: float = Field(ge=0, le=1)
    conf_2: float = Field(ge=0, le=1)
    conf_3: float = Field(ge=0, le=1)
    conf_4: float = Field(ge=0, le=1)
    barrier_score: int = Field(ge=0, le=100, description="技术路线壁垒0-100")
    innovation_score: int = Field(ge=0, le=100, description="创新高度0-100")
    claim_quality_score: int = Field(ge=0, le=100, description="独权质量0-100")
    rationale: str = Field(min_length=20, description="研判依据（CoT结论）")
    evidence_quote: Optional[str] = None
    red_flags: List[str] = Field(default_factory=list)


class PatentReviewOutput(_Strict):
    skill: str = "patent_review"
    patents: List[PatentJudgement] = Field(min_length=1)
    portfolio_rationale: str = Field(min_length=20)


# ---------- Skill 2：商业化对齐 ----------
class BidAlignment(_Strict):
    bid_source_id: Optional[str] = None
    matched_patent_source_ids: List[str] = Field(default_factory=list)
    alignment_level: int = Field(ge=1, le=4, description="4=真实依赖/3=部分依赖/2=泛领域相关/1=贸易型订单")
    is_trading_order: bool = False
    rationale: str = Field(min_length=20)
    evidence_quote: Optional[str] = None


class CommercializationOutput(_Strict):
    skill: str = "commercialization_fit"
    bids: List[BidAlignment] = Field(default_factory=list)
    overall_note: str = Field(min_length=10, description="整体对齐情况说明")
    no_bid_data: bool = Field(default=False, description="中标公告数据源占位时置true")


# ---------- Skill 3：反包装核查（FActScore） ----------
class AtomicClaimCheck(_Strict):
    claim_id: str
    atomic_fact: str = Field(min_length=5, description="单一事实的原子声明")
    source_ids: List[str] = Field(default_factory=list, description="支撑/反驳该声明的证据Source_ID")
    verdict: str = Field(pattern="^(supported|refuted|unverifiable)$")
    evidence_quote: Optional[str] = None


class AntiPackagingOutput(_Strict):
    skill: str = "anti_packaging_factcheck"
    claims: List[AtomicClaimCheck] = Field(min_length=1)
    overall_note: str = Field(min_length=10)


# ---------- Skill 4：银行报告生成 ----------
class InterviewQuestion(_Strict):
    question_id: str
    question: str = Field(min_length=10)
    trigger: str = Field(min_length=3, description="触发该问题的指标/事实/红灯")
    source_ids: List[str] = Field(default_factory=list)


class BankReportOutput(_Strict):
    skill: str = "bank_report"
    report_md: str = Field(min_length=100, description="《技术信用画像摘要》Markdown全文")
    interview_checklist: List[InterviewQuestion] = Field(min_length=3)
    used_source_ids: List[str] = Field(default_factory=list)
    used_metric_codes: List[str] = Field(default_factory=list)

    @field_validator("used_metric_codes")
    @classmethod
    def _codes_in_metric_set(cls, v):
        bad = [c for c in v if c not in METRIC_CODES]
        if bad:
            raise ValueError(f"不存在的指标编码 {bad}，只允许创新积分2.0九项: {sorted(METRIC_CODES)}")
        return v


OUTPUT_MODELS = {
    "patent_review": PatentReviewOutput,
    "commercialization_fit": CommercializationOutput,
    "anti_packaging_factcheck": AntiPackagingOutput,
    "bank_report": BankReportOutput,
}
