#!/usr/bin/env python3
"""Write competition plan into tdai memory and verify by search."""
import asyncio, sys
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

def load_env(path):
    env = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env

async def main():
    content = Path("/root/projects/techcredit/docs/competition_plan.txt").read_text()
    env = load_env("/root/tdai-memory-mcp/mcp.env")
    headers = {"Authorization": "Bearer " + env["MCP_SERVER_AUTH_TOKEN"]}
    async with streamablehttp_client("http://127.0.0.1:8790/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("memory_add", {
                "content": content,
                "role": "user",
                "session_id": "techcredit-competition",
            })
            print("ADD:", res.content[0].text[:300])
            res2 = await s.call_tool("memory_search", {
                "query": "企业技术信用评估系统 比赛 架构",
                "session_id": "techcredit-competition",
            })
            print("SEARCH:", res2.content[0].text[:400])

asyncio.run(main())