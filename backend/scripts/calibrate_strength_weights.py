#!/usr/bin/env python3
"""calibrate_strength_weights.py — 用历史信号前向收益标定趋势/共振加减分权重。

方法
----
从 `tech_signals` 取历史形态信号，重算每条信号日的：
  · 趋势证据（ADX/Swing/Aroon 同向或逆势）
  · 多周期共振方向
按"证据与候选方向同向 / 逆势 / 无证据"分组，比较各组前向 5 日命中与超额；
边际差（同向组 vs 无证据组、逆势组 vs 无证据组）按缩放系数折成强度增量，
给出建议权重，打印到台账；不自动写线上默认值（默认仍是 strength_weights.DEFAULTS）。

只读库、不写线上逻辑。
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))
DB_PATH = BACKEND / "trident_event_bus.db"
HORIZON = 5
MIN_SAMPLE = 20

sys.path.insert(0, str(BACKEND / "src_python"))
import technical_indicators as ti
import tech_signal_bridge as bridge


def load_daily(conn: sqlite3.Connection) -> Dict[str, List[list]]:
    rows = conn.execute(
        "SELECT asset, ts, open, high, low, close, volume FROM ohlcv_daily "
        "WHERE close IS NOT NULL ORDER BY asset, ts"
    ).fetchall()
    series: Dict[str, List[list]] = defaultdict(list)
    for asset, ts, o, h, l, c, v in rows:
        series[asset].append([int(ts), float(o or c), float(h), float(l),
                              float(c), float(v or 0.0)])
    return series


def fwd_returns(candles: List[list]) -> Dict[int, float]:
    idx = {c[0]: i for i, c in enumerate(candles)}
    out: Dict[int, float] = {}
    for ts, i in idx.items():
        base = candles[i][4]
        j = i + HORIZON
        if base and j < len(candles):
            out[ts] = (candles[j][4] - base) / base * 100.0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    series = load_daily(conn)
    fwd: Dict[str, Dict[int, float]] = {a: fwd_returns(c) for a, c in series.items()}

    rows = conn.execute(
        "SELECT asset, ts, signal_json, pct_1d FROM tech_signals "
        "ORDER BY ts ASC"
    ).fetchall()

    # 分组: 候选方向 → 证据桶 → 前向收益（按方向做符号化：看涨取正、看跌取反）
    buckets: Dict[str, List[float]] = defaultdict(list)
    for asset, ts, sig_json, pct1d in rows:
        items = bridge._parse_items(sig_json)
        action, _why, _st = bridge.decide_direction(items, pct1d)
        if action == "HOLD":
            continue
        ret = fwd.get(asset, {}).get(int(ts))
        if ret is None:
            continue
        signed = ret if action == "BUY" else -ret
        keys = {str(it.get("k") or "") for it in items}
        res = bridge.multi_tf_resonance(conn, asset)

        trend_same = (("adx_strong_bull" in keys or "swing_bull" in keys or "aroon_bull" in keys) and action == "BUY") or \
                     (("adx_strong_bear" in keys or "swing_bear" in keys or "aroon_bear" in keys) and action == "SELL")
        trend_against = (("adx_strong_bear" in keys or "swing_bear" in keys or "aroon_bear" in keys) and action == "BUY") or \
                        (("adx_strong_bull" in keys or "swing_bull" in keys or "aroon_bull" in keys) and action == "SELL")
        res_same = (res["direction"] == "bull" and action == "BUY") or (res["direction"] == "bear" and action == "SELL")
        res_against = (res["direction"] == "bear" and action == "BUY") or (res["direction"] == "bull" and action == "SELL")

        buckets["none"].append(signed)
        if trend_same:
            buckets["trend_same"].append(signed)
        if trend_against:
            buckets["trend_against"].append(signed)
        if res_same:
            buckets["resonance_same"].append(signed)
        if res_against:
            buckets["resonance_against"].append(signed)
    conn.close()

    def stats(vals: List[float]) -> Dict[str, Any]:
        if len(vals) < MIN_SAMPLE:
            return {"n": len(vals), "edge": None, "hit": None}
        return {"n": len(vals), "edge": round(statistics.fmean(vals), 3),
                "hit": round(sum(1 for v in vals if v > 0) / len(vals) * 100.0, 2)}

    base = stats(buckets["none"])
    print(f"无证据基线: n={base['n']} 5日超额={base['edge']} 命中={base['hit']}%")
    base_edge = base["edge"] or 0.0

    # 建议系数：边际超额(%) 按 0.1 强度 ≈ 1.0% 超额折算，封顶到默认区间
    SCALE = 10.0
    suggests: Dict[str, float] = {}
    for bucket in ("trend_same", "trend_against", "resonance_same", "resonance_against"):
        s = stats(buckets[bucket])
        if s["edge"] is None:
            print(f"{bucket:18}: 样本不足 (n={s['n']})，保留默认权重")
            continue
        edge_diff = s["edge"] - base_edge
        w = round(max(-0.25, min(0.20, edge_diff / SCALE)), 3)
        suggests[bucket] = w
        print(f"{bucket:18}: n={s['n']} 超额={s['edge']} 命中={s['hit']}% "
              f"边际={edge_diff:+.3f} → 建议权重 {w:+.3f}")

    if suggests:
        print("\n可写入 strength_weights.calibrate() 的标定值：")
        print("    " + ", ".join(f"{k}={v}" for k, v in suggests.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
