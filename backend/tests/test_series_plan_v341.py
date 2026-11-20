# -*- coding: utf-8 -*-
"""v341: 按“序列”排版 —— 一条序列只留一个来源。

真实故障：VIX 在库里有三份副本
    '*'        + '^VIX'
    'US_MACRO' + 'us_vix_cboe'
    'fred'     + 'VIX'
按 asset 遍历时同一条 VIX 被数了三遍，异动/权重跟着膨胀。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import series_namespace as sn
import series_plan as sp


def _db(rows, ohlcv=()):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE external_indicators "
                 "(asset TEXT, indicator TEXT, ts INT, value REAL)")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INT, close REAL)")
    conn.executemany("INSERT INTO external_indicators VALUES (?,?,1,1.0)",
                     [(r[0], r[1]) for r in rows])
    conn.executemany("INSERT INTO ohlcv_daily VALUES (?,?,1.0)",
                     [(r[0], r[1]) for r in ohlcv])
    return conn


def test_drawer_ticker_becomes_its_own_series():
    """'*' 里的 GLD 是 GLD 自己的价格，不是品种 '*'。"""
    conn = _db([("*", "GLD", 1), ("*", "TLT", 1)])
    plan = sp.build_plan(conn)
    assert {p["series"] for p in plan} == {"GLD", "TLT"}
    assert all(p["source_asset"] == "*" for p in plan)


def test_duplicate_series_collapse_to_one():
    conn = _db([("*", "^VIX", 5), ("US_MACRO", "us_vix_cboe", 9), ("fred", "VIX", 30)])
    plan = sp.build_plan(conn)
    vix = [p for p in plan if p["series"] == "VIX"]
    assert len(vix) == 1
    assert len(vix[0]["conflicts"]) == 2     # 另外两份被记下来，但没有产出


def test_ohlcv_preferred_when_long_enough():
    conn = _db([("BTC", "close", 1)], ohlcv=[("BTC", i, 1.0) for i in range(40)])
    plan = sp.build_plan(conn)
    btc = [p for p in plan if p["series"] == "BTC"][0]
    assert btc["source_asset"] is None       # 走 ohlcv_daily
    assert btc["rows"] >= 30


def test_short_ohlcv_falls_back_to_indicator():
    conn = _db([("BTC", "close", 1)], ohlcv=[("BTC", i, 1.0) for i in range(3)])
    plan = sp.build_plan(conn)
    btc = [p for p in plan if p["series"] == "BTC"][0]
    assert btc["source_asset"] == "BTC"


@pytest.mark.parametrize("asset,indicator", [
    ("BTC", "cm_hashrate"),        # 哈希率
    ("BTC", "cm_supply"),          # 流通量
    ("NG", "hf_fut_chg_pct"),      # 涨跌幅
    ("NVDA", "mexc_funding_rate"), # 资金费
    ("SOL", "bybit_open_interest"),
])
def test_non_price_metrics_are_not_series(asset, indicator):
    conn = _db([(asset, indicator, 1)])
    assert sp.build_plan(conn) == []


def test_ng_price_indicator_still_works():
    conn = _db([("NG", "hf_fut_price", 3), ("NG", "hf_fut_chg_pct", 3)])
    plan = sp.build_plan(conn)
    assert [p["series"] for p in plan] == ["NG"]
    assert plan[0]["indicator"] == "hf_fut_price"


# ── 注册表自动重建 + 时差告警 ──────────────────────────────────────────

def _regdb(rows=(("BTC", "close", 1_700_000_000),)):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE external_indicators "
                 "(asset TEXT, indicator TEXT, ts INT, value REAL)")
    conn.executemany("INSERT INTO external_indicators VALUES (?,?,?,1.0)",
                     [(a, i, ts) for a, i, ts in rows])
    return conn


def test_refresh_is_idempotent():
    """小时 cron 每小时都跑一遍，不能每跑一次就多出一批行。"""
    import build_series_registry as reg
    conn = _regdb([("BTC", "close", 1_700_000_000), ("*", "GLD", 1_700_000_000)])
    n1, added1 = reg.refresh(conn)
    n2, added2 = reg.refresh(conn)
    assert n1 == n2 == 2
    assert added1 == 2 and added2 == 0


def test_freshness_reports_staleness_and_missing():
    import build_series_registry as reg
    conn = _regdb([("BTC", "close", 1_700_000_000)])
    reg.refresh(conn)
    fresh = reg.freshness(conn, now=1_700_000_000)
    assert fresh["stale_hours"] == 0.0
    assert fresh["missing"] == 0
    # 一天后再看：数据没更新 → 落后 24h
    stale = reg.freshness(conn, now=1_700_000_000 + 86400)
    assert stale["stale_hours"] == 24.0
