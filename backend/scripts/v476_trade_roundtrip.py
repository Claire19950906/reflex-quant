#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v476 交易全链路自检（只在临时库跑，不碰生产库）。

覆盖：AI 决策 → 前置闸 → 下单 → 镜像进 T2（含开仓费）→ 行情下跌 → 止损平仓
      → 手续费双边扣除 → 账户重算 → 对账归零。

跑法：
    cd backend && python3 scripts/v476_trade_roundtrip.py
    cd backend && python3 scripts/v476_trade_roundtrip.py --keep   # 保留临时库看细节
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src_python"
sys.path.insert(0, str(SRC))

import account_ledger as al
import stops
import trade_executor as te
from risk_manager import RiskManager, parse_whitelist
from trident2 import store as t2store

ASSET = "XAU"
ENTRY_PRICE = 3737.0
NOTIONAL = 1000.0


_DDL = """
CREATE TABLE ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL,
    sentiment_score REAL NOT NULL,
    suggested_action TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'UNREAD',
    market_category TEXT NOT NULL DEFAULT 'OTHER',
    target_asset TEXT NOT NULL DEFAULT 'NONE',
    aggregation_key TEXT DEFAULT '',
    entry_price REAL DEFAULT NULL,
    entry_time TEXT DEFAULT '',
    settled INTEGER DEFAULT 0
);
CREATE TABLE trade_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    asset TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    notional_usdt REAL DEFAULT 0.0,
    quantity REAL DEFAULT 0.0,
    filled_price REAL DEFAULT 0.0,
    order_id TEXT DEFAULT '',
    adapter TEXT DEFAULT '',
    mode TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    raw_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def _new_db() -> str:
    path = tempfile.NamedTemporaryFile(
        prefix="trident_roundtrip_", suffix=".db", delete=False).name
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    t2store.ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, ts INTEGER, price REAL)")
    conn.execute("UPDATE t2_account SET cash=10000, equity=10000, realized_pnl=0, fee_bps=8")
    conn.commit()
    conn.close()
    return path


def _check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时库")
    a = ap.parse_args()

    db = _new_db()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    print(f"[1] 临时库 {db}")

    # ── 行情：先给一个新鲜 tick，否则前置闸会拒单 ──
    conn.execute("INSERT INTO market_ticks VALUES (?,?,?)", (ASSET, int(time.time()), ENTRY_PRICE))
    utc_now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    conn.execute(
        "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, reasoning,"
        " created_at, status, market_category, target_asset, aggregation_key,"
        " entry_price, entry_time, settled)"
        " VALUES (1, 0.9, 'BUY', 'roundtrip', ?, 'AUTO_APPROVED', 'commodity', ?, 'k1', ?, ?, 0)",
        (utc_now, ASSET, ENTRY_PRICE, utc_now),
    )
    conn.commit()
    print("[2] 写入 1 条 BUY 决策 + 新鲜 tick")

    cfg = te.ExecutorConfig(
        mode="paper", poll_interval=5, notional_usdt=NOTIONAL,
        symbol_map={ASSET: "PAXGUSDT"}, unsupported={"WTI", "INDIVIDUAL_STOCK"},
        max_decisions_per_tick=5, testnet=False,
    )
    adapter = te.MockAdapter(
        price_lookup=lambda sym: conn.execute(
            "SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1",
            (ASSET,)).fetchone()[0],
        simulate_fill=True,
    )
    ex = te.TradeExecutor(cfg, adapter, RiskManager(whitelist=parse_whitelist("*"),
                                                    max_position_usdt=NOTIONAL), db_path=db)
    res = ex.tick()
    status = res["processed"][0]["status"] if res.get("processed") else "NONE"
    print(f"[3] executor tick → {status} {res.get('processed')}")
    _check(status == te.STATUS_ORDER_PLACED, f"下单状态应为 ORDER_PLACED，实际 {status}")

    pos = conn.execute("SELECT * FROM t2_positions WHERE status='open'").fetchone()
    _check(pos is not None, "T2 账本里应有 1 笔 open 持仓")
    _check(abs(pos["fee_paid"] - NOTIONAL * 8 / 10_000) < 1e-6,
           f"开仓费应={NOTIONAL * 8 / 10000:.4f}，实际={pos['fee_paid']:.4f}")
    _check(pos["stop_price"] is not None, f"应自动挂止损，实际={pos['stop_price']}")

    c1 = al.canonical(conn)
    print(f"[4] 开仓后 canonical：cash={c1['cash']:.2f} equity={c1['equity']:.2f} "
          f"浮盈={c1['unrealized']:+.2f} 未入账费={c1['unbooked_entry_fee']:.2f}")
    _check(abs(c1["unbooked_entry_fee"]) < 1e-6, "开仓费应已入账，不能再挂未入账费")

    # ── 价格跌破止损 → stops.enforce 平仓 ──
    crash = float(pos["stop_price"]) - 5
    conn.execute("INSERT INTO market_ticks VALUES (?,?,?)", (ASSET, int(time.time()) + 1, crash))
    conn.commit()
    closed = stops.enforce(conn)
    print(f"[5] 止损触发：mark {ENTRY_PRICE} → {crash}，平掉 {len(closed)} 笔")
    _check(len(closed) == 1, "应触发 1 笔止损平仓")

    c2 = al.canonical(conn)
    row = conn.execute("SELECT * FROM t2_positions WHERE id=?", (pos["id"],)).fetchone()
    gross = (crash - ENTRY_PRICE) * row["qty"]
    fees = NOTIONAL * 8 / 10_000 + abs(crash * row["qty"]) * 8 / 10_000
    print(f"[6] 平仓后：gross={gross:+.2f} 双边费={fees:.2f} net={row['realized_pnl']:+.2f} "
          f"cash={c2['cash']:.2f} equity={c2['equity']:.2f}")
    _check(abs(row["realized_pnl"] - (gross - fees)) < 1e-4, "平仓净盈亏 = 毛利 - 双边手续费")
    _check(abs(c2["cash"] - (10000 + gross - fees)) < 1e-3, "现金 = 本金 + 净盈亏")
    _check(c2["n_open"] == 0 and c2["n_closed"] == 1, "账本应 0 开 1 平")

    issues = al.reconcile(conn)
    blocks = [i for i in issues if i["severity"] == "block"]
    for i in issues:
        print(f"[7] {i['severity']:5s} {i['code']}: {i['detail']}")
    _check(not blocks, f"对账不应有 block，实际 {[i['code'] for i in blocks]}")

    al.rebuild(conn, apply=True)
    _check(abs(al.canonical(conn)["equity"] - c2["equity"]) < 0.01, "rebuild 后权益不应漂移")

    conn.close()
    if not a.keep:
        os.unlink(db)
    print("\n✅ 全链路通过：决策→闸→成交→计费→止损→平仓→对账")
    return 0


if __name__ == "__main__":
    sys.exit(main())
