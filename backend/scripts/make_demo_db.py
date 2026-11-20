#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_demo_db.py — 生成一份**合成**演示库，让陌生人 clone 后能看到有数据的界面。

三条纪律：
  1. 数据全部**合成**（随机游走 + 固定文案），不取自任何真实账户、真实决策；
  2. 每条都带 `demo` 标记（raw_news.source='demo'、ai_decisions.status='DEMO'），
     不会和真实数据混淆；
  3. 只写目标库文件，不碰 `trident_event_bus.db`。

用法：
    python3 scripts/make_demo_db.py                # 写到 backend/demo.db
    python3 scripts/make_demo_db.py --force        # 覆盖已有 demo.db
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src_python"))

import db  # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XAUUSD", "SPX", "NVDA"]
NEWS = [
    ("现货比特币 ETF 单日净流出 4.2 亿美元", "BTCUSDT"),
    ("美联储官员称通胀回落慢于预期", "SPX"),
    ("半导体库存周期见底信号出现", "NVDA"),
    ("黄金实际利率分位升至 92%", "XAUUSD"),
    ("以太坊链上手续费环比下降 12%", "ETHUSDT"),
]


def _rand_walk(seed: int, n: int, start: float, vol: float) -> list[float]:
    rnd = random.Random(seed)
    out = [start]
    for _ in range(n - 1):
        out.append(max(0.01, out[-1] * (1 + rnd.gauss(0, vol))))
    return out


def seed(conn: sqlite3.Connection, days: int = 400) -> dict:
    now = int(time.time() * 1000)
    day = 86_400_000
    counts = {}

    # ── 日线（合成随机游走）────────────────────────────────
    for i, sym in enumerate(SYMBOLS):
        prices = _rand_walk(seed=1000 + i, n=days, start=100.0 * (i + 1), vol=0.012)
        rows = []
        for j, close in enumerate(prices):
            ts = now - (days - j) * day
            rows.append((sym, "1d", ts, close * 0.999, close * 1.004, close * 0.996, close, 1000.0 + j))
        conn.executemany(
            "INSERT OR REPLACE INTO kline_bars (symbol,timeframe,ts,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        counts["kline_bars"] = counts.get("kline_bars", 0) + len(rows)

    # ── 实时报价 ───────────────────────────────────────────
    ticks = [(sym, now, 100.0 * (i + 1) * 1.003, 0.012, "demo", 1) for i, sym in enumerate(SYMBOLS)]
    conn.executemany(
        "INSERT OR REPLACE INTO market_ticks (symbol,ts,price,change24h,source,source_count) "
        "VALUES (?,?,?,?,?,?)",
        ticks,
    )
    counts["market_ticks"] = len(ticks)

    # ── 新闻 + 决策 + 闸门评估 ─────────────────────────────
    n_news = n_dec = n_gate = 0
    for k in range(48):
        title, sym = NEWS[k % len(NEWS)]
        ts = now - (48 - k) * 1800_000
        cur = conn.execute(
            "INSERT INTO raw_news (source,content,timestamp,ts,status) VALUES (?,?,?,?,?)",
            ("demo", f"[DEMO] {title}", str(ts // 1000), ts, "DONE"),
        )
        news_id = cur.lastrowid
        n_news += 1
        action = ("BUY", "SELL", "HOLD")[k % 3]
        score = 0.4 if action == "HOLD" else 0.75
        conn.execute(
            "INSERT INTO ai_decisions (news_id,sentiment_score,suggested_action,reasoning,created_at,"
            "status,market_category,target_asset,entry_price) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                news_id,
                score,
                action,
                f"[DEMO] {title}｜三源交叉：一条硬证据、一条反向证据，方向可信度 0.6",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)),
                "UNREAD",
                "crypto" if "USD" in sym else "macro",
                sym,
                100.0,
            ),
        )
        n_dec += 1
        passed = 0 if action == "HOLD" else (1 if k % 4 == 0 else 0)
        conn.execute(
            "INSERT INTO gate_evaluations (evaluated_at,ai_decision_id,news_id,asset,action,passed,reject_code) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, news_id, news_id, sym, action, passed, "" if passed else "no_direction"),
        )
        n_gate += 1
    counts["raw_news"], counts["ai_decisions"], counts["gate_evaluations"] = n_news, n_dec, n_gate

    # ── 纸面账户（3 笔持仓）────────────────────────────────
    try:
        import trident2.store as t2store

        t2store.ensure_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO t2_account (id,mode,cash,equity,realized_pnl,updated_at) "
            "VALUES (1,'paper',9000.0,9960.0,120.0,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        for i, sym in enumerate(SYMBOLS[:3]):
            conn.execute(
                "INSERT INTO t2_positions (asset,side,qty,entry_price,entry_ts,status) VALUES (?,?,?,?,?,?)",
                (sym, "long", 1.0, 100.0 * (i + 1), int(time.time() * 1000), "open"),
            )
        counts["t2_account"], counts["t2_positions"] = 1, 3
    except Exception as exc:  # 纸面账本不可用时不阻塞其它演示数据
        counts["t2_error"] = str(exc)[:80]

    conn.commit()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "demo.db"))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        print(f"{args.out} 已存在；要覆盖请加 --force")
        return 1

    conn = sqlite3.connect(args.out)
    db.migrate(conn)
    counts = seed(conn)
    conn.close()

    size_mb = os.path.getsize(args.out) / 1e6
    print(f"已生成 {args.out}（{size_mb:.1f} MB）")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print("\n使用：TRIDENT_DB_PATH=" + args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
