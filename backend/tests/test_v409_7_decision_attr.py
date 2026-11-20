#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v409.7 决策级归因单测：只测纯函数与取价逻辑，不碰生产库。"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src_python"))

import insight_decision_attr as ia


def test_normalize_direction():
    assert ia.normalize_direction("1") == "BUY"
    assert ia.normalize_direction("-1") == "SELL"
    assert ia.normalize_direction("BUY") == "BUY"
    assert ia.normalize_direction("0") == ""
    assert ia.normalize_direction(None) == ""


def test_score_split_verified_when_blocked_loses():
    # 放行组平均 +0.6%，拦截组平均 -0.6% → "拦得对"，应判 verified
    res = ia.score_split([0.006] * 8, [-0.006] * 8)
    assert res["verdict"] == "verified"
    assert res["score"] >= 0.7
    assert res["n_allowed"] == 8 and res["n_blocked"] == 8


def test_score_split_failed_when_blocked_wins():
    # 拦截组比放行组更好 → 说明这条"提高门槛"的规则是错的
    res = ia.score_split([-0.004] * 6, [0.004] * 6)
    assert res["verdict"] == "failed"
    assert res["score"] <= 0.3


def test_score_split_insufficient_but_keeps_score_for_audit():
    res = ia.score_split([0.01], [-0.01, -0.002])
    assert res["verdict"] == "insufficient_sample"
    assert res["score"] is not None      # 留档但不判定
    assert res["enough"] is False


def test_score_split_empty_group():
    res = ia.score_split([], [0.01, 0.02])
    assert res["verdict"] == "insufficient_sample"
    assert res["score"] is None


def _mk_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kline_bars (id INTEGER PRIMARY KEY, symbol TEXT,"
                 " timeframe TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                 " close REAL, volume REAL, fetched_at TEXT)")
    return conn


def test_price_at_and_after_use_1h_bars():
    conn = _mk_conn()
    base = 1_700_000_000_000
    for i, px in enumerate([100.0, 101.0, 103.0]):
        conn.execute("INSERT INTO kline_bars (symbol,timeframe,ts,close) VALUES ('XAU','1h',?,?)",
                     (base + i * 3600 * 1000, px))
    assert ia._price_at(conn, "XAU", base + 30 * 60 * 1000) == 100.0
    assert ia._price_after(conn, "XAU", base, 2) == 103.0


def test_direction_adjusted_return_sign():
    conn = _mk_conn()
    base = 1_700_000_000_000
    conn.execute("INSERT INTO kline_bars (symbol,timeframe,ts,close) VALUES ('WTI','1h',?,100.0)", (base,))
    conn.execute("INSERT INTO kline_bars (symbol,timeframe,ts,close) VALUES ('WTI','1h',?,90.0)",
                 (base + 24 * 3600 * 1000,))
    buy = ia.direction_adjusted_return(conn, "WTI", base, "BUY")
    sell = ia.direction_adjusted_return(conn, "WTI", base, "SELL")
    assert round(buy, 4) == -0.1   # 跌 → 做多是亏
    assert round(sell, 4) == 0.1   # 跌 → 做空是赚


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("全部通过")
