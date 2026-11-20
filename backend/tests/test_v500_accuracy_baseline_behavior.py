#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v500：准确率基线的行为测试（accuracy_baseline 从 0% 覆盖起步）。

这块是"我这套系统到底准不准"的唯一度量口径。它算错了，
上面所有"决策正确率"的说法就全是空话 —— 所以它必须有测试。
"""
import math
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import accuracy_baseline as ab


# ── 解析与容错 ─────────────────────────────────────────────────
def test_parse_date_both_formats():
    assert ab._parse_date("2026-11-20") == ab._parse_date("2026-11-20 00:00:00")
    assert ab._parse_date("2026-11-20 12:30:00") > ab._parse_date("2026-11-20")


def test_parse_date_rejects_garbage():
    with pytest.raises(ValueError):
        ab._parse_date("不是日期")


def test_safe_float_handles_none_nan_and_garbage():
    assert ab._safe_float(None, 0.0) == 0.0
    assert ab._safe_float("3.5") == 3.5
    assert ab._safe_float(float("nan"), 1.0) == 1.0
    assert ab._safe_float("abc", 9.0) == 9.0


# ── 汇总指标 ───────────────────────────────────────────────────
def _row(pnl, **kw):
    base = {"pnl_pct": pnl, "pnl_usdt": pnl, "horizon_minutes": 60,
            "score": 0.5, "mfe_pct": 1.0, "mae_pct": -1.0,
            "asset": "BTC", "action": "BUY", "event_strength": "A"}
    base.update(kw)
    return base


def test_stats_empty_returns_count_zero():
    assert ab._stats([]) == {"count": 0}


def test_stats_win_rate_and_means():
    rows = [_row(2.0), _row(-1.0), _row(3.0), _row(0.0)]
    s = ab._stats(rows)
    assert s["count"] == 4
    assert s["win_count"] == 2 and s["loss_count"] == 1
    assert s["breakeven_count"] == 1
    assert s["win_rate"] == 0.5
    assert s["avg_pnl_pct"] == pytest.approx(1.0)
    assert s["sum_pnl_usdt"] == pytest.approx(4.0)


def test_stats_profit_factor_and_payoff():
    rows = [_row(4.0), _row(-2.0)]
    s = ab._stats(rows)
    assert s["profit_factor"] == pytest.approx(2.0)
    assert s["payoff_ratio"] == pytest.approx(2.0)
    assert s["avg_win_pct"] == pytest.approx(4.0)
    # 口径：盈亏都以 percent 计，亏损在展示层带负号（与 _print_table 一致）
    assert s["avg_loss_pct"] == pytest.approx(-2.0)


def test_stats_no_loss_profit_factor_is_none():
    s = ab._stats([_row(1.0), _row(2.0)])
    assert s["profit_factor"] is None
    assert s["payoff_ratio"] is None


def test_stats_drawdown_is_positive_number():
    rows = [_row(5.0), _row(-8.0), _row(-3.0)]
    s = ab._stats(rows)
    assert s["max_drawdown_pct"] == pytest.approx(11.0)
    assert s["current_drawdown_pct"] == pytest.approx(11.0)


def test_stats_single_row_has_no_sharpe():
    s = ab._stats([_row(1.0)])
    assert s["sharpe_per_trade"] == 0.0


def test_stats_sharpe_positive_for_evenly_positive_series():
    s = ab._stats([_row(1.0), _row(1.2), _row(1.4)])
    assert s["sharpe_per_trade"] > 0


# ── 分桶 ───────────────────────────────────────────────────────
def test_bucket_by_uppercases_and_defaults():
    rows = [_row(1.0, asset="btc"), _row(2.0, asset=None)]
    b = ab._bucket_by(rows, "asset")
    assert set(b) == {"BTC", "UNKNOWN"}
    assert len(b["BTC"]) == 1


def test_entry_delay_buckets_by_seconds():
    rows = [
        _row(1.0, entry_ts=1000, news_ts=980),     # 20s → <60s
        _row(1.0, entry_ts=1000, news_ts=800),     # 200s → 60-300s
        _row(1.0, entry_ts=1000, news_ts=400),     # 600s → 300-900s
        _row(1.0, entry_ts=1000, news_ts=0),       # 1000s → 900s+
        _row(1.0, entry_ts=None, news_ts=None),    # 跳过
    ]
    out = ab._entry_delay_buckets(rows)
    assert out == {"<60s": 1, "60-300s": 1, "300-900s": 1, "900s+": 1}


# ── 端到端：真查一遍库 ──────────────────────────────────────────
@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE quick_sim_trades(
          id INTEGER, decision_id INTEGER, news_id INTEGER, asset TEXT,
          action TEXT, entry_price REAL, exit_price REAL, pnl_pct REAL,
          pnl_usdt REAL, verdict TEXT, entry_ts INTEGER, exit_ts INTEGER,
          max_price REAL, min_price REAL, mfe_pct REAL, mae_pct REAL,
          horizon_minutes INTEGER, settled INTEGER);
        CREATE TABLE ai_decisions(
          id INTEGER, event_strength TEXT, sentiment_score REAL,
          evidence_confidence REAL);
        CREATE TABLE raw_news(id INTEGER, published_at TEXT, content TEXT);
    """)
    yield c
    c.close()


