---
name: techcredit-anti-packaging
description: 企业技术信用评估·Skill3 反包装核查（FActScore范式）。输入pack_anti_packaging_factcheck.json，把官网/新闻稿等对外宣传声明拆成原子事实，逐条比对证据库（核心专利+中标记录），输出每条supported/refuted/unverifiable判定+支撑Source_ID的结构化JSON（schemas.AntiPackagingOutput）。宣传素材未接入时如实声明缺口，禁止编造。校验层按FActScore=支撑占比计分，<50%触发一票否决。
---

# 反包装核查（FActScore 范式）

## 职责边界

- 输入：`pack_anti_packaging_factcheck.json`（`publicity_claims` 宣传素材 + `evidence` 证据库 + `allowed_source_ids`）
- 输出：一个 JSON 对象，结构与契约完全一致
- 铁律：判定只基于证据库内文件；`source_ids` 只能填 `allowed_source_ids` 中的真实 ID；宣传素材为空（`publicity_source_pending=true`）时输出声明材料缺失的说明行，禁止虚构对外声明。

## FActScore 流程（先拆原子，再逐条核）

1. **拆原子事实**：把每段宣传文本拆成"一条一个事实"的原子声明（如"公司拥有电池组连接片核心技术"拆成 1 条；"全球领先、拥有百项专利"拆成 2 条）。去掉形容词与不可判定的营销话术后再拆。
2. **逐条检索比对**：对每条原子声明，在证据库（专利标题/摘要/独权、中标正文）中寻找直接支撑或反驳材料。
3. **三值判定**：
   - `supported`：证据库中有直接支撑（必须给出 `source_ids` + `evidence_quote`）；
   - `refuted`：证据库材料与之矛盾（如宣称发明专利而证据全是实用新型）；
   - `unverifiable`：证据库无法证实也无法证伪（计入分母，压低 fact_score）。
4. **汇总说明**：在 `overall_note` 说明支撑率与主要缺口。

## 判定尺度

- 宁严勿宽：营销级模糊（"行业领先"）一律 `unverifiable`，不因品牌好感给 `supported`。
- 数字类声明（专利数量、市占率、排名）必须有证据库内数字直接对应才 `supported`。
- "拥有××核心技术"需要核心专利的技术特征链与声明技术领域一致。

## 输出契约

```json
{
  "skill": "anti_packaging_factcheck",
  "claims": [
    {
      "claim_id": "c1",
      "atomic_fact": "≥5字的单一事实声明",
      "source_ids": ["支撑或反驳证据的Source_ID，unverifiable可为空数组"],
      "verdict": "supported",
      "evidence_quote": "证据关键句（可选）"
    }
  ],
  "overall_note": "≥10字：支撑率与缺口说明"
}
```

硬约束：`verdict` 只能是 `supported|refuted|unverifiable` 三值之一；`atomic_fact` ≥5 字；`claims` 至少 1 条；`source_ids` 内所有 ID 必须真实存在。

## 下游影响（务必知悉）

校验层计算 `fact_score = supported / 全部原子声明数`。**fact_score < 50% 时总分一票否决降 D 级**——你的判定直接决定企业是否被"包装识别"机制否决，宁可 unverifiable 也不无证据 supported。

## 常见被拦截原因

- 给 supported 却没有填任何 source_ids（证据空引用）
- source_ids 含白名单外 ID（伪造拦截）
- verdict 拼错（如 "Support"）
- 素材未接入时编造 publicity claims
