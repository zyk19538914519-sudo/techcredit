# techcredit 比赛项目 · API Key 申请清单

> 项目：企业技术信用评估系统（银行审贷科技尽调）
> 数据层位置：火山服务器 `/root/projects/techcredit/data_layer/`
> 整理日期：2026-11-21
> 用法：按「B 自助注册」→「C 商务采购」顺序申请；拿到 Key 后填进 `techcredit.env.example` 对应变量，放到服务器 `/root/projects/techcredit/.env`（权限 600）。

---

## A. 已有 Key，无需申请（只需同步/确认额度）

| 源 | 现状 | 位置 | 备注 |
|---|---|---|---|
| 天眼查 MCP | ✅ 已验证可用 | Mac `~/.tyc/config.json`（`mcpk2_…`） | 服务器尚未配置 `TYC_TOKEN`；限流 **10 次/分钟**，批量采集需排队 |
| 智慧芽 MCP | ✅ 服务器已接通 | 服务器 `/root/.patsnap_mcp_key` | 唯一专利源；昨日曾报余额不足，采集前先确认额度，不够找商务扩容 |
| XCrawl（网页全文抓取） | ✅ Mac 已有 | Mac `~/.zshrc` `XCRAWL_API_KEY`（`xc-…`） | 用于官网/新闻稿全文，反包装核查素材 |
| **Tavily** | ✅ Mac 已有 | `~/.hermes/.env` 第 59 行 + `~/.hermes/config.yaml` `mcp_servers.tavily.env`（`tvly-dev-…`） | 已接成 hermes MCP；dev key，免费 1,000 credits/月 |
| **Firecrawl** | ✅ Mac 已有 | `~/.hermes/config.yaml` `mcp_servers.firecrawl.env`（`fc-…`） | 已接成 hermes MCP；crawl/extract 抓官网全文，可替代 XCrawl |
| **Metaso 秘塔搜索** | ✅ Mac 已有 | Mac `~/.zshrc` `METASO_API_KEY`（`mk-…`） | 中文搜索备选，官网/新闻稿定位 |
| **东方财富妙想（金融数据）** | ✅ Mac 已有 | Mac `~/.zshrc` `MX_APIKEY`（`mkt_…`） | 财务/上市/公告研报数据，经济性维度可不用 Wind |
| WeKnora 底稿库 | ⏳ 部署中 | 服务器 `/root/weknora-mcp/weknora-mcp.env` | 8080 就绪后执行 `ingest_weknora.py` |

---

## B. 自助注册即可拿 Key（免费起步，优先申请）

| # | API | 申请入口 | 申请什么 | 费用/额度 | 环境变量 | 用途（对应比赛环节） | 优先级 |
|---|---|---|---|---|---|---|---|
| B1 | **企查查 MCP（智能体平台）** | https://agent.qcc.com | 注册账号 → 开发者支持计划 → 拿 MCP Key | 注册送 500 积分（30 天有效）；每日赠 100（普通）/150（VIP）/200（SVIP）；充值 1 元=10 积分；单企业每月封顶 100 积分 | `QCC_MCP_TOKEN` | 工商/股东/司法/处罚备选源，与天眼查交叉校验 | P0 |
| B2 | **ImportYeti（美/墨进出口提单）** | https://www.importyeti.com → My Account → Settings → API Credits Settings | 注册 → 拿 API Key（`IYApiKey` 头） | 按 credits 计费，注册后有初始额度，详见其 Pricing | `IMPORTYETI_API_KEY` | 识别「进口整机转售」的贸易型订单，戳穿挂羊头卖狗肉 | P1 |
| B3 | **Serper（Google SERP，备选搜索）** | https://serper.dev | 注册拿 Key | 一次性免费 2,500 次；之后 $50/50,000 次 | `SERPER_API_KEY` | Tavily/Metaso 额度不够时的兜底搜索 | P2 |
| B4 | **高德开放平台** | https://lbs.amap.com | 控制台 → 应用管理 → 创建 Key（Web 服务） | 免费额度（个人/企业认证后提额） | `AMAP_KEY` | 注册地址/厂房真实性、集群注册识别 | P2 |
| B5 | **百度地图开放平台**（与 B4 二选一） | https://lbsyun.baidu.com | 控制台 → 应用管理 → AK | 免费额度 | `BAIDU_MAP_AK` | 同上 | P2 |
| B6 | **Semantic Scholar**（可选） | https://www.semanticscholar.org/product/api | 申请 API Key 提额 | 无 Key 也可用（限流低），有 Key 提额 | `S2_API_KEY` | 论文×发明人交叉，验证研发团队真实性 | P2 |

> 说明：Tavily 已移到 A 节（本机已有 Key）。OpenAlex、Wayback Machine、巨潮资讯、政府采购/公共资源公示、执行/信用官方源、NMPA/CDE、标准平台、CNCA、BIS/OFAC 等**不需要 Key**，见 D 节，直接写爬虫/REST client 即可。

---

## C. 需商务审核 / 采购（按预算决定是否申请）

