"""
backfill_ohlcv_missing.py
=========================

v292 增量补丁: 补齐 v291 漏掉的 6 个核心品种 OHLCV
  - BTC:    BTC-USD 已下线,改用 BTC=F (Yahoo 期货合约)
  - ETH / XAU / WTI / NVDA / TSLA

机制:
  - 沿用 v291 的 ohlcv_daily 表 schema (UNIQUE 约束),INSERT OR IGNORE 幂等
  - yfinance 当前 SSL/crumb 不稳,加重试 3 次 + 指数退避
  - 仅处理缺失品种,已存在品种跳过
  - 串行 0.8s sleep,避免 60s rate-limit

执行:
  python3 src_python/backfill_ohlcv_missing.py
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# v292 修复: BTC-USD 在 yfinance 已下架,用 BTC=F (Yahoo 连续期货合约)
MISSING = {
    "BTC":     "BTC=F",     # Yahoo Finance BTC 期货合约 (不间断)
    "ETH":     "ETH-USD",
    "XAU":     "GC=F",      # COMEX 黄金期货主力
    "WTI":     "CL=F",      # NYMEX WTI 原油主力
    "NVDA":    "NVDA",
    "TSLA":    "TSLA",
}

PERIOD = "2y"
INTERVAL = "1d"
MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 10]   # 秒, 指数退避
SYMBOL_DELAY = 0.8           # 品种间间隔


def fetch_with_retry(yf_symbol: str):
    """包一层重试,SSL/crumb/yfinance 临时错误都能扛"""
    import yfinance as yf
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            df = yf.Ticker(yf_symbol).history(
                period=PERIOD, interval=INTERVAL, auto_adjust=False
            )
            if df is not None and not df.empty:
                keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
                return df[keep].dropna()
            last_err = "empty dataframe"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
        # 退避
        wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
        print(f"    [retry {attempt + 1}/{MAX_RETRIES}] after {wait}s ... ({last_err})")
        time.sleep(wait)
    return None


def main() -> int:
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    print(f"[INFO] Opening {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    grand_inserted = 0
    grand_skipped = 0
    grand_failed = []

    try:
        # 表已由 v291 脚本 ensure_table 建好,这里只确认存在
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ohlcv_daily'"
        )
        if not cur.fetchone():
            print("[FATAL] ohlcv_daily 表不存在, 先跑 backfill_ohlcv.py 建表", file=sys.stderr)
            return 2

        for asset, yf_sym in MISSING.items():
            existing = conn.execute(
                "SELECT COUNT(*) FROM ohlcv_daily WHERE asset=?", (asset,)
            ).fetchone()[0]
            print(f"\n[FETCH] {asset:6s} <- {yf_sym:10s}  (DB现有 {existing} 行)")

            df = fetch_with_retry(yf_sym)
            if df is None or df.empty:
                print(f"  [FAIL] {asset}: 重试 {MAX_RETRIES} 次仍无数据")
                grand_failed.append((asset, yf_sym))
                continue

            rows = []
            for ts_idx, row in df.iterrows():
                ts_epoch = int(ts_idx.timestamp())
                o = float(row["Open"]); h = float(row["High"])
                l = float(row["Low"]);  c = float(row["Close"])
                if c <= 0 or h <= 0 or l <= 0 or o <= 0:
                    continue
                rows.append((asset, ts_epoch, o, h, l, c,
                             int(row.get("Volume", 0) or 0), "yfinance"))
            if not rows:
                print(f"  [SKIP] {asset}: 0 valid rows")
                continue

            cur = conn.executemany(
                "INSERT OR IGNORE INTO ohlcv_daily "
                "(asset, ts, open, high, low, close, volume, source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            n_new = cur.rowcount if cur.rowcount else 0
            grand_inserted += n_new
            grand_skipped += len(rows) - n_new
            print(f"  [OK]   {asset}: {len(rows)} fetched  +{n_new} new  {len(rows) - n_new} dup")

            time.sleep(SYMBOL_DELAY)

        conn.commit()

        # ── 终态报告 ──
        print("\n" + "=" * 60)
        print("BACKFILL_MISSING — final")
        print("=" * 60)
        rows = conn.execute(
            "SELECT asset, COUNT(*) AS n, MIN(ts) AS tmin, MAX(ts) AS tmax "
            "FROM ohlcv_daily WHERE asset IN ("
            + ",".join("?" * len(MISSING)) +
            ") GROUP BY asset ORDER BY asset",
            list(MISSING.keys()),
        ).fetchall()
        for asset, n, tmin, tmax in rows:
            d_min = datetime.utcfromtimestamp(tmin).strftime("%Y-%m-%d")
            d_max = datetime.utcfromtimestamp(tmax).strftime("%Y-%m-%d")
            print(f"  {asset:10s}: {n:5d} bars   {d_min} → {d_max}")
        if grand_failed:
            print(f"\n[WARN] 以下品种失败: {[a for a, _ in grand_failed]}")
        print(f"\n总计: +{grand_inserted} 新增, {grand_skipped} 重复, {len(grand_failed)} 失败")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        conn.rollback()
        return 3
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
