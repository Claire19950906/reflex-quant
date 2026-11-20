#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v477 杠杆释放自检：满仓被闸拒绝 → 平一笔 → 新仓真的能开出来。

这是「能用来交易」的最后一块拼图：证明 R7 不是死锁，减仓后开仓链路完整可用。
只在临时库跑，不碰生产库。

跑法：
    cd backend && python3 scripts/v477_leverage_release_check.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

os.environ.setdefault("MAX_GROSS_LEVERAGE_PCT", "200")
import account_ledger as al
import trade_executor as te
import trade_preflight as pf
from risk_manager import RiskManager, parse_whitelist
from trident2 import store as t2store

# 4 笔各 5100 名义 = 20400，权益 10000 → 204%，稳稳超过 200% 上限
LEG_NOTIONAL = 5100.0
LEGS = [("BTC", "long", 63751.3, 0.08), ("ETH", "short", 1796.33, 2.84),
        ("XAU", "long", 3736.73, 1.3648), ("WTI", "long", 82.311, 61.96)]
NEW_ASSET, NEW_PRICE, NEW_NOTIONAL = "SOL", 138.0, 1000.0


def _check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        raise SystemExit(1)


def main() -> int:
    db = tempfile.NamedTemporaryFile(prefix="trident_levrel_", suffix=".db", delete=False).name
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    t2store.ensure_schema(conn)
    conn.executescript("""
    CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER NOT NULL,
        sentiment_score REAL NOT NULL, suggested_action TEXT NOT NULL, reasoning TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')), status TEXT NOT NULL DEFAULT 'UNREAD',
        market_category TEXT NOT NULL DEFAULT 'OTHER', target_asset TEXT NOT NULL DEFAULT 'NONE',
        aggregation_key TEXT DEFAULT '', entry_price REAL DEFAULT NULL,
        entry_time TEXT DEFAULT '', settled INTEGER DEFAULT 0
    );
    CREATE TABLE trade_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER NOT NULL,
        asset TEXT NOT NULL DEFAULT '', symbol TEXT NOT NULL DEFAULT '', side TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL, notional_usdt REAL DEFAULT 0.0, quantity REAL DEFAULT 0.0,
        filled_price REAL DEFAULT 0.0, order_id TEXT DEFAULT '', adapter TEXT DEFAULT '',
        mode TEXT DEFAULT '', reason TEXT DEFAULT '', raw_json TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE market_ticks (symbol TEXT, ts INTEGER, price REAL);
    """)
    conn.execute("UPDATE t2_account SET cash=10000, equity=10000, realized_pnl=0, fee_bps=8")
    conn.commit()
    print(f"[1] 临时库 {db}")

    # ── 造 4 笔满仓 ──
    now = int(time.time())
    for asset, side, px, qty in LEGS:
        conn.execute("INSERT INTO market_ticks VALUES (?,?,?)", (asset, now, px))
        pid = t2store.insert_position(conn, asset=asset, side=side, qty=qty,
                                      entry_price=px, entry_ts=now, status="open")
        al.book_entry_fee(conn, pid)
    conn.commit()
    c1 = al.canonical(conn)
    print(f"[2] 满仓：{c1['n_open']} 笔 名义={c1['gross_exposure']:.0f} "
          f"权益={c1['equity']:.2f} 杠杆={c1['leverage_pct']:.1f}%（上限 {c1['leverage_cap_pct']:.0f}%）")
    _check(c1["leverage_pct"] > c1["leverage_cap_pct"], "造仓应超过杠杆上限")

    conn.execute("INSERT INTO market_ticks VALUES (?,?,?)", (NEW_ASSET, now, NEW_PRICE))
    conn.commit()
    beeng = pf.preflight(conn, NEW_ASSET, "BUY", NEW_NOTIONAL)
    print(f"[3] 满仓时下 SOL 新单 → ok={beeng['ok']} denies={[d['code'] for d in beeng['denies']]}")
    _check(not beeng["ok"] and any(d["code"] == "PREFLIGHT_LEVERAGE" for d in beeng["denies"]),
           "满仓应被 PREFLIGHT_LEVERAGE 拒绝")

    # ── 平掉 ETH 一笔，腾出杠杆 ──
    eth = conn.execute("SELECT id FROM t2_positions WHERE asset='ETH' AND status='open'").fetchone()
    eth_mark = conn.execute("SELECT price FROM market_ticks WHERE symbol='ETH' ORDER BY ts DESC LIMIT 1").fetchone()[0]
    t2store.close_position(conn, int(eth["id"]), float(eth_mark), note="lev-release-check")
    c2 = al.canonical(conn)
    print(f"[4] 平掉 ETH 后：{c2['n_open']} 笔 名义={c2['gross_exposure']:.0f} "
          f"权益={c2['equity']:.2f} 杠杆={c2['leverage_pct']:.1f}%")
    _check(c2["leverage_pct"] < c2["leverage_cap_pct"], "平仓后杠杆应回到上限内")

    after = pf.preflight(conn, NEW_ASSET, "BUY", NEW_NOTIONAL)
    print(f"[5] 再下 SOL 新单 → ok={after['ok']} denies={[d['code'] for d in after['denies']]}")
    _check(after["ok"], "腾出杠杆后前置闸应放行")

    # ── 走真实执行器开这一单 ──
    utc_now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    conn.execute(
        "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, reasoning,"
        " created_at, status, market_category, target_asset, aggregation_key,"
        " entry_price, entry_time, settled)"
        " VALUES (2, 0.8, 'BUY', 'lev-release', ?, 'AUTO_APPROVED', 'crypto', ?, 'k2', ?, ?, 0)",
        (utc_now, NEW_ASSET, NEW_PRICE, utc_now),
    )
    conn.commit()

    cfg = te.ExecutorConfig(mode="paper", poll_interval=5, notional_usdt=NEW_NOTIONAL,
                            symbol_map={NEW_ASSET: "SOLUSDT"}, unsupported={"WTI"},
                            max_decisions_per_tick=5, testnet=False)
    adapter = te.MockAdapter(
        price_lookup=lambda s: conn.execute(
            "SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1",
            (NEW_ASSET,)).fetchone()[0], simulate_fill=True)
    ex = te.TradeExecutor(cfg, adapter,
                          RiskManager(whitelist=parse_whitelist("*"), max_position_usdt=NEW_NOTIONAL),
                          db_path=db)
    res = ex.tick()
    status = res["processed"][0]["status"] if res.get("processed") else "NONE"
    print(f"[6] executor tick → {status}")
    _check(status == te.STATUS_ORDER_PLACED, f"新仓应成交，实际 {status}")

    c3 = al.canonical(conn)
    sol = [p for p in c3["positions"] if p["asset"] == NEW_ASSET]
    _check(len(sol) == 1, "账本应出现 1 笔 SOL 持仓")
    _check(abs(sol[0]["entry_fee"] - NEW_NOTIONAL * 8 / 10_000) < 1e-6, "新仓开仓费应已入账")
    print(f"[7] 开新仓后：{c3['n_open']} 笔 名义={c3['gross_exposure']:.0f} "
          f"权益={c3['equity']:.2f} 杠杆={c3['leverage_pct']:.1f}% cash={c3['cash']:.2f}")

    issues = [i for i in al.reconcile(conn) if i["severity"] == "block"]
    for i in issues:
        print(f"[8] block {i['code']}: {i['detail']}")
    _check(not issues, f"对账不应有 block，实际 {[i['code'] for i in issues]}")

    conn.close()
    os.unlink(db)
    print("\n✅ 杠杆不是死锁：满仓拒绝 → 平一笔 → 新仓成交，账本对平")
    return 0


if __name__ == "__main__":
    sys.exit(main())
