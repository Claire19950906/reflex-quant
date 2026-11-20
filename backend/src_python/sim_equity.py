#!/usr/bin/env python3
"""纸面净值曲线：把 quick_sim_trades 的已结算盈亏折成权益快照。

为什么需要：
    系统有 quick_sim_trades（含 pnl_usdt / settled / verdict），但**没有任何净值表**。
    结果是"赚了还是亏了"只能手工把几行相加，看不到回撤、看不到曲线，
    也没法回答"这套策略是否在退化"。

口径：
    初始资金 = env SIM_INITIAL_CAPITAL，默认 10000 USDT（库里没有这个字段）
    权益     = 初始资金 + 累计已结算 pnl_usdt
    未平仓   = settled=0 的笔数（只报数量，不用浮动盈亏污染曲线）
    回撤     = (当前权益 - 历史峰值) / 历史峰值

用法：
    python3 sim_equity.py            # 试运行
    python3 sim_equity.py --apply
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import db
import db_retry

_CREATE = """
CREATE TABLE IF NOT EXISTS sim_equity_snapshots (
    ts            INTEGER PRIMARY KEY,
    equity        REAL    NOT NULL,
    realized_pnl  REAL    NOT NULL DEFAULT 0,
    n_settled     INTEGER NOT NULL DEFAULT 0,
    n_open        INTEGER NOT NULL DEFAULT 0,
    peak_equity   REAL    NOT NULL DEFAULT 0,
    drawdown_pct  REAL    NOT NULL DEFAULT 0,
    note          TEXT    NOT NULL DEFAULT ''
)
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    capital = float(os.getenv("SIM_INITIAL_CAPITAL", "10000"))
    conn = sqlite3.connect(db.get_db_path(), timeout=20, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=20000")

    rows = conn.execute(
        "SELECT COALESCE(exit_ts, entry_ts) AS t, pnl_usdt, verdict, asset, action "
        "FROM quick_sim_trades WHERE COALESCE(settled,0)=1 "
        "AND pnl_usdt IS NOT NULL ORDER BY t ASC"
    ).fetchall()
    n_open = conn.execute(
        "SELECT COUNT(*) FROM quick_sim_trades WHERE COALESCE(settled,0)=0"
    ).fetchone()[0]

    snapshots = []
    equity = capital
    peak = capital
    realized = 0.0
    for i, (t, pnl, verdict, asset, action) in enumerate(rows, start=1):
        realized += float(pnl)
        equity = capital + realized
        peak = max(peak, equity)
        dd = (equity - peak) / peak * 100 if peak else 0.0
        snapshots.append({
            "ts": int(t), "equity": round(equity, 4),
            "realized_pnl": round(realized, 4),
            "n_settled": i, "n_open": int(n_open),
            "peak_equity": round(peak, 4),
            "drawdown_pct": round(dd, 4),
            "note": f"{asset} {action} {verdict} {float(pnl):+.3f}",
        })

    if snapshots:
        last = snapshots[-1]
        print(f"初始资金 {capital:.0f} · 已结算 {last['n_settled']} 笔 · 未平仓 {n_open}")
        print(f"  累计盈亏 {last['realized_pnl']:+.2f} USDT "
              f"({last['realized_pnl'] / capital * 100:+.3f}%)")
        print(f"  当前权益 {last['equity']:.2f} · 峰值 {last['peak_equity']:.2f} "
              f"· 最大回撤 {min(s['drawdown_pct'] for s in snapshots):.3f}%")
    else:
        print(f"初始资金 {capital:.0f} · 无已结算交易 · 未平仓 {n_open}")

    if not args.apply:
        print("\n(试运行，未写库 — 加 --apply 生效)")
        return 0

    def _w(cur):
        cur.execute(_CREATE)
        cur.execute("BEGIN IMMEDIATE")
        cur.executemany(
            "INSERT INTO sim_equity_snapshots "
            "(ts, equity, realized_pnl, n_settled, n_open, peak_equity, drawdown_pct, note) "
            "VALUES (:ts,:equity,:realized_pnl,:n_settled,:n_open,:peak_equity,:drawdown_pct,:note) "
            "ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity, "
            "realized_pnl=excluded.realized_pnl, n_open=excluded.n_open, "
            "peak_equity=excluded.peak_equity, drawdown_pct=excluded.drawdown_pct",
            snapshots,
        )
        cur.execute("COMMIT")
    db_retry.run_write(conn, _w)
    print(f"已写入 {len(snapshots)} 个快照")
    return 0


if __name__ == "__main__":
    sys.exit(main())
