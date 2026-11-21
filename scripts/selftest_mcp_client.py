#!/usr/bin/env python3
"""MCP 正例自测：Bearer=FateBuddy 接入码 → initialize/list_tools/search/analyze。"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    url, tok = sys.argv[1], sys.argv[2]
    async with streamablehttp_client(url, headers={"Authorization": "Bearer " + tok}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])
            res = await s.call_tool("search_companies", {"query": "宁德"})
            print("SEARCH:", res.content[0].text[:260])
            res = await s.call_tool("analyze_company", {"company_key": "91350900587527783P"})
            d = json.loads(res.content[0].text)
            pqi = (d.get("pqi") or {}).get("composite")
            inn = d.get("innovation") or {}
            print("ANALYZE ok:", d.get("ok"), "| pqi:", pqi, "| score:", inn.get("core_score"), inn.get("grade"),
                  "| flags:", len(d.get("risk_flags") or []), "| funnel:", len((d.get("funnel") or {}).get("core_patents") or []))


asyncio.run(main())