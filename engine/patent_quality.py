#!/usr/bin/env python3
"""OECD PQI 专利质量子引擎（创新产出维度"质"的深化）。

依据：OECD DSTI/DOC(2013)3《Measuring Patent Quality: Indicators of
Technological and Economic Value》，按比赛规划实现五项：
  1. 发明占比        —— 组合中发明专利占比（发明授权+发明申请 / 总量）
  2. 同族规模        —— FAMILY_P = 同族专利局数，按同期群（技术领域×年）最大值归一化，
                        并按论文口径在1%/99%分位 winsorize
  3. 4位IPC广度      —— SCOPE_P = 独立4位IPC子类数（Lerner 1994），同期群最大值归一化
  4. 授权周期        —— GRANT_Pi = 1 - Δt/Max(Δt同期群)，Δt=申请→授权天数。
                        口径纪律：U/B/Y 公告号的 pbdt 即授权公告日；A 文献 pbdt 仅为
                        公开日，不得当授权日用——A 文献须有显式 grant_date 才计授权
                        周期，否则该件该维度缺失（动态调权），避免"公开速度"冒充"授权速度"。
  5. 发明人留存率    —— Top3核心发明人在近24个月新申请中的覆盖率（团队稳定性扩展指标）。
                        近24个月无样本内新申请时标"不可算"（数据窗口问题，非团队流失
                        信号），维度留空走动态调权，不机械归0拖低组合分。

数据源：data_layer 落盘的原始专利 JSON（PAT-ZHY-*）。
当前抓取仅含著录项（pn/apno/title/inventor/apdt/pbdt）；IPC/同族/法律状态
字段缺失时按"缺失维度动态调权"只对可计算维度合成，并把缺口显式上报。
单件价值分（漏斗用）纳入法律状态折减：已知失效×0.35、审中×0.85、
有效/缺失×1.0（缺失不惩罚，降级不虚构）；per_patent 回传 legal_status 供语义层交叉。
"""
from __future__ import annotations

import json
import re
from datetime import date

IPC4_KEY = "ipc4_list"
FAMILY_KEY = "family_offices"
LEGAL_KEY = "legal_status"
ABSTRACT_KEY = "abstract"
CLAIM1_KEY = "claim_1"

UTILITY_PN_SUFFIX = ("U",)          # 公告号尾缀 U=实用新型
INVENTION_PN_SUFFIX = ("B", "Y")     # B/Y=发明授权
INVENTION_PENDING = ("A",)          # A=发明申请公开

# 法律状态折减系数（单件价值分用）：已知失效大幅折减但不清零（海外同族可能承接价值）；
# 审中轻度折减；有效/未确认/缺失不惩罚（降级不虚构）。
# 失效判定统一走 is_lapsed_status（本模块与 funnel.R3 共用同一函数，防两处口径分叉）。
LAPSED_STATUSES = ("无效", "无效宣告", "失效", "invalid", "dead", "届满", "终止", "放弃")
PENDING_STATUSES = ("审中", "pending", "实质审查", "公开")


def is_lapsed_status(ls) -> bool:
    """失效判定（单件价值折减与漏斗R3共用同一口径，防分叉）：
    精确匹配 + 归一化子串兜底（驳回/视撤/撤回/Revoked/withdrawn 等外来措辞，
    零记忆agent上传的 legal_status 措辞不可控）；未识别值不计失效（宁缺不滥报）。"""
    s = str(ls or "").strip()
    if not s:
        return False
    low = s.lower()
    return low in LAPSED_STATUSES or any(k in low for k in
            ("驳回", "视撤", "撤回", "无效", "失效", "终止", "放弃", "届满",
             "invalid", "dead", "lapsed", "expired", "revoked", "withdrawn"))


def _legal_factor(p: dict) -> float:
    """法律状态折减：失效×0.35（is_lapsed_status 共用口径）、审中×0.85（含
    在审/pending/under review 等外来表述子串兜底）、有效/未识别/缺失×1.0
    （未识别不惩罚，降级不虚构）。"""
    ls = str(p.get("legal_status") or "").strip()
    if not ls:
        return 1.0
    if is_lapsed_status(ls):
        return 0.35
    low = ls.lower()
    if low in PENDING_STATUSES or any(k in low for k in
            ("审中", "在审", "pending", "under review", "substantive exam")):
        return 0.85
    return 1.0


_DATE_RE = re.compile(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})")


