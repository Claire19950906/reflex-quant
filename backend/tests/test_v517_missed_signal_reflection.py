#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v517：漏检决策反思（missed_signal_reflection）的行为测试。

漏检反思是"系统没做的单，事后怎么记账"的地方，最容易被写成自我表扬。这里钉死：
  ① 只有涨跌真的达到 1% 才配说"本应触发交易"，否则只能记"仅留痕"；
  ② 置信度 0.0 不是"很低"，是"没算过" → 落库必须 NULL；
  ③ 涨跌取不到就留空，不许用 0 顶替；5 日窗口必须真的跨过 5 天；
  ④ 一条决策只写一条（幂等），降级决策不产出反思；
  ⑤ 同一套 (维度, 小类) 词表与漏检分类器对齐，判据优先级不许乱。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import missed_signal_reflection as msr  # noqa: E402

T = 1_760_000_000
D5 = 5 * 86400


def _schema(conn):
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, target_asset TEXT,
            suggested_action TEXT, sentiment_score REAL, impact_score REAL,
            forward_move_pct REAL, ts INTEGER, created_at TEXT,
            paper_trading_run_id INTEGER, event_strength REAL, no_trade_reason TEXT,
            missed_signal INTEGER);
        CREATE TABLE kline_bars (symbol TEXT, ts INTEGER, close REAL);
        CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE news_events (id INTEGER PRIMARY KEY, asset TEXT, action TEXT,
                                  score REAL, ts INTEGER, headline TEXT);
        CREATE TABLE causal_chains (id INTEGER PRIMARY KEY, asset TEXT, enabled INTEGER);
        CREATE TABLE critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER,
                                           move_pct REAL, move_kind TEXT,
                                           duration_min INTEGER, algo_score REAL);
        """
    )
    conn.commit()


def _db_file(tmp_path):
    path = tmp_path / "v517.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _schema(conn)
    return conn, str(path)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    conn, path = _db_file(tmp_path)
    monkeypatch.setattr(msr, "_conn", lambda: sqlite3.connect(path, timeout=60))
    yield conn
    conn.close()


def _dec(conn, did, **kw):
    row = {
        "id": did, "target_asset": "BTC", "suggested_action": "HOLD",
        "sentiment_score": None, "impact_score": 0.5, "forward_move_pct": None,
        "ts": T, "created_at": "", "paper_trading_run_id": 1,
        "event_strength": 0.4, "no_trade_reason": "", "missed_signal": 1,
    }
    row.update(kw)
    conn.execute(
        "INSERT INTO ai_decisions (id, target_asset, suggested_action, sentiment_score, "
        "impact_score, forward_move_pct, ts, created_at, paper_trading_run_id, "
        "event_strength, no_trade_reason, missed_signal) VALUES "
        "(:id,:target_asset,:suggested_action,:sentiment_score,:impact_score,"
        ":forward_move_pct,:ts,:created_at,:paper_trading_run_id,:event_strength,"
        ":no_trade_reason,:missed_signal)", row)
    conn.commit()
    return dict(row)


# ── 基础口径 ───────────────────────────────────────────────────
def test_sign_of_and_action_norm_never_invent_a_direction():
    assert msr._sign_of(None) == 0 and msr._sign_of("x") == 0 and msr._sign_of(0) == 0
    assert msr._sign_of(-1.2) == -1 and msr._sign_of(1.2) == 1
    assert msr._norm_action("long") == "BUY" and msr._norm_action("bearish") == "SELL"
    assert msr._norm_action("HOLD") == ""


def test_decision_ts_prefers_ts_then_parses_created_at():
    assert msr._decision_ts({"ts": T}) == T
    assert msr._decision_ts({"ts": 0, "created_at": "2026-03-04 09:30:00"}) > 0
    assert msr._decision_ts({"ts": 0, "created_at": "garbage"}) == 0


def test_price_bar_presence_is_three_valued():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    assert msr._has_price_bar(conn, "", T) is None
    assert msr._has_price_bar(conn, "BTC", 0) is None
    assert msr._has_price_bar(conn, "BTC", T) is False
    conn.execute("INSERT INTO kline_bars VALUES ('BTC', ?, 100)", (T * 1000,))
    assert msr._has_price_bar(conn, "BTC", T) is True
    conn.execute("DELETE FROM kline_bars")
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T + 2 * 86400,))
    assert msr._has_price_bar(conn, "BTC", T) is True


def test_forward_move_requires_a_real_five_day_span():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    assert msr._forward_move(conn, "", T) is None
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T,))
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 101)", (T + 3600,))
    assert msr._forward_move(conn, "BTC", T) is None  # 只隔 1 小时 → 不许当成 5 日结果
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 105)", (T + D5,))
    assert msr._forward_move(conn, "BTC", T) == pytest.approx(5.0)


def test_forward_move_with_zero_base_is_none_not_infinity():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 0)", (T,))
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 105)", (T + D5,))
    assert msr._forward_move(conn, "BTC", T) is None


# ── 分类：优先级 ─────────────────────────────────────────────
def test_feed_missing_beats_delay_and_news():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    dec = _dec(conn, 1, ts=T - 40 * 86400, forward_move_pct=2.0)
    conn.execute("INSERT INTO news_events VALUES (1,'BTC','SELL',0.5,?, 'h')",
                 (T - 40 * 86400,))
    out = msr.classify_missed_decision(conn, dec, now_ts=T)
    assert out["subcategory"] == "feed_missing"


def test_old_decision_beyond_window_is_signal_delay():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T - 40 * 86400,))
    dec = _dec(conn, 1, ts=T - 40 * 86400)
    out = msr.classify_missed_decision(conn, dec, now_ts=T)
    assert out["subcategory"] == "signal_delay"
    assert "40 天前" in out["reason"]


def test_opposite_news_wins_over_same_direction():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T,))
    conn.execute("INSERT INTO news_events VALUES (1,'BTC','BUY',0.5,?, 'h')", (T,))
    conn.execute("INSERT INTO news_events VALUES (2,'BTC','SELL',0.5,?, 'h')", (T,))
    out = msr.classify_missed_decision(
        conn, _dec(conn, 1, forward_move_pct=2.0), now_ts=T)
    assert out["subcategory"] == "news_direction_inverted"


def test_same_direction_news_is_an_event_chain_break():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T,))
    conn.execute("INSERT INTO news_events VALUES (1,'BTC','SELL',0.5,?, 'h')", (T,))
    out = msr.classify_missed_decision(
        conn, _dec(conn, 1, forward_move_pct=-2.0), now_ts=T)
    assert out["subcategory"] == "event_chain_break"


def test_unknown_direction_skips_the_news_branch():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T,))
    conn.execute("INSERT INTO news_events VALUES (1,'BTC','BUY',0.5,?, 'h')", (T,))
    conn.execute("INSERT INTO news_events VALUES (2,'BTC','SELL',0.5,?, 'h')", (T,))
    out = msr.classify_missed_decision(conn, _dec(conn, 1), now_ts=T)
    assert out["dimension"] != "B"


def test_high_confidence_with_flat_outcome_is_calibration_drift():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T,))
    out = msr.classify_missed_decision(
        conn, _dec(conn, 1, sentiment_score=0.85, forward_move_pct=0.1), now_ts=T)
    assert out["subcategory"] == "calibration_drift"
    out2 = msr.classify_missed_decision(
        conn, _dec(conn, 2, sentiment_score=0.5, forward_move_pct=0.1), now_ts=T)
    assert out2["subcategory"] != "calibration_drift"


def test_no_trade_reason_mapping_beats_the_chain_guess():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T,))
    conn.execute("INSERT INTO causal_chains VALUES (1,'BTC',1)")
    out = msr.classify_missed_decision(
        conn, _dec(conn, 1, forward_move_pct=3.0, no_trade_reason="asset_limit_full"),
        now_ts=T)
    assert out["subcategory"] in {"risk_limit_blocked", "market_regime_misread"}
    assert out["dimension"] == "A"


def test_peer_co_move_splits_sector_from_single_asset():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 100)", (T,))
    conn.execute("INSERT INTO causal_chains VALUES (1,'BTC',1)")
    conn.execute("INSERT INTO critical_price_moves VALUES (1,'ETH',?,2.0,'f',5,0.5)",
                 (T,))
    conn.execute("INSERT INTO critical_price_moves VALUES (2,'SOL',?,1.8,'f',5,0.5)",
                 (T,))
    two = msr.classify_missed_decision(
        conn, _dec(conn, 1, forward_move_pct=3.0), now_ts=T)
    assert two["subcategory"] == "market_regime_misread"
    conn.execute("DELETE FROM critical_price_moves WHERE id=2")
    one = msr.classify_missed_decision(
        conn, _dec(conn, 2, forward_move_pct=3.0), now_ts=T)
    assert one["subcategory"] == "pattern_recognition"


def test_peer_count_ignores_inverse_and_dirty_moves():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    for i, (asset, pct) in enumerate([("ETH", -2.0), ("SOL", 95.0), ("ADA", 2.0)], 1):
        conn.execute(
            "INSERT INTO critical_price_moves VALUES (?,?,?,?,'f',5,0.5)",
            (i, asset, T, pct))
    # 反向被排除；95% 那条脏值**仍会被算进来**（本模块的同类读法没有量纲上界，
    # 已在 v514 的 KNOWN_UNBOUNDED 里登记为研究/展示口径缺口）
    assert msr._peer_co_move_count(conn, "BTC", T, 1) == 2
    assert msr._peer_co_move_count(conn, "BTC", T, -1) == 1
    assert msr._peer_co_move_count(conn, "NOPE", T, 1) == 0


def test_bare_decision_falls_back_to_pattern_recognition():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    out = msr.classify_missed_decision(conn, {"target_asset": "NOPE", "ts": 0}, now_ts=T)
    assert out["subcategory"] == "pattern_recognition"


# ── 落库：诚实与幂等 ─────────────────────────────────────────
def test_run_writes_only_what_it_can_prove(db):
    _dec(db, 1, forward_move_pct=2.5, sentiment_score=0.0)
    res = msr.run(db, days=30, now_ts=T)
    assert res["candidates"] == 1 and res["written"] == 1
    row = db.execute(
        "SELECT hit, move_pct, confidence_at_time, lesson, evidence, action "
        "FROM missed_signal_reflections WHERE decision_id=1").fetchone()
    assert row["hit"] == 1 and row["move_pct"] == pytest.approx(2.5)
    assert row["confidence_at_time"] is None  # 0.0 不是"很低"，是"没算过"
    assert "本应触发交易" in row["lesson"]
    assert row["action"] == "HOLD"
    evidence = __import__("json").loads(row["evidence"])
    assert evidence["attribution"] == "rule_bucket"
    assert evidence["window_days"] == 5


def test_small_move_is_not_called_a_missed_trade(db):
    _dec(db, 1, forward_move_pct=0.2)
    msr.run(db, days=30, now_ts=T)
    lesson = db.execute(
        "SELECT lesson FROM missed_signal_reflections WHERE decision_id=1").fetchone()[0]
    assert "不构成漏检" in lesson and "本应触发交易" not in lesson


def test_missing_outcome_stays_null_not_zero(db):
    _dec(db, 1, forward_move_pct=None)  # 库里也没有行情 → 取不到
    msr.run(db, days=30, now_ts=T)
    row = db.execute(
        "SELECT move_pct, hit FROM missed_signal_reflections WHERE decision_id=1"
    ).fetchone()
    assert row["move_pct"] is None and row["hit"] == 0


def test_rerun_upserts_instead_of_duplicating(db):
    _dec(db, 1, forward_move_pct=3.0)
    msr.run(db, days=30, now_ts=T)
    _dec(db, 1, forward_move_pct=0.1) if False else None
    db.execute("UPDATE ai_decisions SET forward_move_pct=0.1 WHERE id=1")
    db.commit()
    msr.run(db, days=30, now_ts=T)
    rows = db.execute(
        "SELECT COUNT(*), MAX(hit) FROM missed_signal_reflections").fetchall()
    assert rows[0][0] == 1
    assert rows[0][1] == 0  # 后一次的结果覆盖前一次


def test_decision_without_id_is_skipped(db):
    dec = _dec(db, 1)
    dec["id"] = 0
    assert msr._upsert_one(db, dec, 1, T) is None


def test_degraded_decisions_produce_no_reflection(db):
    db.execute("ALTER TABLE ai_decisions ADD COLUMN analysis_degraded INTEGER DEFAULT 0")
    _dec(db, 1, forward_move_pct=3.0)
    db.execute("UPDATE ai_decisions SET analysis_degraded=1 WHERE id=1")
    db.commit()
    res = msr.run(db, days=30, now_ts=T)
    assert res["written"] == 0 and res["skipped_degraded"] == 1


def test_candidates_only_include_missed_and_recent(db):
    _dec(db, 1, ts=T, missed_signal=1)
    _dec(db, 2, ts=T, missed_signal=0)
    _dec(db, 3, ts=T - 10 * 86400, missed_signal=1)
    res = msr.run(db, days=1, now_ts=T)
    assert res["candidates"] == 1
    assert res["since"] == T - 86400
    assert sum(res["dist"].values()) == res["written"]


def test_run_survives_a_broken_candidate_table(db):
    db.execute("ALTER TABLE ai_decisions RENAME TO ai_decisions_old")
    db.commit()
    res = msr.run(db, days=1, now_ts=T)
    assert res["candidates"] == 0 and res["written"] == 0
