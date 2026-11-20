#!/usr/bin/env python3
"""把腾讯快照价聚合成 K 线，落进 kline_bars（给 MU/NVDA/TSLA/AMD/NDX/A股 用）。

背景：
  · 这些标的海外行情源全断（Yahoo 停 35 天），只有腾讯还能拿到价和量。
  · 腾讯接口只给"当前价"，不给历史 K 线，所以只能靠**自己攒**：
    tencent_quotes 每 5 分钟写一条 tencent_price，本脚本把这些点聚成
    1m / 5m / 1h / 1d 四种周期的 OHLC，写进 kline_bars。
  · 成交量：腾讯给的是**当日累计成交量**，不是单根 K 线的量。用它倒推每根
    bar 的量会造假（跨日归零、停牌跳变），所以 volume 一律写 NULL，
    宁可缺也不编。

写入：kline_bars(symbol, timeframe, ts, open, high, low, close, volume)
  ts 是毫秒（该表既有数据就是毫秒），我们这里统一写毫秒。
"""
import os
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Dict, List, Tuple

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("TRIDENT_DB", os.path.join(BACKEND, "trident_event_bus.db"))

# 资产 -> 腾讯 indicator 前缀（tencent_price）
# 账户只做美元/黄金/加密/美股，A 股不做。
# 而且这两个名字下面存的其实是「上证指数 sh000001」「深证成指 sz399001」，
# 名字与内容对不上（A50 是富时中国 A50 期货，不是上证指数），留着就是在库里放假数据。
ASSETS = ("MU", "NVDA", "TSLA", "AMD", "NDX", "TENCENT")

TIMEFRAMES: Dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "1h": 3600,
    "1d": 86400,
}


def _load_points(conn: sqlite3.Connection, asset: str,
                 days: int = 3) -> List[Tuple[int, float]]:
    """只取最近 N 天的快照点。

    v371: 原来无时间过滤, 每次把该品种**全部历史**快照重算一遍 1m/5m/1h/1d,
    再 DELETE 一大段 + 重插几千行 —— 单次 hold 写锁数秒, 别的采集器排队等锁。
    实测这就是"偶发 6 秒锁"的来源之一。日内 bar 只需要最近几天, 更早的已定型。
    """
    since = int(time.time()) - days * 86400
    rows = conn.execute(
        "SELECT ts, value FROM external_indicators"
        " WHERE asset=? AND indicator='tencent_price' AND value IS NOT NULL"
        "   AND ts >= ?"
        " ORDER BY ts ASC",
        (asset, since),
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows if r[1] is not None]


def _aggregate(points: List[Tuple[int, float]], bucket_s: int) -> List[Tuple[int, float, float, float, float]]:
    """按 bucket 聚合 → [(bucket_ts, open, high, low, close)]，桶内按时间序。"""
    buckets: Dict[int, List[float]] = defaultdict(list)
    for ts, price in points:
        buckets[(ts // bucket_s) * bucket_s].append(price)
    out = []
    for bts in sorted(buckets):
        vals = buckets[bts]
        out.append((bts, vals[0], max(vals), min(vals), vals[-1]))
    return out


def main() -> int:
    if not os.path.exists(DB):
        print(f"[FATAL] DB 不存在: {DB}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=30000")
    written = 0
    try:
        for asset in ASSETS:
            points = _load_points(conn, asset)
            if len(points) < 2:
                continue
            for tf, bucket_s in TIMEFRAMES.items():
                bars = _aggregate(points, bucket_s)
                if not bars:
                    continue
                # 同一根 bar 重算时要覆盖（分钟桶会随最新点变化）
                oldest_ms = bars[0][0] * 1000
                conn.execute(
                    "DELETE FROM kline_bars WHERE symbol=? AND timeframe=? AND ts>=?",
                    (asset, tf, oldest_ms),
                )
                rows = [(asset, tf, b[0] * 1000, b[1], b[2], b[3], b[4]) for b in bars]
                # 之前是"全部资产×全部周期"攒成一个大事务最后 commit，
                # 几千行持写锁十几秒，别的采集器全在排队等锁 → 一堆 database is locked。
                # 改成"每根周期段提交一次"：事务变短，别的 writer 才有插空的机会。
                # v371: 再切细 —— 每 300 行提交一次, 单次持锁从"数秒"降到"毫秒"
                for i0 in range(0, len(rows), 300):
                    chunk = rows[i0:i0 + 300]
                    for attempt in range(4):
                        try:
                            conn.executemany(
                                "INSERT INTO kline_bars"
                                " (symbol, timeframe, ts, open, high, low, close, volume)"
                                " VALUES (?,?,?,?,?,?,?,NULL)", chunk,
                            )
                            conn.commit()
                            break
                        except sqlite3.OperationalError as exc:
                            if "locked" in str(exc).lower() and attempt < 3:
                                time.sleep(0.5 * (attempt + 1))
                                continue
                            raise
                written += len(rows)
    except Exception as exc:
        conn.rollback()
        print(f"[ERROR] 聚合失败: {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] 写入 {written} 根 K 线", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
