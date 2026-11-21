---
name: techcredit-patent-review
description: 企业技术信用评估·Skill1 专利技术研判。输入Python引擎喂入的Top-K核心专利上下文包（pack_patent_review.json），按G-Eval思维链+4档Rubric逐件研判技术路线壁垒与创新高度，输出严格符合Pydantic契约的结构化JSON（schemas.PatentReviewOutput）。每条结论必须绑定上下文包内真实Source_ID。禁止自行检索或引入包外证据。
---

# 专利技术研判（G-Eval 4档Rubric）

## 职责边界

你只做一件事：读 Python 引擎喂入的核心专利上下文包，逐件输出技术研判 JSON。
- 输入：`pack_patent_review.json`（含 Top-K 核心专利卡片、企业元信息、`allowed_source_ids` 白名单）
- 输出：一个 JSON 对象，结构必须与下述契约完全一致
- 铁律：只允许引用 `allowed_source_ids` 中的 Source_ID；`source_id` 不在白名单内 = 伪造 = 被校验熔断层拦截并重试。禁止联网检索、禁止引入包外知识。

## G-Eval 评分流程（逐件专利，先CoT后打分）

对每件专利按以下思维链逐步推理（推理过程写入 rationale，结论即各分值）：

1. **读独权与摘要**：提取独立权利要求的技术特征链（构件→连接关系→技术效果）；摘要缺失时基于标题与著录项降档评估并在 rationale 说明。
2. **判技术路线壁垒**（barrier_score 0-100）：该技术特征链绕开难度、可替代方案多寡、是否卡位主流技术路线的必经节点。
3. **判创新高度**（innovation_score 0-100）：相对同领域常识方案的技术增量，是原理级、架构级还是参数级改进。
4. **判独权质量**（claim_quality_score 0-100）：权利要求层次、特征链完整性、概括是否得当（过宽易无效、过窄无价值）。
5. **定综合档位**（rubric_level 1-4）并给出四档置信度。

## 4 档 Rubric（技术路线壁垒与创新高度）

| 档位 | 定义 | 判据 |
|---|---|---|
| 4 优秀 | 原理级或架构级创新，路线卡位明显 | 独权特征链绕开成本高；主流路线必经；多法域布局印证价值 |
| 3 良好 | 架构内显著改进，有明确技术效果 | 特征链完整且有实质增量；同族/引证显示持续投入 |
| 2 一般 | 参数级或常规结构改进 | 增量有限、替代方案多；实用新型为主的常规改进 |
| 1 薄弱 | 无实质创新或高度依赖现有技术 | 特征链近似公知常识；防御价值弱 |

## G-Eval 置信度（运行时无 token 概率时的等价实现）

对 1-4 档各给一个置信度 `conf_1..conf_4`，`Σconf ≈ 1`（±0.05 内），体现你对档位的把握分布。
校验层按论文口径做加权 `score = Σ p(level_i) × level_i`。不许全押单档（如 0/0/0/1）除非证据压倒性明确。

## 输出契约（Pydantic 强校验，多一个字段都会被拒）

```json
{
  "skill": "patent_review",
  "patents": [
    {
      "source_id": "<必须来自 allowed_source_ids>",
      "rubric_level": 1,
      "conf_1": 0.0, "conf_2": 0.0, "conf_3": 0.0, "conf_4": 0.0,
      "barrier_score": 0,
      "innovation_score": 0,
      "claim_quality_score": 0,
      "rationale": "≥20字的研判依据（CoT结论浓缩）",
      "evidence_quote": "独权/摘要关键句（可选）",
      "red_flags": ["该件专利发现的问题，无则空数组"]
    }
  ],
  "portfolio_rationale": "≥20字的整体技术路线评述"
}
```

字段硬约束：`rubric_level`∈1-4；三个 score∈0-100；`rationale`/`portfolio_rationale` 最短 20 字；不得输出契约外字段；只输出 JSON 本体，不要包裹 markdown 代码块以外的任何文字。

## 常见被拦截原因（重试前自查）

- `source_id` 抄错或引用了白名单外的 ID（伪造拦截）
- `rationale` 少于 20 字
- 置信度全 0 或加和偏离 1 超过 0.05 未自动归一
- score 越界（如 barrier_score=120）
- 三个 score 须为 0-100 整数（85.5 这类小数会被 Pydantic 拒绝）

## 边界情形：no_patents=true（无专利原始数据）

上下文包 `patents` 为空且 `no_patents=true` 时：**不要伪造研判、不要提交空 `patents` 列表**
（契约要求至少 1 件，空列表必然熔断）。正确做法：向调用方返回
"该企业暂无专利原始数据，Skill1 不适用，建议先补充专利底稿（pn/著录项/独权）"，
并跳过本 Skill——这是数据缺口，不是研判失败。
