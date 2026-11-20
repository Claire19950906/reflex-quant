#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_settlement_outcomes.py — v310 P0: 用同源口径重算已结算决策的收益

背景
----
v310 P0 把 trade_decisions.entry_price 从 NULL 回填到 100% (2101/2101), 结算率
从 1.19% 拉到 95%, 但暴露出一个**取价口径不一致**的隐性错误:

  · entry_price   → db.resolve_entry_price, 优先取 ohlcv_intraday (分钟线)
  · forward 价    → 当时固定取 ohlcv_daily (日线)
                    [v310 P1 已修正] 现在统一走 db.forward_outcome_pct
                    (intraday 优先, daily 回退) —— 全系统唯一实现

两源同刻收盘价实测偏差:
    WTI 绝对均值 3.22% (p10 -8.0%) / ETH 1.32% / BTC 1.04% / XAU 0.37%

而收益幅度本身只有 0.6~3.6% —— 口径噪声与信号同量级, 于是:
  · WTI 结算胜率虚高到 100%
  · 同一条决策用两种口径算出正负相反的收益 (#2000 BTC: daily -0.405% / intraday +1.109%)

本脚本按修正后的同源口径 (intraday 优先, daily 回退) 重算所有已结算行的
actual_outcome_pct, 并同步 trade_reflections_v2.pnl_pct / actual_outcome_pct /
root_cause。

[v310 P1 补充] 之前本脚本只回填 trade_reflections_v2.pnl_pct, 没有同步
actual_outcome_pct —— 于是 2026-10-26 那批被污染的 actual_outcome_pct 在回填后
依然留在库里(这正是"真实持仓业绩"被带偏到 +117% 的原因)。现在三处同源同值。

安全性
------
· 默认 dry-run; 加 --commit 才写库。
· 只 UPDATE status='settled' 且 entry_price>0 的行, 不碰未结算决策。
· 同步更新 trade_reflections_v2 中对应 decision_id 的 pnl_pct / root_cause。
· 会打印重算前后的胜率对比 (按 decision_type 分组), 便于核对真实性。

用法
----
    python3 backfill_settlement_outcomes.py            # 只报告
    python3 backfill_settlement_outcomes.py --commit   # 落库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db as _db  # noqa: E402

DB_PATH = BACKEND / "trident_event_bus.db"
SETTLE_DAYS = 5


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _forward_same_source(conn, asset: str, from_ts: int, entry_price: float):
    """与修正后的 _forward_outcome_pct 完全同源: intraday 优先, daily 回退。

    v310 P1: 改为委托 db.forward_outcome_pct —— 全系统唯一口径实现,
    避免"脚本一套、bridge 一套、decision_engine 一套"再次漂移。
    """
    return _db.forward_outcome_pct(conn, asset, from_ts, entry_price,
                                   settle_days=SETTLE_DAYS)


def _hit_for(action: str, outcome_pct: float) -> int:
    if action == "BUY":
        return 1 if outcome_pct > 0 else 0
    if action == "SELL":
        return 1 if outcome_pct < 0 else 0
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库 (默认只报告)")
    args = ap.parse_args()

    conn = _connect()
    t0 = time.time()
    try:
        rows = conn.execute(
            """SELECT id, asset, final_action, intended_action, decision_type,
                      entry_price, decision_ts, actual_outcome_pct
               FROM trade_decisions
               WHERE status='settled' AND entry_price IS NOT NULL AND entry_price>0"""
        ).fetchall()

        before = defaultdict(lambda: [0, 0])   # (type) -> [n, hit]
        after = defaultdict(lambda: [0, 0])
        changed = 0
        flipped = 0
        no_data = 0
        updates = []

        for r in rows:
            action = (r["final_action"] or "").upper()
            # 统计用: REJECTED 行不参与方向判定, 单独看原始涨跌
            stat_key = f"{r['decision_type']}/{action}"
            old = r["actual_outcome_pct"]
            if old is not None:
                before[stat_key][0] += 1
                if _hit_for(action, old):
                    before[stat_key][1] += 1
            new = _forward_same_source(conn, r["asset"], int(r["decision_ts"]),
                                       r["entry_price"])
            if new is None:
                no_data += 1
                continue
            after[stat_key][0] += 1
            if _hit_for(action, new):
                after[stat_key][1] += 1
            if old is None or abs(new - old) > 1e-6:
                changed += 1
                if old is not None and (old > 0) != (new > 0):
                    flipped += 1
                updates.append((new, r["id"], action, r["asset"], old))

        print("=" * 68)
        print("结算收益同源重算 — v310 P0")
        print("=" * 68)
        print(f"  已结算可算行: {len(rows)}   需要改写: {changed}   "
              f"正负翻转: {flipped}   无 forward 数据: {no_data}")
        print()
        print(f"  {'分组':<22} {'n':>5} {'旧胜率':>9} {'新胜率':>9}")
        for key in sorted(set(list(before.keys()) + list(after.keys()))):
            b = before[key]
            a = after[key]
            bh = f"{b[1]*100//b[0]}%" if b[0] else "-"
            ah = f"{a[1]*100//a[0]}%" if a[0] else "-"
            mark = "  ← 修正" if bh != ah else ""
            print(f"  {key:<22} {a[0]:>5} {bh:>9} {ah:>9}{mark}")

        if args.commit:
            for new, did, action, asset, old in updates:
                conn.execute(
                    "UPDATE trade_decisions SET actual_outcome_pct=? WHERE id=?",
                    (new, did),
                )
                hit = _hit_for(action, new)
                conn.execute(
                    "UPDATE trade_reflections_v2 SET pnl_pct=?, actual_outcome_pct=?, "
                    "root_cause=?, settled=?, hit=? "
                    "WHERE decision_id=? AND paper_run_id IS NULL",
                    (new, new, f"{action} {new:+.2f}%", 1, hit, did),
                )
            conn.commit()
            print(f"\n  ✅ 已落库 {len(updates)} 行 (trade_decisions + trade_reflections_v2)")
        else:
            print("\n  (dry-run, 未落库 — 加 --commit 生效)")
        print(f"  耗时 {time.time() - t0:.1f}s")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
