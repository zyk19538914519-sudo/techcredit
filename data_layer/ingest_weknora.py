#!/usr/bin/env python3
"""WeKnora 底稿入库（云端火山服务器本机后端，绝不触碰本地服务器）。

流程：健康检查 -> 确保知识库存在 -> 逐份上传底稿 Markdown -> 回写 weknora_doc_id。
后端未就绪时明确退出码 2，底稿保持 pending，可稍后重跑（幂等）。
"""
from __future__ import annotations
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import config
import store


def _cfg():
    env = config.load_env_file(config.WEKNORA_ENV_FILE)
    base = env.get("WEKNORA_BASE_URL", "http://127.0.0.1:8080/api/v1").rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    key = env.get("WEKNORA_API_KEY", "")
    if not key:
        raise SystemExit("WEKNORA_API_KEY 未配置")
    return base, key


def _req(method, url, key, body=None, content_type="application/json", timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", key)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


def health(base, key):
    try:
        _req("GET", base + "/api/v1/knowledge-bases", key, timeout=8)
        return True
    except urllib.error.URLError:
        return False


def ensure_kb(base, key, name):
    listing = _req("GET", base + "/api/v1/knowledge-bases", key)
    items = listing.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("list") or []
    for kb in items:
        if kb.get("name") == name:
            return kb.get("id")
    created = _req("POST", base + "/api/v1/knowledge-bases", key,
                   {"name": name, "description": "企业技术信用评估系统原始底稿库（专利/中标/司法），每份底稿带全局唯一 Source_ID"})
    data = created.get("data") or {}
    kb_id = data.get("id")
    if not kb_id:
        raise SystemExit(f"创建知识库失败: {str(created)[:300]}")
    return kb_id


def ensure_kb_ready(base, key, name):
    """幂等：确保 KB 存在且已绑定 LLM/Embedding/Rerank 模型。
    新建 KB 默认不绑 embedding 模型，直接上传会导致 parse_status=failed、
    检索永远 0 命中；此处对未绑模型的库走 initialization/initialize 绑定
    （模型连接参数读 weknora_models_export.json，不落日志）。返回 kb_id。"""
    listing = _req("GET", base + "/api/v1/knowledge-bases", key)
    items = listing.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("list") or []
    kb_id = None
    for kb in items:
        if kb.get("name") == name:
            kb_id = kb.get("id")
            if kb.get("embedding_model_id"):
                return kb_id          # 已绑模型，就绪
            break
    if not kb_id:
        created = _req("POST", base + "/api/v1/knowledge-bases", key,
                       {"name": name, "description": "企业技术信用评估系统原始底稿库（专利/中标/司法），每份底稿带全局唯一 Source_ID"})
        kb_id = (created.get("data") or {}).get("id")
        if not kb_id:
            raise RuntimeError(f"创建知识库失败: {str(created)[:300]}")
    export = Path(__file__).resolve().parent / "weknora_models_export.json"
    models = json.loads(export.read_text(encoding="utf-8"))

    def _pick(typ, n):
        return next(m for m in models if m["type"] == typ and m["name"] == n)

    def _conn(m, dimension=None):
        p = m.get("parameters") or {}
        out = {"source": m["source"], "modelName": m["name"],
               "baseUrl": p.get("base_url", ""), "apiKey": p.get("api_key", "")}
        if p.get("provider"):
            out["provider"] = p["provider"]
        if p.get("interface_type"):
            out["interfaceType"] = p["interface_type"]
        if dimension:
            out["dimension"] = dimension
        return out

    emb = _pick("Embedding", "BAAI/bge-m3")
    rr = _pick("Rerank", "BAAI/bge-reranker-v2-m3")
    llm = _pick("KnowledgeQA", "glm-4.7-flash")
    dim = (emb.get("parameters") or {}).get("dimension") or 1024
    init = {"llm": _conn(llm), "embedding": _conn(emb, dim),
            "rerank": dict(_conn(rr), enabled=True),
            "documentSplitting": {"chunkSize": 512, "chunkOverlap": 50,
                                  "separators": ["\n\n", "\n"]}}
    _req("POST", base + f"/api/v1/initialization/initialize/{kb_id}", key, init)
    return kb_id


def upload_file(base, key, kb_id, path: Path):
    boundary = uuid.uuid4().hex
    filename = path.name
    content = path.read_bytes()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: text/markdown\r\n\r\n",
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    url = base + f"/api/v1/knowledge-bases/{kb_id}/knowledge/file"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("X-API-Key", key)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8", "replace")
    result = json.loads(raw) if raw else {}
    data = result.get("data") or {}
    doc_id = data.get("id") or data.get("knowledge_id")
    if not doc_id:
        raise RuntimeError(f"上传无 doc_id: {str(result)[:300]}")
    return doc_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", help="仅入库该企业（company_key）")
    ap.add_argument("--dry-run", action="store_true", help="只列出待入库底稿，不上传")
    args = ap.parse_args()

    conn = store.connect()
    rows = conn.execute(
        "SELECT source_id, company_key, dossier_path FROM sources "
        "WHERE weknora_status!='uploaded' AND dossier_path IS NOT NULL "
        "AND (project_id IS NULL OR project_id='') "
        + ("AND company_key=? " if args.company else "")
        + "ORDER BY company_key, source_id",
        ((args.company,) if args.company else ())).fetchall()
    print(f"待入库底稿: {len(rows)} 份")
    if args.dry_run:
        for r in rows:
            print(" ", r["source_id"], r["dossier_path"])
        return

    base, key = _cfg()
    if not health(base, key):
        print("WeKnora 后端未就绪（连接失败）。底稿保持 pending，部署完成后重跑本脚本即可。")
        sys.exit(2)
    kb_id = ensure_kb_ready(base, key, config.WEKNORA_KB_NAME)
    print(f"知识库: {config.WEKNORA_KB_NAME} ({kb_id}) 已就绪（含 embedding/rerank/LLM 绑定）")
    ok = fail = 0
    for r in rows:
        sid = r["source_id"]
        try:
            doc_id = upload_file(base, key, kb_id, Path(r["dossier_path"]))
            conn.execute("UPDATE sources SET weknora_status='uploaded', weknora_doc_id=? "
                         "WHERE source_id=?", (doc_id, sid))
            conn.commit()
            ok += 1
            print(f"  uploaded {sid} -> {doc_id}")
        except Exception as exc:
            conn.execute("UPDATE sources SET weknora_status='failed' WHERE source_id=?", (sid,))
            conn.commit()
            fail += 1
            print(f"  FAILED {sid}: {exc}")
    print(f"完成: uploaded={ok} failed={fail}")


if __name__ == "__main__":
    main()