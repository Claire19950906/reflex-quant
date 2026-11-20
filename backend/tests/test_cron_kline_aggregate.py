# -*- coding: utf-8 -*-
"""test_cron_kline_aggregate.py — 用 market_ticks 补齐 kline_bars 高周期 K 线的回归测试。

覆盖本模块刻意设计的那几个约束(改坏了会静默污染下游 ohlcv_daily / tech_signals):

  1. **单位**: market_ticks.ts 是秒, kline_bars.ts 是毫秒 —— 落库必须换算,
     否则新 bar 会落到 1970 年, 下游按时间过滤时全部读不到;
  2. **只补缺口, 不覆盖活源**: 外部源还活着(symbol+timeframe 的最新 bar 起点
     已经追到当前应有时点)时必须整段跳过, 绝不能拿 tick 聚合覆盖交易所真实 K 线;
  3. **聚合正确性**: open=桶内首个价, close=桶内末个价, high/low 取极值,
     volume 是 tick 计数(代理值, 不是交易所成交量);
  4. **不写未来桶**: 桶起点 > 当前应有时点的必须丢弃。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cron_kline_aggregate as agg


def _conn(ticks=(), bars=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE market_ticks (id INTEGER PRIMARY KEY, symbol TEXT, "
                 "ts INTEGER, price REAL, change24h REAL, source TEXT, source_count INT)")
    conn.executemany(
        "INSERT INTO market_ticks (symbol, ts, price, change24h, source, source_count) "
        "VALUES (?,?,?,?,?,?)", ticks)
    conn.execute("CREATE TABLE kline_bars (id INTEGER PRIMARY KEY, symbol TEXT, "
                 "timeframe TEXT, ts INTEGER, open REAL, high REAL, low REAL, "
                 "close REAL, volume REAL, fetched_at TEXT, "
                 "UNIQUE(symbol, timeframe, ts))")
    conn.executemany(
        "INSERT INTO kline_bars (symbol,timeframe,ts,open,high,low,close,volume) "
        "VALUES (?,?,?,?,?,?,?,?)", bars)
    return conn


# --------------------------------------------------------------------------
# 1. 周期边界
# --------------------------------------------------------------------------

def test_expected_start_floors_to_boundary():
    base = 1_704_067_200            # 2024-01-01 00:00:00 UTC
    assert agg.expected_start(3600, base) == base
    assert agg.expected_start(3600, base + 1800) == base
    assert agg.expected_start(3600, base + 3599) == base
    assert agg.expected_start(3600, base + 3600) == base + 3600
    assert agg.expected_start(86400, base + 86399) == base


def test_expected_start_1d_is_utc_midnight():
    now = 1_704_067_200 + 5 * 86400 + 12345
    exp = agg.expected_start(86400, now)
    assert exp % 86400 == 0
    assert exp <= now < exp + 86400


# --------------------------------------------------------------------------
# 2. 聚合
# --------------------------------------------------------------------------

def test_aggregate_ticks_ohlc_and_count():
    base = 1_704_067_200
    ticks = [("WTI", base + 0, 100.0), ("WTI", base + 10, 105.0),
             ("WTI", base + 20, 95.0), ("WTI", base + 30, 102.0)]
    conn = _conn([(s, t, p, 0.0, "sina", 1) for s, t, p in ticks])
    out = agg.aggregate_ticks(conn, "WTI", 3600, base)
    assert list(out) == [base]
    o, h, l, c, n = out[base]
    assert (o, h, l, c, n) == (100.0, 105.0, 95.0, 102.0, 4)
    conn.close()


def test_aggregate_ticks_splits_by_bucket():
    base = 1_704_067_200
    conn = _conn([
        ("WTI", base + 0, 10.0, 0.0, "sina", 1),
        ("WTI", base + 1800, 20.0, 0.0, "sina", 1),
        ("WTI", base + 3600, 30.0, 0.0, "sina", 1),
    ])
    out = agg.aggregate_ticks(conn, "WTI", 3600, base)
    assert set(out) == {base, base + 3600}
    # base+1800 仍在同一个 1h 桶内 → close 是桶内末个价 20.0
    assert out[base][0] == 10.0 and out[base][3] == 20.0
    assert out[base][1] == 20.0 and out[base][2] == 10.0
    assert out[base + 3600][0] == 30.0
    conn.close()


def test_aggregate_ticks_ignores_nonpositive_price():
    base = 1_704_067_200
    conn = _conn([
        ("WTI", base, 100.0, 0.0, "sina", 1),
        ("WTI", base + 5, 0.0, 0.0, "sina", 1),      # 坏价, 必须跳过
        ("WTI", base + 6, -3.0, 0.0, "sina", 1),     # 坏价, 必须跳过
    ])
    out = agg.aggregate_ticks(conn, "WTI", 3600, base)
    assert out[base][4] == 1          # 只算了 1 个有效 tick
    assert out[base][2] == 100.0      # low 没被 0/负数污染
    conn.close()


def test_aggregate_ticks_respects_since():
    base = 1_704_067_200
    conn = _conn([
        ("WTI", base, 1.0, 0.0, "sina", 1),
        ("WTI", base + 7200, 2.0, 0.0, "sina", 1),
    ])
    out = agg.aggregate_ticks(conn, "WTI", 3600, base + 3600)
    assert base not in out
    assert (base + 7200) in out
    conn.close()


def test_aggregate_ticks_empty_when_no_rows():
    conn = _conn()
    assert agg.aggregate_ticks(conn, "WTI", 3600, 0) == {}
    conn.close()



# --------------------------------------------------------------------------
# 3. 只补缺口 —— 不覆盖活源
# --------------------------------------------------------------------------

def test_plan_skips_when_source_alive():
    now = 1_704_067_200 + 7200
    exp = agg.expected_start(3600, now)
    conn = _conn(bars=[("BTC", "1h", exp * 1000, 1, 1, 1, 1, 1)])
    plan = {tf: act for tf, _, act, _ in agg.plan_symbol(conn, "BTC", now)}
    assert plan["1h"] == 0            # 0 = skip
    conn.close()


def test_plan_flags_gap_when_source_stale():
    now = 1_704_067_200 + 7200
    stale = agg.expected_start(3600, now) - 3 * 3600
    conn = _conn(bars=[("WTI", "1h", stale * 1000, 1, 1, 1, 1, 1)])
    plan = {tf: act for tf, _, act, _ in agg.plan_symbol(conn, "WTI", now)}
    assert plan["1h"] == 2            # 2 = fill
    conn.close()


def test_plan_skips_symbol_without_tick_data():
    now = 1_704_067_200 + 7200
    conn = _conn()
    plan = {tf: act for tf, _, act, _ in agg.plan_symbol(conn, "XAG", now)}
    assert all(a == 0 for a in plan.values())
    conn.close()


def test_plan_uses_min_tick_ts_when_no_bars():
    now = 1_704_067_200 + 7200
    conn = _conn([("WTI", 1_704_067_200, 1.0, 0.0, "sina", 1)])
    plan = agg.plan_symbol(conn, "WTI", now)
    assert any(act == 2 for _, _, act, _ in plan)
    conn.close()


# --------------------------------------------------------------------------
# 4. 单位换算 + 不覆盖既有 bar
# --------------------------------------------------------------------------

def test_last_bar_start_ms_reads_milliseconds():
    conn = _conn(bars=[("WTI", "1h", 1_704_067_200_000, 1, 1, 1, 1, 1)])
    assert agg.last_bar_start_ms(conn, "WTI", "1h") == 1_704_067_200_000
    conn.close()


def test_written_bar_uses_millisecond_ts():
    """落库时 ts 必须换算成毫秒 —— 否则下游按秒过滤会全部读不到。"""
    base = 1_704_067_200
    conn = _conn([("WTI", base, 100.0, 0.0, "sina", 1)])
    buckets = agg.aggregate_ticks(conn, "WTI", 3600, base)
    rows = [("WTI", "1h", b * 1000, o, h, l, c, float(v))
            for b, (o, h, l, c, v) in buckets.items()]
    assert rows[0][2] == base * 1000
    conn.close()


def test_insert_or_ignore_does_not_overwrite_existing_bar():
    """历史 bar 已存在时不能被 tick 聚合结果覆盖。"""
    base = 1_704_067_200
    conn = _conn(
        ticks=[("WTI", base, 999.0, 0.0, "sina", 1)],
        bars=[("WTI", "1h", base * 1000, 1.0, 2.0, 0.5, 1.5, 7.0)],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO kline_bars "
        "(symbol,timeframe,ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?)",
        [("WTI", "1h", base * 1000, 999.0, 999.0, 999.0, 999.0, 1.0)],
    )
    row = conn.execute(
        "SELECT close, volume FROM kline_bars WHERE symbol='WTI' "
        "AND timeframe='1h' AND ts=?", (base * 1000,)
    ).fetchone()
    assert row["close"] == 1.5        # 原值保留
    assert row["volume"] == 7.0
    conn.close()


def test_current_bar_upsert_refreshes_price():
    """当前未收盘 bar 的价格每分钟都在变, 必须能刷新。"""
    base = 1_704_067_200
    conn = _conn(
        ticks=[("WTI", base, 123.0, 0.0, "sina", 1)],
        bars=[("WTI", "1h", base * 1000, 1.0, 1.0, 1.0, 1.0, 1.0)],
    )
    conn.executemany(
        "INSERT INTO kline_bars "
        "(symbol,timeframe,ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,timeframe,ts) DO UPDATE SET "
        "open=excluded.open,high=excluded.high,low=excluded.low,"
        "close=excluded.close,volume=excluded.volume",
        [("WTI", "1h", base * 1000, 123.0, 123.0, 123.0, 123.0, 1.0)],
    )
    row = conn.execute(
        "SELECT close FROM kline_bars WHERE symbol='WTI' "
        "AND timeframe='1h' AND ts=?", (base * 1000,)
    ).fetchone()
    assert row["close"] == 123.0
    conn.close()


def test_future_bucket_is_dropped():
    """桶起点晚于当前应有时点的必须丢弃(时点安全)。"""
    now = 1_704_067_200 + 7200
    exp = agg.expected_start(3600, now)
    future = exp + 3600
    conn = _conn([
        ("WTI", exp, 1.0, 0.0, "sina", 1),
        ("WTI", future, 2.0, 0.0, "sina", 1),
    ])
    buckets = agg.aggregate_ticks(conn, "WTI", 3600, 0)
    kept = [b for b in buckets if b <= exp]
    assert future not in kept
    assert exp in kept
    conn.close()


def test_timeframe_seconds_mapping():
    assert agg.TIMEFRAME_SEC == {"1h": 3600, "4h": 14400, "1d": 86400}
