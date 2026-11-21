#!/usr/bin/env python3
"""底稿渲染：每条 Source_ID 生成一份可入库 WeKnora 的 Markdown 底稿。

底稿头部固定携带溯源元数据（Source_ID / 数据商 / 自然主键 / 抓取时间 / 版本），
正文为人类可读字段 + 原始 JSON 附录，保证 Agent 引用可回溯到原始数据包。
"""
from __future__ import annotations
import json
from pathlib import Path

import config
import store

KIND_CN = {"patent": "专利", "bid": "中标公告", "judicial": "司法记录",
           "company": "工商登记", "penalty": "行政处罚", "risk": "经营风险", "web": "官网/新闻稿",
           "financial": "财务指标", "ipx": "其他知识产权(布图/植物新品种)",
           "standard": "标准制定", "cert": "科技资质", "award": "荣誉奖励",
           "fund": "融资记录", "rank": "行业榜单", "hr": "招聘结构"}
PROVIDER_CN = {"zhihuiya": "智慧芽", "yuandian": "元典（open.chineselaw.com）",
               "tianyancha": "天眼查", "jianyu": "剑鱼标讯", "tavily": "Tavily",
               "firecrawl": "Firecrawl", "metaso": "秘塔搜索", "mx": "东方财富妙想", "qcc": "企查查",
               "akshare": "AKShare（东财/新浪/巨潮公开接口）", "cnipr": "CNIPR开放平台",
               "ggzy": "全国公共资源交易平台", "cninfo": "巨潮资讯年报"}


def render(conn, source_id: str) -> Path:
    row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
    if row is None:
        raise KeyError(source_id)
    envelope = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))
    payload = envelope["payload"]
    cdir = config.DOSSIER_DIR / row["company_key"]
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / f"{source_id}.md"
    lines = [
        f"# 底稿 {source_id}｜{KIND_CN.get(row['kind'], row['kind'])}｜{row['title'] or row['natural_key']}",
        "",
        "## 溯源信息",
        f"- Source_ID: `{source_id}`",
        f"- 企业: {row['company_name']}（{row['company_key']}）",
        f"- 数据商: {PROVIDER_CN.get(row['provider'], row['provider'])}",
        f"- 记录类型: {KIND_CN.get(row['kind'], row['kind'])}",
        f"- 自然主键: `{row['natural_key']}`",
        f"- 首次抓取: {row['first_fetched_at']}｜最近抓取: {row['last_fetched_at']}｜版本: v{row['version']}",
        f"- 原始数据包: `{row['raw_path']}`",
        "",
        "## 记录内容",
        "```json",
        json.dumps(payload, ensure_ascii=False, indent=1),
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    conn.execute("UPDATE sources SET dossier_path=? WHERE source_id=?", (str(path), source_id))
    conn.commit()
    return path


def render_all(conn, company_key: str) -> int:
    n = 0
    for row in store.list_sources(conn, company_key=company_key):
        render(conn, row["source_id"])
        n += 1
    return n