def _to_date(yyyymmdd):
    """日期容错：原生 YYYYMMDD（整数/字符串）之外，兼容零记忆 agent 常见外来格式
    2023-01-10 / 2023.1.10 / 2023年11月19日 / 2023/1/5 / 2023-01-10 00:00:00——
    按分隔符切年/月/日，缺零自动补；歧义形式（纯数字≠8位，如 2023110）不猜，
    返回 None 走维度缺失动态调权。"""
    if yyyymmdd in (None, ""):
        return None
    s = str(yyyymmdd).strip()
    if s.isdigit() and len(s) == 8:            # 原生 YYYYMMDD：整数或字符串
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    m = _DATE_RE.search(s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def patent_type(payload: dict) -> str:
    """'invention' / 'utility' / 'design' / 'unknown'。

    双信号：公告号尾缀（U→实用新型，B/Y→发明授权，A→发明申请）+
    申请号第5位（CN2016**2**…→2=实用新型，1=发明）。
    """
    pn = str(payload.get("pn", "")).strip()
    apno = str(payload.get("apno", "")).strip()
    if pn.endswith(UTILITY_PN_SUFFIX):
        return "utility"
    if pn.endswith(INVENTION_PN_SUFFIX):
        return "invention"
    if pn.endswith(INVENTION_PENDING):
        return "invention_pending"
    if apno.startswith("CN") and len(apno) > 7 and apno[6].isdigit():
        # 中国申请号 CN+YYYY+类型位：1=发明 2=实用新型 3=外观（GB/T 标准申请号结构）
        return "invention" if apno[6] == "1" else ("utility" if apno[6] == "2" else "unknown")
    return "unknown"


def normalize_payload(payload: dict, source_id: str) -> dict:
    """把专利原始 payload 标准化为引擎记录（SQLite/文件/暂存三条供给路径共用）。"""
    p = payload or {}
    apdt, pbdt = _to_date(p.get("apdt")), _to_date(p.get("pbdt"))
    ptype = patent_type(p)
    # 授权周期口径：U/B/Y 公告号 pbdt 即授权公告日；A 文献（invention_pending）及
    # 类型不明者 pbdt 仅为公开日，须有显式 grant_date 才计授权周期，否则缺失。
    gdt = _to_date(p.get("grant_date"))
    grant_dt = (gdt or pbdt) if ptype in ("utility", "invention") else gdt
    inventors = [x for x in str(p.get("inventor", "")).split("|") if x]
    return {
        "source_id": source_id,
        "pn": p.get("pn"), "apno": p.get("apno"), "title": p.get("title"),
        "type": ptype,
        "apdt": p.get("apdt"), "pbdt": p.get("pbdt"),
        "grant_date": p.get("grant_date"),
        "grant_days": (grant_dt - apdt).days if (apdt and grant_dt) else None,
        "inventors": inventors,
        "ipc4_list": [x.strip()[:4] for x in p.get(IPC4_KEY, []) if x.strip()],
        "family_offices": p.get(FAMILY_KEY, []) or [],
        "legal_status": p.get(LEGAL_KEY),
        "abstract": p.get(ABSTRACT_KEY),
        "claim_1": p.get(CLAIM1_KEY),
    }


def parse_patents(raw_dir, company_key: str) -> list:
    """读取某企业的全部专利原始包，标准化为引擎记录。"""
    out = []
    cdir = raw_dir / company_key
    if not cdir.exists():
        return out
    for f in sorted(cdir.glob("PAT-*.json")):
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append(normalize_payload(env.get("payload", {}),
                                     env.get("source_id", f.stem)))
    return out


def winsorize(values, p_lo=0.01, p_hi=0.99):
    """论文口径：按1%/99%分位 winsorize（样本≤2时不做处理）。"""
    if len(values) <= 2:
        return list(values)
    s = sorted(values)
    import math
    def pct(ps, q):
        idx = min(len(ps) - 1, max(0, int(math.floor(q * (len(ps) - 1)))))
        return ps[idx]
    lo, hi = pct(s, p_lo), pct(s, p_hi)
    return [min(hi, max(lo, v)) for v in values]


def compute_pqi(patents, today=None) -> dict:
    """五项PQI指标 + 组合分（可计算维度动态调权）。同期群=本组合按申请年分组。"""
    if not patents:
        return {"available": False, "reason": "no_patents",
                "note": "该企业当前无专利原始数据，PQI 维度留空并提示数据源缺口"}

    today = today or date.today()
    # —— 1. 发明占比
    n_inv = sum(1 for p in patents if p["type"] in ("invention", "invention_pending"))
    invention_share = n_inv / len(patents)

    # —— 2. 同族规模（同期群=申请年，归一化+winsorize）
    fam_raw = [len(p["family_offices"]) or None for p in patents]
    fam_norm, fam_note = [], None
    if any(fam_raw):
        fam_w = winsorize([v for v in fam_raw if v])
        filled, wi = [], 0
        for v in fam_raw:
            if v:
                filled.append(fam_w[wi]); wi += 1
            else:
                filled.append(None)
        by_year = {}
        for p, v in zip(patents, filled):
            if v:
                by_year.setdefault(str(p["apdt"])[:4], []).append(v)
        max_by_year = {y: max(vs) for y, vs in by_year.items()}
        fam_norm = [round(v / max_by_year[str(p["apdt"])[:4]], 4) if v else None
                    for p, v in zip(patents, filled)]
        avg_family = round(sum(v for v in fam_norm if v) / max(1, len([v for v in fam_norm if v])), 4)
    else:
        fam_note = "同族数据源字段缺失（gateway 待扩展），维度留空"
        fam_norm = [None] * len(patents)

    # —— 3. 4位IPC广度
    scope_norm, scope_note = [], None
    if any(p["ipc4_list"] for p in patents):
        by_year = {}
        for p in patents:
            by_year.setdefault(str(p["apdt"])[:4], []).append(len(set(p["ipc4_list"])) or None)
        max_by_year = {y: max(v for v in vs if v) for y, vs in by_year.items() if any(vs)}
        for p in patents:
            n = len(set(p["ipc4_list"]))
            year = str(p["apdt"])[:4]
            scope_norm.append(round(n / max_by_year[year], 4) if n and year in max_by_year else None)
        avg_scope = round(sum(v for v in scope_norm if v) / max(1, len([v for v in scope_norm if v])), 4)
    else:
        scope_note = "IPC 分类号字段缺失，维度留空"
        scope_norm = [None] * len(patents)

    # —— 4. 授权周期（GRANT_Pi = 1 - Δt/Max(Δt同期群)）
    grant_norm = []
    by_year = {}
    for p in patents:
        if p["grant_days"] is not None:
            by_year.setdefault(str(p["apdt"])[:4], []).append(p["grant_days"])
    max_by_year = {y: max(vs) for y, vs in by_year.items()}
    for p in patents:
        year = str(p["apdt"])[:4]
        if p["grant_days"] is not None and year in max_by_year:
            mx = max_by_year[year]
            # 同期群最大Δt=0（同年同日授权）时按最优值1.0处理，避免除零
            grant_norm.append(1.0 if mx == 0 else round(1 - p["grant_days"] / mx, 4))
        else:
            grant_norm.append(None)
    grant_avail = [v for v in grant_norm if v is not None]
    avg_grant = round(sum(grant_avail) / len(grant_avail), 4) if grant_avail else None

    # —— 5. 发明人留存率（Top3核心发明人 × 近24个月新申请）
    from collections import Counter
    cnt = Counter(i for p in patents for i in p["inventors"])
    core = [n for n, _ in cnt.most_common(3)]
    cutoff = date(today.year - 2, today.month, today.day)
    recent = [p for p in patents if _to_date(p["apdt"]) and _to_date(p["apdt"]) >= cutoff]
    recent_inventors = {i for p in recent for i in p["inventors"]}
    if not recent:
        # 数据窗口问题（样本只含历史申请）≠团队流失：标不可算、留空走动态调权
        retention, retention_note = None, (
            "近24个月无样本内新申请，留存率不可算（数据窗口问题，非团队流失信号）；"
            "维度留空走动态调权，请核实抓取范围是否覆盖近期申请")
    else:
        retention = (len([i for i in core if i in recent_inventors]) / len(core)) if core else None
        retention_note = None

    per_patent = []
    for p, fam, sco, gra in zip(patents, fam_norm, scope_norm, grant_norm):
        per_patent.append({"source_id": p["source_id"], "pn": p["pn"], "title": p["title"],
                           "type": p["type"], "family_index": fam, "scope_index": sco,
                           "grant_index": gra, "legal_status": p.get("legal_status"),
                           "legal_factor": _legal_factor(p),
                           "value_score": _value_score(p, fam, sco, gra)})

    dims = {"invention_share": {"value": round(invention_share, 4), "weight": 0.25, "available": True},
            "avg_family_index": {"value": avg_family if any(fam_raw) else None, "weight": 0.25,
                                  "available": any(fam_raw), "note": fam_note},
            "avg_scope_index": {"value": avg_scope if any(p["ipc4_list"] for p in patents) else None,
                                "weight": 0.20, "available": any(p["ipc4_list"] for p in patents),
                                "note": scope_note},
            "avg_grant_index": {"value": avg_grant, "weight": 0.15,
                                "available": avg_grant is not None},
            "inventor_retention": {"value": retention, "weight": 0.15,
                                   "available": retention is not None, "note": retention_note}}
    avail = [d for d in dims.values() if d["available"]]
    total_w = sum(d["weight"] for d in avail)
    for d in dims.values():
        d["effective_weight"] = round(d["weight"] / total_w, 4) if d["available"] else 0.0
    composite = round(sum(d["value"] * d["effective_weight"] for d in avail), 4) if avail else None

    return {"available": True, "n_patents": len(patents),
            "n_invention": n_inv, "n_utility": len(patents) - n_inv,
            "dims": dims, "composite": composite,
            "per_patent": per_patent,
            "recent_applications_24m": len(recent)}


def _value_score(p, fam, sco, gra) -> float | None:
    """漏斗用单件专利价值分：同族/广度/授权速度加权（缺失维度动态调权）×法律状态折减。
    法律状态缺失不惩罚（降级不虚构）；已知失效×0.35、审中×0.85，避免失效件霸榜漏斗。"""
    items = []
    if fam is not None:
        items.append((fam, 0.4))
    if sco is not None:
        items.append((sco, 0.3))
    if gra is not None:
        items.append((gra, 0.3))
    if not items:
        # 仅著录项可用时的退化打分：发明专利 > 实用新型，授权越快越好（无周期数据时取0.5）
        base = 0.6 if p["type"] in ("invention", "invention_pending") else 0.4
        return round(base * _legal_factor(p), 4)
    tw = sum(w for _, w in items)
    return round(sum(v * w for v, w in items) / tw * _legal_factor(p), 4)
