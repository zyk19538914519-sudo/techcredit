#!/usr/bin/env python3
"""用户上传底稿分析管线：上传 → SQLite 落库 → 底稿渲染 → WeKnora 入库 → 引擎分析。

用户在第二层功能页上传自己的底稿文件（Markdown/TXT/JSON），填写企业信息后：
  1. 文件内容包装为 payload，经 store.upsert_source 落 SQLite（provider=upload/UPL，
     Source_ID 与抓取数据同规范，全程可溯源、幂等）；
  2. dossier.render 生成标准溯源底稿 Markdown；
  3. 上传进 WeKnora 知识库 techcredit-dossiers（与抓取底稿同库，靠 company_key
     白名单隔离，复用 ingest_weknora 的上传实现）；
  4. run_engine.run() 跑完整三层分析：SQLite 供数（专利/财务自动识别）+
     WeKnora hybrid 事实段 + 创新积分2.0/PQI/排雷/漏斗 → 上下文包。

全程零付费 API 调用；上传文件不落任何本地用户目录之外的位置。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "data_layer"))

import config           # noqa: E402
import store            # noqa: E402
import dossier          # noqa: E402
import innovation2      # noqa: E402
import run_engine       # noqa: E402
from ingest_weknora import _cfg, health, ensure_kb_ready, upload_file  # noqa: E402

MAX_FILES = 6
MAX_BYTES = 2 * 1024 * 1024
ANALYSIS_DIR = config.DATA_DIR / "analysis_results"
ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

# 前端可选的上传类型 → store.kind
KIND_MAP = {
    "patent": "专利文件", "financial": "财务数据", "bid": "中标公告",
    "judicial": "司法记录", "company": "工商资料", "risk": "风险信息",
    "web": "官网/宣传稿", "other": "其他材料",
}

USCC_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{18}$")


def _valid_key(company_key: str) -> bool:
    return bool(USCC_RE.match(company_key or ""))


def _clean_company_key(raw: str) -> str:
    """容忍空格/全角；大小写归一（USCC 可能含字母）。"""
    s = (raw or "").strip().replace("（", "").replace("）", "")
    return s.upper()


_PATENT_KEY_ALIASES = {
    "pn": ("pn", "patent_number", "publication_number", "公开号"),
    "apno": ("apno", "application_number", "申请号"),
    "title": ("title", "名称", "专利名称"),
    "inventor": ("inventor", "inventors", "发明人"),
    "apdt": ("apdt", "apply_date", "application_date", "申请日"),
    "pbdt": ("pbdt", "pub_date", "publication_date", "公开日"),
    "grant_date": ("grant_date", "grant", "授权日"),
    "legal_status": ("legal_status", "法律状态"),
    "ipc4_list": ("ipc4_list", "ipc", "ipc_list"),
    "family_offices": ("family_offices", "family", "同族"),
    "abstract": ("abstract", "摘要"),
    "claim_1": ("claim_1", "claims"),
}


def parse_patent_list(text: str) -> list:
    """解析上传的专利清单 JSON 为逐件标准化 payload 列表（纯函数，无 IO）。
    接受三种形态：JSON数组 [专利,...] / {"patents":[...]} / 单件 {pn:...}。
    字段名宽容映射（发明人列表→竖线分隔、申请日/授权日/IPC/同族/法律状态别名兼容）。
    解析失败或无有效 pn → 返回 []（调用方回退按非结构化文本存档）。"""
    try:
        data = json.loads(text.strip())
    except Exception:
        return []
    if isinstance(data, dict) and isinstance(data.get("patents"), list):
        data = data["patents"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rec = {}
        for canon, aliases in _PATENT_KEY_ALIASES.items():
            for a in aliases:
                if item.get(a) not in (None, "", []):
                    rec[canon] = item[a]
                    break
        if not rec.get("pn"):
            continue
        inv = rec.get("inventor")
        if isinstance(inv, list):
            rec["inventor"] = "|".join(str(x) for x in inv)
        if isinstance(rec.get("family_offices"), list):
            pass
        out.append(rec)
    return out


def store_uploads(conn, *, company_key: str, company_name: str,
                  files: list[tuple[str, bytes]], kinds: list[str],
                  project_id: str | None = None) -> dict:
    """文件落库 + 渲染底稿。files=[(filename, bytes)]，kinds 同长。
    project_id 给定时把底稿挂到项目（团队共享、成员可见），否则仅平台公共层。"""
    import projects as _proj
    saved, skipped, patents_ingested = [], [], 0
    for (fname, blob), kind in zip(files, kinds):
        if not blob:
            continue
        if len(blob) > MAX_BYTES:
            skipped.append({"file": fname, "reason": f"超过2MB限制"})
            continue
        text = blob.decode("utf-8", "replace")
        # ★结构化专利清单：kind=patent 且文本为 JSON → 逐件拆分落库，
        # payload=专利本体（pn/apdt/pbdt/IPC/同族/法律状态/独权），
        # data_provider.load_patents 经 normalize_payload 直接可读，
        # 引擎 PQI/创新积分/漏斗/Skill1 才能真正消费上传专利。
        plist = parse_patent_list(text) if kind == "patent" else []
        if plist:
            for rec in plist:
                pn = str(rec.get("pn"))
                sid = store.upsert_source(
                    conn, kind="patent", provider="upload",
                    company_key=company_key, company_name=company_name,
                    natural_key=f"{company_key}|{pn}",
                    title=str(rec.get("title") or pn)[:120],
                    payload=rec, meta={"uploaded": True, "source_file": fname})
                if project_id:
                    _proj.ensure_schema(conn)
                    conn.execute("UPDATE sources SET project_id=? WHERE source_id=?",
                                 (project_id, sid))
                dossier.render(conn, sid)
                patents_ingested += 1
            saved.append({"file": fname, "patents_ingested": len(plist),
                          "note": "结构化JSON专利清单已逐件落库（供引擎硬指标/Skill1消费）"})
            continue
        payload = {"filename": fname, "kind_label": KIND_MAP.get(kind, kind),
                   "text": text}
        # 幂等键带项目维度：不同团队对同一企业上传同名文件不互相覆盖
        natural_key = f"upload|{company_key}|{project_id or '-'}|{fname}"
        sid = store.upsert_source(
            conn, kind=kind if kind in store.KINDS else "web", provider="upload",
            company_key=company_key, company_name=company_name,
            natural_key=natural_key, title=f"用户上传底稿｜{fname}",
            payload=payload, meta={"uploaded": True})
        if project_id:
            _proj.ensure_schema(conn)
            conn.execute("UPDATE sources SET project_id=? WHERE source_id=?",
                         (project_id, sid))
        md_path = dossier.render(conn, sid)
        saved.append({"file": fname, "source_id": sid, "dossier": str(md_path)})
    if patents_ingested:
        saved.insert(0, {"patents_total_ingested": patents_ingested})
    return {"saved": saved, "skipped": skipped,
            "patents_ingested": patents_ingested}


def ingest_uploads_weknora(conn, company_key: str) -> dict:
    """把该企业 pending 状态的上传底稿推入 WeKnora。
    项目伴随知识库：挂项目的底稿 → 该项目专属库（techcredit-prj-*）；
    未挂项目的底稿 → 平台公共库。一个项目一个知识库，物理隔离。"""
    rows = conn.execute(
        "SELECT source_id, dossier_path, project_id FROM sources "
        "WHERE company_key=? AND provider='upload' AND weknora_status!='uploaded' "
        "AND dossier_path IS NOT NULL", (company_key,)).fetchall()
    if not rows:
        return {"uploaded": 0, "failed": 0, "note": "无待入库底稿（或已全部入库）"}
    base, key = _cfg()
    if not health(base, key):
        return {"uploaded": 0, "failed": len(rows), "note": "WeKnora 后端未就绪，底稿保持 pending"}
    import projects as _proj
    groups = {}   # kb_name -> [rows]
    for r in rows:
        kb = _proj.kb_name(r["project_id"]) if r["project_id"] else config.WEKNORA_KB_NAME
        groups.setdefault(kb, []).append(r)
    ok = fail = 0
    notes = []
    for kb, rs in groups.items():
        pid = rs[0]["project_id"]
        if pid:
            wk = _proj.ensure_weknora_workspace(conn, pid)
            gkey = wk["api_key"]          # 项目密钥：库落在项目独立工作区
        else:
            gkey = key                    # 平台公共库：管理员密钥
        kb_id = ensure_kb_ready(base, gkey, kb)   # 新项目库自动绑 embedding 模型
        if pid:
            _proj.set_project_kb_id(conn, pid, kb_id)
        n_ok = n_fail = 0
        for r in rs:
            try:
                doc_id = upload_file(base, gkey, kb_id, Path(r["dossier_path"]))
                conn.execute("UPDATE sources SET weknora_status='uploaded', weknora_doc_id=? "
                             "WHERE source_id=?", (doc_id, r["source_id"]))
                conn.commit()
                n_ok += 1
            except Exception:
                conn.execute("UPDATE sources SET weknora_status='failed' WHERE source_id=?",
                             (r["source_id"],))
                conn.commit()
                n_fail += 1
        ok += n_ok
        fail += n_fail
        notes.append(f"{kb}×{n_ok}")
    return {"uploaded": ok, "failed": fail, "note": "；".join(notes)}


def analyze_dossiers(*, company_key: str, company_name: str, domain: str, stage: str,
                     files: list[tuple[str, bytes]], kinds: list[str],
                     actor_email: str | None = None,
                     project_id: str | None = None) -> dict:
    """完整管线入口（同步执行，供 webapp 调用）。

    actor_email：触发者的登录邮箱；给定时引擎按其团队可见性跑
    （平台公共数据 + 本人所在项目上传的底稿），防止跨项目数据串入评分。
    project_id：底稿归属项目（团队共享知识库）。"""
    company_key = _clean_company_key(company_key)
    if not _valid_key(company_key):
        return {"ok": False, "error": "请填写有效的18位统一社会信用代码（USCC）"}
    if not files:
        return {"ok": False, "error": "请至少上传一份底稿文件"}
    if domain not in innovation2.DOMAINS:
        domain = "通用"
    if stage not in innovation2.STAGES:
        stage = "成长期"

    conn = store.connect()
    try:
        # ① 落库 + 渲染底稿（挂项目归属）
        st = store_uploads(conn, company_key=company_key, company_name=company_name,
                           files=files[:MAX_FILES], kinds=kinds[:MAX_FILES],
                           project_id=project_id)
        # ② 入 WeKnora（全局库；跨项目隔离由 SQLite 白名单在检索侧完成）
        ing = ingest_uploads_weknora(conn, company_key)
        # ③ 引擎三层分析（SQLite 供数 + WeKnora 事实桥自动注入）
        #    团队鉴权：数据供给与事实桥白名单按 actor 的项目成员可见性收窄
        base = run_engine.run(company_key, domain, stage, {}, {}, patent_top=5,
                              viewer_email=actor_email)
        run_id = base["run_id"]
        conn.execute("UPDATE evaluation_runs SET company_name=?, mode='dossier' "
                     "WHERE run_id=?", (company_name or company_key, run_id))
        conn.commit()
        payload = {k: base[k] for k in ("run_id", "company", "pqi", "innovation",
                                        "risk_flags", "funnel", "packs",
                                        "facts_summary")}
        payload["company"]["company_name"] = company_name or company_key
        payload["uploads"] = st
        payload["weknora_ingest"] = ing
        (ANALYSIS_DIR / f"{run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        return {"ok": True, **payload}
    finally:
        conn.close()
