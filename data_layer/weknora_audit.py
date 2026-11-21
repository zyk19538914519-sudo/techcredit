#!/usr/bin/env python3
"""WeKnora 云端实例全功能体检。逐项输出 PASS / FAIL / INFO，不打印任何密钥。"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import config

BASE = "http://127.0.0.1:8080"
KB_NAME = config.WEKNORA_KB_NAME
RESULTS = []


def key():
    return config.load_env_file(config.WEKNORA_ENV_FILE)["WEKNORA_API_KEY"]


def req(method, path, body=None, timeout=60, base=BASE, raw_body=None, content_type=None):
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("X-API-Key", key())
    r.add_header("Accept", "application/json")
    if data is not None:
        r.add_header("Content-Type", content_type or "application/json")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
    return json.loads(text) if text.strip().startswith(("{", "[")) else {"_text": text}


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append(("PASS", name, detail or ""))
        print(f"[PASS] {name} {('| ' + str(detail)) if detail else ''}")
    except Exception as exc:
        msg = str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            try:
                msg = f"HTTP {exc.code}: {exc.read().decode()[:150]}"
            except Exception:
                msg = f"HTTP {exc.code}"
        RESULTS.append(("FAIL", name, msg[:200]))
        print(f"[FAIL] {name} | {msg[:200]}")


def info(name, detail):
    RESULTS.append(("INFO", name, detail))
    print(f"[INFO] {name} | {detail}")


def psql(q):
    return subprocess.run(
        ["docker", "exec", "WeKnora-postgres", "psql", "-U", "postgres", "-d", "WeKnora", "-tAc", q],
        stdout=subprocess.PIPE, timeout=30).stdout.decode().strip()


def make_pdf() -> bytes:
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        None,
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream = b"BT /F1 12 Tf 72 720 Td (TechCredit WeKnora PDF parse test. Ref PAT-TEST-001.) Tj ET"
    objs[3] = b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream"
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + o + b"endobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<</Size {len(objs)+1}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF").encode()
    return out


def upload(kb_id, filename, content, mime):
    boundary = uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(), content,
        f"\r\n--{boundary}--\r\n".encode()])
    return req("POST", f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
               raw_body=body, content_type=f"multipart/form-data; boundary={boundary}", timeout=120)


def main():
    # ── 基础服务 ──
    check("后端健康 /health", lambda: req("GET", "/health", base=BASE, timeout=8).get("status"))
    check("前端页面 :8088", lambda: "HTTP 200" if urllib.request.urlopen(
        "http://127.0.0.1:8088", timeout=8).status == 200 else "?")
    check("API Key 鉴权", lambda: f"{len(req('GET', '/api/v1/knowledge-bases').get('data') or [])} 个知识库")

    kbs = req("GET", "/api/v1/knowledge-bases").get("data") or []
    kb = next(k for k in kbs if k.get("name") == KB_NAME)
    kb_id = kb["id"]

    # ── 知识库与文档 ──
    st = psql(f"SELECT parse_status||':'||count(*) FROM knowledges WHERE knowledge_base_id='{kb_id}' "
              f"AND deleted_at IS NULL GROUP BY parse_status;")
    check("文档解析状态", lambda: st.replace("\n", " "))
    check("分块入库", lambda: psql(
        f"SELECT count(*)||' chunks' FROM chunks c JOIN knowledges k ON c.knowledge_id=k.id "
        f"WHERE k.knowledge_base_id='{kb_id}';"))

    # ── 检索 ──
    def search():
        d = req("POST", "/api/v1/knowledge-search",
                {"knowledge_base_ids": [kb_id], "query": "线束卷绕实验设备"})
        hits = d.get("data") or []
        rr = (d.get("meta") or {}).get("rerank") or {}
        return f"{len(hits)} 命中, rerank.applied={rr.get('applied')}, top={hits[0]['score']:.2f}" if hits else "0 命中"
    check("语义检索 knowledge-search", search)

    def hybrid():
        d = req("POST", f"/api/v1/knowledge-bases/{kb_id}/hybrid-search",
                {"query_text": "电池 专利", "match_count": 5})
        return f"{len(d.get('data') or [])} 条原始召回"
    check("混合召回 hybrid-search", hybrid)

    # ── 知识问答（SSE，Qwen3-8B）──
    def chat():
        s = req("POST", "/api/v1/sessions", {"title": "功能体检测试"})
        sid = s["data"]["id"]
        body = json.dumps({"query": "底稿里有没有线束卷绕相关的专利？一句话回答并给出Source_ID。",
                           "knowledge_base_ids": [kb_id]}).encode()
        r = urllib.request.Request(f"{BASE}/api/v1/knowledge-chat/{sid}", data=body, method="POST")
        r.add_header("X-API-Key", key())
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "text/event-stream")
        answer, refs, done = [], 0, False
        t0 = time.time()
        with urllib.request.urlopen(r, timeout=180) as resp:
            for raw in resp:
                if time.time() - t0 > 150:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except ValueError:
                    continue
                rt = ev.get("response_type")
                if rt == "answer":
                    answer.append(ev.get("content") or "")
                elif rt == "references":
                    refs = len(ev.get("knowledge_references") or [])
                elif rt == "error":
                    raise RuntimeError("chat error: " + str(ev.get("content"))[:150])
                elif rt == "complete":
                    done = True
                    break
        text = "".join(answer).strip()
        if not done or not text:
            raise RuntimeError(f"未完成 done={done} len={len(text)}")
        return f"回答{len(text)}字, 引用{refs}条 | {text[:80]}"
    check("知识库问答 knowledge-chat", chat)

    # ── Wiki / FAQ ──
    check("Wiki 页面列表", lambda: str(req("GET", f"/api/v1/knowledgebase/{kb_id}/wiki/pages"))[:120])
    check("Wiki 跨库检索", lambda: f"{len(req('POST', '/api/v1/wiki-search', {'query': '专利', 'knowledge_base_ids': [kb_id]}).get('data') or [])} 命中")
    check("Wiki 一致性检查 lint", lambda: str(req("GET", f"/api/v1/knowledgebase/{kb_id}/wiki/lint"))[:120])
    def faq():
        try:
            return str(req("GET", f"/api/v1/knowledge-bases/{kb_id}/faq/entries"))[:120]
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and "FAQ" in exc.read().decode("utf-8", "replace"):
                return "按设计：FAQ 接口仅适用于 FAQ 型知识库（当前为 document 型，非缺陷）"
            raise
    check("FAQ 接口", faq)

    # ── 模型连通性 ──
    models = req("GET", "/api/v1/models").get("data") or []
    for m in models:
        typ, name, mid = m.get("type"), m.get("name"), m.get("id")
        p = m.get("parameters") or {}
        burl = p.get("base_url") or ""
        if typ == "Embedding":
            check(f"模型连通 {name}", lambda name=name, mid=mid, burl=burl, p=p: req(
                  "POST", "/api/v1/initialization/embedding/test",
                  {"modelName": name, "modelId": mid, "baseUrl": burl,
                   "dimension": p.get("dimension") or 1024}, timeout=90).get("data"))
        elif typ in ("KnowledgeQA", "VLLM"):
            check(f"模型连通 {name}", lambda name=name, mid=mid, burl=burl, p=p: req(
                  "POST", "/api/v1/initialization/remote/check",
                  {"modelName": name, "modelId": mid, "baseUrl": burl,
                   "provider": p.get("provider") or ""}, timeout=90).get("data"))
        else:
            info(f"模型 {name} ({typ})", "已配置（无独立连通性测试接口，随检索/问答链路验证）")

    # ── PDF 解析（docreader）──
    def pdf_test():
        up = upload(kb_id, "pdf-parse-test.pdf", make_pdf(), "application/pdf")
        kid = (up.get("data") or {}).get("id")
        if not kid:
            raise RuntimeError(str(up)[:150])
        for _ in range(30):
            time.sleep(3)
            row = psql(f"SELECT parse_status FROM knowledges WHERE id='{kid}';")
            if row in ("completed", "failed"):
                break
        if row != "completed":
            err = psql(f"SELECT left(error_message,120) FROM knowledges WHERE id='{kid}';")
            raise RuntimeError(f"parse_status={row} {err}")
        try:
            req("DELETE", f"/api/v1/knowledge/{kid}")  # 清理测试文件
            return "PDF 上传→解析 completed（测试文件已清理）"
        except Exception:
            return f"PDF 上传→解析 completed（测试文件 {kid} 待手动清理）"
    check("PDF 解析 docreader", pdf_test)

    # ── 平台能力 ──
    check("Agent 列表", lambda: f"{len(req('GET', '/api/v1/agents').get('data') or [])} 个")
    check("MCP 服务端点", lambda: f"{len(req('GET', '/api/v1/mcp-endpoints').get('data') or [])} 个")
    check("MCP 外部服务", lambda: str(req("GET", "/api/v1/mcp-services"))[:100])
    check("向量存储引擎", lambda: str(req("GET", "/api/v1/vector-stores"))[:150])
    check("存储后端", lambda: str(req("GET", "/api/v1/storage-backends"))[:150])
    check("联网搜索提供方", lambda: str(req("GET", "/api/v1/web-search-providers"))[:150])
    check("长期记忆配置", lambda: str(req("GET", "/api/v1/tenants/kv/memory-config"))[:150])
    check("检索配置", lambda: str(req("GET", "/api/v1/tenants/kv/retrieval-config"))[:150])
    check("沙箱配置", lambda: str(req("GET", "/api/v1/sandbox-configs"))[:120])
    def skills():
        try:
            return str(req("GET", "/api/v1/skills"))[:120]
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                return "按设计：技能接口仅限 JWT 用户会话，API Key 不可调用（非缺陷）"
            raise
    check("技能接口", skills)

    # ── 汇总 ──
    p = sum(1 for r in RESULTS if r[0] == "PASS")
    f = sum(1 for r in RESULTS if r[0] == "FAIL")
    print(f"\n=== 体检汇总: PASS={p} FAIL={f} INFO={len(RESULTS)-p-f} ===")
    for status, name, detail in RESULTS:
        if status == "FAIL":
            print(f"  FAIL {name}: {detail}")


if __name__ == "__main__":
    main()