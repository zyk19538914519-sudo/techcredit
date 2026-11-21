---
name: techcredit-commercialization-fit
description: 企业技术信用评估·Skill2 商业化对齐。输入Python引擎喂入的上下文包（pack_commercialization_fit.json），对比Top-K核心专利技术领域与中标公告正文，逐条判断中标项目是否真实依赖核心专利技术，识别"挂羊头卖狗肉"的贸易型订单，输出严格符合Pydantic契约的结构化JSON（schemas.CommercializationOutput）。每条结论只允许绑定包内真实Source_ID。中标数据源占位时输出no_bid_data=true并说明缺口。
---

# 商业化对齐（专利技术 × 中标公告）

## 职责边界

- 输入：`pack_commercialization_fit.json`（核心专利卡片 + 中标公告 + `allowed_source_ids` + `bid_source_pending`）
- 输出：一个 JSON 对象，结构与契约完全一致
- 铁律：Source_ID 只能来自 `allowed_source_ids`；不联网、不引入包外信息；`bid_source_pending=true` 或 `bids` 为空时，如实输出 `no_bid_data: true`，禁止编造中标对齐结论。

## 对齐研判流程（逐条中标公告）

1. **读专利技术特征**：从核心专利卡片提取技术领域（IPC4）与技术效果。
2. **读中标公告正文**：提取采购标的名称、技术规格、交付物描述。
3. **双向比对**：
   - 采购标的的技术规格是否落在核心专利的技术特征链内（真实依赖）；
   - 还是仅属于同一行业大类的通用采购（泛领域相关）；
   - 标的若为标准品/通用设备的转售（无技术规格要求），判贸易型订单。
4. **定对齐档位**（alignment_level 1-4）并写 rationale。

## 4 档对齐量表

| 档位 | 定义 | 判据 |
|---|---|---|
| 4 真实依赖 | 采购标的就是专利技术的直接实施 | 标的技术规格逐项对应独权特征链；招标文件出现与专利权利要求同源的技术参数 |
| 3 部分依赖 | 标的包含专利技术所在的子系统/工艺环节 | 部分规格对应核心专利；或依赖专利技术所在的产线环节 |
| 2 泛领域相关 | 同行业采购但未体现专利技术特征 | 仅行业大类重合，规格无对应关系 |
| 1 贸易型订单 | 标准品转售/通用物资，与技术能力无关 | 无技术规格要求、可由任意供应商履约；疑似刷流水 |

`is_trading_order` 仅在档位=1 且标的明显可由非技术方履约时为 true。

## 输出契约

```json
{
  "skill": "commercialization_fit",
  "bids": [
    {
      "bid_source_id": "<中标公告Source_ID，无则null>",
      "matched_patent_source_ids": ["<对应的核心专利Source_ID，无则空数组>"],
      "alignment_level": 1,
      "is_trading_order": false,
      "rationale": "≥20字对齐/不对齐的具体理由",
      "evidence_quote": "中标正文关键句（可选）"
    }
  ],
  "overall_note": "≥10字整体对齐情况（含数据缺口说明）",
  "no_bid_data": false
}
```

硬约束：`alignment_level`∈1-4；`matched_patent_source_ids` 中每个 ID 必须真实存在于 `allowed_source_ids`；无中标数据时 `bids=[]`、`no_bid_data=true`，并在 `overall_note` 说明"中标公告数据源占位（TYC_TOKEN 待配置）"。

## 常见被拦截原因

- 无数据时编造对齐结论（校验层会连同 no_bid_data 一起核对）
- matched_patent_source_ids 填了白名单外的 ID
- rationale 少于 20 字
