"""backfill_intraday_from_kline_bars.py — 用本地 kline_bars 填充 ohlcv_intraday
===============================================================================
背景
----
归因页 K 线读的 ohlcv_intraday 一直是 **0 行**, 前端弹
"ohlcv_intraday 无 XXX 数据(需先回补)"。但库里明明有 8 万多行真实分钟级
K 线 —— 存在另一张表 kline_bars(1m 44212 / 10m 9761 / 5m 5901 / 15m 5859 /
1h 4988 / 4h 2756 / 3m 1171, 覆盖 BTC/ETH/SOL/XAU/XAG/WTI)。

即典型的"同名不同表"漂移: 上游写入 kline_bars, 下游只认 ohlcv_intraday。
本脚本做一次**纯本地、零网络、零编造**的搬运, 数据一个字节都不改。

用法
----
    python3 backfill_intraday_from_kline_bars.py            # 全量
    python3 backfill_intraday_from_kline_bars.py --dry-run  # 只看会写多少

幂等: 走 INSERT OR IGNORE(主键 asset+interval+ts), 重跑不会重复。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intraday_store import (  # noqa: E402
    DB_PATH,
    NATIVE_INTERVALS,
    init_schema,
)

# ohlcv_intraday 语义是**分钟级**, 日线归 ohlcv_daily, 这里不搬。
INTRADAY_ONLY = ("1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "4h")

# ohlcv_intraday.ts 是 **epoch 秒**, 而 kline_bars.ts 是 **epoch 毫秒**
# (实测 BTC 1m: 1791107400000)。不换算会整体溢出成公元 58751 年,
# datetime 直接抛 ValueError —— 所以这里按量级自动归一化, 而不是写死 /1000。
MS_THRESHOLD = 100_000_000_000        # 1e11: 两者分界(1e11 秒 = 公元 5138 年)

TS_MIN = 946_684_800          # 2000-01-01 UTC
TS_MAX = 4_102_444_800        # 2100-01-01 UTC


def normalize_ts(ts):
    """毫秒/秒自动归一 → epoch 秒; 明显损坏返回 None。"""
    if ts is None:
        return None
    v = int(ts)
    if abs(v) >= MS_THRESHOLD:
        v //= 1000
    return v if TS_MIN <= v <= TS_MAX else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_schema()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    pairs = conn.execute(
        "SELECT symbol, timeframe, COUNT(*) n FROM kline_bars "
        "WHERE timeframe != '1d' GROUP BY 1,2 ORDER BY symbol, timeframe"
    ).fetchall()

    grand_total = 0
    print(f"{'asset':<9}{'interval':<9}{'库内':>8}{'待搬':>8}{'丢弃坏ts':>10}")
    print("-" * 46)

    for row in pairs:
        sym = (row["symbol"] or "").upper()
        tf = row["timeframe"]
        if not sym or tf not in INTRADAY_ONLY or tf not in NATIVE_INTERVALS:
            continue

        raw = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM kline_bars "
            "WHERE symbol = ? AND timeframe = ?",
            (row["symbol"], tf),
        ).fetchall()

        step = NATIVE_INTERVALS[tf]
        now = int(time.time())
        rows, dropped = [], 0
        for r in raw:
            ts = normalize_ts(r["ts"])
            # ts 缺失/越界 = 上游写入时的时间戳损坏, 不能用来画 K 线
            if ts is None:
                dropped += 1
                continue
            o, h, l, c = r["open"], r["high"], r["low"], r["close"]
            if o is None or h is None or l is None or c is None:
                dropped += 1
                continue
            rows.append((
                sym, tf, int(ts), float(o), float(h), float(l), float(c),
                float(r["volume"]) if r["volume"] is not None else 0.0,
                0, 0 if ts + step > now else 1, f"kline_bars:{tf}",
            ))

        have = conn.execute(
            "SELECT COUNT(*) FROM ohlcv_intraday WHERE asset=? AND interval=?",
            (sym, tf),
        ).fetchone()[0]

        if args.dry_run:
            inserted = 0
        elif rows:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO ohlcv_intraday "
                "(asset, interval, ts, open, high, low, close, volume, "
                " trades, closed, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            inserted = cur.rowcount
        else:
            inserted = 0

        grand_total += inserted
        print(f"{sym:<9}{tf:<9}{have:>8}{inserted:>8}{dropped:>10}")

    total = conn.execute("SELECT COUNT(*) FROM ohlcv_intraday").fetchone()[0]
    print("-" * 46)
    print(f"{'合计':<18}{'新增':>8}{grand_total:>8}    ohlcv_intraday 现有 {total} 行")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())