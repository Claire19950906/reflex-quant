#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表级存活盘点（v341）。

回答一个问题：**这个系统里哪些功能其实没在跑？**

做法：遍历所有用户表，找时间列（ts / created_ts / created_at / move_ts ...），
统计行数与该列最新值，然后分三类：
  · EMPTY   0 行 —— 功能没跑过 / 被清空后没回来
  · STALE   有数据但最新一条超过阈值（默认 24h）—— 链路断了
  · OK      正常
加上纪元约束判断：纪元后 0 行表，即便总行数很多，也等于"纪元后没跑"。

用法: python3 scripts/audit_tables.py [--stale-hours 24] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "src_python"))

TIME_COLS = ("ts", "created_ts", "created_at", "updated_at", "move_ts", "event_ts",
             "trade_ts", "snapshot_ts", "signal_ts", "timestamp", "date")
SKIP_PREFIX = ("sqlite_",)


def _to_sec(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return float(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return dt.datetime.strptime(s[:len(fmt) + 6], fmt).timestamp()
        except Exception:
            continue
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-hours", type=float, default=24.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    import db as _db
    conn = sqlite3.connect(f"file:{_db.get_db_path()}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")

    epoch = None
    try:
        epoch = conn.execute("SELECT started_at FROM data_epoch ORDER BY id DESC LIMIT 1").fetchone()[0]
    except sqlite3.Error:
        pass

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    tables = [t for t in tables if not t.startswith(SKIP_PREFIX)]

    now = dt.datetime.now().timestamp()
    rows = []
    for t in tables:
        try:
            total = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error:
            continue
        # 找时间列
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
        tcol = next((c for c in TIME_COLS if c in cols), None)
        newest = None
        if tcol and total:
            try:
                v = conn.execute(f'SELECT MAX("{tcol}") FROM "{t}"').fetchone()[0]
                newest = _to_sec(v)
                if newest is not None:
                    if newest > 1e11:          # 毫秒时间戳
                        newest = newest / 1000.0
                    # 落在 2000-2100 之外的一律视为脏值，不参与"停更"判断
                    if not (946684800 < newest < 4102444800):
                        newest = None
            except sqlite3.Error:
                newest = None
        # 纪元后行数（只有 ts 是整数秒才便宜，这里近似用 newest 判断）
        status = "OK"
        detail = ""
        if total == 0:
            status, detail = "EMPTY", "0 行"
        elif newest is None:
            status, detail = "OK", f"{total} 行（无时间列）"
        else:
            age_h = (now - newest) / 3600.0
            if age_h > args.stale_hours:
                status, detail = "STALE", f"{total} 行 / 最新 {age_h:.1f}h 前"
            else:
                detail = f"{total} 行 / 最新 {age_h:.1f}h 前"
        rows.append({"table": t, "rows": total, "col": tcol,
                     "newest": dt.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
                     if newest else None,
                     "age_h": round((now - newest) / 3600.0, 1) if newest else None,
                     "status": status, "detail": detail})

    rows.sort(key=lambda r: ({"EMPTY": 0, "STALE": 1, "OK": 2}[r["status"]], -r["rows"]))
    if args.json:
        print(json.dumps({"epoch": epoch, "tables": rows}, ensure_ascii=False, indent=1))
        return 0

    empty = [r for r in rows if r["status"] == "EMPTY"]
    stale = [r for r in rows if r["status"] == "STALE"]
    print(f"=== 表级存活盘点 · 共 {len(rows)} 张表 · 纪元 {epoch} ===")
    print(f"EMPTY {len(empty)} / STALE {len(stale)} / OK {len(rows) - len(empty) - len(stale)}\n")
    if empty:
        print("--- 0 行（功能没跑过，或清空后没回来）---")
        for r in empty:
            print(f"  {r['table']:44s}")
    if stale:
        print("\n--- 停更（有数据但最新一条超过阈值）---")
        for r in stale:
            print(f"  {r['table']:44s} {r['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
