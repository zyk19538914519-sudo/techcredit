#!/usr/bin/env python3
"""上市企业研发人员/研发投入采集helper（venv 运行，stdout 仅 JSON）。

来源：巨潮资讯年报PDF全文（static.cninfo.com.cn，免费无Key）。
覆盖创新积分2.0 指标3 研发人员占比分子、指标5 分母、指标1/2 研发费用交叉。
"""
from __future__ import annotations
import argparse
import io
import json
import re
import urllib.request

import akshare as ak
from pypdf import PdfReader

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/126 Safari/537.36"}


def _latest_annual(code: str):
    df = ak.stock_zh_a_disclosure_report_cninfo(symbol=code, market="沪深京", category="",
                                                start_date="20230101", end_date="20261231")
    nd = df[df["公告标题"].str.contains(r"20\d{2}年年度报告", regex=True, na=False)
            & ~df["公告标题"].str.contains("摘要|英文|取消", na=False)]
    if nd.empty:
        return None, None
    nd = nd.sort_values("公告时间", ascending=False)
    row = nd.iloc[0]
    m = re.search(r"announcementId=(\d+)", str(row["公告链接"]))
    if not m:
        return None, None
    date = str(row["公告时间"])[:10]
    return str(row["公告标题"]), f"http://static.cninfo.com.cn/finalpage/{date}/{m.group(1)}.PDF"


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return None


def _extract(text: str) -> dict:
    out = {}
    pats = {
        "研发人员数量(人)": [r"研发人员数量[（(]人[)）]\s*([\d,]+)", r"研发人员人数[（(]人[)）]\s*([\d,]+)",
                       r"研发人员[共合计]*\s*([\d,]+)\s*人"],
        "研发人员占比(%)": [r"研发人员数量占比[（(]%[)）]\s*([\d.]+)", r"研发人员数量占比\s*([\d.]+)\s*%?",
                       r"研发人员占[公司员工总数比例]*\s*([\d.]+)\s*%"],
        "员工总数(人)": [],  # 年报口径易混母公司/合并，改用 研发人员/占比 推算
        "研发费用(元)": [],  # PDF 单位易混(千元/万元)，研发费用以 AKShare 利润表(元)为准
        "研发投入占比(%)": [r"研发投入占营业收入比例[（(]%[)）]\s*([\d.]+)", r"研发投入总额占营业收入[比重比例]*\s*([\d.]+)\s*%",
                       r"研发投入占营业收入[比重比例]*\s*([\d.]+)\s*%"],
    }
    for key, ps in pats.items():
        for p in ps:
            m = re.search(p, text)
            if m:
                out[key] = _num(m.group(1))
                break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code")
    ap.add_argument("--name")
    a = ap.parse_args()
    code, name = a.code, a.name
    if not code and name:
        lst = ak.stock_info_a_code_name()
        hit = lst[lst["name"] == name]
        if hit.empty:
            cand = lst[lst["name"].apply(lambda s: isinstance(s, str) and len(s) >= 3 and s in name)]
            if not cand.empty:
                hit = cand.assign(_len=cand["name"].str.len()).sort_values("_len", ascending=False).head(1)
        if hit.empty:
            print(json.dumps({"code": None, "name": name, "records": [],
                              "error": "未匹配A股代码（非上市）"}, ensure_ascii=False))
            return
        code = str(hit.iloc[0]["code"]); name = str(hit.iloc[0]["name"])
    if not code:
        print(json.dumps({"records": [], "error": "缺 code/name"}, ensure_ascii=False)); return
    title, pdf = _latest_annual(code)
    if not pdf:
        print(json.dumps({"code": code, "name": name, "records": [], "error": "未找到年报"}, ensure_ascii=False)); return
    req = urllib.request.Request(pdf, headers=UA)
    raw = urllib.request.urlopen(req, timeout=180).read()
    reader = PdfReader(io.BytesIO(raw))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    fields = _extract(text)
    hc, ratio = fields.get("研发人员数量(人)"), fields.get("研发人员占比(%)")
    if hc and ratio:
        fields["员工总数(人,推算)"] = round(hc / ratio * 100)
    fields["_口径说明"] = "研发人员/占比/研发投入占比取自年报PDF；员工总数为推算；研发费用(元)以AKShare利润表为准"
    period = (re.search(r"(20\d{2})\s*年年度报告", title) or [None, ""])[1] if re.search(r"(20\d{2})\s*年年度报告", title) else ""
    payload = {"period": period, "report": title, "pdf_url": pdf, "source": "cninfo年报PDF",
               "fields": fields, "pages": len(reader.pages)}
    if not fields:
        payload["_note"] = "PDF未匹配到研发人员/研发费用字段"
    rec = {"natural_key": f"{code}|rd|{period}", "title": f"{name or code}-研发人员/研发投入({period}年报)",
           "payload": payload}
    print(json.dumps({"code": code, "name": name, "records": [rec]}, ensure_ascii=False))


if __name__ == "__main__":
    main()