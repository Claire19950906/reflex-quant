#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v515：漏检信号错因分类（missed_signal_taxonomy）的行为测试。

这里钉的是"为什么漏了"这条链路的口径，而不是覆盖率数字：
  ① 词表：每个 (维度, 小类) 必须落到一个真实的 pattern_type，不许悄悄塌到兜底；
  ② 缺数据就是缺数据：NULL 幅度 ≠ 0% 幅度，不许把"没取到"算成"市场没动"；
  ③ 判据优先级：数据缺失 > 新闻方向 > 校准漂移 > 因果链 > 时效 > 兜底；
  ④ 5 日涨跌必须真的跨过 5 天，且 base/fut 同源，不许跨表混算出 -300%；
  ⑤ 回填幂等：列不在就补，写库的 reason 截 300 字，分布统计与行数对得上。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import missed_signal_taxonomy as mst  # noqa: E402

NOW = 1_760_000_000  # 固定"现在"，避免用例随真实时间变红
D5 = 5 * 86400


_OPEN = []


@pytest.fixture(autouse=True)
def _close_conns():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _conn():
    conn = sqlite3.connect(":memory:")
    _OPEN.append(conn)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT,
                                           ts INTEGER, move_pct REAL);
        CREATE TABLE kline_bars (symbol TEXT, ts INTEGER, close REAL);
        CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE news_events (id INTEGER PRIMARY KEY, asset TEXT, action TEXT,
                                  score REAL, ts INTEGER, headline TEXT);
        CREATE TABLE causal_chains (id INTEGER PRIMARY KEY, asset TEXT, enabled INTEGER);
        CREATE TABLE missed_signals (id INTEGER PRIMARY KEY, asset TEXT, signal_id INTEGER,
                                      pattern_type TEXT, confidence_at_time REAL,
                                      actual_outcome_5d REAL, ignored_reason TEXT,
                                      signal TEXT, decision_date TEXT);
        """
    )
    return conn


def _bar(conn, symbol, ts, close):
    conn.execute("INSERT INTO kline_bars VALUES (?,?,?)", (symbol, ts * 1000, close))


def _daily(conn, asset, ts, close):
    conn.execute("INSERT INTO ohlcv_daily VALUES (?,?,?)", (asset, ts, close))


def _move(conn, mid, asset, ts, pct):
    conn.execute("INSERT INTO critical_price_moves VALUES (?,?,?,?)", (mid, asset, ts, pct))


def _news(conn, nid, asset, action, ts):
    conn.execute("INSERT INTO news_events VALUES (?,?,?,?,?,?)",
                 (nid, asset, action, 0.5, ts, "headline"))


# ── ① 词表与兜底 ────────────────────────────────────────────────
def test_every_subcategory_maps_to_a_real_pattern_type():
    assert set(mst.PATTERN_BY_SUBCATEGORY) == {
        ("B", "news_direction_inverted"), ("B", "event_chain_break"),
        ("C", "feed_missing"), ("C", "calibration_drift"), ("C", "signal_delay"),
        ("A", "market_regime_misread"), ("A", "pattern_recognition"),
    }
    for (dim, sub), pat in mst.PATTERN_BY_SUBCATEGORY.items():
        out = mst._out(dim, sub, "x")
        assert out["dimension"] == dim and out["subcategory"] == sub
        assert out["pattern_type"] == pat


def test_unknown_subcategory_falls_back_but_keeps_its_own_name():
    out = mst._out("A", "strength_gate_blocked", "强度闸门拦下")
    # 词表里没有这一格 → pattern_type 走兜底，但 dimension/subcategory 必须原样保留，
    # 否则前端会把它算进别的桶，统计就串了。
    assert out["pattern_type"] == mst.DEFAULT_PATTERN
    assert out["subcategory"] == "strength_gate_blocked"
    assert out["reason"] == "强度闸门拦下"


# ── ② 缺数据 ≠ 0% ───────────────────────────────────────────────
def test_sign_of_never_turns_missing_data_into_a_direction():
    assert mst._sign_of(None) == 0
    assert mst._sign_of("not-a-number") == 0
    assert mst._sign_of(0) == 0
    assert mst._sign_of(6.26) == 1
    assert mst._sign_of(-0.01) == -1


def test_ts_from_row_reads_move_row_and_keeps_null_pct_null():
    conn = _conn()
    _move(conn, 7, "BTC", NOW, None)
    ts, pct = mst._ts_from_row(conn, {"signal_id": 7})
    assert ts == NOW
    assert pct is None  # 关键：NULL 不变成 0.0


def test_ts_from_row_falls_back_to_text_pct_then_to_decision_date():
    conn = _conn()
    row = {"signal_id": 999, "signal": "move_pct=-6.26 move_kind=slow_fall",
           "decision_date": "2026-03-04"}
    ts, pct = mst._ts_from_row(conn, row)
    assert pct == pytest.approx(-6.26)
    # 当天 UTC 中点
    assert ts % 86400 == 43200


def test_ts_from_row_without_any_clue_returns_zero_and_none():
    conn = _conn()
    ts, pct = mst._ts_from_row(conn, {"signal": "no pct here", "decision_date": "bad"})
    assert (ts, pct) == (0, None)


# ── ③ 判据优先级 ───────────────────────────────────────────────
def test_feed_missing_wins_over_everything_else():
    conn = _conn()
    _move(conn, 1, "XAU", NOW, 2.0)
    _news(conn, 1, "XAU", "SELL", NOW)  # 方向相反也不该抢在数据缺失前面
    out = mst.classify_missed(conn, {"signal_id": 1, "asset": "XAU"}, now_ts=NOW)
    assert out["subcategory"] == "feed_missing"
    assert out["pattern_type"] == "C1_feed_missing_missed"


def test_opposite_news_beats_same_direction_news():
    conn = _conn()
    _move(conn, 1, "BTC", NOW, 2.0)
    _bar(conn, "BTC", NOW, 100)
    _news(conn, 1, "BTC", "BUY", NOW)
    _news(conn, 2, "BTC", "SELL", NOW)
    out = mst.classify_missed(conn, {"signal_id": 1, "asset": "BTC"}, now_ts=NOW)
    assert out["subcategory"] == "news_direction_inverted"


def test_same_direction_news_means_the_chain_broke():
    conn = _conn()
    _move(conn, 1, "BTC", NOW, -2.0)
    _bar(conn, "BTC", NOW, 100)
    _news(conn, 1, "BTC", "SELL", NOW)
    out = mst.classify_missed(conn, {"signal_id": 1, "asset": "BTC"}, now_ts=NOW)
    assert out["subcategory"] == "event_chain_break"


def test_news_branch_is_skipped_when_direction_is_unknown():
    """幅度缺失 → 没有方向可比，不许拿它去跟新闻对撞出 B 类。"""
    conn = _conn()
    _move(conn, 1, "BTC", NOW, None)
    _bar(conn, "BTC", NOW, 100)
    _news(conn, 1, "BTC", "BUY", NOW)
    _news(conn, 2, "BTC", "SELL", NOW)
    out = mst.classify_missed(conn, {"signal_id": 1, "asset": "BTC"}, now_ts=NOW)
    assert out["dimension"] != "B"


def test_calibration_drift_needs_high_confidence_and_a_flat_outcome():
    conn = _conn()
    _move(conn, 1, "BTC", NOW, 3.0)
    _bar(conn, "BTC", NOW, 100)
    _daily(conn, "BTC", NOW, 100)
    _daily(conn, "BTC", NOW + D5, 100.1)  # +0.1%
    hi = mst.classify_missed(
        conn, {"signal_id": 1, "asset": "BTC", "confidence_at_time": 0.8}, now_ts=NOW)
    assert hi["subcategory"] == "calibration_drift"
    lo = mst.classify_missed(
        conn, {"signal_id": 1, "asset": "BTC", "confidence_at_time": 0.6}, now_ts=NOW)
    assert lo["subcategory"] != "calibration_drift"


def test_chain_with_peer_co_move_is_a_sector_regime_miss():
    conn = _conn()
    _move(conn, 1, "BTC", NOW, 3.0)
    _move(conn, 2, "ETH", NOW + 600, 2.0)
    _move(conn, 3, "SOL", NOW - 300, 1.8)
    _bar(conn, "BTC", NOW, 100)
    conn.execute("INSERT INTO causal_chains VALUES (1,'BTC',1)")
    out = mst.classify_missed(conn, {"signal_id": 1, "asset": "BTC"}, now_ts=NOW)
    assert out["subcategory"] == "market_regime_misread"


def test_chain_without_peers_is_a_single_asset_pattern_miss():
    conn = _conn()
    _move(conn, 1, "BTC", NOW, 3.0)
    _move(conn, 2, "ETH", NOW, -2.5)  # 反向，不算联动
    _bar(conn, "BTC", NOW, 100)
    conn.execute("INSERT INTO causal_chains VALUES (1,'BTC',1)")
    out = mst.classify_missed(conn, {"signal_id": 1, "asset": "BTC"}, now_ts=NOW)
    assert out["subcategory"] == "pattern_recognition"


def test_peer_count_ignores_implausible_moves_and_unknown_assets():
    conn = _conn()
    for i, pct in enumerate([95.0, -95.0, 1.0], start=1):  # 前两条是脏量纲
        _move(conn, i, ["ETH", "SOL", "ADA"][i - 1], NOW, pct)
    assert mst._peer_co_move_count(conn, "BTC", NOW, 1) == 1
    assert mst._peer_co_move_count(conn, "UNKNOWN_XYZ", NOW, 1) == 0
    assert mst._peer_co_move_count(conn, "BTC", 0, 1) == 0


def test_signal_delay_only_when_old_and_no_chain():
    conn = _conn()
    old = NOW - 40 * 86400
    _move(conn, 1, "BTC", old, 3.0)
    _bar(conn, "BTC", old, 100)
    out = mst.classify_missed(conn, {"signal_id": 1, "asset": "BTC"}, now_ts=NOW)
    assert out["subcategory"] == "signal_delay"
    assert out["pattern_type"] == "C3_signal_delay_missed"
    assert "40" in out["reason"]


def test_deny_reason_is_consumed_before_the_chain_guess():
    conn = _conn()
    _move(conn, 1, "BTC", NOW, 3.0)
    _bar(conn, "BTC", NOW, 100)
    out = mst.classify_missed(
        conn,
        {"signal_id": 1, "asset": "BTC", "deny_reason": "high_strength_event"},
        now_ts=NOW,
    )
    assert out["subcategory"] == "strength_gate_blocked"
    assert out["dimension"] == "A"


def test_unmapped_deny_reason_does_not_invent_a_bucket():
    conn = _conn()
    _move(conn, 1, "BTC", NOW, 3.0)
    _bar(conn, "BTC", NOW, 100)
    out = mst.classify_missed(
        conn, {"signal_id": 1, "asset": "BTC", "deny_reason": "whatever"}, now_ts=NOW)
    assert out["subcategory"] in {"pattern_recognition", "signal_delay"}


def test_bare_signal_lands_in_the_honest_fallback():
    conn = _conn()
    out = mst.classify_missed(conn, {"asset": "NOPE", "pattern_type": ""}, now_ts=NOW)
    assert out["subcategory"] == "pattern_recognition"
    assert "纯形态" in out["reason"]


# ── ④ 5 日涨跌：同源 + 真跨期 ──────────────────────────────────
def test_adjacent_bars_are_rejected_for_a_5d_horizon():
    conn = _conn()
    _daily(conn, "BTC", NOW, 100)
    _daily(conn, "BTC", NOW + 3600, 101)  # 只隔 1 小时
    assert mst.compute_outcome(conn, "BTC", NOW, 5) is None


def test_five_day_move_uses_same_source_for_both_ends():
    conn = _conn()
    _daily(conn, "BTC", NOW, 100)
    _daily(conn, "BTC", NOW + D5, 105)
    # 分钟线给一组完全不同的价 —— 若跨表混算会得到别的数
    _bar(conn, "BTC", NOW, 10)
    _bar(conn, "BTC", NOW + D5, 100)
    assert mst.compute_outcome(conn, "BTC", NOW, 5) == pytest.approx(5.0)


def test_kline_fallback_is_in_milliseconds_and_consistent():
    conn = _conn()
    _bar(conn, "BTC", NOW, 100)
    _bar(conn, "BTC", NOW + D5, 98)
    assert mst.compute_outcome(conn, "BTC", NOW, 5) == pytest.approx(-2.0)


def test_bad_base_price_returns_none_instead_of_infinity():
    conn = _conn()
    _daily(conn, "BTC", NOW, 0)
    _daily(conn, "BTC", NOW + D5, 105)
    assert mst.compute_outcome(conn, "BTC", NOW, 5) is None
    assert mst.compute_outcome(conn, "", NOW, 5) is None
    assert mst.compute_outcome(conn, "BTC", 0, 5) is None


# ── ⑤ 回填 ────────────────────────────────────────────────────
def test_backfill_adds_columns_then_writes_classification():
    conn = _conn()
    conn.execute(
        "INSERT INTO missed_signals (id, asset, signal_id, pattern_type, "
        "confidence_at_time, actual_outcome_5d, ignored_reason) "
        "VALUES (1,'BTC',1,'A1_strong_buy_missed',0.9,0.05,'')"
    )
    _move(conn, 1, "BTC", NOW, 0.2)
    _bar(conn, "BTC", NOW, 100)
    res = mst.backfill(conn)
    assert res["rows"] == 1 and res["updated"] == 1
    row = conn.execute(
        "SELECT dimension, subcategory, pattern_type, miss_reason FROM missed_signals"
    ).fetchone()
    assert row["dimension"] == "C" and row["subcategory"] == "calibration_drift"
    assert len(row["miss_reason"]) <= 300
    assert sum(res["pattern_types"].values()) == 1


def test_backfill_is_idempotent_on_long_reasons():
    conn = _conn()
    conn.execute(
        "INSERT INTO missed_signals (id, asset, signal_id, pattern_type) "
        "VALUES (1,'BTC',NULL,'')"
    )
    mst.backfill(conn)
    first = conn.execute("SELECT miss_reason FROM missed_signals").fetchone()[0]
    mst.backfill(conn)
    second = conn.execute("SELECT miss_reason FROM missed_signals").fetchone()[0]
    assert first == second


def test_ensure_columns_leaves_existing_columns_alone():
    conn = _conn()
    conn.execute("ALTER TABLE missed_signals ADD COLUMN dimension TEXT")
    mst.ensure_columns(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(missed_signals)")]
    assert cols.count("dimension") == 1
    assert "subcategory" in cols and "miss_reason" in cols
