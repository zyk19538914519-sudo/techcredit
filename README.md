# 一画专利评估分析系统

> 企业技术信用评估（银行审贷科技尽调）—— 面向科技型中小企业的技术维度授信辅助工具

以**创新积分制 2.0**（科技部 2025 年 10 月全国试行版）为核心评分框架，结合
OECD PQI 专利质量指标深化"产出质量"维度，以 G-Eval / FActScore / Rubric
等论文方法论支撑 Agent 研判层，为银行客户经理输出可溯源的技术信用画像。

---

## 核心设计原则

**每一条结论都必须绑定真实数据源。** 系统不接受无法溯源的技术结论。

- 每条原始专利 / 中标公告 / 司法记录分配全局唯一 `Source_ID`
  （格式 `KIND-PROVIDER-hash10`，确定性生成、可幂等重放）
- Agent 返回的结论必须携带 `Source_ID`，引擎侧回查原始数据包
- **校验熔断**：Source_ID 不存在即判定为伪造，拦截并重试，不进入计分

## 架构

```
├── data_layer/     数据层：Source_ID 溯源、SQLite + JSON 落盘、WeKnora 入库
├── engine/         功能实现层：硬指标引擎、漏斗抽样、校验熔断、总分汇总
├── webapp/         服务面：Web 科创企业雷达 + MCP 服务入口 + 账户鉴权
├── skills/         Agent 层：4 个职责单一 Skill（见下）
├── screening/      底池扫描：上市 / 中标 / 专精特新多源互补
└── scripts/        辅助脚本与自测
```

## 评分模型

### 一、客观硬指标（严格按 OECD PQI 公式）

发明占比、同族规模、IPC 广度、授权周期、发明人留存率；
行业同期群归一化 + 缺失值动态调权。

### 二、漏斗式抽样与硬排雷

统计规则自动标记「突击申请 / 发明人断层 / 专利无效」等红灯，
按价值筛出 Top 5-8 核心专利 + Top 10 中标公告后再交给 Agent 深查，
相比全量分析节省约 80% Token。

### 三、Agent 层：4 个职责单一 Skill

| Skill | 职责 | 方法论 |
|---|---|---|
| `skill1_patent_review` | 专利技术研判：技术路线壁垒、创新高度 | G-Eval 思维链 + 4 档 Rubric |
| `skill2_commercialization_fit` | 商业化对齐：中标项目是否真实依赖核心专利 | 语义比对，识别贸易型挂靠订单 |
| `skill3_anti_packaging` | 反包装核查：官网/新闻稿原子声明逐条比对 | FActScore 范式，输出 True/False + Source_ID |
| `skill4_bank_report` | 银行报告生成：技术信用画像 + 下户访谈清单 | 消费校验通过的硬指标与语义分 |

### 四、UI 展示

输入企业全称或统一社会信用代码 + 细分赛道（半导体设备 / 工业机器人 / 生物医药等）
匹配行业基准线，展示四维评估结果、风险红黄绿灯、可点击溯源的证据链条，
一键生成可直接粘贴进银行《授信尽职调查报告》的技术评估章节。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 配置凭证（见 docs/techcredit.env.example，勿提交真实值）
cp docs/techcredit.env.example .env
$EDITOR .env

# 运行引擎自测
python3 engine/test_engine.py
```

启动 Web 服务：

```bash
uvicorn webapp.app:app --host 127.0.0.1 --port 8090
```

MCP 服务入口见 `webapp/README.md`。

## 配置

所有数据源凭证通过环境变量或本地密钥文件注入，**不随代码分发**：

- `docs/techcredit.env.example` — 环境变量模板（复制为 `.env` 后填写）
- `docs/qcc_mcp.json.example` — 企查查 MCP 配置模板
- `docs/jianyu_mcp.json.example` — 剑鱼标讯 MCP 配置模板

WeKnora 作为本地 RAG 底座，默认监听 `127.0.0.1:8080`。

## 免责声明

本系统输出的技术信用评分与风险提示为**辅助尽调参考**，不构成授信决策。
最终授信判断由银行客户经理结合财务、征信、现场尽调等信息综合作出。

## License

MIT — 见 [LICENSE](LICENSE)
