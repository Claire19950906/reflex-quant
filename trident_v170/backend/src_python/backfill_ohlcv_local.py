"""
backfill_ohlcv_local.py
=======================
从**本地** kline_bars 表回填 ohlcv_daily (v303 归因页 K线链路)

为什么需要这个脚本
-----------------
backfill_ohlcv.py 走 yfinance 拉数据, 但当前环境被 Yahoo 限流
("Too Many Requests"), 且它的标的表(MU/NVDA/TSLA 等美股)与金融版
实际使用的资产不一致 —— 缺 XAG, 且不含本地已有的 XAU/WTI。

本地 kline_bars 已有真实 OHLCV, 覆盖金融版实际使用的
BTC/ETH/SOL/XAU/XAG/WTI 六品种, 且更新到当天, 因此以它为源更合适。

关键约束
--------
* **有数据才写, 不伪造** —— OHLC 非法(close<=0 / high<low)的行直接丢弃
* **单位统一** —— kline_bars.ts 是**毫秒**, 全库其它表是**秒**, 此处换算
* **幂等** —— UNIQUE(asset,ts) + INSERT OR IGNORE, 重复运行安全
* **纯新增** —— 只 CREATE TABLE IF NOT EXISTS + INSERT, 不动任何既有表,
  回滚只需 DROP TABLE ohlcv_daily

用法:
    python3 backfill_ohlcv_local.py            # 回填
    python3 backfill_ohlcv_local.py --dry-run  # 只报告不写库
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"

# 与 backfill_ohlcv.py 完全一致的 DDL, 保证两张表可互换
DDL = """
CREATE TABLE IF NOT EXISTS ohlcv_daily (
  asset     TEXT NOT NULL,
  ts        INTEGER NOT NULL,
  open      REAL NOT NULL,
  high      REAL NOT NULL,
  low       REAL NOT NULL,
  close     REAL NOT NULL,
  volume    INTEGER NOT NULL,
  source    TEXT NOT NULL DEFAULT 'yfinance',
  UNIQUE(asset, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_asset_ts
ON ohlcv_daily(asset, ts DESC);
"""

SOURCE = "kline_bars_1d"


def _to_sec(ts: int) -> int:
    """毫秒 -> 秒。与全库其它表(新闻/异动/宏观)保持同一时间轴。"""
    return ts // 1000 if ts > 100_000_000_000 else ts


def build_rows(conn: sqlite3.Connection) -> tuple[list[tuple], dict]:
    """从 kline_bars 抽取合法日线行。返回 (rows, 统计)。"""
    stats = {"src_rows": 0, "dropped": 0, "assets": {}}
    rows: list[tuple] = []
    cur = conn.execute(
        "SELECT symbol, ts, open, high, low, close, volume "
        "FROM kline_bars WHERE timeframe='1d' ORDER BY symbol, ts"
    )
    for sym, ts, o, h, l, c, v in cur:
        stats["src_rows"] += 1
        # 非法 OHLC 一律丢弃, 绝不参与指标计算(与 backfill_tech_signals 的防御一致)
        if not all(x is not None for x in (o, h, l, c)) or c <= 0 or h <= 0 or l <= 0 or o <= 0:
            stats["dropped"] += 1
            continue
        if h < l:
            stats["dropped"] += 1
            continue
        rows.append((sym, _to_sec(int(ts)), float(o), float(h), float(l), float(c),
                     int(v or 0), SOURCE))
        stats["assets"][sym] = stats["assets"].get(sym, 0) + 1
    return rows, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kline_bars'"
        ).fetchone()
        if not has:
            print("[FATAL] kline_bars 不存在, 无本地源可用", file=sys.stderr)
            return 1

        if not args.dry_run:
            conn.executescript(DDL)

        rows, stats = build_rows(conn)

        # 去重: 同一 (asset,ts) 只留最后一条(按 ts 升序遍历, 后者覆盖前者)
        dedup: dict[tuple[str, int], tuple] = {}
        for r in rows:
            dedup[(r[0], r[1])] = r
        uniq = list(dedup.values())

        print(f"[INFO] 源 kline_bars 日线 {stats['src_rows']} 行, "
              f"丢弃非法 {stats['dropped']} 行, 去重后 {len(uniq)} 行")

        inserted = 0
        if not args.dry_run and uniq:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO ohlcv_daily "
                "(asset, ts, open, high, low, close, volume, source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                uniq,
            )
            inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()

        total = None
        try:
            total = conn.execute("SELECT COUNT(*) FROM ohlcv_daily").fetchone()[0]
        except sqlite3.OperationalError:
            pass

        print(f"[OK] 新增 {inserted} 行{'  (dry-run 未落库)' if args.dry_run else ''}")
        print(f"[OK] ohlcv_daily 现共 {total} 行\n")

        print(f"{'标的':<10}{'根数':>7}   {'最新交易日':<12}{'数据滞后':>8}")
        now = int(dt.datetime.now(dt.UTC).timestamp())
        for sym, n in sorted(stats["assets"].items(), key=lambda x: -x[1]):
            mx = conn.execute(
                "SELECT MAX(ts) FROM ohlcv_daily WHERE asset=?", (sym,)
            ).fetchone()[0] if total else None
            if mx:
                day = dt.datetime.fromtimestamp(mx, dt.UTC).strftime("%Y-%m-%d")
                lag = f"{(now - mx) // 86400}d"
            else:
                day, lag = "-", "-"
            print(f"  {sym:<8}{n:>7}   {day:<12}{lag:>8}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())