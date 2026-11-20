#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_trade_decisions_entry_price.py — v310 P0: 补齐历史 trade_decisions.entry_price

背景
----
trade_decisions 2101 行里 2074 行 entry_price IS NULL (99% 缺失)。根因:
写入方 (trade_decisions.log_decision / quick_sim) 调用时没传价, 而决策引擎
又一直从 `entry_price` 读入场价去算 forward 收益 —— 于是:

    结算率 = 25/2101 = 1.19%
    → 「结算 → 漏检 → 复盘」三条下游一起断
    → A 类 (买卖信号判断) 反思维度永远 0 样本
    → 因果链 direction_hit 无法回填, 真命中率停在 0.30

本脚本按每行自己的 decision_ts, 从行情表回溯该时点之前最近一根真实价格:

    ohlcv_intraday (分钟线, 优先) → ohlcv_daily (日线) → market_ticks (快照)

安全性
------
· 幂等: 只 UPDATE `entry_price IS NULL` 的行, 重复跑不会覆盖已有价。
· 不猜价: 三张表都查不到时保持 NULL, 绝不写入任何插值/估算值。
· 不动其他列, 不删行。
· 默认 dry-run, 加 --commit 才落库。

用法
----
    python3 backfill_trade_decisions_entry_price.py            # 只报告
    python3 backfill_trade_decisions_entry_price.py --commit   # 落库
    python3 backfill_trade_decisions_entry_price.py --commit --limit 500
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db as _db  # noqa: E402

DB_PATH = BACKEND / "trident_event_bus.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库 (默认只报告)")
    ap.add_argument("--limit", type=int, default=100000, help="最多处理多少行")
    args = ap.parse_args()

    conn = _connect()
    t0 = time.time()
    try:
        before = conn.execute(
            """SELECT COUNT(*) FROM trade_decisions
               WHERE entry_price IS NULL OR entry_price <= 0"""
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0]
        print("=" * 64)
        print("entry_price 回填 — v310 P0")
        print("=" * 64)
        print(f"  trade_decisions 总数 : {total}")
        print(f"  待回填 (NULL/<=0)    : {before}")

        rows = conn.execute(
            """SELECT id, asset, decision_ts, final_action, decision_type
               FROM trade_decisions
               WHERE entry_price IS NULL OR entry_price <= 0
               ORDER BY decision_ts DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()

        filled = 0
        by_asset: dict[str, int] = {}
        by_source: dict[str, int] = {}
        missed: list = []
        for r in rows:
            asset = r["asset"]
            ts = r["decision_ts"]
            price = _db.resolve_entry_price(conn, asset, ts)
            if price and price > 0:
                filled += 1
                by_asset[asset] = by_asset.get(asset, 0) + 1
                by_source["resolved"] = by_source.get("resolved", 0) + 1
                if args.commit:
                    conn.execute(
                        "UPDATE trade_decisions SET entry_price=? WHERE id=?",
                        (price, r["id"]),
                    )
            else:
                by_source["no_market_data"] = by_source.get("no_market_data", 0) + 1
                if len(missed) < 8:
                    missed.append((r["id"], asset, ts))

        if args.commit:
            conn.commit()

        after = conn.execute(
            """SELECT COUNT(*) FROM trade_decisions
               WHERE entry_price IS NULL OR entry_price <= 0"""
        ).fetchone()[0]

        print(f"\n  成功回填              : {filled}")
        print(f"  无行情可取 (保持 NULL) : {by_source.get('no_market_data', 0)}")
        print(f"  回填后仍缺价           : {after}  (回填前 {before})")
        if by_asset:
            print("  按品种:", dict(sorted(by_asset.items(), key=lambda kv: -kv[1])))
        if missed:
            print("  无行情样例 (id, asset, ts):")
            for m in missed:
                print(f"    {m[0]}  {m[1]}  {m[2]}")
        if not args.commit:
            print("\n  (dry-run, 未落库 — 加 --commit 生效)")
        else:
            print(f"\n  ✅ 已落库, 耗时 {time.time() - t0:.1f}s")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
