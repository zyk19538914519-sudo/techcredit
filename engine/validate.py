#!/usr/bin/env python3
"""校验熔断层（Python功能实现层③）。

职责：
  1) Pydantic 强校验 Agent 返回 JSON（schemas.py 契约）；
  2) 核查每条结论绑定的 Source_ID 是否真实存在于原始数据包（SQLite sources 表），
     伪造 → 拦截并要求重试；
  3) G-Eval 置信度归一（Σconf 偏离1±0.05 时按比例归一并记录）；
  4) 熔断器：同一 Skill 连续 3 次校验失败则熔断，整次评估终止（防低质量输出污染总分）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data_layer"))

from pydantic import ValidationError

import schemas

MAX_RETRIES = 3


class CircuitBroken(Exception):
    """熔断：连续校验失败达上限。"""


def load_allowed_source_ids(conn, company_key=None) -> set:
    """从 sources 表取真实存在的 Source_ID（即原始数据包全集）。"""
    q = "SELECT source_id FROM sources"
    args = ()
    if company_key:
        q += " WHERE company_key=?"
        args = (company_key,)
    return {r[0] for r in conn.execute(q, args)}


def _collect_source_ids(obj) -> list:
    """递归收集 Agent 输出里所有 Source_ID 字段值。"""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("source_id", "bid_source_id") and isinstance(v, str):
                found.append(v)
            elif k in ("source_ids", "matched_patent_source_ids", "used_source_ids") \
                    and isinstance(v, list):
                found.extend(x for x in v if isinstance(x, str))
            elif k == "source_ids_json":
                continue
            else:
                found.extend(_collect_source_ids(v))
    elif isinstance(obj, list):
        for x in obj:
            found.extend(_collect_source_ids(x))
    return found


def _geval_weighted(level: int, confs: dict) -> float:
    """G-Eval 加权：Σ p(level_i)×level_i（4档制，输出0-4）。"""
    return sum(confs.get(i, 0.0) * i for i in range(1, 5))


def validate_output(skill: str, payload, allowed_source_ids: set) -> dict:
    """校验并加工 Agent 输出。

    返回 {"ok": bool, "errors": [...], "result": model|None, "geval_scores": {...}}
    ok=False 时 errors 逐条给出可反馈给 Skill 重试的原因。
    """
    errors = []
    model_cls = schemas.OUTPUT_MODELS.get(skill)
    if model_cls is None:
        return {"ok": False, "errors": [f"未注册的skill: {skill}"], "result": None}

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as e:
            return {"ok": False, "errors": [f"非法JSON: {e}"], "result": None}

    # 1) Pydantic 结构校验
    try:
        model = model_cls.model_validate(payload)
    except ValidationError as e:
        errs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            errs.append(f"{loc}: {err['msg']}")
        return {"ok": False, "errors": errs, "result": None}

    # 2) Source_ID 真实性核查（伪造即拦截）
    referenced = _collect_source_ids(model.model_dump())
    forged = sorted({s for s in referenced if s not in allowed_source_ids})
    if forged:
        errors.append(f"伪造Source_ID，库中不存在: {forged}；"
                      f"只允许绑定原始数据包内的 {len(allowed_source_ids)} 个真实ID，请重试")

    # 3) G-Eval 置信度归一
    geval_scores = {}
    if skill == "patent_review":
        for p in model.patents:
            confs = {1: p.conf_1, 2: p.conf_2, 3: p.conf_3, 4: p.conf_4}
            total = sum(confs.values())
            if total <= 0:
                errors.append(f"{p.source_id}: 四档置信度全为0")
            elif abs(total - 1.0) > 0.05:
                confs = {k: v / total for k, v in confs.items()}
            geval_scores[p.source_id] = round(_geval_weighted(p.rubric_level, confs), 4)

    if errors:
        return {"ok": False, "errors": errors, "result": None}
    return {"ok": True, "errors": [], "result": model, "geval_scores": geval_scores}


class CircuitBreaker:
    """连续失败熔断：MAX_RETRIES 次校验不过即抛 CircuitBroken。"""

    def __init__(self, max_retries: int = MAX_RETRIES):
        self.max_retries = max_retries
        self.fails: dict = {}

    def attempt(self, skill: str, payload, allowed_source_ids: set) -> dict:
        verdict = validate_output(skill, payload, allowed_source_ids)
        if verdict["ok"]:
            self.fails.pop(skill, None)
            return verdict
        self.fails[skill] = self.fails.get(skill, 0) + 1
        if self.fails[skill] >= self.max_retries:
            raise CircuitBroken(
                f"skill {skill} 连续{self.max_retries}次校验失败，评估流程熔断终止: "
                f"{verdict['errors'][:3]}")
        return verdict


if __name__ == "__main__":
    import argparse
    import store

    ap = argparse.ArgumentParser(description="独立校验 Agent 输出")
    ap.add_argument("--skill", required=True, choices=list(schemas.OUTPUT_MODELS))
    ap.add_argument("--payload", required=True, help="Agent 输出 JSON 文件")
    ap.add_argument("--company", default=None, help="限定该企业的 Source_ID 白名单")
    a = ap.parse_args()
    conn = store.connect()
    allowed = load_allowed_source_ids(conn, a.company)
    payload = json.loads(Path(a.payload).read_text(encoding="utf-8"))
    v = validate_output(a.skill, payload, allowed)
    print(json.dumps({"ok": v["ok"], "errors": v["errors"]}, ensure_ascii=False, indent=1))
    if v["ok"] and v["geval_scores"]:
        print("G-Eval加权分:", v["geval_scores"])
    sys.exit(0 if v["ok"] else 1)
