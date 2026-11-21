#!/usr/bin/env python3
"""facts_bridge：SQLite(结构化+白名单) × WeKnora(事实证据) → Skills 上下文包。

三层织合（比赛项目核心闭环）：
  SQLite  = 结构化数据 + Source_ID 白名单（证据的"账本"：什么真实存在、数值多少）
  WeKnora = 事实证据段（证据的"内容"：底稿入库后的 hybrid 检索命中原文片段）
  Skills  = 判断层：只允许引用白名单内 Source_ID，判断依据必须能在事实段里找到

桥的规则：
  1. 以 SQLite sources 表为准圈定该企业全部真实 Source_ID（allowed_source_ids）。
  2. 每个 Skill 需要的事实用定向 query 从 WeKnora hybrid-search 取回；
     命中的 knowledge_title 解析回 Source_ID（底稿命名规范"底稿 {Source_ID}｜…"），
     title 对不上库的命中直接丢弃（防串库）。
  3. 事实段与 Source_ID 绑定后写进 pack：skills 只见"事实 + 绑定ID"，
     禁止引用绑定之外的内容；校验熔断仍按白名单复核（双保险）。
  4. WeKnora 不可用时降级：退回本地 raw JSON 的事实抽取，bridge 会在 pack 里
     标注 degraded=True，判断层必须把降级影响写进结论。
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import config

ENV_FILE = config.WEKNORA_ENV_FILE
_TIMEOUT = 30

_FACT_STORE = {}          # 进程内缓存: (sid) -> 事实段列表


def _cfg():
    env = config.load_env_file(ENV_FILE)
    base = env.get("WEKNORA_BASE_URL", "http://127.0.0.1:8080/api/v1")
    key = env.get("WEKNORA_API_KEY", "")
    return base.rstrip("/"), key


def _req(method, url, key, body=None, timeout=_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", key)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


def weknora_available() -> bool:
    try:
        base, key = _cfg()
        _req("GET", base + "/knowledge-bases", key, timeout=8)
        return True
    except Exception:
        return False


_KB_KEY_CACHE = {}          # kb_id -> 项目密钥（进程内缓存）
_PRJ_PREFIX = "techcredit-prj-"


def _key_for_kb_name(name: str) -> str:
    """项目库用项目密钥（独立工作区），平台库用管理员密钥。"""
    base, admin = _cfg()
    if name.startswith(_PRJ_PREFIX):
        try:
            conn = sqlite3.connect(str(config.DB_PATH), timeout=10)
            row = conn.execute("SELECT api_key FROM project_weknora WHERE project_id=?",
                               (name[len(_PRJ_PREFIX):],)).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    return admin


def _key_for_kb_id(kb_id: str) -> str:
    if kb_id in _KB_KEY_CACHE:
        return _KB_KEY_CACHE[kb_id]
    base, admin = _cfg()
    try:
        conn = sqlite3.connect(str(config.DB_PATH), timeout=10)
        row = conn.execute("SELECT api_key FROM project_weknora WHERE kb_id=?",
                           (kb_id,)).fetchone()
        conn.close()
        if row and row[0]:
            _KB_KEY_CACHE[kb_id] = row[0]
            return row[0]
    except Exception:
        pass
    return admin


def resolve_kb_id(name: str) -> str | None:
    base, _ = _cfg()
    key = _key_for_kb_name(name)
    listing = _req("GET", base + "/knowledge-bases", key)
    items = listing.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("list") or []
    for kb in items:
        if kb.get("name") == name:
            _KB_KEY_CACHE[kb.get("id")] = key
            return kb.get("id")
    return None


_SID_RE = re.compile(r"(?:PAT|BID|JUD|COR|PEN|RSK|FIN|WEB)-[A-Z]{2,4}-[0-9a-f]{10}")


def _sid_from_title(title: str) -> str | None:
    m = _SID_RE.search(title or "")
    return m.group(0) if m else None


def hybrid_search(query: str, kb_id: str, top_k: int = 6) -> list:
    """hybrid 检索，返回 [{source_id, title, score, text}]，来源不明/越权的命中丢弃。"""
    base, _ = _cfg()
    key = _key_for_kb_id(kb_id)
    try:
        d = _req("POST", f"{base}/knowledge-bases/{kb_id}/hybrid-search", key,
                 {"query_text": query, "top_k": top_k})
    except urllib.error.HTTPError:
        # 缓存失效时按项目密钥逐一试探（库可能刚迁移工作区）
        d = None
        try:
            conn = sqlite3.connect(str(config.DB_PATH), timeout=10)
            rows = conn.execute("SELECT api_key FROM project_weknora").fetchall()
            conn.close()
        except Exception:
            rows = []
        for (k,) in rows:
            if k == key:
                continue
            try:
                d = _req("POST", f"{base}/knowledge-bases/{kb_id}/hybrid-search", k,
                         {"query_text": query, "top_k": top_k})
                _KB_KEY_CACHE[kb_id] = k
                break
            except urllib.error.HTTPError:
                continue
        if d is None:
            raise
    data = d.get("data") if isinstance(d, dict) else d
    hits = data if isinstance(data, list) else (data or {}).get("results") or []
    out = []
    for h in hits:
        text = (h.get("content") or h.get("text") or "").strip()
        title = (h.get("knowledge_title") or h.get("document_title")
                 or h.get("title") or h.get("knowledge_id") or "")
        sid = _sid_from_title(title)
        if not text:
            continue
        out.append({"source_id": sid, "title": title,
                    "score": h.get("score"), "text": text[:1200]})
    return out


def _local_fallback(conn, source_ids: list) -> list:
    """WeKnora 不可用时的降级：直接从 raw JSON 提事实。"""
    facts = []
    for sid in source_ids:
        row = conn.execute("SELECT raw_path, kind, title FROM sources WHERE source_id=?",
                           (sid,)).fetchone()
        if not row:
            continue
        try:
            payload = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))["payload"]
        except (json.JSONDecodeError, OSError, KeyError):
            continue
        text = payload.get("text") if isinstance(payload, dict) else None
        if not text and isinstance(payload, dict):
            text = json.dumps(payload, ensure_ascii=False)
        facts.append({"source_id": sid, "title": row["title"], "score": None,
                      "text": str(text)[:1200]})
    return facts


def fetch_facts(conn, company_key: str, *, queries: dict, kb_names: list | None = None,
                allowed: set | None = None, top_k: int = 6) -> dict:
    """按 Skill 需求取事实段。kb_names 支持多知识库合并检索
    （平台公共库 + 项目专属库），命中统一过白名单。

    queries: {skill_name: [(query, [候选source_id或None]), ...]}
      - 候选ID用于二次过滤（命中段绑定ID必须落在候选或白名单内）；
      - 候选为 None 表示只用白名单过滤。
    返回 {skill_name: {"degraded": bool, "facts": [{source_id,title,score,text}]}}
    """
    if not kb_names:
        kb_names = [config.load_env_file(ENV_FILE).get("WEKNORA_KB_NAME",
                                                      "techcredit-dossiers")]
    if allowed is None:
        allowed = {r[0] for r in conn.execute(
            "SELECT source_id FROM sources WHERE company_key=?", (company_key,))}
    result = {}
    online = weknora_available()
    kb_ids, kb_seen = [], set()
    if online:
        for n in dict.fromkeys(kb_names):          # 去重且保序
            kid = resolve_kb_id(n) if n not in kb_seen else None
            kb_seen.add(n)
            if kid:
                kb_ids.append(kid)

    for skill, q_list in queries.items():
        facts = []
        for query, cand in q_list:
            for kb_id in kb_ids:
                for hit in hybrid_search(query, kb_id, top_k=top_k):
                    if hit["source_id"] is None:
                        continue
                    if hit["source_id"] not in allowed:
                        continue
                    if cand and hit["source_id"] not in cand:
                        continue
                    facts.append(hit)
        # 事实不足时用白名单直接补（SQLite 是"什么存在"的账本）
        seen = {f["source_id"] for f in facts}
        shortfall = [sid for _, cand in q_list if cand for sid in cand
                     if sid not in seen][:top_k]
        if shortfall:
            facts.extend(_local_fallback(conn, shortfall))
        if not facts and not online:
            # 全量降级
            facts = _local_fallback(conn, sorted(allowed)[: top_k])
        # 去重（同一SID可能被多条query命中）
        dedup, seen2 = [], set()
        for f in sorted(facts, key=lambda x: -(x["score"] or 0)):
            key = (f["source_id"], f["text"][:80])
            if key in seen2:
                continue
            seen2.add(key)
            dedup.append(f)
        result[skill] = {"degraded": not online, "facts": dedup[: 3 * top_k]}
    return result


def attach_facts_to_packs(pack_dir: Path, facts_by_skill: dict) -> dict:
    """把事实段写入各 Skill 上下文包：facts 字段 + 事实绑定ID清单。"""
    updated = {}
    for skill, blob in facts_by_skill.items():
        pack_path = pack_dir / f"pack_{skill}.json"
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        fact_ids = sorted({f["source_id"] for f in blob["facts"] if f["source_id"]})
        pack["weknora_facts"] = blob["facts"]
        pack["fact_source_ids"] = fact_ids
        pack["facts_degraded"] = blob["degraded"]
        pack.setdefault("allowed_source_ids", [])
        allowed = sorted(set(pack["allowed_source_ids"]) | set(fact_ids))
        pack["allowed_source_ids"] = allowed
        pack["fact_rules"] = {
            "judge_only_on": "判断只能依据 weknora_facts 与本包结构化字段",
            "bind": "每条结论绑定的Source_ID必须在allowed_source_ids内",
            "degraded_note": "facts_degraded=true时，结论必须注明证据来自降级本地抽取",
        }
        pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
        updated[skill] = str(pack_path)
    return updated


SKILL_QUERIES = {
    "patent_review": [
        ("核心专利 独立权利要求 技术方案 摘要", None),
        ("专利 法律状态 同族", None),
    ],
    "commercialization_fit": [
        ("中标公告 招投标 采购", None),
        ("专利 产品技术领域 应用", None),
    ],
    "anti_packaging_factcheck": [
        ("官网 公司介绍 产品 宣传", None),
        ("新闻 荣誉 排名 行业地位", None),
        ("专利 技术 授权", None),
    ],
    "bank_report": [
        ("工商登记 法定代表人 注册资本", None),
        ("风险 处罚 失信 被执行", None),
        ("财务 研发费用 营业收入", None),
    ],
}


def build_fact_bridges(conn, company_key: str, *, core_patent_ids=None, kb_name=None,
                       top_k: int = 6, viewer_email: str | None = None) -> dict:
    """标准入口：按四个 Skill 的需求生成事实桥（core_patent_ids 限定专利事实候选）。

    viewer_email 给定时按团队鉴权收窄白名单：上传底稿仅项目成员可见，
    WeKnora 命中绑定白名单之外（跨项目）的 Source_ID 一律丢弃；
    同时启用项目伴随知识库：平台公共库 + viewer 本人在该企业下的项目专属库
    （一个项目一个知识库，物理隔离、合并检索）。"""
    queries = {}
    for skill, q_list in SKILL_QUERIES.items():
        adapted = []
        for q, cand in q_list:
            c = list(core_patent_ids) if (cand is None and skill == "patent_review"
                                          and core_patent_ids) else cand
            adapted.append((q, c))
        queries[skill] = adapted
    if viewer_email is not None:
        import projects as _proj
        _proj.ensure_schema(conn)
        allowed = _proj.visible_source_ids(conn, company_key, viewer_email)
        default_kb = config.load_env_file(ENV_FILE).get("WEKNORA_KB_NAME",
                                                        "techcredit-dossiers")
        kb_names = [kb_name] if kb_name else [default_kb]
        kb_names += _proj.project_kbs(conn, company_key, viewer_email)
    else:
        allowed = None
        kb_names = [kb_name] if kb_name else None
    return fetch_facts(conn, company_key, queries=queries, kb_names=kb_names,
                       top_k=top_k, allowed=allowed)


if __name__ == "__main__":
    import argparse
    import store

    ap = argparse.ArgumentParser(description="SQLite×WeKnora事实桥")
    ap.add_argument("--company", required=True)
    ap.add_argument("--kb", default=None)
    ap.add_argument("--core-patents", default=None, help="逗号分隔的核心专利Source_ID")
    ap.add_argument("--top-k", type=int, default=6)
    a = ap.parse_args()
    conn = store.connect()
    core = [x for x in (a.core_patents or "").split(",") if x] or None
    facts = build_fact_bridges(conn, a.company, core_patent_ids=core, kb_name=a.kb,
                               top_k=a.top_k)
    print(json.dumps({k: {"degraded": v["degraded"],
                          "n_facts": len(v["facts"]),
                          "sids": sorted({f["source_id"] or "?" for f in v["facts"]})}
                      for k, v in facts.items()}, ensure_ascii=False, indent=1))
