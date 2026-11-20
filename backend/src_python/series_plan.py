# -*- coding: utf-8 -*-
"""按“序列”而不是按“asset”排版（v341）。

原来 backfill 这样枚举品种：
    SELECT DISTINCT asset FROM external_indicators → 逐个 fetch_prices
问题是 asset 有时是抽屉：`*` 里装着 GLD/TLT/^GSPC 的价格，`fred` 里装着 VIX。
于是「一条 VIX 被数三遍」、「GLD 的价格被记成品种 '*' 的行情」。

这里改成从 series_registry 反推：
  每个 kind=price 的行给出 (source_asset, indicator) → 归到 series_key
  同一个 series_key 有多个来源时只留一个（谁历史长留谁），并记下没被选的。
"""
from __future__ import annotations

import collections
import sqlite3

import series_namespace as sn

MIN_OHLCV_BARS = 30     # 日线少于这个就当没有


def _ohlcv_count(conn, series: str) -> int:
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM ohlcv_daily WHERE asset=? AND close>0",
            (series,)).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def build_plan(conn, min_bars: int = MIN_OHLCV_BARS) -> list[dict]:
    """→ [{'series','source_asset','indicator','rows','conflicts'}...]

    source_asset / indicator 为 None 表示走 ohlcv_daily。
    """
    rows = conn.execute(
        "SELECT asset, indicator, COUNT(*) FROM external_indicators "
        "GROUP BY asset, indicator ORDER BY 3 DESC").fetchall()

    cands: dict[str, list[tuple]] = collections.defaultdict(list)
    for asset, indicator, n in rows:
        s = sn.resolve(asset, indicator)
        if s.kind != "price" or s.namespace != "asset":
            continue
        cands[s.series].append((asset, indicator, int(n)))

    plan = []
    for series in sorted(cands):
        items = sorted(cands[series], key=lambda x: (-x[2], x[0] != sn.is_drawer(x[0])))
        best = items[0]
        n_ohlcv = _ohlcv_count(conn, series)
        conflicts = [f"{a}.{i}({n}行)" for a, i, n in items[1:]]
        if n_ohlcv >= min_bars:
            plan.append({"series": series, "source_asset": None, "indicator": None,
                         "rows": n_ohlcv, "conflicts": conflicts,
                         "source_note": f"ohlcv_daily {n_ohlcv} 根"})
        else:
            plan.append({"series": series, "source_asset": best[0], "indicator": best[1],
                         "rows": best[2], "conflicts": conflicts,
                         "source_note": f"{best[0]}.{best[1]}"})
    return plan


def report(plan: list[dict]) -> None:
    dup = [p for p in plan if p["conflicts"]]
    print(f"[PLAN] {len(plan)} 条价格序列；其中 {len(dup)} 条有多份来源(已各留一份)")
    for p in dup[:12]:
        print(f"   {p['series']:10s} 留 {p['source_note']:28s} 弃 {', '.join(p['conflicts'][:3])}")
