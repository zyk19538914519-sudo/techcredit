# techcredit webapp / MCP 服务面

科创企业雷达（Web）+ 企业技术信用详细分析 MCP（对方 Agent 服务入口）。

- Web：`https://fatebuddy.asia/techcredit/`（systemd `techcredit-web`，127.0.0.1:8090）
- MCP：`https://fatebuddy.asia/techcredit-mcp/mcp`（systemd `techcredit-mcp`，127.0.0.1:8091，occult venv）
- 鉴权：Bearer = FateBuddy 账户接入码（只读校验，不扣额度）；Web 用 fb_session Cookie。

## 两段漏斗（海选 → 选定 → 贵复查）+ 两大功能

**漏斗①·便宜海选**（三个底池互补）：
- `screen_market`：全A股业绩报表底池（约1.15万家上市公司，免费缓存零网络）广筛硬科技潜客。
- `screen_sme`：**小微企业**——GGZY全国公共资源交易中标主体库（官方免费无Key）关键词扫池，找有中标的科技小微。
- `screen_sme_web`：**小微企业**——Tavily搜索（廉价付费）发现资质公示/网络可见科技小微（专精特新/小巨人/科技型中小企业名单等）。
- `screen_ipo`：**拟上市企业**——东财IPO申报/辅导备案池（免费缓存），已过券商/监管scrutiny的优质成长科技中小微；北交所=创新型中小企业；含招股书PDF（免费完整底稿）。

**漏斗②·贵数据复查**：客户选定几家后 `deep_verify(confirm=true)` 上贵数据（智慧芽/天眼查/剑鱼等）异步复查。
**功能一·潜客侦察**：精筛榜 `discover_prospects` + 公开触达 `get_prospect_pack`（底稿前）。
**功能二·底稿分析**：上传底稿 `upload_dossier` 或贵复查 → 4 Skill 嵌套研判 → 校验后综合信用分回传。

## ★ 数据落库边界（前期筛选 vs 底稿认证 · 2026-11-21 确立）

系统两层结构：**前期筛选企业阶段**（两层筛选：①便宜海选 → ②贵数据复查）→ **底稿认证阶段**（4-Skill 闭环出正式信用分）。
落库规则按阶段严格区分：

- **①便宜海选 screen_market / screen_sme / screen_sme_web**：纯本地计算或免费在线/廉价搜索，结果不写任何 SQL 库、不入 WeKnora RAG。
- **②贵数据复查 deep_verify**：抓取数据默认**只落筛选暂存区**（`data/screening_staging/staging.db` 独立库 +
  staging raw/底稿目录），与正式证据库 `techcredit.db` 的 sources 表物理隔离；暂存数据不渲染正式底稿、
  **不入 WeKnora 底稿 RAG**；get_deep_verify 完成后给的是**筛选预分析参考分**（staging 模式：只读暂存库供数、
  跳过 RAG 事实桥、评估记录也落 staging.db）。
- **只有客户明确表述要入库时**，筛选数据才进正式 SQL 与 RAG，两个入口：
  1. `deep_verify(..., confirm=true, persist=true)`：发起贵复查时同步声明入库，作业完成自动提升落库+入RAG；
  2. `persist_screening_result(company_key, confirm=true)`：先按默认暂存复查，客户确认后再显式提升
     （暂存→正式sources表→正式底稿→ingest_weknora 入RAG，幂等，promoted=1 留痕）。
  没有客户明确表述，任何筛选阶段数据都不会主动进入正式库或 RAG。
- **底稿认证阶段**（upload_dossier 客户主动交付 / 4-Skill 闭环）：按既有流程正常双落库（正式 SQL + WeKnora RAG），
  不受上述边界影响——该阶段数据本就是客户明确交付的底稿。

## MCP 工具面（24 个）

**名单与档案**
- `search_companies` / `get_company_profile`：免费粗筛名单（62家）与企业档案。

