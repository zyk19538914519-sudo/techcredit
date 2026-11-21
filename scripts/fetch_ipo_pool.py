#!/usr/bin/env python3
"""抓取东财IPO申报/辅导备案池 → data/screening/ipo_declare.json + ipo_tutor.json（免费缓存）。
在 .venv（含akshare）运行：.venv/bin/python scripts/fetch_ipo_pool.py"""
import datetime
import json
from pathlib import Path

import akshare as ak
import pandas as pd

OUT = Path("/root/projects/techcredit/data/screening")
OUT.mkdir(parents=True, exist_ok=True)


def norm(df):
    df = df.astype(object).where(pd.notnull(df), None)
    recs = df.to_dict("records")
    for r in recs:
        for k, v in list(r.items()):
            if isinstance(v, (datetime.date, datetime.datetime)):
                r[k] = v.isoformat()
    return recs


for fn, name in [("stock_ipo_declare_em", "ipo_declare"), ("stock_ipo_tutor_em", "ipo_tutor")]:
    df = getattr(ak, fn)()
    recs = norm(df)
    fp = OUT / f"{name}.json"
    fp.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    print(f"{name}: {len(recs)} rows -> {fp}")