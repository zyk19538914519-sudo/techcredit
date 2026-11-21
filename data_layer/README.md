# techcredit 数据层（比赛项目：企业技术信用评估系统）

部署位置：**火山云服务器** `/root/projects/techcredit/data_layer/`（明确不碰本地服务器）。
仅依赖 Python 标准库，`/usr/bin/python3` 可直接运行。

## 架构

```
fetch_company.py  入口：企业解析 -> 多源抓取 -> 落盘编号 -> 底稿渲染
gateway.py        数据网关：智慧芽 MCP / 元典企业·司法 MCP / 天眼查（待凭证）
store.py          SQLite 索引 + 原始 JSON 落盘 + 全局唯一 Source_ID
dossier.py        每 Source_ID 一份 Markdown 底稿（头部带完整溯源元数据）
ingest_weknora.py 底稿批量入库云端 WeKnora，回写 weknora_doc_id（幂等可重跑）
config.py         路径与凭证配置（Key 只读受限权限文件，不落盘不外泄）
```

数据目录：`/root/projects/techcredit/data/`
- `techcredit.db`：sources（溯源索引）+ fetch_log（抓取审计）
- `raw/{uscc}/{Source_ID}.json`：原始数据包（内容变更保留历史版本）
- `dossiers/{uscc}/{Source_ID}.md`：WeKnora 入库底稿

## Source_ID 规范

`{KIND}-{PROVIDER}-{sha1(provider|natural_key)[:10]}`

- KIND：PAT 专利 / BID 中标 / JUD 司法 / COR 工商 / PEN 处罚 / RSK 经营风险
- PROVIDER：ZHY 智慧芽 / YD 元典 / TYC 天眼查
- natural_key：业务自然主键（专利公开号、案号、处罚文号；元典记录为 `uscc|endpoint|id`）
- 确定性生成：同一记录重复抓取幂等 upsert；内容变化版本 +1 并保留旧版，
  WeKnora 状态自动回到 pending 触发重新入库。

下游 Agent 的每条结论必须绑定 Source_ID；校验层用 `sources` 表核查其真实存在。

## 用法

```bash
cd /root/projects/techcredit/data_layer
python3 fetch_company.py --name "企业全称" --patent-limit 20 --claims 3
python3 ingest_weknora.py --company <USCC>          # WeKnora 就绪后执行
python3 ingest_weknora.py --dry-run                 # 查看待入库清单
```

## 数据源现状（2026-11-21 用户确认：不用元典，缺的源占位）

| 数据源 | 状态 | 覆盖 |
|---|---|---|
| 智慧芽 MCP（1458a4） | ✅ 已接通 | 专利检索/著录/权利要求/同族/法律状态/无效宣告 |
| 元典 | 🚫 按用户要求停用 | 代码保留；需显式 TECHCREDIT_ENABLE_YUANDIAN=1 才可启用 |
| 天眼查 MCP | ✅ 已接通（免费工具） | 工商画像/风险总览可用；股东/涉诉/招投标摘要需 VIP（当前 403） |
| 企查查 | 📌 占位（QCC_TOKEN） | 工商备选源 |
| 剑鱼标讯 MCP | ✅ 已接通 | 中标/招标正文与详情、拟在建项目、余额/消耗审计 |
| 网页搜索/全文（Tavily） | ✅ 已接通 | search 定位 + extract 抓官网/新闻稿全文；Firecrawl 备选 |
| WeKnora 后端 | ✅ 已就绪 | 8080 在线，知识库 techcredit-dossiers，底稿带 Source_ID 入库 |

占位源在 fetch_log 中显式登记缺口（status=placeholder），凭证配置后状态变 ready。
中标公告正文通道已接剑鱼标讯 MCP（search_bidding + get_bidding_detail，按中标单位精确过滤）。注意：剑鱼关键词为分词 OR 模糊召回，首页多为无关中标，需翻页/精确过滤才能命中目标企业；天眼查 get_bidding_info 需 VIP（当前 403）。

## SQLite 部署与运维（2026-11-21 完成）

数据库 `data/techcredit.db` 已启用 WAL 日志（`journal_mode=wal`）、`synchronous=NORMAL`、
`busy_timeout=10s`、`foreign_keys=ON`，多 Agent 并发读写不会互相锁死；`PRAGMA user_version=1`
作为 schema 版本号，后续迁移在此递增。

索引与约束：