def _insert(c, **kw):
    base = dict(id=kw.get("id", 1), decision_id=1, news_id=1, asset="BTC",
                action="BUY", entry_price=100.0, exit_price=102.0, pnl_pct=2.0,
                pnl_usdt=2.0, verdict="WIN", entry_ts=1000, exit_ts=2000,
                max_price=103.0, min_price=99.0, mfe_pct=3.0, mae_pct=-1.0,
                horizon_minutes=60, settled=1)
    base.update(kw)
    keys = list(base)
    c.execute(
        "INSERT INTO quick_sim_trades(%s) VALUES(%s)"
        % (",".join(keys), ",".join("?" * len(keys))),
        tuple(base[k] for k in keys))
    c.commit()


def test_compute_baseline_counts_only_settled(conn):
    conn.execute("INSERT INTO ai_decisions VALUES(1,'A',0.6,0.8)")
    conn.execute("INSERT INTO raw_news VALUES(1,'2026-11-20 10:00:00','x')")
    _insert(conn, id=1, pnl_pct=2.0)
    _insert(conn, id=2, pnl_pct=-1.0, asset="ETH", action="SELL", settled=0)  # 未结算，不应计入
    report = ab.compute_baseline(conn)
    assert report["window"]["n_settled"] == 1
    assert report["overall"]["count"] == 1
    assert [b["asset"] for b in report["by_asset"]] == ["BTC"]
    assert report["generated_at"].endswith("Z")


def test_compute_baseline_empty_db_is_not_an_error(conn):
    report = ab.compute_baseline(conn)
    assert report["overall"] == {"count": 0}
    assert report["by_asset"] == []
    assert report["entry_delay_buckets"] == {"<60s": 0, "60-300s": 0, "300-900s": 0, "900s+": 0}


def test_compute_baseline_asset_filter(conn):
    conn.execute("INSERT INTO ai_decisions VALUES(1,'A',0.6,0.8)")
    conn.execute("INSERT INTO raw_news VALUES(1,'2026-11-20 10:00:00','x')")
    _insert(conn, id=1, asset="BTC")
    _insert(conn, id=2, asset="ETH")
    report = ab.compute_baseline(conn, asset="eth")
    assert report["overall"]["count"] == 1
    assert report["by_asset"][0]["asset"] == "ETH"


def test_fetch_trades_on_broken_schema_raises_runtime():
    c = sqlite3.connect(":memory:")
    try:
        with pytest.raises(RuntimeError):
            ab._fetch_trades(c, None, None, None)
    finally:
        c.close()
