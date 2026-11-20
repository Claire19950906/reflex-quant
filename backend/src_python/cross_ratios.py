#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cross_ratios.py —— 跨品种比值（金银比 / 金铜比 / 金油比 / 金股比）

为什么单独做这个：
  单看黄金涨跌，判断不了"是避险在定价、还是美元在走弱、还是通胀预期在起"。
  比值把两个价格相除，把共同因子消掉，剩下的就是相对强弱 —— 这是最省事的一类尺子。

数据来源：kline_bars 的 1d 序列（XAU / XAG / HG / WTI / SPX），全部是本机已落库的干净日线。
写入：external_indicators（source='derived_ratio'，asset='*'），按 (asset, indicator, ts) 幂等。

用法：
  python3 cross_ratios.py            # 默认最近 800 个交易日
  python3 cross_ratios.py --days 2500
  python3 cross_ratios.py --dry-run
"""
import argparse
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

import db
import external_data

SOURCE = "derived_ratio"
OWNER = "*"          # cross_asset_context 只认 asset IN ('*','',当前标的)

# 分子/分母 -> 指标名。分母缺失就整条不写，不做插值、不做补数。
PAIRS: Dict[str, Tuple[str, str]] = {
    "xau_xag_ratio": ("XAU", "XAG"),   # 金银比：>80 通常避险/白银弱，<50 反过来
    "xau_hg_ratio": ("XAU", "HG"),     # 金铜比：走高=增长预期弱、避险强
    "xau_wti_ratio": ("XAU", "WTI"),   # 金油比：>30 常见于需求塌陷
    "xau_spx_ratio": ("XAU", "SPX"),   # 金股比：黄金相对美股
}


def load_series(conn: sqlite3.Connection, symbol: str, since_ts: int) -> Dict[int, float]:
    """按"日"取收盘价；同一天多行时取最后一根。

    v343：优先读 `ohlcv_daily` —— 那才是日线的正表（XAU 有 2016 至今 2625 根、最新到今天）。
    原来只读 `kline_bars`，而那张表的日线覆盖不全：实测 XAU 的 1d 停在 05-29（5 个月前），
    于是金铜比/金股比这些比值**天天算不出结果**（日志"无共同交易日，跳过"），
    但看起来像"没数据"，其实是读错了表。kline_bars 只留作兜底。
    """
    out: Dict[int, float] = {}
    rows = conn.execute(
        """SELECT ts, close FROM ohlcv_daily
           WHERE asset=? AND close IS NOT NULL AND ts>=?
           ORDER BY ts""", (symbol, int(since_ts) // 1000)).fetchall()
    for ts, close in rows:
        out[int(ts) // 86400] = float(close)      # 同一天多行：后写覆盖
    if out:
        return out
    rows = conn.execute(
        """SELECT ts, close FROM kline_bars
           WHERE symbol=? AND timeframe='1d' AND close IS NOT NULL AND ts>=?
           ORDER BY ts""", (symbol, since_ts)).fetchall()
    for ts, close in rows:
        day = int(ts) // 86400000
        out[day] = float(close)     # 同日多根：后写覆盖
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=800)
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    since = int((time.time() - args.days * 86400) * 1000)
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")

    cache: Dict[str, Dict[int, float]] = {}
    written = 0
    for name, (num, den) in PAIRS.items():
        if num not in cache:
            cache[num] = load_series(conn, num, since)
        if den not in cache:
            cache[den] = load_series(conn, den, since)
        a, b = cache[num], cache[den]
        days = sorted(set(a) & set(b))          # 只取两边都有数的日子
        if not days:
            print(f"  {name:<16} 无共同交易日，跳过（不编数）")
            continue
        latest_ratio = None
        records: List[Dict[str, int | float]] = []
        for d in days:
            if b[d] <= 0:
                continue                        # 分母非正，比值无意义
            ratio = a[d] / b[d]
            ts = d * 86400000
            latest_ratio = ratio
            if args.dry_run:
                continue
            records.append({"ts": ts, "value": round(ratio, 4)})
        if records and not args.dry_run:
            # 统一走 external_data.ingest_external：它负责按 (asset,indicator,ts) 幂等去重，
            # 直接 ON CONFLICT 会报错——这张表没有对应唯一约束。
            external_data.ingest_external(asset=OWNER, indicator=name, records=records,
                                          source=SOURCE,
                                          meta={"basis": "kline_bars 1d close"})
            written += len(records)
        prev = a[days[-2]] / b[days[-2]] if len(days) > 1 and b[days[-2]] > 0 else None
        chg = ""
        if prev and latest_ratio:
            chg = f"（较上一交易日 {(latest_ratio / prev - 1) * 100:+.2f}%）"
        print(f"  {name:<16} {len(days):>4} 个交易日，最新 {latest_ratio:.3f}{chg}")

    if not args.dry_run:
        conn.commit()
        print(f"  写入 {written} 行 → external_indicators(source={SOURCE})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
