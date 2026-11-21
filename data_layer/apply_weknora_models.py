#!/usr/bin/env python3
"""把本机导出的 WeKnora 整套模型配置写入云端实例。

步骤：
  1) 用 initialize 接口为 techcredit-dossiers 知识库绑定 LLM/Embedding/Rerank
  2) 其余模型经 POST /api/v1/models 逐个创建（按 name+type 去重）
  3) 重解析知识库中 parse_status=failed 的底稿
密钥只从 600 权限的导出文件读取，不打印。
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import config

BASE = "http://127.0.0.1:8080"
EXPORT = Path(__file__).resolve().parent / "weknora_models_export.json"


def key():
    return config.load_env_file(config.WEKNORA_ENV_FILE)["WEKNORA_API_KEY"]


def req(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("X-API-Key", key())
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode()[:400]}")


def pick(models, typ, name):
    return next(m for m in models if m["type"] == typ and m["name"] == name)


def conn_params(m, dimension=None):
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


def main():
    models = json.loads(EXPORT.read_text())
    emb = pick(models, "Embedding", "BAAI/bge-m3")
    rr = pick(models, "Rerank", "BAAI/bge-reranker-v2-m3")
    llm = pick(models, "KnowledgeQA", "glm-4.7-flash")

    # 定位知识库
    kbs = req("GET", "/api/v1/knowledge-bases").get("data") or []
    kb = next(k for k in kbs if k.get("name") == config.WEKNORA_KB_NAME)
    kb_id = kb["id"]
    print(f"知识库: {kb['name']} ({kb_id})")

    # 1) 初始化绑定三类模型
    dim = (emb.get("parameters") or {}).get("dimension") or 1024
    init = {
        "llm": conn_params(llm),
        "embedding": conn_params(emb, dimension=dim),
        "rerank": dict(conn_params(rr), enabled=True),
        "documentSplitting": {"chunkSize": 512, "chunkOverlap": 50,
                              "separators": ["\n\n", "\n"]},
    }
    res = req("POST", f"/api/v1/initialization/initialize/{kb_id}", init)
    print("KB 模型绑定:", res.get("message") or res.get("success"))

    # 2) 创建其余模型（去重）
    existing = req("GET", "/api/v1/models").get("data") or []
    seen = {(m.get("name"), m.get("type")) for m in existing}
    created = skipped = 0
    for m in models:
        ident = (m["name"], m["type"])
        if ident in seen:
            skipped += 1
            continue
        body = {"name": m["name"], "type": m["type"], "source": m["source"],
                "parameters": m.get("parameters") or {}}
        if m.get("display_name"):
            body["display_name"] = m["display_name"]
        if m.get("description"):
            body["description"] = m["description"]
        req("POST", "/api/v1/models", body)
        seen.add(ident)
        created += 1
        print(f"  创建模型: {m['type']} {m['name']}")
    print(f"模型创建: {created} 新建 / {skipped} 已存在")

    # 3) 重解析失败底稿
    ids = subprocess.run(
        ["docker", "exec", "WeKnora-postgres", "psql", "-U", "postgres", "-d", "WeKnora",
         "-tAc", f"SELECT id FROM knowledges WHERE knowledge_base_id='{kb_id}' "
                 f"AND parse_status='failed' AND deleted_at IS NULL;"],
        stdout=subprocess.PIPE, timeout=30).stdout.decode().split()
    ok = 0
    for kid in ids:
        try:
            req("POST", f"/api/v1/knowledge/{kid}/reparse", {})
            ok += 1
        except SystemExit as exc:
            print(f"  reparse 失败 {kid}: {exc}")
        time.sleep(1)
    print(f"重解析触发: {ok}/{len(ids)}")


if __name__ == "__main__":
    main()