**两段漏斗（2026-11-21 新增）**
- `screen_market(domain?, industry?, min_growth?, min_roe?, limit?, date?)`：漏斗①便宜海选（上市）。
  全A股业绩报表底池（约1.15万家，东财免费口径本地缓存）→ 行业→技术领域映射、剔ST/退市/营收≤0
  → 增速40%+ROE35%+规模25%分位数预排序。逐企业返回代码/简称/行业/领域/营收/增速/ROE/预排序分
  + 是否已在精筛名单(in_screened)。**零成本纯计算，不调外部API**。limit≤100。
- `screen_sme(keyword, min_bids?, limit?, max_pages?, region_code?)`：漏斗①便宜海选（**小微**，免费官方）。
  底池=GGZY全国公共资源交易平台中标主体库（官方免费无Key，2019至今全国中标成交），覆盖上市底池抓不到的科技小微。
  keyword 可逗号分隔多词扫池（如"电池,模具,精密,半导体"），每词≤max_pages(≤6)页×50条，按USCC去重。
  信号模型（小微不公开财务）：**中标数=商业化活跃度、成立年限=经营稳定性、法人=触达线索**。
  min_bids 过滤中标下限；region_code 按USCC行政区划码前缀过滤（4403深圳/3302宁波/3205苏州/1101北京）；
  剔机关事业单位（USCC非9开头）。逐企业返回 USCC/全称/法人/成立年份/中标数/区划码。**零付费、零落库**。
  选定后 `deep_verify(uscc=候选uscc, company_name=全称, confirm=true)`——USCC直传绕过天眼查解析。
- `screen_sme_web(query, max_results?, extract?, extract_top?)`：漏斗①便宜海选（**小微Web**，Tavily廉价付费）。
  用 Tavily 搜索公开网络，发现 GGZY 中标库覆盖不到的科技小微：专精特新/小巨人/科技型中小企业/高新技术公示名单、
  行业目录、园区入驻企业、企业官网、新闻。query 自然语言（如"宁波 精密模具 专精特新 小巨人 企业"）。
  max_results≤10；extract=true 对前 extract_top(≤3) 条URL批量取全文。逐条线索返回 标题/URL/摘要 +
  **企业名候选（正则收紧，去散文噪声）+ 资质关键词 + 联系方式线索（电话/邮箱/官网）**，全 verified=false。
  成本：Tavily search 1次 + extract≤1次（批量），廉价远低于贵复查。**零落库**。与 screen_sme 互补。
