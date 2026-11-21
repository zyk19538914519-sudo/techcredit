#!/usr/bin/env python3
"""统一数据网关：智慧芽 / 天眼查 / 剑鱼标讯 / 网页搜索·全文抓取 / 元典（默认停用）。

仅用标准库。所有出站请求带超时与有限重试；凭证只从受限权限文件/环境读取，
绝不写入落盘数据与日志。MCP 走 Streamable HTTP（兼容 SSE 与纯 JSON 两种响应）。
"""
from __future__ import annotations
import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path

import config


class GatewayError(RuntimeError):
    pass


def _unwrap(obj):
    """逐层剥开 JSON-RPC / MCP content 包装，取最内层业务 JSON。"""
    for _ in range(8):
        if not isinstance(obj, dict):
            return obj
        if isinstance(obj.get("structuredContent"), dict):
            return obj["structuredContent"]
        if isinstance(obj.get("result"), dict):
            obj = obj["result"]
            continue
        content = obj.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = content[0].get("text")
            if text:
                try:
                    obj = json.loads(text)
                    continue
                except (ValueError, TypeError):
                    return {"text": text}
        return obj
    return obj


def _parse_mcp_body(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise GatewayError(f"MCP 响应无法解析: {raw[:200]}")


class _HttpMcp:
    """Streamable HTTP MCP 客户端基类。session=True 时先 initialize 拿会话 ID。"""

    def __init__(self, url: str, headers: dict, session: bool = False, timeout: int = 90):
        self.url = url
        self.headers = dict(headers)
        self.headers.setdefault("Content-Type", "application/json")
        self.headers.setdefault("Accept", "application/json, text/event-stream")
        self.session = session
        self.timeout = timeout
        self._sid = None
        self._id = 0
        self._ready = False

    def _post(self, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST")
        for k, v in self.headers.items():
            req.add_header(k, v)
        if self._sid:
            req.add_header("mcp-session-id", self._sid)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._sid = sid
            raw = resp.read().decode("utf-8", "replace")
        return _parse_mcp_body(raw)

    def _ensure_ready(self):
        if self._ready or not self.session:
            self._ready = True
            return
        env = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "techcredit-gateway", "version": "1.0"}}})
        if env.get("error"):
            raise GatewayError(f"MCP initialize 失败: {env['error']}")
        self._ready = True

    def call(self, tool: str, arguments: dict, retries: int = 2) -> dict:
        last_err = None
        for attempt in range(retries + 1):
            try:
                self._ensure_ready()
                self._id += 1
                env = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                                  "params": {"name": tool, "arguments": arguments}})
                if env.get("error"):
                    raise GatewayError(f"MCP 错误: {env['error']}")
                result = env.get("result", {})
                if result.get("isError"):
                    raise GatewayError(f"MCP 工具报错: {str(result)[:300]}")
                return _unwrap(result)
            except GatewayError:
                raise
            except Exception as exc:
                last_err = exc
                time.sleep(2 * (attempt + 1))
        raise GatewayError(f"MCP 请求失败: {last_err}")


class PatsnapClient(_HttpMcp):
    """智慧芽全球专利库 MCP（Bearer Key，无会话直调）。"""

    def __init__(self):
        if not config.PATSNAP_KEY_FILE.exists():
            raise GatewayError(f"智慧芽 Key 文件不存在: {config.PATSNAP_KEY_FILE}")
        key = config.PATSNAP_KEY_FILE.read_text().strip()
        super().__init__(config.PATSNAP_MCP_URL,
                         {"Authorization": "Bearer " + key,
                          "mcp-protocol-version": "2025-11-25"}, session=False)

    def search_patents(self, query_text: str, limit: int = 20, offset: int = 0) -> dict:
        return self.call("search_patents", {"query_text": query_text, "limit": limit, "offset": offset})

    def bibliography(self, patent_id: str) -> dict:
        return self.call("bibliography", {"patent_id": patent_id})

    def claims(self, patent_id: str) -> dict:
        return self.call("claims", {"patent_id": patent_id})

    def family(self, patent_id: str) -> dict:
        return self.call("family", {"patent_id": patent_id})

    def legal_status(self, patent_id: str) -> dict:
        return self.call("get_patent_legal_status", {"patent_id": patent_id})

    def invalidation(self, patent_id: str) -> dict:
        return self.call("reexamination_invalidation", {"patent_id": patent_id})


