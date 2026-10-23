"""
backfill_ohlcv.py
=================

P0-1: 从 yfinance 拉取真实 OHLCV 全字段, 存入新表 ohlcv_daily。

为什么需要:
  external_indicators 只存了 close (value 列), Open/High/Low/Volume 被丢弃
  → 量化 K 线形态识别 / 成交量异动 等能力全部失效
  → 修复: 新建 ohlcv_daily 表, 存全字段

数据来源 (100% 真实 yfinance, 绝不伪造):
  - ADA-USD / BNB-USD / DOGE-USD / SOL-USD / XRP-USD  (crypto)
  - MU  (US stock)
  - 000660.KS  (Samsung Electronics, KOSPI)

表结构:
  ohlcv_daily(asset TEXT, ts INTEGER, open REAL, high REAL, low REAL,
              close REAL, volume INTEGER, source TEXT, UNIQUE(asset, ts))

幂等: UNIQUE 约束 + INSERT OR IGNORE, 重复 ts 不会插入。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# yfinance 符号映射 (asset → yf symbol)
# v291+ 扩展: 加入 BTC/ETH/XAU/WTI 等核心品种, 让归因页/反思页默认展示不为空
ASSETS = {
    # 加密货币
    "BTC":     "BTC-USD",
    "ETH":     "ETH-USD",
    "ADA":     "ADA-USD",
    "BNB":     "BNB-USD",
    "DOGE":    "DOGE-USD",
    "SOL":     "SOL-USD",
    "XRP":     "XRP-USD",
    # 贵金属 / 能源 (商品期货)
    "XAU":     "GC=F",     # 黄金期货 (COMEX)
    "WTI":     "CL=F",     # 原油期货 (NYMEX)
    # 美股 (trident 默认展示的核心标的)
    "MU":      "MU",       # 美光
    "SKHYNIX": "000660.KS", # 海力士 (KRX)
    "NVDA":    "NVDA",     # 英伟达
    "TSLA":    "TSLA",     # 特斯拉
}

# 拉取窗口 (2 年)
PERIOD = "2y"
INTERVAL = "1d"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ohlcv_daily ("
        "  asset     TEXT NOT NULL,"
        "  ts        INTEGER NOT NULL,"
        "  open      REAL NOT NULL,"
        "  high      REAL NOT NULL,"
        "  low       REAL NOT NULL,"
        "  close     REAL NOT NULL,"
        "  volume    INTEGER NOT NULL,"
        "  source    TEXT NOT NULL DEFAULT 'yfinance',"
        "  UNIQUE(asset, ts)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ohlcv_asset_ts "
        "ON ohlcv_daily(asset, ts DESC)"
    )
    conn.commit()


def fetch_ohlcv(yf_symbol: str):
    """从 yfinance 拉真实 OHLCV, 返回 DataFrame 或 None"""
    try:
        import yfinance as yf
    except ImportError:
        print("[FATAL] yfinance not installed. pip install yfinance", file=sys.stderr)
        sys.exit(3)
    try:
        df = yf.Ticker(yf_symbol).history(period=PERIOD, interval=INTERVAL, auto_adjust=False)
        if df is None or df.empty:
            return None
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[keep].dropna()
        return df
    except Exception as e:
        print(f"  [WARN] yfinance {yf_symbol}: {e}", file=sys.stderr)
        return None


def main() -> int:
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    print(f"[INFO] Opening {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        ensure_table(conn)
        print(f"[OK] ohlcv_daily table ready")

        total_inserted = 0
        total_skipped = 0
        for asset, yf_sym in ASSETS.items():
            print(f"[FETCH] {asset} ({yf_sym}) period={PERIOD} interval={INTERVAL}")
            df = fetch_ohlcv(yf_sym)
            if df is None or df.empty:
                print(f"  [SKIP] {asset}: no data")
                continue
            rows = []
            for ts_idx, row in df.iterrows():
                ts_epoch = int(ts_idx.timestamp())
                vol = int(row.get("Volume", 0) or 0)
                o = float(row["Open"])
                h = float(row["High"])
                l = float(row["Low"])
                c = float(row["Close"])
                if c <= 0 or h <= 0 or l <= 0 or o <= 0:
                    continue
                rows.append((asset, ts_epoch, o, h, l, c, vol, "yfinance"))
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
            total_inserted += n_new
            total_skipped += len(rows) - n_new
            print(f"  [OK] {asset}: {len(rows)} fetched, +{n_new} new, {len(rows) - n_new} dup")
            time.sleep(0.5)

        conn.commit()

        cnt = conn.execute("SELECT COUNT(*) FROM ohlcv_daily").fetchone()[0]
        by_asset = conn.execute(
            "SELECT asset, COUNT(*), MIN(ts), MAX(ts) FROM ohlcv_daily GROUP BY asset ORDER BY asset"
        ).fetchall()
        print(f"\n[OK] ohlcv_daily total: {cnt} rows (+{total_inserted} new this run, {total_skipped} dup)")
        for a, c, tmin, tmax in by_asset:
            from datetime import datetime
            d_min = datetime.utcfromtimestamp(tmin).strftime("%Y-%m-%d")
            d_max = datetime.utcfromtimestamp(tmax).strftime("%Y-%m-%d")
            print(f"  {a}: {c} bars  ({d_min} -> {d_max})")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