- `screen_ipo(stage?, board?, province?, status?, name_kw?, exclude_failed?, limit?)`：漏斗①便宜海选（**拟上市**，免费缓存）。
  底池=东财IPO申报（stage=declare，约3900家，含招股书PDF）或辅导备案（stage=tutor，约5400家，更早期）。
  已过券商/监管基本scrutiny的优质成长科技企业，多为中小微；**北交所拟上市=创新型中小企业精准目标**（约860家）。
  board 拟上市地点（北交所/科创板/创业板/主板）；province 注册地/派出机构省份；status 状态关键词；
  name_kw 企业名称关键词（行业代理）；exclude_failed=true 剔终止/中止/暂缓。按日期倒序。
  逐企业返回 名称/板块/省份/状态/保荐或辅导机构/律所/日期/**招股书PDF URL**。**零成本纯计算、零落库**。
  ★招股说明书=免费完整底稿（财务/专利/业务/风险），可下载解析喂引擎，省贵数据复查。缓存刷新 `scripts/fetch_ipo_pool.py`。
- `deep_verify(company_name, stock_code?, uscc?, project_id?, confirm?, patent_limit?, claims_limit?,
  bid_limit?, web_limit?, domain?, stage?, persist?)`：漏斗②贵数据复查，**成本闸门**。
  - `confirm=false`(默认)：只返回成本预估+剑鱼币余额+剩余配额，**不发起任何贵调用**。
  - `confirm=true`：异步多源抓取作业（智慧芽专利全深度bibliography/claims + 天眼查工商/股东/司法/风险
    + 企查查免费积分 + 剑鱼中标 + 公共资源平台 + AKShare财务 + 官网全文）→ **落筛选暂存区**（默认不入
    正式SQL/不入RAG）→ 专利嵌套全深度展平喂PQI → 引擎筛选预分析 → run_id。约2-5分钟。
  - `persist=true`（仅客户明确表述要入库时传）：作业完成后自动把暂存数据提升落正式SQL并入WeKnora RAG，
    返回的 run_id 可直接接 4-Skill 闭环；persist=false(默认)则停留在暂存口径。
  - 成本杠杆：`bid_limit=0`→跳过剑鱼（省剑鱼币）、`web_limit=0`→跳过网页抓取；`uscc`直传（小微无股票代码时
    用 screen_sme 返回的USCC）company_key 直接取USCC，绕过天眼查名称解析（天眼查配额受限/402时仍可抓取，
    解析失败自动跳过工商详情，智慧芽/GGZY/企查查照常）。每账户贵复查上限10次。
- `get_deep_verify(job_id)`：轮询贵复查作业（仅发起者本人）。running→进度+日志尾；done→筛选预分析（默认暂存口径）
  或 persist=true 时正式 run_id 接 get_context_pack 4-Skill闭环；failed→错误+日志尾。
  作业表 deep_verify_jobs（MCP层），日志 data/deep_verify/{job_id}.log，退出码 .exit；并发轮询原子占位防重复跑引擎。
- `persist_screening_result(company_key, project_id?, ingest_weknora?, confirm?)`：**筛选数据入正式库的唯一显式通道**。
  confirm=false(默认)只回待提升清单；confirm=true 且客户已明确表述要入库，才把暂存区数据提升落正式 SQL
  sources 表 + 渲染正式底稿 + 入 WeKnora RAG（幂等，promoted 留痕），随后 analyze_company 重跑正式分析
  进入 4-Skill 闭环。

**第一层质量过滤/确认（2026-11-21 新增，QCC免费积分主、Tavily廉价兜底，零落库）**
- `risk_screen(company_name, uscc?)`：负面风险筛（**排除层**）。红旗（失信/被执行/限高/破产/清算/严重违法/吊销/刑事）、
  黄旗（行政处罚/经营异常/税收违法/欠税/股权冻结/涉诉）。优先企查查 risk `get_company_risk_scan`（免费积分，结构化权威）；
  积分不足 402 降级 Tavily 搜信用中国/执行信息/裁判公开记录（廉价，verified=false）。返回 red_flags/yellow_flags/clean
  + 来源 + source_mode。**red≥1 建议海选剔除**。用于各 screen_* 候选选定前质量过滤。
- `qualify_company(company_name, uscc?, with_contact?)`：资质核实+触达补全（**确认层**）。核实专精特新/小巨人/高新/
  科技型中小/单项冠军/瞪羚/企业技术中心/荣誉榜单。优先企查查 operation `get_qualifications`+`get_honor_info`（免费积分）
  + company `get_contact_info`（联系方式）；402 降级 Tavily 搜官方公示名单。返回 qualification_signals/is_qualified
  + contact_clues（电话/邮箱/官网，触达用）+ 来源 + source_mode。用于小微候选优质度确认与触达（接功能一）。
- 降级机制：QCC 积分耗尽（402）时自动切 Tavily，**现在即可用**；积分恢复后自动回 QCC 结构化口径（verified=true）。
  免费公共 gov 直连（信用中国/执行信息/科技型中小网）从本服务器不可达（412反爬/502/拒连），故走 QCC/Tavily 间接口径。

**功能一 · 潜客侦察（底稿前）**
- `discover_prospects(domain?, grade?, min_score?, limit?, sort_by?)`：免费公开口径粗筛名单（62家）
  按优质度/潜力排序。sort_by∈total_score/prescore(PQI)/rev_growth。零成本。
- `get_prospect_pack(company_key, enrich=false)`：公开证据包 + 公开触达渠道 + skill5_prospect_scout 量表。
  渠道：粗筛官网(high)/注册地址(medium)/本人可见底稿电话邮箱URL线索(medium)，全 verified=false。
  enrich=true 走既有网关天眼查1+Tavily搜索1+全文1封顶（≤2分钟，失败降级）。附5类底稿索取清单+合规红线。
- `create_project(name, company_key, company_name?)`：MCP侧建项目（owner=调用者，每账户≤30）。

**分析（功能二入口）**
- `analyze_company(company_key, domain?, stage?, company_name?)`：引擎三层分析
  （OECD PQI + 创新积分2.0 + 硬排雷 + 漏斗 + WeKnora 事实桥）。名单内带粗筛财务口径；
  任意企业（18位USCC）只要有本人可见底稿即可分析（dossier 模式）。
- `get_latest_analysis`：最近一次本人可见运行结果（含持久化综合分 final/semantic_score/fact_score）。

**团队协同（项目=团队×企业）**
- `list_my_projects` / `add_project_member` / `search_project_facts`（项目专属库+平台公共库合并检索）
- `project_memory_add` / `project_memory_search`（Memory Core 127.0.0.1:8420，一项目一记忆服务
  `tc-prj-{project_id}`，service级隔离；检索为关键词/混合匹配，查询词须与内容字面相关）

**功能二 · 服务闭环**
1. `upload_dossier(company_key, files_json, company_name?, domain?, stage?, project_id?, analyze=true)`
   - kind∈patent/financial/bid/judicial/company/risk/web/other；≤6份×2MB，.md/.txt/.json。
   - 双落库 SQLite（幂等Source_ID可挂project_id）+ WeKnora；结构化专利JSON著录项并入payload跑PQI。
2. `get_context_pack(run_id, skill?)`：4个Skill上下文包内容（weknora_facts+allowed_source_ids白名单）
   + 指定skill附SKILL.md量表；run快照隔离；上游通过后bank_report包回填语义分。
3. `submit_skill_outputs(run_id, outputs_json)`：白名单→Pydantic强校验→Source_ID伪造拦截→G-Eval归一
   →持久熔断（连续3次失败终止）→引擎唯一评分口径（硬55%+语义35%−风险/否决）→综合分持久化到运行JSON。

## 对方 Agent 推荐调用序列

```
漏斗①便宜海选（按目标企业规模选底池）：
上市/硬科技：screen_market(domain=..., min_growth=..., limit=50) → 潜客榜（零成本）
小微·有中标：screen_sme(keyword="电池,模具,精密", region_code="3302", min_bids=1) → 小微榜（免费，含USCC）
小微·资质/网络：screen_sme_web(query="宁波 精密模具 专精特新 小巨人 企业") → 资质公示小微线索（Tavily廉价）
拟上市/优质成长：screen_ipo(board="北交所", province="浙江") → 创新型中小微榜（免费，含招股书PDF底稿）
  → 客户选定几家

质量过滤/确认（选定前，QCC积分主+Tavily兜底）：
risk_screen(company_name) → 红黄旗标，red≥1建议剔除
qualify_company(company_name) → 专精特新/高新资质确认 + 联系方式触达

漏斗②贵复查（选定后）：
deep_verify(company_name, stock_code或uscc, confirm=false) → 成本预估+剑鱼余额+配额
deep_verify(..., confirm=true, project_id=...) → job_id（异步贵抓取；小微附uscc绕过天眼查解析）
get_deep_verify(job_id) 轮询 → done: 筛选预分析（默认暂存口径，不入正式SQL/RAG）
客户明确表述要入库后：persist_screening_result(company_key, confirm=true) → 提升落库+入RAG → analyze_company 重跑正式分析
（或发起贵复查时客户已明确要入库：deep_verify(..., persist=true) → get_deep_verify done 即可接闭环）

潜客侦察（可选，底稿前触达）：
discover_prospects / get_prospect_pack(enrich=true) → 触达方案 → create_project + project_memory_add

功能二底稿分析闭环（贵复查run 或 企业上传底稿）：
upload_dossier(company_key, files_json, project_id=...) → run_id（企业主动给底稿时）
get_context_pack(run_id, skill="patent_review") → 包+量表 → 跑Skill1
get_context_pack(run_id, skill="commercialization_fit") → 跑Skill2
get_context_pack(run_id, skill="anti_packaging_factcheck") → 跑Skill3
submit_skill_outputs(run_id, {skill1,skill2,skill3}) → 校验+语义分
get_context_pack(run_id, skill="bank_report") → 已回填语义分 → 跑Skill4
submit_skill_outputs(run_id, {bank_report}) → 综合信用分+报告，闭环完成
```

## 鉴权与可见性边界

- **平台抓取数据公共口径**：网关贵抓底稿（provider≠upload，如 PAT-ZHY/COR-TYC）按平台一贯设计
  对全员可见（与既有缓存专利一致，任何人可独立 analyze_company）。`projects.VISIBLE_SQL` 仅对
  provider='upload' 的上传底稿按项目成员隔离。
- **团队隔离的是评估成果**：deep_verify/upload_dossier 挂 project_id 后，运行结果/上下文包/作业/
  项目记忆按 `projects.run_visible` + 成员鉴权团队隔离（他人团队 run/packs/job 不可见）。
- 上传底稿挂项目须成员；不挂项目=平台公共层。校验白名单=平台公共+本人项目底稿，跨项目Source_ID按伪造拦截。
- prospect 包：公开粗筛全员可见；底稿线索/可见证据量/最近运行按本人可见性过滤。

## 自测

`scripts/selftest_auth.py`（种子/清理自测账户）+ 本地归档：
- `loop_selftest.py`：功能二全闭环。2026-11-21 全过：综合分20.48/D，语义73.75，fact_score 0.5。
- `prospect_selftest.py`：功能一+二联测。全过：enrich三源全通渠道14→28；底稿线索全提取；隔离全拒；CATL回归不变。
- `funnel_selftest.py`：两段漏斗。海选11534→2430硬科技→2429过滤零成本排序全对；成本闸门预估+剑鱼余额；
  贵复查震裕科技(300953)真实异步抓取17源6供方双落库挂项目→引擎61.88/C→专利Skill冒烟34.85/D→作业/取包隔离全拒→配额计数。
  专利全深度展平器离线验证（abstract/ipc4=['B21D','B30B']/claim_1独权剥HTML→引擎归一化消费，PQI不再空转）。
  注：复测时天眼查MCP返回402（配额被并行会话当日密集调用耗尽），贵复查live复跑待配额恢复；已加uscc直传绕过解析。
- **小微海选（2026-11-21 新增，实测全过，零落库）**：
  - `screen_sme`：宁波"电池,模具,精密"扫138家企业→region_code=3302+min_bids≥1筛出5家典型小微
    （宁波敦方精密机械中标48/2007年/法人孔协群、宁海县第一注塑模具中标12等），USCC直出，零成本。
  - `screen_sme_web`：Tavily"宁波 精密模具 专精特新 小巨人 企业"→6条线索，命中**国家级专精特新小巨人公示名单**
    （宁波望通锁业/力品格工业机械/菲仕电机技术/华平智控科技/天安智能电网/澳玛特高精冲压机床等），
    资质关键词全中、企业名正则收紧后无散文噪声；成本 search 1+extract 1（廉价）。
  - `deep_verify(uscc=...)` 预估口确认 uscc 回显、配额计数正确（confirm=false 零扣费）。
- **拟上市海选 screen_ipo（2026-11-21 新增，实测全过，零落库）**：申报池3915家/辅导池5390家免费缓存；
  北交所+浙江筛出74家活跃创新型中小微（宁波赛维达/舒友仪器/帅特龙汽车/陌桑高科/晨泰科技，全带招股书PDF）；
  辅导+北京316家（五一视界数字孪生/辉羲科技/高拓讯达微电子）；板块分布/状态过滤/剔终止中止正确；招股书PDF免费可达（HTTP 200/%PDF-）。
- **质量过滤/确认 risk_screen + qualify_company（2026-11-21 新增，实测全过，零落库）**：QCC积分402自动降级Tavily；
  qualify_company(宁波菲仕电机技术)命中专精特新/小巨人+2条联系方式；risk_screen同企业无负面旗标(clean)；
  source_mode/降级警告/verified标记正确。QCC积分恢复后自动回结构化口径。

## 第一层数据源评估（2026-11-21）

按"价值×成本×覆盖科技小微"排序，已接入与候选：

| 数据源 | 覆盖 | 成本 | 状态 | 价值点 |
|---|---|---|---|---|
| 东财IPO申报/辅导池 | 拟上市优质成长中小微（北交所860家创新型中小） | 免费 | **已接入 screen_ipo** | 已过监管scrutiny；招股书=免费完整底稿，省贵复查 |
| GGZY中标主体库 | 有中标的科技小微 | 免费无Key | 已接入 screen_sme | 中标数=商业化信号，USCC直出 |
| Tavily网络搜索 | 资质公示/网络可见小微 | 廉价付费 | 已接入 screen_sme_web | 专精特新/小巨人名单、联系方式 |
| 全A股业绩报表 | 上市硬科技 | 免费缓存 | 已接入 screen_market | 财务增速/ROE排序 |
| 官方资质名单（专精特新/科技型中小/高新） | 官方预认定科技小微 | 免费/积分 | **已接入 qualify_company** | QCC资质/荣誉主+Tavily公示名单兜底；gov直连不可达走间接口径 |
| 企查查免费积分（risk/qualification/contact） | 工商全量含小微 | 免费积分（当前402耗尽） | **已接入 risk_screen/qualify_company** | 结构化权威；积分不足自动降级Tavily，恢复后回QCC |
| 负面风险筛（失信/被执行/处罚/经营异常） | 负面排除 | 免费/积分 | **已接入 risk_screen** | 红黄旗标排除层；QCC risk主+Tavily兜底 |
| 上市公司供应链客户/供应商 | 上市年报披露的中小微合作方 | 免费 | 受阻（akshare无接口） | 需解析年报PDF附注，便宜层暂不可行 |
| CNIPR专利申请人聚合 | 有专利的科技小微 | 配额 | **不可用（403 Forbidden）** | 直接喂PQI核心，但凭证/配额受阻；QCC ipr.get_patent_info可替代（积分） |
| 新三板/区域股权 | 较小挂牌企业 | 免费 | 受阻（akshare 1.18.88无接口） | 需股转系统公开抓取，IPO池已更优覆盖 |

## 运维

- 重启：`systemctl restart techcredit-web techcredit-mcp`
- 审计：`data/mcp_audit.log`；贵复查作业日志 `data/deep_verify/{job_id}.log`
- 备份：`_backup_20261121/mcp_server.py.bak-before-loop`、`.bak-before-prospect`、`.bak-before-funnel`、
  `.bak-before-sme`、`dossier_pipeline.py.bak-before-loop`
- Skill 目录：`skills/skill1_patent_review` … `skill5_prospect_scout`
- 海选缓存刷新：业绩报表 `screening/screen_companies.py`；IPO池 `.venv/bin/python scripts/fetch_ipo_pool.py`（东财免费，建议周期性刷新）。
- 外部配额：天眼查/智慧芽/剑鱼/企查查为付费或积分制，Tavily廉价付费，并行会话共用凭证；当前天眼查与企查查积分均402耗尽（并行会话当日密集调用），
  risk_screen/qualify_company 自动降级Tavily可用，deep_verify 可附uscc绕过天眼查解析；剑鱼余额可经 deep_verify(confirm=false) 查看。
  免费公共gov直连（信用中国/执行信息/科技型中小网）从本服务器不可达（412反爬/502/拒连），负面/资质走QCC/Tavily间接口径。
- 并行会话协同：mcp_server.py 由多会话增量共建，改动前务必以服务器现行版为基线合并（勿盲目覆盖），
  数据落库边界（暂存区/ persist 显式提升）为共享约定。