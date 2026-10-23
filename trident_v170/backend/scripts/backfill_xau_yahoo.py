#!/usr/bin/env python3
"""回填 Yahoo Finance 期货 K 线到 kline_bars（OKX 不可达时的备用数据通路）。

设计原则（v2026.10.05 新增）：
  - 用规范的 quote_sources.fetch_yahoo_candles（单一事实来源，含限速+熔断+重试）
  - 分页：按 days 自动切 period1/period2 窗口
  - 每页单独 commit（v174 修复同款：避免与 live writer 长事务互锁）
  - 写入用 INSERT ... ON CONFLICT DO UPDATE（与 knowledge_base.persist 一致）
  - 默认 --dry-run 仅规划页 + 探针 1 次，不写库
  - 数据诚实：Yahoo 失败返回 None → 跳过，不合成扁平假 bar
  - 限速内置（_YAHOO_MIN_GAP_S=3s），无需手动 sleep

用法（无代理 / OKX SNI 阻断时使用）：
  python3 scripts/backfill_xau_yahoo.py --dry-run
  python3 scripts/backfill_xau_yahoo.py --symbol XAU --interval 60m --days 60
  python3 scripts/backfill_xau_yahoo.py --symbol XAG --interval 60m --days 90
  python3 scripts/backfill_xau_yahoo.py --symbol BTC --interval 1d --days 365
  python3 scripts/backfill_xau_yahoo.py --symbol WTI --interval 1d --days 180
"""
from __future__ import annotations
import argparse, os, sys, time

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND_ROOT, "src_python"))

import sqlite3, db as dbmod, quote_sources as qs

# Yahoo 支持的 interval 字符串
YAHOO_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m",
                   "1h", "1d", "5d", "1wk", "1mo", "3mo"}

# Yahoo symbol 与 kline_bars.symbol 的映射
YAHOO_BACKFILL_SYMBOLS = {
    "XAU":    "GC=F",        # COMEX 黄金
    "GOLD":   "GC=F",
    "XAG":    "SI=F",        # COMEX 白银
    "SILVER": "SI=F",
    "WTI":    "CL=F",        # NYMEX WTI
    "OIL":    "CL=F",
    "BTC":    "BTC-USD",
    "ETH":    "ETH-USD",
    "SOL":    "SOL-USD",
    "HG":     "HG=F",        # COMEX 铜
    "COPPER": "HG=F",
    "DXY":    "DX-Y.NYB",
    "VIX":    "^VIX",
}

# 单次 Yahoo 调用上限（保守值）
YAHOO_PAGE_BAR_CAP = 5_000


def _tf_name(interval: str) -> str:
    """kline_bars.timeframe 字段用的统一字符串。"""
    if interval.endswith("m") or interval in ("60m", "90m"):
        return interval
    if interval == "1d":
        return "1d"
    if interval == "1wk":
        return "1w"
    if interval == "1mo":
        return "1mo"
    return interval


def plan(symbol, yahoo_sym, interval, days, page_days):
    if interval not in YAHOO_INTERVALS:
        raise SystemExit(f"interval 必须是 {sorted(YAHOO_INTERVALS)} 之一, got {interval!r}")
    if page_days < 1 or page_days > 730:
        raise SystemExit(f"--page-days 必须在 [1, 730], got {page_days}")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60_000
    n_pages = (days + page_days - 1) // page_days
    return dict(
        symbol=symbol, yahoo_sym=yahoo_sym, interval=interval, days=days,
        page_days=page_days, now_ms=end_ms, start_ms=start_ms,
        n_pages=n_pages,
        end_dt=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(end_ms / 1000)),
        start_dt=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(start_ms / 1000)),
    )


