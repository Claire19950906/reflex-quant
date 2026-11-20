#!/usr/bin/env python3
"""analysis_hold_counterfactual.py — 给 HOLD 补上「事后价格」的分母。

问题：今天 482 条决策里 479 条是 HOLD，但只有 3 条带 `forward_pnl`。
HOLD 从不结算，于是「观望到底对不对」没有任何分母——任何「提高准确率」的动作
都无法验证，只能靠感觉调参（这正是要避免的）。

本脚本只读，用 `kline_bars`（1 分钟线）给每条 HOLD 补两个事实：
  1. 决策时刻的参考价（最近一根 1m 收盘）
  2. 之后 30 / 60 分钟的价格变动幅度
再按幅度分桶：HOLD 之后市场几乎不动 → 观望正确；出现大位移 → 漏单。

用法：
  python3 backend/scripts/analysis_hold_counterfactual.py --date 2026-11-16
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_BACKEND, "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from analysis import contract as ac

_BJ = timezone(timedelta(hours=8))
# 只有这几个品种有 1m 线；WTI 只有日线，日线算 30 分钟位移没有意义，先排除
_ONE_MIN_ASSETS = ("BTC", "ETH", "SOL", "XAU")
_BUCKETS = ((0.10, "<0.10%"), (0.30, "0.10-0.30%"), (0.60, "0.30-0.60%"), (10, ">0.60%"))


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _epoch_ms(local_iso: str) -> int | None:
    try:
        dt = datetime.fromisoformat(str(local_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_BJ)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _close_at(conn, symbol: str, ts_ms: int) -> float | None:
    row = conn.execute(
        "SELECT close FROM kline_bars WHERE symbol=? AND timeframe='1m' "
        "AND ts<=? AND close>0 ORDER BY ts DESC LIMIT 1",
        (symbol, ts_ms),
    ).fetchone()
    return float(row[0]) if row else None


def _bucket(x: float) -> str:
    for limit, label in _BUCKETS:
        if x <= limit:
            return label
    return _BUCKETS[-1][1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(_BACKEND, "trident_event_bus.db"))
    ap.add_argument("--date", default=None, help="YYYY-MM-DD（本地日界）")
    ap.add_argument("--horizon", type=int, default=30, help="分钟（默认 30，另算 60）")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"[FATAL] 库不存在：{args.db}")
        return 2

    conn = _connect(args.db)
    try:
        holds = conn.execute(
            "SELECT id, created_at, target_asset, sentiment_score, analysis_degraded "
            "FROM ai_decisions WHERE upper(suggested_action)='HOLD' "
            "AND target_asset IN ('BTC','ETH','SOL','XAU') "
            + ("AND created_at >= ? AND created_at < ? " if args.date else ""),
            ([f"{args.date}T00:00:00", f"{args.date}T23:59:59.999"] if args.date else []),
        ).fetchall()
    finally:
        conn.close()

    conn = _connect(args.db)  # 只读连接复用给价格查询
    movers30, movers60 = Counter(), Counter()
    per_asset = defaultdict(Counter)
    no_price = 0
    big = []
    try:
        for row in holds:
            ts = _epoch_ms(row["created_at"])
            if ts is None:
                continue
            sym = ac.canonical_asset(row["target_asset"])
            if sym not in _ONE_MIN_ASSETS:
                continue
            p0 = _close_at(conn, sym, ts)
            if not p0:
                no_price += 1
                continue
            for h, counter in ((30, movers30), (60, movers60)):
                pe = _close_at(conn, sym, ts + h * 60_000)
                if not pe:
                    continue
                move = abs(pe - p0) / p0 * 100
                counter[_bucket(move)] += 1
                if h == args.horizon:
                    per_asset[sym][_bucket(move)] += 1
                    if move >= 0.60:
                        big.append((row["id"], sym, row["created_at"][11:16],
                                    round(move, 2), int(row["analysis_degraded"] or 0),
                                    row["sentiment_score"]))
    finally:
        conn.close()

    def _dump(title, counter):
        total = sum(counter.values())
        print(f"\n{title}（有价的 HOLD = {total}）")
        for _, label in _BUCKETS:
            n = counter.get(label, 0)
            print(f"  {label:<12} {n:>4}  {(n / total * 100 if total else 0):5.1f}%")
        quiet = counter.get("<0.10%", 0)
        print(f"  观望在「几乎没波动」的意义上是对的：{quiet}/{total}"
              f" = {(quiet / total * 100 if total else 0):.1f}%")

    print(f"\n== HOLD 反事实结算 ==  范围={args.date or '全部'}  HOLD 行={len(holds)}"
          f"  取不到价格={no_price}")
    _dump("之后 30 分钟位移", movers30)
    _dump("之后 60 分钟位移", movers60)

    print("\n分品种（30 分钟）")
    for sym, counter in sorted(per_asset.items()):
        total = sum(counter.values())
        parts = " ".join(f"{label}={counter.get(label, 0)}" for _, label in _BUCKETS)
        print(f"  {sym:<5} {total:>3} 条  {parts}")

    print(f"\n位移 ≥0.60% 的 HOLD（潜在漏单）Top {min(len(big), 12)}")
    for x in sorted(big, key=lambda z: -z[3])[:12]:
        print(f"  id={x[0]} {x[1]} {x[2]} 位移={x[3]}% 降级={x[4]} score={x[5]}")

    if big:
        deg = sum(1 for x in big if x[4] == 1)
        print(f"\n其中「分析降级」占 {deg}/{len(big)} = {deg / len(big) * 100:.1f}%"
              "（漏单与分析失败强相关；这些不是阈值问题，是没产出方向）")

    print("\n口径声明：这是「机会成本」不是「准确率」——HOLD 没有方向，只能测"
          "「不动 vs 动」的位移。真准确率要等方向性样本 ≥60 笔、且 HOLD 也按反事实结算。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