class TianyanchaClient(_HttpMcp):
    """天眼查 MCP（需会话；限流约 10 次/分钟，调用方应控制节奏）。"""

    def __init__(self):
        if not config.TYC_TOKEN:
            raise GatewayError("天眼查凭证缺失：请配置 TYC_TOKEN 或 /root/.tyc_mcp_key")
        super().__init__(config.TYC_MCP_URL,
                         {"Authorization": config.TYC_TOKEN}, session=True)

    def search_companies(self, query: str, page_size: int = 5) -> dict:
        return self.call("search_companies", {"query": query, "page_size": page_size})

    def basic_profile(self, company_name: str) -> dict:
        return self.call("get_company_basic_profile", {"company_name": company_name})

    def capabilities(self, company_name: str, company_id: str = "") -> dict:
        args = {"company_name": company_name}
        if company_id:
            args["company_id"] = company_id
        return self.call("get_company_capabilities", args)

    def call_tool(self, tool_name: str, company_name: str, arguments: dict | None = None) -> dict:
        return self.call("call_tool", {"tool_name": tool_name, "company_name": company_name,
                                       "arguments": arguments or {"page": 1, "page_size": 20}})


class JianYuClient(_HttpMcp):
    """剑鱼标讯 MCP（无会话，Jianyu-Api-Key 头；search/detail 消耗剑鱼币）。"""

    def __init__(self):
        if not config.JIANYU_API_KEY:
            raise GatewayError("剑鱼凭证缺失：请配置 JIANYU_API_KEY 或 /root/.jianyu_mcp_key")
        super().__init__(config.JIANYU_MCP_URL,
                         {"Content-Type": "application/json; charset=utf-8",
                          "Jianyu-Api-Key": config.JIANYU_API_KEY}, session=False)

    def balance(self) -> dict:
        return self.call("usage_balance", {})

    def search_bidding(self, **kw) -> dict:
        return self.call("search_bidding", kw)

    def bidding_detail(self, bid_id: str) -> dict:
        return self.call("get_bidding_detail", {"id": bid_id})


class TavilyClient:
    """Tavily REST：search 定位官网/新闻稿，extract 批量抓全文。"""

    BASE = "https://api.tavily.com"

    def __init__(self):
        if not config.TAVILY_API_KEY:
            raise GatewayError("Tavily 凭证缺失：请配置 TAVILY_API_KEY 或 /root/.tavily_key")
        self.key = config.TAVILY_API_KEY

    def _post(self, path: str, body: dict, timeout: int = 60) -> dict:
        body = dict(body, api_key=self.key)
        req = urllib.request.Request(self.BASE + path, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def search(self, query: str, max_results: int = 5) -> dict:
        return self._post("/search", {"query": query, "max_results": max_results,
                                      "search_depth": "basic", "include_answer": False})

    def extract(self, urls: list) -> dict:
        return self._post("/extract", {"urls": urls[:20]}, timeout=90)


class FirecrawlClient:
    """Firecrawl REST：单页 scrape 抓 Markdown 全文（Tavily extract 的备选）。"""

    BASE = "https://api.firecrawl.dev/v1"

    def __init__(self):
        if not config.FIRECRAWL_API_KEY:
            raise GatewayError("Firecrawl 凭证缺失：请配置 FIRECRAWL_API_KEY 或 /root/.firecrawl_key")
        self.key = config.FIRECRAWL_API_KEY

    def scrape(self, url: str, timeout: int = 90) -> dict:
        req = urllib.request.Request(self.BASE + "/scrape",
                                     data=json.dumps({"url": url, "formats": ["markdown"]}).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))


