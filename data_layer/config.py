#!/usr/bin/env python3
"""techcredit 数据层全局配置。所有路径均指向云端服务器，不写本地开发机。

凭证优先级：环境变量 > 受限权限 Key 文件（600）。绝不把凭证明文写入落盘数据/日志。
"""
from __future__ import annotations
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent          # /root/projects/techcredit
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"                                    # 原始 JSON 落盘
DOSSIER_DIR = DATA_DIR / "dossiers"                           # 底稿 Markdown
DB_PATH = DATA_DIR / "techcredit.db"                          # SQLite 缓存索引

# ---------- 前期筛选阶段暂存区（数据落库边界） ----------
# 两段漏斗（①便宜海选 screen_market ②贵数据复查 deep_verify）产生的企业数据
# 默认只进下面的暂存区，与正式证据库（techcredit.db 的 sources 表）物理隔离，
# 也绝不进入 WeKnora 底稿 RAG；只有客户明确表述要入库时，才经 persist 流程
# （deep_verify(persist=true) 或 persist_screening_result）提升到正式库与 RAG。
STAGING_DIR = DATA_DIR / "screening_staging"
STAGING_DB_PATH = STAGING_DIR / "staging.db"                   # 筛选暂存索引库
STAGING_RAW_DIR = STAGING_DIR / "raw"                          # 暂存原始 JSON
STAGING_DOSSIER_DIR = STAGING_DIR / "dossiers"                 # 暂存底稿（仅供预览，不入 RAG）
STAGING_CONTEXT_DIR = STAGING_DIR / "context"                 # 暂存上下文包


def _secret(env_name: str, *file_candidates: str) -> str:
    """环境变量优先，其次按顺序读 Key 文件；支持天眼查 config.json 形式。"""
    v = os.environ.get(env_name, "")
    if v:
        return v.strip()
    for cand in file_candidates:
        p = Path(cand)
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8").strip()
        if cand.endswith(".json"):
            try:
                return json.loads(txt).get("headers", {}).get("Authorization", "").strip()
            except ValueError:
                continue
        if txt:
            return txt
    return ""


# 智慧芽 MCP（Bearer Key 直连，Key 文件权限 600）
PATSNAP_KEY_FILE = Path(os.environ.get("PATSNAP_KEY_FILE", "/root/.patsnap_mcp_key"))
PATSNAP_MCP_URL = os.environ.get("PATSNAP_MCP_URL", "https://connect.zhihuiya.com/1458a4/mcp")

# 元典企业/司法数据（复用 Hermes 的 stdio<->SSE 桥接脚本；默认停用）
YUANDIAN_COMPANY_BRIDGE = os.environ.get(
    "YUANDIAN_COMPANY_BRIDGE",
    "/root/.hermes/profiles/hermsm2/mcp_servers/yuandian_company_mcp.py")
YUANDIAN_PYTHON = os.environ.get("YUANDIAN_PYTHON", "/usr/bin/python3")

# 天眼查 MCP（Streamable HTTP，需会话；Key 为 mcpk2_ 开头）
TYC_TOKEN = _secret("TYC_TOKEN", "/root/.tyc_mcp_key", "/root/.tyc/config.json")
TYC_MCP_URL = os.environ.get("TYC_MCP_URL", "https://mcp.tianyancha.com/v1")

# 剑鱼标讯 MCP（中标/招标正文与详情、拟在建项目；无会话，Jianyu-Api-Key 头）
JIANYU_KEY_FILE = Path(os.environ.get("JIANYU_KEY_FILE", "/root/.jianyu_mcp_key"))
JIANYU_MCP_URL = os.environ.get("JIANYU_MCP_URL", "https://jyaiopen.jianyu360.cn/mcp")
JIANYU_API_KEY = _secret("JIANYU_API_KEY", str(JIANYU_KEY_FILE))

# 网页搜索 / 全文抓取（反包装核查素材）
TAVILY_API_KEY = _secret("TAVILY_API_KEY", "/root/.tavily_key")
FIRECRAWL_API_KEY = _secret("FIRECRAWL_API_KEY", "/root/.firecrawl_key")
METASO_API_KEY = _secret("METASO_API_KEY", "/root/.metaso_key")
MX_APIKEY = _secret("MX_APIKEY", "/root/.mx_key")

# AKShare 财务（免费公开接口，跑在独立 venv，helper 仅 stdout JSON）
AKSHARE_PYTHON = os.environ.get("AKSHARE_PYTHON", str(PROJECT_ROOT / ".venv" / "bin" / "python"))
FIN_HELPER = Path(__file__).resolve().parent / "fin_akshare.py"

# 企查查 MCP（智能体平台，Bearer Key；company/risk/case/tender/ipr/operation 六服务）
QCC_KEY_FILE = Path(os.environ.get("QCC_KEY_FILE", "/root/.qcc_mcp_key"))
QCC_MCP_BASE = os.environ.get("QCC_MCP_BASE", "https://agent.qcc.com/mcp")
QCC_API_KEY = _secret("QCC_API_KEY", str(QCC_KEY_FILE))

# 全国公共资源交易平台·数据服务（免费无Key，2019至今中标/黑名单/资质）
GGZY_BASE = os.environ.get("GGZY_BASE", "https://data.ggzy.gov.cn/yjcx/index")

# CNIPR 开放平台 MCP（Bearer sk- Key；布图/植物新品种/专利交叉校验）
CNIPR_KEY_FILE = Path(os.environ.get("CNIPR_KEY_FILE", "/root/.cnipr_mcp_key"))
CNIPR_MCP_URL = os.environ.get("CNIPR_MCP_URL", "https://open.cnipr.com/cnipr-api/mcp-patent/mcp")
CNIPR_API_KEY = _secret("CNIPR_API_KEY", str(CNIPR_KEY_FILE))

# CNIPR REST（OAuth：client_id + openid + access_token；sk- 仅用于 MCP，不能调 REST）
CNIPR_REST_BASE = os.environ.get("CNIPR_REST_BASE", "https://open.cnipr.com/cnipr-api")
CNIPR_OAUTH_TOKEN_URL = os.environ.get("CNIPR_OAUTH_TOKEN_URL", "https://open.cnipr.com/oauth2/token")
CNIPR_CLIENT_ID = _secret("CNIPR_CLIENT_ID", "/root/.cnipr_client_id")
CNIPR_CLIENT_SECRET = _secret("CNIPR_CLIENT_SECRET", "/root/.cnipr_client_secret")
CNIPR_OPENID = _secret("CNIPR_OPENID", "/root/.cnipr_openid")
CNIPR_ACCESS_TOKEN = _secret("CNIPR_ACCESS_TOKEN", "/root/.cnipr_access_token")
CNIPR_USERNAME = _secret("CNIPR_USERNAME", "/root/.cnipr_username")
CNIPR_PASSWORD = _secret("CNIPR_PASSWORD", "/root/.cnipr_password")

# WeKnora 底稿库（云端火山服务器本机后端）
WEKNORA_ENV_FILE = os.environ.get("WEKNORA_ENV_FILE", "/root/weknora-mcp/weknora-mcp.env")
WEKNORA_KB_NAME = os.environ.get("WEKNORA_KB_NAME", "techcredit-dossiers")

for d in (RAW_DIR, DOSSIER_DIR, STAGING_DIR, STAGING_RAW_DIR, STAGING_DOSSIER_DIR):
    d.mkdir(parents=True, exist_ok=True)


def load_env_file(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out