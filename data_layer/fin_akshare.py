#!/usr/bin/env python3
"""AKShare 财务采集helper（在 .venv 中运行，stdout 仅输出 JSON）。

覆盖创新积分2.0：研发费用金额/占比(1,2)、主营业务收入(7)、营收增长率(8)、净资产利润率(9)。
数据源：新浪财经利润表 + 新浪财务指标（AKShare 封装的公开接口，免费无 Key）。
研发人员占比(3)无公开接口，留空待自报/代理。
"""
from __future__ import annotations
import argparse
import json
import sys

import akshare as ak

INCOME_KEYS = ["营业总收入", "营业收入", "研发费用", "净利润"]
IND_KEYS = ["净资产收益率(%)", "加权净资产收益率(%)", "主营业务收入增长率(%)",
            "净利润增长率(%)", "每股净资产_调整前(元)"]


def _pick(row: dict, keys: list) -> dict:
    out = {}
    for k in keys:
        if k in row and row[k] not in (None, "", "--"):
            out[k] = row[k]
    # 模糊兜底（列名带后缀/前缀时）
    for k in keys:
        if k in out:
            continue
        for col, val in row.items():
            if k.split("(")[0] in str(col) and val not in (None, "", "--"):
                out[k] = val
                break
    return out


def _annual(df, date_col: str, n: int):
    norm = df[date_col].astype(str).str.replace("-", "", regex=False)
    d = df[norm.str.endswith("1231")].copy()
    d = d.sort_values(date_col, ascending=False).head(n)
    return json.loads(d.to_json(orient="records", force_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code")
    ap.add_argument("--name")
    ap.add_argument("--years", type=int, default=4)
    a = ap.parse_args()
    code, name = a.code, a.name
    if not code and name:
        lst = ak.stock_info_a_code_name()
        hit = lst[lst["name"] == name]
        if hit.empty:
            hit = lst[lst["name"].str.contains(name[:6], na=False)]
        if hit.empty:
            print(json.dumps({"code": None, "name": name, "records": [],
                              "error": "未匹配到A股代码（非上市）"}, ensure_ascii=False))
            return
        code = str(hit.iloc[0]["code"])
        name = str(hit.iloc[0]["name"])
    if not code:
        print(json.dumps({"records": [], "error": "缺少 code/name"}, ensure_ascii=False))
        return
    prefix = "sh" if code.startswith("6") else ("bj" if code.startswith(("4", "8")) else "sz")
    records = []
    try:
        inc = ak.stock_financial_report_sina(stock=prefix + code, symbol="利润表")
        for row in _annual(inc, "报告日", a.years):
            period = str(row.get("报告日"))
            fields = _pick(row, INCOME_KEYS)
            rev = fields.get("营业收入") or fields.get("营业总收入")
            rd = fields.get("研发费用")
            if rd not in (None, "") and rev:
                try:
                    fields["研发费用占营收比(%)"] = round(float(rd) / float(rev) * 100, 2)
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            records.append({"natural_key": f"{code}|income|{period}",
                            "title": f"{name or code}-利润表(研发/营收/净利)-{period}",
                            "payload": {"period": period, "statement": "利润表",
                                        "source": "sina via akshare", "fields": fields}})
    except Exception as exc:
        records.append({"natural_key": f"{code}|income|error",
                        "title": f"{name or code}-利润表采集失败",
                        "payload": {"_error": repr(exc)[:200]}})
    try:
        ind = ak.stock_financial_analysis_indicator(symbol=code, start_year="2019")
        for row in _annual(ind, "日期", a.years):
            period = str(row.get("日期"))
            records.append({"natural_key": f"{code}|indicator|{period}",
                            "title": f"{name or code}-财务指标(ROE/增速)-{period}",
                            "payload": {"period": period, "statement": "财务指标",
                                        "source": "sina via akshare",
                                        "fields": _pick(row, IND_KEYS)}})
    except Exception as exc:
        records.append({"natural_key": f"{code}|indicator|error",
                        "title": f"{name or code}-财务指标采集失败",
                        "payload": {"_error": repr(exc)[:200]}})
    print(json.dumps({"code": code, "name": name, "records": records}, ensure_ascii=False))


if __name__ == "__main__":
    main()