class GgzyClient:
    """全国公共资源交易平台·数据服务（官方免费无Key）。

    /yjcx/index/* 无需登录：search(主体+中标数)/bid_list(中标)/bid_show(详情)/
    black_list_info(交易黑名单)/qualient(资质)。覆盖2019至今全国中标成交。
    """

    def _get(self, ep: str, **params) -> dict:
        url = f"{config.GGZY_BASE}/{ep}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Chrome/126"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            env = json.loads(resp.read().decode("utf-8", "replace"))
        if not env.get("success"):
            raise GatewayError(f"ggzy {ep}: {env.get('message')}")
        return env

    def search_company(self, keyword: str, page: int = 1, size: int = 10) -> dict:
        return self._get("search", keyword=keyword, pageIndex=page, pageSize=size)

    def bid_list(self, uniscid: str, page: int = 1, size: int = 10) -> dict:
        return self._get("bid_list", uniscid=uniscid, pageIndex=page, pageSize=size)

    def bid_show(self, deal_id: str) -> dict:
        return self._get("bid_show", id=deal_id)

    def black_list(self, uniscid: str) -> dict:
        return self._get("black_list_info", uniscid=uniscid)

    def qualient(self, uniscid: str, page: int = 1, size: int = 10) -> dict:
        return self._get("qualient", uniscid=uniscid, pageIndex=page, pageSize=size)


class QccClient:
    """企查查 MCP（智能体平台，免费积分）。六服务：company/risk/case/tender/ipr/operation。

    一次覆盖工商/股东/年报/财务/处罚/失信/被执行/裁判/中标/布图/软著/标准/资质/荣誉/榜单/融资/招聘。
    """

    def __init__(self):
        if not config.QCC_API_KEY:
            raise GatewayError("企查查凭证缺失：请配置 QCC_API_KEY 或 /root/.qcc_mcp_key")
        self._clients = {}

    def _svc(self, name: str) -> _HttpMcp:
        if name not in self._clients:
            self._clients[name] = _HttpMcp(f"{config.QCC_MCP_BASE}/{name}/stream",
                                           {"Authorization": "Bearer " + config.QCC_API_KEY},
                                           session=True)
        return self._clients[name]

    def call(self, svc: str, tool: str, **args) -> dict:
        return self._svc(svc).call(tool, args)


class CniprClient(_HttpMcp):
    """CNIPR 开放平台 MCP（知识产权出版社；Bearer sk- Key，需会话）。

    覆盖集成电路布图/植物新品种/专利交叉校验。注意：平台按接口配额授权，
    未开通时返回 code 11906 quota_exceeded，需控制台配置访问次数。
    """

    def __init__(self):
        if not config.CNIPR_API_KEY:
            raise GatewayError("CNIPR 凭证缺失：请配置 CNIPR_API_KEY 或 /root/.cnipr_mcp_key")
        super().__init__(config.CNIPR_MCP_URL,
                         {"Authorization": "Bearer " + config.CNIPR_API_KEY}, session=True)

    def list_tools(self) -> dict:
        self._ensure_ready()
        return self._post({"jsonrpc": "2.0", "id": 99, "method": "tools/list"})

    def call_tool(self, tool: str, arguments: dict) -> dict:
        return self.call(tool, arguments)


class CniprRestClient:
    """CNIPR REST（OAuth2）。覆盖布图 lo1/植物 pv1/标准 co1/版权 ca1/裁判 ve1/专利 sf1。

    鉴权需 client_id + openid + access_token（sk- MCP Key 不适用，实测 REST 返回 1602 clientId错误）。
    优先用现成 openid+access_token；否则用账号密码走 Password Grant 换 token。
    """

    def __init__(self):
        if not config.CNIPR_CLIENT_ID:
            raise GatewayError("CNIPR REST 缺 client_id：请控制台创建应用后配置 CNIPR_CLIENT_ID")
        self.client_id = config.CNIPR_CLIENT_ID
        self._openid = config.CNIPR_OPENID
        self._token = config.CNIPR_ACCESS_TOKEN

    def _ensure_token(self):
        if self._openid and self._token:
            return
        if not (config.CNIPR_USERNAME and config.CNIPR_PASSWORD and config.CNIPR_CLIENT_SECRET):
            raise GatewayError("CNIPR REST 缺 openid+access_token 或 账号密码+client_secret")
        body = urllib.parse.urlencode({
            "grant_type": "password", "client_id": self.client_id,
            "client_secret": config.CNIPR_CLIENT_SECRET,
            "username": config.CNIPR_USERNAME, "password": config.CNIPR_PASSWORD}).encode()
        req = urllib.request.Request(config.CNIPR_OAUTH_TOKEN_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=60) as resp:
            env = json.loads(resp.read().decode("utf-8", "replace"))
        self._token = env.get("access_token") or ""
        self._openid = env.get("openid") or self._openid or ""
        if not self._token:
            raise GatewayError(f"CNIPR OAuth 换 token 失败: {str(env)[:200]}")

    def search(self, api: str, exp: str, size: int = 20, frm: int = 0, dbs: str = None) -> dict:
        self._ensure_token()
        group = "search" if api in ("sf1", "sf28") else "cocave"
        url = f"{config.CNIPR_REST_BASE}/v1/api/{group}/{api}/{self.client_id}"
        form = {"openid": self._openid, "access_token": self._token,
                "exp": exp, "from": frm, "size": size, "queryType": 2}
        if dbs:
            form["dbs"] = dbs
        req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def ic_design(self, exp, **kw): return self.search("lo1", exp, **kw)
    def plant(self, exp, **kw): return self.search("pv1", exp, **kw)
    def standard(self, exp, **kw): return self.search("co1", exp, **kw)
    def copyright(self, exp, **kw): return self.search("ca1", exp, **kw)
    def judgment(self, exp, **kw): return self.search("ve1", exp, **kw)
    def patent(self, exp, dbs="FMZL,FMSQ,WGZL,SYXX", **kw): return self.search("sf1", exp, dbs=dbs, **kw)


