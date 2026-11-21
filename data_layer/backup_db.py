#!/usr/bin/env python3
"""techcredit SQLite 热备份：在线一致性备份 + 保留最近 N 份。

用法：
  python3 backup_db.py               # 备份一次
  python3 backup_db.py --list        # 查看已有备份
  python3 backup_db.py --keep 30     # 指定保留份数（默认 14）
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime

import config

BACKUP_DIR = config.DATA_DIR / "backups"


def backup(keep: int = 14) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"techcredit-{ts}.db"
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)          # 在线备份，写入过程中也保持一致快照
    finally:
        dst.close()
        src.close()
    backups = sorted(BACKUP_DIR.glob("techcredit-*.db"))
    for old in backups[:-keep]:
        old.unlink()
    print(f"备份完成: {dest} ({dest.stat().st_size/1024:.1f} KB)，当前保留 {min(len(backups), keep)} 份")


def list_backups() -> None:
    items = sorted(BACKUP_DIR.glob("techcredit-*.db"))
    if not items:
        print("暂无备份")
        return
    for p in items:
        print(f"  {p.name}  {p.stat().st_size/1024:.1f} KB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=14)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    list_backups() if a.list else backup(a.keep)