| # | API | 申请入口 / 联系方式 | 申请什么 | 费用 | 环境变量 | 用途 | 优先级 |
|---|---|---|---|---|---|---|---|
| C1 | **剑鱼标讯 数据 API** | https://www.jianyu360.cn/front/dataMarket/dataInterface ；客服 400-108-6670 | 「招标采购信息正文获取（普通/高级字段包）」+「附件下载」+「企业高级画像（含中标分析）」；留公司信息审核开通 | 商业 API 询价；另有「免费数据合作」通道（需有面向用户的网站/APP/公众号 + 开发能力，换品牌曝光） | `JIANYU_API_KEY` | **中标公告正文**——商业化对齐 Agent 的核心缺口 | P0 |
| C2 | **天眼查开放平台**（批量/高并发时） | https://open.tianyancha.com ；400-608-0000 | 预充值或包年套餐，拿 Token | 按次 0.10–2.00 元/次（基本信息 0.15、诉讼 1.5、股权穿透 1–2 等） | `TYC_OPENAPI_TOKEN` | MCP 限流 10 次/分钟不够批量时用；MCP Key 已够小规模 | P1 |
| C3 | **企查查开放平台**（按次计费备选） | https://openapi.qcc.com ；api@qcc.com / 400-088-8275 | 企业实名认证 → 选接口套餐（如行政处罚核查 865、工商 410） | 例：行政处罚 0.5 元/次，1,000 元/2,000 次，免费试用 20 次；需应用场景审核 | `QCC_APPKEY` + `QCC_SECRET` | 与 B1 二选一或互补；B1 免费积分够小样本 | P2 |
| C4 | **腾道海关数据 API**（全球覆盖时） | https://www.tendata.net ；info@tendata.cn | API 接口服务签约 | 商务询价，无免费账号（可在线演示） | `TENDATA_API_KEY` | B3 只覆盖美/墨；要全球提单再上这个 | P2 |
| C5 | **IT桔子 / 烯牛**（融资事件，可选） | itjuzi.com / xiniudata.com 商务 | 融资事件 API | 询价 | `ITJUZI_API_KEY` | 融资节奏×专利突击申请的时间相关性 | P3 |
| C6 | **新榜**（公众号文章，可选） | newrank.cn 商务 | 公众号内容 API | 询价 | `NEWRANK_API_KEY` | 宣传稿时间分布，反包装时间轴证据 | P3 |

---

## D. 无需 Key，直接接入（写 client 即可，零成本）

| 源 | 入口 | 用途 |
|---|---|---|
| Wayback Machine | web.archive.org（CDX/availability API） | 官网历史快照，识别突击包装 |
| 中国政府采购网 / 全国公共资源交易平台 | ccgp.gov.cn / ggzy.gov.cn | 中标公示正文（C1 的免费兜底） |
| 中国执行信息公开网 / 信用中国 | zxgk.court.gov.cn / creditchina.gov.cn | 失信、被执行、行政处罚、重大税收违法 |
| 全国企业破产重整案件信息网 / 人民法院公告网 | pccz.court.gov.cn / rmfygg.court.gov.cn | 破产、公告类风险 |
| 巨潮资讯网 | cninfo.com.cn | 上市/挂牌公告全文、供应商客户、募资投向 |
| OpenAlex / arXiv | api.openalex.org / arxiv.org | 论文与发明人交叉 |
| NMPA / CDE | nmpa.gov.cn / cde.org.cn | 生物医药管线、器械注册证真实性 |
| 全国标准信息公共服务平台 | std.samr.gov.cn | 国标/行标起草单位 |
| CNCA 认证认可 | cnca.gov.cn | CCC/ISO 体系证书 |
| BIS Entity List / OFAC | bis.gov / ofac.treasury.gov | 半导体制裁/断供风险 |
| 中国土地市场网 / 环评公示 | landchina.com / 各省环评平台 | 拿地、产能、建厂真实性 |
| crt.sh | crt.sh | 证书透明度，官网搭建时间弱信号 |

---

## 申请顺序建议

1. **今天就能拿**：B1 企查查 MCP（5 分钟，免费）→ 补上工商备选源。搜索/抓全文不用申请：Tavily、Firecrawl、Metaso、XCrawl 本机都已有 Key。
2. **本周推进**：C1 剑鱼标讯（中标正文是比赛核心缺口，先问免费数据合作通道，不行再询价）；B2 ImportYeti。
3. **看预算**：C2 天眼查开放平台（仅当批量跑不动 10 次/分钟）、C4 腾道（仅当需要全球提单）。
4. **同步动作**：把 Mac 上的天眼查 Key 写入服务器 `TYC_TOKEN`，Tavily/Firecrawl/Metaso/MX/XCrawl 写入对应变量；确认智慧芽额度。

## 编号规范扩展（接新源时同步改 store.py）

- KIND 增加：`STD` 标准 / `CER` 资质认证 / `CUS` 海关 / `FIN` 财务公告 / `WEB` 官网快照
- PROVIDER 增加：`QCC` 企查查 / `JY` 剑鱼 / `IY` ImportYeti / `WB` Wayback / `CCGP` 政采 / `CRED` 信用 / `OA` OpenAlex / `CNINFO` 巨潮 / `TAV` Tavily