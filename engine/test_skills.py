#!/usr/bin/env python3
"""Skill 层自检：用真实上下文包按各 SKILL.md 量表生成"Agent 合规输出"，
过校验熔断（Pydantic + Source_ID 真实性核查）→ 总分汇总全链路。

同时验证各 SKILL.md 中标注的"常见被拦截原因"确实会被拦。

用法：python3 test_skills.py [--company 91350900587527783P]
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "data_layer"))

import store                     # noqa: E402
import config                    # noqa: E402
import schemas                   # noqa: E402
import validate                  # noqa: E402
import run_engine                # noqa: E402

PASS, FAIL = "OK ", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"  {PASS if cond else FAIL} {name}{(' | ' + detail) if detail and not cond else ''}")


def build_agent_outputs(company_key: str) -> dict:
    """按4个SKILL.md的量表对真实数据生成合规输出（模拟遵循量表的Agent）。"""
    ctx = config.DATA_DIR / "context" / company_key
    pack_pr = json.loads((ctx / "pack_patent_review.json").read_text(encoding="utf-8"))
    allowed = pack_pr["allowed_source_ids"]

    # --- Skill1：专利技术研判（G-Eval 4档 + 置信度）---
    patents_out = []
    for p in pack_pr["patents"]:
        # 量表判据：全部为实用新型、无独权文本 → 按SKILL.md降档评估规则给2档
        patents_out.append({
            "source_id": p["source_id"], "rubric_level": 2,
            "conf_1": 0.1, "conf_2": 0.5, "conf_3": 0.3, "conf_4": 0.1,
            "barrier_score": 45, "innovation_score": 40, "claim_quality_score": 50,
            "rationale": "实用新型且独权文本缺失，按降档规则评估：结构改进类常规方案，"
                         "壁垒有限，同族与IPC证据不足。",
            "evidence_quote": None, "red_flags": ["独权与摘要文本缺失，评估置信度受限"],
        })
    s1 = {"skill": "patent_review", "patents": patents_out,
          "portfolio_rationale": "组合以电池组结构件实用新型为主，技术增量集中在连接与防护结构，"
                                "无同族与IPC数据，整体研判置信度中等偏低。"}

    # --- Skill2：商业化对齐（中标源占位 → no_bid_data）---
    s2 = {"skill": "commercialization_fit", "bids": [],
          "overall_note": "中标公告数据源占位（TYC_TOKEN 待配置），本批无对齐结论，不排除贸易型订单风险。",
          "no_bid_data": True}

    # --- Skill3：反包装核查（宣传素材未接入 → 缺口声明行）---
    s3 = {"skill": "anti_packaging_factcheck",
          "claims": [{"claim_id": "c0",
                      "atomic_fact": "对外宣传素材尚未接入，暂无可核查原子声明",
                      "source_ids": [], "verdict": "unverifiable",
                      "evidence_quote": None}],
          "overall_note": "官网/新闻稿素材未接入（Tavily 占位），fact_score 暂不可计算，不构成否决。"}

    # --- Skill4：银行报告 ---
    pack_br = json.loads((ctx / "pack_bank_report.json").read_text(encoding="utf-8"))
    inn = pack_br["innovation_score"]
    used = sorted(allowed)[:3]
    s4 = {
        "skill": "bank_report",
        "report_md": (
            "# 技术信用画像摘要\n\n"
            f"## 一、企业技术底色\n{pack_pr['meta']['company_name']}（新能源与节能·稳定期），"
            f"创新积分2.0核心得分{inn['core_score']}（{inn['grade']}级），总分{inn['total_score']}。\n\n"
            "## 二、创新能力量化结果\n仅专利口径可回填：发明专利授权量按专利组合统计，其余八项"
            "（研发费用、研发强度、研发人员占比、每百人知识产权、技术合同成交额、主营收入、"
            "收入增长率、净资产利润率）数据缺失，权重已动态再分配，评级对数据缺口敏感，须补数复核。\n\n"
            "## 三、专利质量与技术壁垒\n样本以实用新型为主（发明占比0.375），技术研判2档："
            "结构件常规改进；同族与IPC字段缺失暂无法评估广度与地理布局。\n\n"
            "## 四、商业化与订单真实性\n中标公告数据源占位，订单真实性核查待TYC_TOKEN配置后补做。\n\n"
            "## 五、风险提示\n红灯R2发明人断层：样本专利发明人近24个月未再出现；黄灯Y4数据窗口："
            "样本全部为2014-2016年申请，疑为数据窗口问题，需扩大抓取范围核实。\n\n"
            "## 六、综合意见\n证据链不完整且存在红灯，建议补充财务与全量专利数据后重跑评估，"
            "暂不宜直接采信当前积分。"),
        "interview_checklist": [
            {"question_id": "q1",
             "question": "请提供近三年研发费用加计扣除申报表与研发人员名册，核实研发投入与队伍规模。",
             "trigger": "RD_EXPENSE等八项指标缺失", "source_ids": []},
            {"question_id": "q2",
             "question": "请说明2016年后核心研发团队的流动情况及继任发明人名单，确认研发连续性。",
             "trigger": "R2_INVENTOR_GAP", "source_ids": used},
            {"question_id": "q3",
             "question": "请提供近三年招投标中标台账与代表性合同，配合核查订单技术含量。",
             "trigger": "BID数据源缺口", "source_ids": []},
        ],
        "used_source_ids": used,
        "used_metric_codes": ["INVENTION_GRANTS"],
    }
    return {"patent_review": s1, "commercialization_fit": s2,
            "anti_packaging_factcheck": s3, "bank_report": s4}


def main():
    company_key = "91350900587527783P"
    if "--company" in sys.argv:
        company_key = sys.argv[sys.argv.index("--company") + 1]

    print("[S1] 生成4个Skill合规输出并写盘")
    outs = build_agent_outputs(company_key)
    out_dir = Path(tempfile.mkdtemp(prefix="agent_out_"))
    for name, body in outs.items():
        (out_dir / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    check("4个输出文件就绪", len(list(out_dir.glob("*.json"))) == 4)

    print("[S2] 校验熔断：4个输出逐一过Pydantic+Source_ID核查")
    conn = store.connect()
    allowed = validate.load_allowed_source_ids(conn, company_key)
    pack_pr = json.loads((config.DATA_DIR / "context" / company_key
                          / "pack_patent_review.json").read_text(encoding="utf-8"))
    allowed |= set(pack_pr["allowed_source_ids"])
    breaker = validate.CircuitBreaker()
    for name in schemas.OUTPUT_MODELS:
        v = breaker.attempt(name, outs[name], allowed)
        check(f"{name} 通过校验熔断", v["ok"], str(v["errors"])[:120])
    geval = validate.validate_output("patent_review", outs["patent_review"], allowed)["geval_scores"]
    check("G-Eval加权分已产出（0-4）", all(0 <= x <= 4 for x in geval.values()), str(geval))

    print("[S3] 契约反例：SKILL.md标注的被拦截原因逐一复现")
    bad = json.loads(json.dumps(outs["patent_review"]))
    bad["patents"][0]["source_id"] = "PAT-ZHY-999fake"
    v = validate.validate_output("patent_review", bad, allowed)
    check("伪造Source_ID被拦", not v["ok"])
    bad2 = json.loads(json.dumps(outs["patent_review"]))
    bad2["patents"][0]["rationale"] = "太短"
    v = validate.validate_output("patent_review", bad2, allowed)
    check("rationale<20字被拦", not v["ok"])
    bad3 = json.loads(json.dumps(outs["bank_report"]))
    bad3["interview_checklist"] = bad3["interview_checklist"][:2]
    v = validate.validate_output("bank_report", bad3, allowed)
    check("访谈清单<3条被拦", not v["ok"])
    bad4 = json.loads(json.dumps(outs["bank_report"]))
    bad4["used_metric_codes"] = ["NOT_EXIST_METRIC"]
    v = validate.validate_output("bank_report", bad4, allowed)
    check("不存在的指标编码被拦", not v["ok"])

    print("[S4] 全链路：引擎基线 + 校验熔断 + 总分汇总")
    base = run_engine.run(company_key, "新能源与节能", "稳定期", {}, {}, 5)
    final = run_engine.run_with_agent_outputs(base, out_dir)
    f = final["final"]
    check("语义分参与汇总", f["weights_used"]["semantic_available"] is True)
    check("红黄灯进入扣分", f["risk_penalty"] > 0, str(f["risk_penalty"]))
    check("R2红灯计入红灯清单", "R2_INVENTOR_GAP" in f["red_lights"], str(f["red_lights"]))
    check("综合分与分级产出", 0 <= f["final_score"] <= 100 and f["grade"] in "ABCD")
    check("fact_score=None时未误触发否决或如实未触发",
          (f["fact_score"] is None) == (not any("V2" in r for r in f["veto_reasons"])))
    print(f"    综合信用分 {f['final_score']}（{f['grade']}级）→ {f['recommendation']}")

    # 校验结果已随 run_with_agent_outputs 落库
    row = conn.execute("SELECT grade, final_score FROM evaluation_runs "
                       "WHERE run_id=? AND final_score IS NOT NULL",
                       (base["run_id"],)).fetchone()
    check("评估结果已落库evaluation_runs", row is not None and row[0] in "ABCD")
    n = conn.execute("SELECT count(*) FROM agent_outputs WHERE run_id=? AND ok=1",
                     (base["run_id"],)).fetchone()[0]
    check("4个Skill输出审计落库", n == 4, f"ok条数={n}")

    print("[S5] upload专利清单解析容错（dossier_pipeline.parse_patent_list）")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dossier_pipeline_probe", ROOT / "webapp" / "dossier_pipeline.py")
    dp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dp)
    plist = dp.parse_patent_list(
        '{"patents":[{"pn":"CN115000001B","inventor":["张三","李四"],"apdt":"2023-01-10",'
        '"legal_status":"授权"},{"publication_number":"CN216000009U","inventor":"王五",'
        '"apdt":"20230110","ipc":["H02J3/00"]}]}')
    check("patents包装形态解析2件", len(plist) == 2)
    check("别名映射publication_number→pn", plist[1].get("pn") == "CN216000009U")
    check("别名映射ipc→ipc4_list", plist[1].get("ipc4_list") == ["H02J3/00"])
    check("发明人列表转竖线分隔", plist[0].get("inventor") == "张三|李四")
    check("单件直传形态", len(dp.parse_patent_list('{"pn":"CN115000002B"}')) == 1)
    check("非JSON文本回退空列表", dp.parse_patent_list("普通文本专利说明，无结构化数据") == [])
    check("无pn记录被跳过", dp.parse_patent_list('[{"title":"缺公告号"}]') == [])

    shutil.rmtree(out_dir, ignore_errors=True)
    n_fail = sum(1 for c in results if not c)
    print(f"\nSkill自检完成：{len(results)}项，通过{len(results)-n_fail}项，失败{n_fail}项")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