| 对象 | 作用 |
|---|---|
| `ux_sources_natural(provider, natural_key)` | 数据库层兜底幂等，同一自然键不可能重复登记 |
| `idx_sources_company(company_key, kind)` | 按企业 + 类型取证据链 |
| `idx_sources_kind(kind)` | 跨企业按类型统计 |
| `idx_sources_weknora(weknora_status)` | 待入库队列快速筛选 |
| `idx_fetch_log_company(company_key, provider)` | 抓取审计按企业回溯 |

运维命令（均在 `data_layer/` 下执行）：

```bash
python3 deploy_sqlite.py      # 幂等部署 + 体检 + 隔离自检（改 schema 后必须跑）
python3 backup_db.py          # 在线热备份到 data/backups/，默认保留 14 份
python3 backup_db.py --list   # 查看现有备份
```

体检覆盖 integrity_check、journal_mode、schema 版本、索引齐全、raw 文件与 Markdown 底稿零缺失、
自然键零重复；隔离自检在临时库里验证「重复抓取幂等 / 内容变更版本递增并留 v1 快照 /
伪造 Source_ID 被校验层拦截」三条语义。当前结果：8 条 test record、17 条抓取审计，
全部通过。

## 凭证

- 智慧芽：`/root/.patsnap_mcp_key`（600）
- 元典：复用 Hermes 桥接脚本内置 Key
- WeKnora：`/root/weknora-mcp/weknora-mcp.env`
- 天眼查：`TYC_TOKEN` 环境变量（待提供）
## 运行记录（2026-11-21 整合后首跑）

示例企业：北京北方华创微电子装备有限公司（91110302801786752A）+ 既有宁德时代专利。
- SQLite sources 共 21 条：patent/zhihuiya 16、company/tianyancha 1、risk/tianyancha 1、web/tavily 3；全部 weknora_status=uploaded。
- WeKnora 知识库 techcredit-dossiers 已上传 21 份底稿（含 Source_ID 溯源头）。
- 缺口：BID/JUD/PEN 本次为 0——天眼查股东/涉诉/招投标需 VIP（403）；剑鱼模糊召回未命中精确中标且剑鱼币余额仅余 40（今日已耗 960）。
- 凭证：/root/.tyc_mcp_key、/root/.jianyu_mcp_key、/root/.tavily_key、/root/.firecrawl_key、/root/.metaso_key、/root/.mx_key（均 600）。


## 企查查 MCP 接入（2026-11-21，演示用）
- 凭证: /root/.qcc_mcp_key (600); 配置快照 docs/qcc_mcp.json (600); 环境变量 QCC_API_KEY / QCC_MCP_BASE 优先。
- 网关: gateway.QccClient，六服务 company/risk/case/tender/ipr/operation（Bearer + mcp-session-id）。
- 采集编排: fetch_company.py 步骤1.5，18 类 + 中标(operation/get_bidding_info, role=中标方)。
- 新增 KIND: STD/CER/AWD/FUND/RANK/HR/IPX; PROVIDER qcc=QCC。
- 演示命令: python3 fetch_company.py --name <企业全称> --uscc <USCC> --ingest
  （仅企查查: 加 --skip-tyc --skip-jianyu --skip-patsnap --skip-web --skip-fin）
- 积分注意: 约19积分/次，注册500积分≈26次调用即耗尽(code 300008)；演示用已入库55条底稿即可，
  补采需充值或按每日赠送100分批。中标 role 枚举必须为 招采方/投标方/中标方/代理方/被提及。
- 现状: 北方华创32条/14类、宁德24条；全部 uploaded WeKnora(techcredit-dossiers)。


## 免费官方源接入（2026-11-21 第二轮接线）
- gateway.GgzyClient：全国公共资源交易平台数据服务 data.ggzy.gov.cn/yjcx/index/*（免费无Key）。
  search(主体+bidCount)/bid_list(中标,含bidPrice/tenderOrgName/createTime)/black_list_info(交易黑名单)/qualient(资质)。
  覆盖2019至今；北方华创 bidCount=30、宁德 bidCount=24，已入库 BID+RSK。
- fin_rd.py（venv, pypdf）：巨潮年报PDF(static.cninfo.com.cn)解析研发人员数量/占比/研发投入占比；
  员工总数由 研发人员/占比 推算；研发费用(元)以 AKShare 利润表为准（PDF单位易混）。
  宁德2025年报：研发人员22901人、占比12.32%、研发投入占比5.23%。已入库 HR/CNI。
- 注意：/yjcx/dlindex/* 需登录Token（不用）；ccgp/search 有反爬（不用）；省级ggzy域名服务器DNS不可达（用全国data.ggzy）。
- 地方开放平台(山东/达州技术合同+科技奖、北京股权出质、杭州动产抵押)与信易贷API 需 key/区域/机构身份，未接（见 docs 映射文档第八节）。
