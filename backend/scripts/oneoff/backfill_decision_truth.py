#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_decision_truth.py — 用真实 K 线回填"决策后市场实际涨跌"。

    python3 backfill_decision_truth.py --dry-run   # 只看会算什么
    python3 backfill_decision_truth.py --apply     # 真正写入

背景见 decision_truth.py 的模块说明。核心区别:

    forward_pnl      = 模拟盘这笔单的净盈亏 (只有开仓的 BUY/SELL 有)
    forward_move_pct = 决策之后市场本身走了多少 (所有决策都有, HOLD 也有)

验收 #2 要的是后者 —— 前者只能覆盖 0.2% 的决策, 样本量根本不够。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

from signals import decision_truth as dt  # noqa: E402

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    str(Path(__file__).resolve().parents[2]), "trident_event_bus.db"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 ai_decisions.forward_move_pct (真实行情)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--apply", action="store_true", help="真正写入 (默认 dry-run)")
    ap.add_argument("--limit", type=int, default=20000)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    print("[INFO] DB =", args.db)

    # 列可能还不存在 (旧库) —— 先补上再算
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    if "forward_move_pct" not in cols:
        conn.execute("ALTER TABLE ai_decisions ADD COLUMN forward_move_pct REAL DEFAULT NULL")
        conn.commit()
        print("[OK] 新增列 forward_move_pct")

    t0 = time.time()
    index = dt.KlineIndex(conn)
    print("[INFO] K 线索引装载: %.1fs, %d 组 (symbol, timeframe)"
          % (time.time() - t0, len(index._series)))

    truths = dt.collect_decision_truth(conn, index=index, limit=args.limit)
    by_asset: dict = {}
    for t in truths:
        by_asset[t["asset"]] = by_asset.get(t["asset"], 0) + 1
    print("[INFO] 可判定真值: %d 条" % len(truths))
    print("[INFO] 按品种:", json.dumps(by_asset, ensure_ascii=False))
    if truths:
        vals = sorted(abs(t["forward_move_pct"]) for t in truths)
        print("[INFO] |涨跌幅| 中位=%.4f%% p90=%.4f%% max=%.4f%%"
              % (vals[len(vals) // 2], vals[int(len(vals) * 0.9)], vals[-1]))
        print("[INFO] 样例:", json.dumps(truths[:3], ensure_ascii=False))

    if not args.apply:
        print("[DRY-RUN] 加 --apply 执行回填")
        conn.close()
        return 0

    n = dt.persist_truth(conn, truths)
    print("[OK] 已写入 forward_move_pct: %d 行" % n)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
