#!/usr/bin/env python3
"""回填 OKX 永续 K 线到 kline_bars（生产历史补齐脚本）。

设计原则（沿用 codebase v174 + 数据诚实规范）：
  - 用规范的 quote_sources.fetch_okx_candles（单一事实来源，含 MARKET_USE_PROXY + 熔断）
  - 分页：OKX 返回 latest→oldest，传 after=<oldest_ms-1> 取更早一页
  - 每页单独 commit（v174 修复，避免与 live writer 长事务互锁）
  - 写入用 INSERT ... ON CONFLICT DO UPDATE（与 knowledge_base.persist 一致，
    天然幂等，可安全覆盖旧扁平/占位 bars）
  - 默认 --dry-run 仅规划页 + 探针 1 次，不写库
  - 数据诚实：OKX 失败返回 None → 跳过，不合成扁平假 bar

示例：
  python3 scripts/backfill_xau_okx.py --dry-run --bar 1H --days 120
  python3 scripts/backfill_xau_okx.py --bar 1H --days 120 --page-size 100
  python3 scripts/backfill_xau_okx.py --inst-id XAG-USDT-SWAP --bar 4H --days 180
"""
from __future__ import annotations
import argparse, os, sys, time

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND_ROOT, "src_python"))

import sqlite3, db as dbmod, quote_sources as qs

# OKX bar 单位 → 毫秒
BAR_MS = {
    "1m": 60_000, "3m": 3 * 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000,
    "30m": 30 * 60_000, "1H": 60 * 60_000, "2H": 2 * 60 * 60_000,
    "4H": 4 * 60 * 60_000, "6H": 6 * 60 * 60_000, "12H": 12 * 60 * 60_000,
    "1D": 24 * 60 * 60_000, "1W": 7 * 24 * 60 * 60_000,
}


def plan(inst_id, bar, days, page_size):
    if bar not in BAR_MS:
        raise SystemExit(f"bar 必须是 {sorted(BAR_MS)} 之一, got {bar!r}")
    if page_size < 2 or page_size > 300:
        raise SystemExit(f"page_size 必须在 [2, 300], got {page_size}")
    bar_ms = BAR_MS[bar]
    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // bar_ms) * bar_ms
    start_ms = end_ms - days * 24 * 60 * 60_000
    n_bars = (end_ms - start_ms) // bar_ms
    n_pages = (n_bars + page_size - 1) // page_size
    return dict(inst_id=inst_id, bar=bar, bar_ms=bar_ms, days=days,
                page_size=page_size, now_ms=now_ms, end_ms=end_ms,
                start_ms=start_ms, n_bars_estimated=n_bars, n_pages=n_pages)


def write_page(conn, inst_id, tf, rows):
    written = 0
    for r in rows:
        ts_ms, o, h, l, c, vol = r[0], r[1], r[2], r[3], r[4], r[5]
        conn.execute(
            """INSERT INTO kline_bars
               (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                 open=excluded.open, high=excluded.high,
                 low=excluded.low,  close=excluded.close,
                 volume=excluded.volume, fetched_at=excluded.fetched_at""",
            (inst_id, tf, int(ts_ms), float(o), float(h), float(l), float(c),
             float(vol), time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        written += 1
    return written


def run(inst_id, bar, days, page_size, dry_run, probe):
    p = plan(inst_id, bar, days, page_size)
    print(f"=== BACKFILL PLAN (dry_run={dry_run}) ===")
    for k, v in p.items():
        print(f"  {k}: {v}")

    if probe:
        print("\n=== CONNECTIVITY PROBE ===")
        probe_data = qs.fetch_okx_candles(inst_id, bar, limit=2)
        if not probe_data:
            print("  PROBE FAILED: OKX 不可达或返回空。")
            print("  → 启动本地代理（Clash 7890 / OpenRouter 22307），")
            print("    在 .env 取消注释对应 HTTPS_PROXY 行，并设 MARKET_USE_PROXY=true，")
            print("    重启引擎后再跑本脚本。")
            if dry_run:
                return
            sys.exit(2)
        print(f"  PROBE OK: got {len(probe_data)} bars, "
              f"ts [{probe_data[0][0]} .. {probe_data[-1][0]}]")
        if dry_run:
            print("  --dry-run: 探针通过，未写库。去掉 --dry-run 真正执行回填。")
            return

    if dry_run:
        print("\n=== DRY-RUN: 仅规划，未调用 OKX，未写入。去掉 --dry-run 真正执行。 ===")
        return

    conn = dbmod.get_connection()
    tf = bar.lower() if bar.lower() in {"1m", "5m", "15m", "30m"} else bar
    print(f"\n=== START BACKFILL → kline_bars symbol='{inst_id}' tf={tf} ===")
    total_written = 0
    total_pages = p["n_pages"]
    cursor_after = None
    for page_idx in range(total_pages):
        rows = qs.fetch_okx_candles(inst_id, bar, limit=page_size, after=cursor_after)
        if not rows:
            print(f"  page {page_idx+1}/{total_pages}: OKX returned None → break")
            break
        oldest_ts = int(rows[0][0])
        try:
            n = write_page(conn, inst_id, tf, rows)
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            print(f"  page {page_idx+1}: SQLite ERR {type(e).__name__}: {e} → stop")
            sys.exit(3)
        total_written += n
        print(f"  page {page_idx+1}/{total_pages}: wrote {n} bars "
              f"(page_oldest_ts={oldest_ts})")
        cursor_after = oldest_ts - 1
        if oldest_ts <= p["start_ms"]:
            print(f"  reached start bound ({p['start_ms']}) → done")
            break
        time.sleep(0.15)
    conn.close()
    print(f"\n=== DONE: total_written={total_written} bars across ≤{total_pages} pages ===")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inst-id", default="XAU-USDT-SWAP")
    ap.add_argument("--bar", default="1H")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    run(args.inst_id, args.bar, args.days, args.page_size,
        dry_run=args.dry_run, probe=args.probe)


if __name__ == "__main__":
    main()