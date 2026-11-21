# techcredit 引擎与 Skills（比赛项目：企业技术信用评估系统）

以**创新积分制2.0**（科技部 2025 年 10 月全国试行版）为核心评分框架，
OECD PQI 专利质量指标深化"产出质量"维度，G-Eval / FActScore / Rubric 论文
方法论支撑 Agent 层，按记忆服务器确定的要求实现。

## 目录结构

```
/root/projects/techcredit/
├── data_layer/          数据层（SQLite+JSON 落盘、Source_ID 溯源、WeKnora 入库）
├── engine/              Python 功能实现层
│   ├── innovation2.py   创新积分2.0：9指标×9领域×3阶段权重矩阵、分段归一化、
│   │                    缺失值动态调权、3加分项规则、A/B/C/D分级
│   ├── patent_quality.py OECD PQI：发明占比/同族规模(1%-99% winsorize)/4位IPC广度/
│   │                    授权周期(1-Δt/MaxΔt)/发明人留存率，同期群归一化
│   ├── funnel.py       硬排雷（R1突击申请/R2发明人断层/R3无效雷 + Y1-Y4黄灯）
│   │                    + 漏斗抽样（Top5-8核心专利+Top10中标）+ 4个Skill上下文包
│   ├── schemas.py      4个Skill输出的Pydantic v2契约（extra=forbid，指标编码枚举）
│   ├── validate.py     校验熔断：Pydantic强校验 + Source_ID真实性核查（伪造即拦截）
│   │                    + G-Eval置信度归一加权 + 3次失败熔断
│   ├── score.py        综合信用分 = 硬指标55% + 语义35% - 风险扣分 / 一票否决
│   │                    否决线：红灯≥2 / fact_score<50% / 核心专利全无效
│   ├── run_engine.py   CLI入口（跑引擎 / --agent-out 走熔断+汇总）
│   ├── test_engine.py  引擎自检（81项）
│   └── test_skills.py  Skill链路自检（17项，真实宁德时代数据全链路）
└── skills/             4个职责单一Agent Skill（每个只有 SKILL.md，喂精简上下文）
    ├── skill1_patent_review          专利技术研判：G-Eval CoT + 4档Rubric + 置信度
    ├── skill2_commercialization_fit  商业化对齐：4档对齐量表、贸易型订单识别
    ├── skill3_anti_packaging         反包装核查：FActScore原子声明三值判定
    └── skill4_bank_report           银行报告：六节审贷语言+下户访谈清单
```

## 运行

```bash
cd /root/projects/techcredit/engine

# ① 跑引擎：PQI + 创新积分2.0 + 排雷 + 漏斗 → 生成4个上下文包
python3 run_engine.py --company 91350900587527783P --domain "新能源与节能" --stage "稳定期"

# 带财务口径（税务/天眼查接入后自动回填，支持部分缺失）
python3 run_engine.py --company <USCC> --domain "先进制造与自动化" --stage "成长期" \
    --finance-file finance.json --bonus-file bonus.json --patent-top 8

# ② Agent 按 4 个 SKILL.md 各自消费上下文包，输出 JSON 存入某目录
#    （pack_*.json → skill输出 <skill名>.json）

# ③ 校验熔断 + 总分汇总
python3 run_engine.py --company <USCC> --domain … --stage … --agent-out <输出目录>

# 自检（改代码后必跑）
python3 test_engine.py   # 81项
python3 test_skills.py   # 17项全链路（会新增一条 evaluation_runs 记录）
```

## 创新积分2.0 实现要点

- **权重矩阵**：附件2通用表 + 附件3八大技术领域表，9×9×3 全量硬编码，自检校验每列和=100
- **行业同期群归一化**：分段线性（≤p25→0-40 / p25-p50→40-70 / p50-p75→70-90 /
  p75-p90→90-100 / ≥p90→100）；基准线占位，`engine/benchmarks.json` 标定后覆盖，
  支持领域×阶段分基准（by_domain）
- **缺失值动态调权**：组内（一级指标内）按比例再分配，保持一级结构（投入50:产出30:发展20），
  全缺失组时组间再分配；缺失指标在结果中显式列出
- **加分项**：科技计划/标准（省级4/地市2/标准5，封顶10）、融资分档（2000/1000/500）、
  排名资质（国际10/国家5/省级3/资质5），三项封顶30
- **分级**：A≥85 / B≥70 / C≥60 / D<60（核心百分制口径）

## 与论文方法论的对应

| 论文 | 落点 |
|---|---|
| OECD DSTI/DOC(2013)3 | patent_quality.py 五项PQI + 同期群归一化 + winsorize |
| G-Eval (Liu et al. 2023) | Skill1 4档置信度 + validate.py 概率加权 Σp(level)×level |
| Learning to Judge (EACL 2026) | 4档Rubric显式判据表；量表固定不漂移（模型自生成量表不可靠） |
| FActScore (Min et al. 2023) | Skill3 原子声明三值判定 + score.py fact_score=supported占比 |

## 已验证结果（2026-11-21，宁德时代真实样本）

- 引擎自检 81/81 通过；Skill 链路自检 17/17 通过
- 真实数据端到端：8件专利→Top5 精选；R2发明人断层红灯 + Y4数据窗口黄灯正确触发
  （样本仅2014-2016年专利，属数据窗口问题，引擎如实暴露而非掩盖）
- 校验熔断：伪造Source_ID、契约外字段、量程越界、短rationale、不存在的指标编码均被拦截
- SQLite 新增 evaluation_runs / metric_values / risk_flags / agent_outputs 四张表，
  评估全链路可审计可回溯

## 数据缺口（下一步）

1. 天眼查 TYC_TOKEN → 中标公告/工商/司法/财务口径 → 商业化对齐与财务八项指标
2. Tavily → 官网/新闻稿素材 → 反包装核查 fact_score
3. 智慧芽 gateway 扩展 IPC/同族/法律状态/独权字段 → PQI 五维全量
4. WeKnora 8080 就绪 → 底稿入库
5. 行业基准线标定 → benchmarks.json（官方口径为全国20余万家积分企业样本分位数）