class AkshareClient:
    """AKShare 财务（东财/新浪/巨潮公开接口，免费无 Key）。

    在独立 venv 子进程运行 fin_akshare.py，避免 pandas 依赖污染网关主进程；
    覆盖创新积分2.0 的研发费用/占比、主营收入、营收增速、净资产利润率。
    """

    def __init__(self):
        self.py = config.AKSHARE_PYTHON
        if not Path(self.py).exists():
            raise GatewayError(f"AKShare venv 不存在: {self.py}（请先 python3 -m venv .venv && pip install akshare）")

    def financial(self, code: str = None, name: str = None, years: int = 4, timeout: int = 240) -> dict:
        cmd = [self.py, str(config.FIN_HELPER), "--years", str(years)]
        if code:
            cmd += ["--code", code]
        if name:
            cmd += ["--name", name]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise GatewayError(f"AKShare 子进程失败: {proc.stderr[-300:]}")
        try:
            return json.loads(proc.stdout)
        except ValueError:
            raise GatewayError(f"AKShare 输出非 JSON: {proc.stdout[:200]}")


class YuandianClient:
    """元典企业/司法数据：按用户要求默认停用（2026-11-21）。代码保留备用。"""

    def __init__(self, bridge: str = None):
        if os.environ.get("TECHCREDIT_ENABLE_YUANDIAN") != "1":
            raise GatewayError("元典数据源已按用户要求停用；如确需启用请设置 TECHCREDIT_ENABLE_YUANDIAN=1")
        self.bridge = bridge or config.YUANDIAN_COMPANY_BRIDGE

    def call_many(self, calls: list, timeout: int = 150) -> list:
        lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "techcredit-gateway", "version": "1.0"}}})]
        for i, (tool, args) in enumerate(calls, start=1):
            lines.append(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                     "params": {"name": tool, "arguments": args}}))
        proc = subprocess.run([config.YUANDIAN_PYTHON, self.bridge],
                              input=("\n".join(lines) + "\n").encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        results = {}
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                env = json.loads(line)
            except ValueError:
                continue
            if isinstance(env.get("id"), int) and env["id"] >= 1:
                results[env["id"]] = ({"_error": env["error"]} if env.get("error")
                                      else _unwrap(env.get("result", {})))
        return [results.get(i, {"_error": f"元典无响应: {t}"}) for i, (t, _) in enumerate(calls, start=1)]

    def call(self, tool: str, arguments: dict) -> dict:
        return self.call_many([(tool, arguments)])[0]


# 企查查仍为占位（无凭证）；其余源已实现真实抓取。
PLACEHOLDER_CREDENTIALS = {"qcc": "QCC_TOKEN"}


def placeholder_status(provider: str) -> bool:
    env_name = PLACEHOLDER_CREDENTIALS.get(provider)
    return bool(env_name and os.environ.get(env_name))