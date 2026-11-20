#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把生产库按时间窗导成回放 bundle（只读，不写生产库）。

配合 replay_bundle.py 用：
    python3 scripts/export_bundle.py --days 7 --out /tmp/bundle.json
    python3 scripts/replay_bundle.py --bundle /tmp/bundle.json --md /tmp/report.md

为什么单独一个脚本：
    手写 SQL 拼 JSON 容易漏表、漏列、把整数 ts 和文本 created_at 混在一起。
    这里按「表 → 时间列」的固定映射导出，列名原样保留，回放侧不用二次翻译。

只读保证：
    连接用 `file:...?mode=ro`，即使 --db 指到生产库也写不进去；
    导出过程不建临时表、不改任何行。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BACKEND, "src_python")
for p in (SRC, BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

# 表 -> (时间列, 是否 unix 秒)。None 表示整表导出（表本身很小）。
TABLES = {
    "ai_decisions": ("created_at", False),
    "quick_sim_trades": ("created_at", False),
    "trade_reflections_v2": ("created_at", False),
    "decision_reflection": ("ts", True),
    "signal_performance": ("ts", True),
    "news_events": ("ts", True),
    "critical_price_moves": ("ts", True),
    "ohlcv_intraday": ("ts", True),
    "event_move_attribution": ("move_ts", True),
    "t2_positions": ("entry_ts", True),
    "sim_equity_snapshots": ("ts", True),
}


def export(db_path: str, days: int) -> dict:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    since_ts = int(time.time()) - days * 86400
    since_txt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_ts))
    out = {
        "meta": {
            "source": os.path.basename(db_path),
            "db_path": db_path,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "window_days": days,
            "note": "只读导出；time 列为 created_at 的用文本比较，其余用 unix 秒",
        },
        "tables": {},
    }
    for name, (tcol, is_int) in TABLES.items():
        exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()[0]
        if not exists:
            out["tables"][name] = []
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        if tcol not in cols:
            sql = f"SELECT * FROM {name}"
            rows = conn.execute(sql).fetchall()
        else:
            cond = f"{tcol} >= ?"
            rows = conn.execute(f"SELECT * FROM {name} WHERE {cond}", (since_ts,) if is_int
                                else (since_txt,)).fetchall()
        out["tables"][name] = [dict(r) for r in rows]
    conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="只读导出回放 bundle")
    ap.add_argument("--db", default=None,
                    help="源库路径（默认 backend/trident_event_bus.db）")
    ap.add_argument("--days", type=int, default=7, help="时间窗（天）")
    ap.add_argument("--out", required=True, help="输出 bundle.json")
    ap.add_argument("--tables", default=None,
                    help="只导这些表，逗号分隔（默认全部）")
    args = ap.parse_args()

    db_path = args.db or os.path.join(BACKEND, "trident_event_bus.db")
    if not os.path.exists(db_path):
        print(f"库不存在：{db_path}", file=sys.stderr)
        return 2
    global TABLES
    if args.tables:
        keep = {t.strip() for t in args.tables.split(",") if t.strip()}
        TABLES = {k: v for k, v in TABLES.items() if k in keep}

    bundle = export(db_path, args.days)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(bundle, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"导出 → {args.out}")
    for name, rows in bundle["tables"].items():
        print(f"  {name}: {len(rows)} 行")
    print(f"时间窗：近 {args.days} 天（{bundle['meta']['exported_at']} 导出）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
