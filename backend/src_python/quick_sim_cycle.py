#!/usr/bin/env python3
"""quick_sim 周期执行器（v477）。

背景
----
`quick_sim.py` 里 `open_trades()` / `settle_trades()` 是**唯一的结算权威**，
但全库没有任何调度调用它们（engine 里 forward_tracker 已废弃，注释说"结算唯一
权威已切到 quick_sim"，而 quick_sim 本身从来没被跑过）。后果：
  · quick_sim_trades 永远 0 笔；
  · `ai_decisions.settled / is_correct / forward_pnl` 永远为空 →
    胜率、赔率、反思、样本外校准全部无料可用，界面永远是"暂无数据"。

本脚本只做一件事：按真实行情开仓 + 结算到期仓，跑完打印一行统计。
取不到价格的资产**不建仓也不结算**（保持原状），绝不编造成交价。

用法：
    python3 quick_sim_cycle.py            # 跑一轮
    python3 quick_sim_cycle.py --dry-run  # 只统计，不落库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time

import config
import db


def _price_fn(conn: sqlite3.Connection):
    """返回 Callable[asset -> 最新价 | None]。

    优先级：market_ticks（多源实时 tick）→ kline_bars（分钟/日线收盘）。
    两个都取不到就返回 None，由 quick_sim 自己决定"跳过"。
    """

    def _lookup(asset: str):
        a = str(asset or "").strip().upper()
        if not a:
            return None
        cands = [a]
        if a.endswith("USDT"):
            cands.append(a[:-4])
        else:
            cands.append(a + "USDT")
        for s in cands:
            try:
                row = conn.execute(
                    "SELECT price FROM market_ticks WHERE symbol=? "
                    "ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
                if row and row[0]:
                    return float(row[0])
            except sqlite3.Error:
                pass
        for s in cands:
            try:
                row = conn.execute(
                    "SELECT close FROM kline_bars WHERE symbol=? "
                    "ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
                if row and row[0]:
                    return float(row[0])
            except sqlite3.Error:
                pass
        return None

    return _lookup


def main() -> int:
    ap = argparse.ArgumentParser(description="quick_sim 开仓+结算周期执行器")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不落库")
    ap.add_argument("--limit", type=int, default=50, help="单轮最多建仓数")
    args = ap.parse_args()

    import quick_sim

    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    t0 = time.time()
    try:
        opened = quick_sim.open_trades(conn, _price_fn(conn), limit=args.limit)
        settled = quick_sim.settle_trades(conn, _price_fn(conn))
        if not args.dry_run:
            conn.commit()
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(settled) st FROM quick_sim_trades").fetchone()
        dec = conn.execute(
            "SELECT COUNT(*) n, SUM(settled) st FROM ai_decisions").fetchone()
        print(f"[QUICK-SIM] opened={len(opened)} settled={len(settled)} "
              f"trades={row[0]}/{row[1] or 0}settled "
              f"decisions={dec[0]}/{dec[1] or 0}settled "
              f"{time.time() - t0:.1f}s"
              f"{' (dry-run)' if args.dry_run else ''}", flush=True)
        for s in settled[:5]:
            print(f"   · {s.get('asset')} {s.get('side')} "
                  f"pnl={s.get('pnl_pct')} verdict={s.get('verdict')}",
                  flush=True)
    except sqlite3.Error as exc:
        conn.rollback()
        print(f"[QUICK-SIM] DB error: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