def write_page(conn, sym, tf, rows):
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
            (sym, tf, int(ts_ms), float(o), float(h), float(l), float(c),
             float(vol), time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        written += 1
    return written


def run(symbol, interval, days, page_days, dry_run, probe):
    yahoo_sym = YAHOO_BACKFILL_SYMBOLS.get(symbol.upper())
    if not yahoo_sym:
        raise SystemExit(
            f"--symbol {symbol!r} 不在已知映射中，可选: {sorted(YAHOO_BACKFILL_SYMBOLS)}"
        )
    p = plan(symbol, yahoo_sym, interval, days, page_days)
    print("=== BACKFILL PLAN (Yahoo Finance fallback, dry_run={}) ===".format(dry_run))
    for k, v in p.items():
        print(f"  {k}: {v}")
    tf = _tf_name(interval)
    print(f"  → kline_bars.symbol='{symbol}' timeframe='{tf}'")

    if probe:
        print("\n=== CONNECTIVITY PROBE ===")
        test_iv = "60m" if interval in ("60m", "1h", "30m", "15m", "5m", "1m") else interval
        test_rng = "5d" if test_iv in ("1m", "2m", "5m", "15m", "30m",
                                       "60m", "90m", "1h") else "1mo"
        probe_data = qs.fetch_yahoo_candles(
            yahoo_sym, interval=test_iv, range_or_period=test_rng, limit=5
        )
        if not probe_data:
            print(f"  PROBE FAILED: Yahoo Finance 不可达（{yahoo_sym}）。")
            print("  → 等待 Yahoo SNI 阻断恢复（实测几分钟-几十分钟）或配好代理。")
            print("    限速阈值内置 3s/次；不要高频重试避免触发 429。")
            if dry_run:
                return
            sys.exit(2)
        print(f"  PROBE OK: got {len(probe_data)} bars of {yahoo_sym} "
              f"({test_iv}/{test_rng}), close ~ {probe_data[-1][4]:.2f}")
        if dry_run:
            print("  --dry-run: 探针通过，未写库。去掉 --dry-run 真正执行回填。")
            return

    if dry_run:
        print("\n=== DRY-RUN: 仅规划，未调用 Yahoo，未写入。去掉 --dry-run 真正执行。 ===")
        return

    conn = dbmod.get_connection()
    total_written = 0
    total_pages = p["n_pages"]
    print(f"\n=== START BACKFILL → kline_bars symbol='{symbol}' tf='{tf}' ===")
    end_ms = p["now_ms"]
    for page_idx in range(total_pages):
        page_end_ms = end_ms - page_idx * page_days * 24 * 60 * 60_000
        page_start_ms = max(page_end_ms - page_days * 24 * 60 * 60_000, p["start_ms"])
        rows = qs.fetch_yahoo_candles(
            yahoo_sym,
            interval=interval,
            period1=int(page_start_ms / 1000),
            period2=int(page_end_ms / 1000),
        )
        if not rows:
            print(f"  page {page_idx+1}/{total_pages}: Yahoo returned None → break")
            break
        oldest_ts = int(rows[0][0])
        newest_ts = int(rows[-1][0])
        try:
            n = write_page(conn, symbol, tf, rows)
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            print(f"  page {page_idx+1}: SQLite ERR {type(e).__name__}: {e} → stop")
            sys.exit(3)
        total_written += n
        oldest_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(oldest_ts / 1000))
        newest_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(newest_ts / 1000))
        print(f"  page {page_idx+1}/{total_pages}: wrote {n} bars "
              f"[{oldest_str} .. {newest_str}]")
        if oldest_ts <= p["start_ms"]:
            print(f"  reached start bound ({p['start_ms']}) → done")
            break
        # 内置限速（_yahoo_throttle）已自动 sleep，无需手动加
    conn.close()
    print(f"\n=== DONE: total_written={total_written} bars across ≤{total_pages} pages ===")
    print("  提示：与 OKX 回填相比，Yahoo 数据带 15-20min 延迟 + 合约价 vs 现货 ~$1-3 基差；")
    print("        适合做'历史位置/趋势参考'，不适合做'实时价位判定'。")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="XAU",
                    help=f"kline_bars.symbol；可选: {sorted(YAHOO_BACKFILL_SYMBOLS)}")
    ap.add_argument("--interval", default="60m",
                    help=f"Yahoo interval；可选: {sorted(YAHOO_INTERVALS)}")
    ap.add_argument("--days", type=int, default=60, help="回填天数（默认 60）")
    ap.add_argument("--page-days", type=int, default=30,
                    help="每页跨度天数（默认 30；大间隔可加到 60-90）")
    ap.add_argument("--dry-run", action="store_true", help="仅规划 + 探针，不写库")
    ap.add_argument("--probe", action="store_true", help="先做小窗口探针")
    args = ap.parse_args()
    run(args.symbol, args.interval, args.days, args.page_days,
        dry_run=args.dry_run, probe=args.probe)


if __name__ == "__main__":
    main()
