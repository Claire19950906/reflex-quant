"""trade_reflection_llm 事后客观事实注入测试。

覆盖 _collect_posthoc_evidence：
  - 无决策时间/无资产 → 诚实「无数据」
  - 上行 K 线 → realized_move 反映真实方向
  - 扁平占位 K 线 → 被过滤 → 无数据（不造方向）
  - 跨资产品种方向 + 同类历史反例取到
"""

import os
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db as _db  # noqa: E402
import trade_reflection_llm as t  # noqa: E402


def _mk_ts(dt: str) -> int:
    return int(time.mktime(time.strptime(dt, "%Y-%m-%d %H:%M:%S")) * 1000)


@pytest.fixture
def conn():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(_db._CREATE_REVIEW_INSIGHTS)
    c.execute(_db._CREATE_KLINE_BARS)
    _db._ensure_columns(c, "review_insights", _db._REVIEW_INSIGHTS_COLUMNS)
    c.commit()
    yield c
    c.close()


def _seed_klines(conn, symbol, ts0, start=1, end=11, open_=100.0, high=101.0, low=99.5, close=100.8):
    for i in range(start, end):
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (symbol, "1h", ts0 + i * 3600 * 1000, open_, high, low, close, 10.0),
        )


def test_posthoc_no_inputs(conn):
    out = t._collect_posthoc_evidence(conn, "XAU", None)
    assert out["realized_move"] == "(无数据)"
    assert out["posthoc_cross_asset"] == "(无数据)"
    assert out["similar_past_lessons"] == "(无)"


def test_posthoc_directional(conn):
    ts0 = _mk_ts("2020-01-01 00:00:00")
    _seed_klines(conn, "XAU", ts0)
    conn.commit()
    out = t._collect_posthoc_evidence(conn, "XAU", ts0)
    assert "(无数据)" not in out["realized_move"]
    assert "up" in out["realized_move"]  # close 100.8 > open 100.0


def test_posthoc_flat_placeholder_filtered(conn):
    ts0 = _mk_ts("2020-01-01 00:00:00")
    for i in range(1, 6):
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("XAU", "1h", ts0 + i * 3600 * 1000, 4139.28, 4139.28, 4139.28, 4139.28, 0.0),
        )
    conn.commit()
    out = t._collect_posthoc_evidence(conn, "XAU", ts0)
    # 扁平 bar 高-低=0，被 _fetch_kline_window 过滤 → 无数据，绝不编造方向
    assert out["realized_move"] == "(无数据)"


def test_posthoc_cross_asset_and_lessons(conn):
    ts0 = _mk_ts("2020-01-01 00:00:00")
    _seed_klines(conn, "XAU", ts0)
    _seed_klines(conn, "BTC", ts0, open_=100.0, high=101.0, low=99.5, close=99.0)  # down
    conn.execute(
        "INSERT INTO review_insights (rule, asset, scope, counter_factual) "
        "VALUES ('buy rule','XAU','all','当 VIX 急升时不适用')",
    )
    conn.commit()
    out = t._collect_posthoc_evidence(conn, "XAU", ts0)
    assert "BTC" in out["posthoc_cross_asset"]
    assert "(无)" not in out["similar_past_lessons"]
    assert "VIX" in out["similar_past_lessons"]