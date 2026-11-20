#!/usr/bin/env python3
"""
E1 方向重算（不改定义，只换价格底座）

背景：E1 的 direction 是 event_kline_impact 自己算的，算的时候用的是 ohlcv_daily 里
      同一天多根K线（实测 1181 个资产-日 / 4853 行 + 2653 条占位行），所以"当日两根价差"
      被当成方向依据 → 命中率系统性地停在 42%。

做法：不写回原表、不改 E1 定义，只用「每资产每日一根真实K线」按尺子同一标签重算，
      与库里的 direction 比对。属于对照测量。

用法：python3 fix_e1_direction.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"
EXCLUDE_NEUTRAL = 0.0005


def clean_book(conn):
    rows = conn.execute(
        "SELECT asset, d, close FROM ("
        "  SELECT asset, date(ts,'unixepoch') AS d, close,"
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY asset, date(ts,'unixepoch')"
        "           ORDER BY (CASE WHEN volume>0 AND NOT (open=high AND high=low)"
        "                         THEN 0 ELSE 1 END), ts DESC) rn"
        "  FROM ohlcv_daily WHERE close IS NOT NULL AND close > 0"
        ") WHERE rn=1 ORDER BY asset, d").fetchall()
    book = {}
    for a, d, c in rows:
        book.setdefault((a or "").strip().upper(), {})[d] = float(c)
    book = {a: dict(sorted(m.items())) for a, m in book.items()}
    # 每日横截面中位数
    daily = {}
    for a, m in book.items():
        keys = list(m)
        for i in range(1, len(keys)):
            c0, c1 = m[keys[i - 1]], m[keys[i]]
            if c0 > 0:
                daily.setdefault(keys[i], []).append(c1 / c0 - 1)
    cs = {d: median(v) for d, v in daily.items() if len(v) >= 3}
    return book, cs


def label(book, cs, asset, ts, window_end=None):
    a = (asset or "").strip().upper()
    m = book.get(a)
    if not m:
        return None
    cutoff = int(ts)
    if window_end is not None:
        try:
            cutoff = max(cutoff, int(window_end))
        except (TypeError, ValueError):
            pass
    day = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d")
    keys = list(m)
    entry = next((i for i, d in enumerate(keys) if d > day), None)
    if entry is None or entry + 1 >= len(keys):
        return None
    d1 = keys[entry]
    d2 = keys[entry + 1]
    r = m[d2] / m[d1] - 1
    base = cs.get(d2)
    if base is None:
        return None
    excess = r - base
    if abs(excess) < EXCLUDE_NEUTRAL:
        return None
    return 1 if excess > 0 else -1


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    book, cs = clean_book(conn)
    rows = conn.execute(
        "SELECT asset, event_time, direction, impact_window_end FROM event_kline_impact "
        "WHERE direction IS NOT NULL AND event_time IS NOT NULL").fetchall()
    hit = n = skipped = 0
    by_asset = {}
    for asset, ts, direction, wend in rows:
        want = {"up": 1, "down": -1}.get(str(direction).strip().lower())
        if want is None:
            skipped += 1
            continue
        got = label(book, cs, asset, ts, wend)
        if got is None:
            skipped += 1
            continue
        n += 1
        ok = int(got == want)
        hit += ok
        a = (asset or "").strip().upper()
        d = by_asset.setdefault(a, [0, 0])
        d[0] += ok
        d[1] += 1
    print("E1 重算（每资产每日一根真实K线）")
    if n:
        print(f"  命中 {hit}/{n} = {hit/n*100:.2f}%   （原口径 42.63%，n=5142）")
        print(f"  跳过 {skipped} 条（无行情/中性/非 up-down）")
        print("\n  按资产（n≥30）：")
        for a, (h, t) in sorted(by_asset.items(), key=lambda x: -x[1][1]):
            if t >= 30:
                print(f"    {a:<6} {h}/{t} = {h/t*100:.1f}%")
    else:
        print("  没有可算样本")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
