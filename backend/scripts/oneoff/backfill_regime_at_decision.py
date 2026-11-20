#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_regime_at_decision.py — 补决策当时的市场风格 (v337)

为什么需要它
------------
`signal_weight_history` 的 6 个乘子里，`regime_mult` 只有 2 个取值
（1.0 占 90.5%、1.2 占 9.5%）——所谓"乘子坍缩"。
根因不在乘子函数，在输入：`ai_decisions.regime_at_decision` 有 5,762 条
是空串或字面 `unknown`（占 65%），`_regime_mult('')` 只能 fail-closed 回 1.0。
也就是说，这条维度**不是算错了，是压根没喂数据**。

而 `market_style_snapshot` 表里有 35,621 条带 regime 的快照（2026-10-03 起），
决策时刻基本都能在 30 分钟内找到一条。本脚本把这条现成的事实接回去：
  · 只在 regime 为空/unknown 时补，不覆盖已有值；
  · 只取决策时刻**之前 30 分钟内、同一标的、非 unknown** 的最近一条快照；
  · 找不到就留空，不猜。

用法:
    python3 backfill_regime_at_decision.py --dry-run
    python3 backfill_regime_at_decision.py --commit
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db # noqa: E402

# target_asset → kline_bars.symbol（1m 分钟线）
_SYMBOL_MAP = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XAU": "XAU", "WTI": "WTI",
    "US_STOCK": "SPX", "CN_STOCK": "000300.SS", "INDIVIDUAL_STOCK": "SPX",
    "000300.SS": "000300.SS", "NDX": "NDX",
}


def _regime_from_bars(conn, symbol: str, ts_sec: int, lookback: int = 30) -> str:
    """用决策前 30 根 1m K 线判定风格（纯确定性，不看未来）。

    快照缺失时（10-03 之前没有 market_style_snapshot，10-31 之后 42% 快照本身
    返回 unknown）用价格行为补：趋势斜率 + 波动率两档阈值。
    词表与 market_style_snapshot 保持一致：trending_up / trending_down /
    volatile_low / volatile_high / mean_revert。
    """
    closes: list = []
    # v337: 按 1m → 1h → 1d 逐级降采样。
    # 原来写死 1m，而美股票（NVDA/META/TSLA/AAPL/GOOGL…）只有 1d 线 ——
    # 那一整批决策的 regime 永远补不出来，残留 unknown 让 regime 这一维
    # 继续在权重里并成同一个 1.0。
    for tf in ("1m", "1h", "1d"):
        rows = conn.execute(
            """SELECT close FROM kline_bars
                WHERE symbol=? AND timeframe=? AND ts/1000 <= ?
                ORDER BY ts DESC LIMIT ?""",
            (symbol, tf, int(ts_sec), lookback),
        ).fetchall()
        closes = [float(r[0]) for r in rows if r and r[0]]
        if len(closes) >= 10:
            break
    if len(closes) < 10:
        return ""
    closes.reverse()
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    # 一阶斜率（每根 bar 的平均涨幅）
    slope = (closes[-1] - closes[0]) / closes[0] / max(1, len(closes) - 1)
    if vol > 0.006:
        return "volatile_high"
    if abs(slope) * len(closes) > 0.004:
        return "trending_up" if slope > 0 else "trending_down"
    if vol > 0.0018:
        return "volatile_low"
    return "mean_revert"


def run(commit: bool = False, window_sec: int = 1800, days: int = 0) -> dict:
    conn = db.get_connection()
    stats = {"candidates": 0, "filled_snapshot": 0, "filled_bars": 0,
             "still_unknown": 0, "regime_before": {}, "regime_after": {}}
    try:
        cur = conn.cursor()
        for r in cur.execute(
            "SELECT regime_at_decision AS r, COUNT(*) AS n FROM ai_decisions GROUP BY 1"
        ).fetchall():
            stats["regime_before"][str(r[0] or "(empty)")] = r[1]

        since = 0
        if days > 0:
            since = int(__import__("time").time()) - days * 86400
        rows = conn.execute(
            """SELECT d.id, d.ts, d.target_asset FROM ai_decisions d
                WHERE (COALESCE(d.regime_at_decision,'')='' OR d.regime_at_decision='unknown')
                  AND d.ts > ?""",
            (since,),
        ).fetchall()
        stats["candidates"] = len(rows)
        snap_cache = {}
        for aid, ts, asset in rows:
            key = str(asset or "").upper()
            picked = ""
            if key not in snap_cache:
                snap_cache[key] = conn.execute(
                    "SELECT ts, regime FROM market_style_snapshot WHERE UPPER(asset)=? ORDER BY ts DESC",
                    (key,)).fetchall()
            for s_ts, s_reg in snap_cache[key]:
                if s_ts > ts:
                    continue
                if ts - s_ts > window_sec:
                    break
                if s_reg and str(s_reg).lower() != "unknown":
                    picked = str(s_reg)
                break
            if not picked:
                sym = _SYMBOL_MAP.get(key, key)
                picked = _regime_from_bars(conn, sym, int(ts))
                if picked:
                    stats["filled_bars"] += 1
                else:
                    stats["still_unknown"] += 1
                    continue
            else:
                stats["filled_snapshot"] += 1
            if commit:
                cur.execute("UPDATE ai_decisions SET regime_at_decision=? WHERE id=?", (picked, aid))
                if commit and stats["filled_bars"] + stats["filled_snapshot"] % 500 == 0:
                    conn.commit()
        if commit:
            conn.commit()
            for r in cur.execute(
                "SELECT regime_at_decision AS r, COUNT(*) AS n FROM ai_decisions GROUP BY 1"
            ).fetchall():
                stats["regime_after"][str(r[0] or "(empty)")] = r[1]
    finally:
        conn.close()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--window", type=int, default=1800)
    ap.add_argument("--days", type=int, default=0, help="只补最近 N 天(0=全部)")
    a = ap.parse_args()
    out = run(commit=a.commit, window_sec=a.window, days=a.days)
    print(f"[regime-backfill] candidates={out['candidates']} "
          f"snapshot={out['filled_snapshot']} bars={out['filled_bars']} "
          f"still_unknown={out['still_unknown']} commit={a.commit}")
    print(f"  before: {out['regime_before']}")
    if out["regime_after"]:
        print(f"  after : {out['regime_after']}")
