# -*- coding: utf-8 -*-
"""v230 反思系统深度增强 — 测试

覆盖:
  - reflection_deep.py 6 个核心函数(A-F)
  - meta_reflection_loop.py 自循环推断 + level 2 升级
  - enrich_reflection 端到端
  - schema 迁移幂等性
"""
import os
import sys
import json
import sqlite3
import datetime
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import db
import reflection_deep as rd
import meta_reflection_loop as mr


@pytest.fixture(autouse=True)
def _setup_kline_data():
    asset = "BTC"
    ts_ms = 1700000000000
    conn = db.get_connection()
    db.migrate(conn)
    for i in range(20):
        bar_ts = ts_ms - i * 3600 * 1000
        conn.execute("INSERT OR REPLACE INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
                     (asset, "1h", bar_ts, 95000 + i*10, 95100 + i*10, 94900 + i*10, 95050 + i*10, 100.0))
    for i in range(20):
        bar_ts = ts_ms - i * 3600 * 1000
        conn.execute("INSERT OR REPLACE INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
                     ("ETH", "1h", bar_ts, 3500 + i, 3510 + i, 3495 + i, 3505 + i, 50.0))
    for i in range(10):
        bar_ts = ts_ms - i * 86400 * 1000
        conn.execute("INSERT OR REPLACE INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
                     (asset, "1d", bar_ts, 93000 + i*200, 93200 + i*200, 92800 + i*200, 93100 + i*200, 1000.0))
    news_ts_str = datetime.datetime.fromtimestamp((ts_ms - 60*60*1000)/1000, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn.execute("INSERT OR REPLACE INTO raw_news (id, source, content, content_fingerprint, first_seen_ts) VALUES (?, ?, ?, ?, ?)",
                     (-990001, "test", "test", "fp1", news_ts_str))
    except sqlite3.IntegrityError:
        pass
    conn.commit()
    conn.close()
    yield
    conn = db.get_connection()
    for tbl in ["trade_reflections_v2", "ai_decisions", "raw_news",
                "kline_at_decision_cache", "reflection_cycles"]:
        try:
            conn.execute(f"DELETE FROM {tbl} WHERE id < 0 OR decision_id < 0 OR parent_reflection_id < 0")
        except Exception:
            pass
    conn.execute("DELETE FROM kline_bars WHERE symbol IN ('BTC', 'ETH') AND ts BETWEEN ? AND ?",
                 (ts_ms - 7 * 86400 * 1000, ts_ms + 86400 * 1000))
    conn.commit()
    conn.close()


def test_schema_migration_idempotent():
    """迁移幂等性: 多次调用不报错 + 列/表都在."""
    conn = db.get_connection()
    db.migrate(conn)
    db.migrate(conn)
    db.migrate(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_reflections_v2)").fetchall()]
    for c in ("kline_at_decision", "kline_pattern", "cross_asset_sync_score",
              "front_run_score", "luck_vs_signal_score", "deep_root_cause", "deep_lesson",
              "secondary_reflection_id", "enriched_at", "enrich_status"):
        assert c in cols, f"missing column {c}"
    for t in ("reflection_cycles", "kline_at_decision_cache"):
        row = conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{t}'").fetchone()
        assert row, f"missing table {t}"
    conn.close()


def test_compute_kline_context_full():
    conn = db.get_connection()
    ctx = rd.compute_kline_context(conn, "BTC", 1700000000000)
    assert ctx["ohlc_1h"]["c"] is not None
    assert ctx["ohlc_1d"]["c"] is not None
    assert ctx["atr_pct"] > 0
    assert 0 <= ctx["pos_pct_7d"] <= 1
    conn.close()


def test_classify_kline_pattern_labels():
    valid_labels = {"breakout_up", "breakout_down", "inside_bar", "engulfing_bull",
                    "engulfing_bear", "doji", "marubozu_bull", "marubozu_bear",
                    "double_top", "double_bottom", "hammer", "shooting_star",
                    "range_bound", "unknown"}
    ctx = rd.compute_kline_context(db.get_connection(), "BTC", 1700000000000)
    pat, det = rd.classify_kline_pattern(ctx)
    assert pat in valid_labels
    assert isinstance(det, str)


def test_compute_cross_asset_sync_bounded():
    conn = db.get_connection()
    sync, det = rd.compute_cross_asset_sync(conn, "BTC", 1700000000000)
    assert -1.0 <= sync <= 1.0
    assert isinstance(det, dict)
    conn.close()


def test_compute_front_run_with_news():
    conn = db.get_connection()
    score, det = rd.compute_front_run_score(conn, "BTC", 1700000000000, -990001)
    assert 0 <= score <= 1.0
    assert "delta_min" in det
    conn.close()


def test_compute_luck_vs_signal_loss_extreme():
    row = {"pnl_pct": -0.012, "mfe_pct": 0.005, "mae_pct": -0.015,
           "event_strength": "medium", "market_confirmation": "no"}
    score, det = rd.compute_luck_vs_signal(row, {}, 0.0, 0.0)
    assert score < 0
    assert "LOSS" in det


def test_compute_luck_vs_signal_win_skill():
    row = {"pnl_pct": 0.020, "mfe_pct": 0.025, "mae_pct": -0.003,
           "event_strength": "high", "market_confirmation": "yes"}
    score, _ = rd.compute_luck_vs_signal(row, {}, 0.8, 0.1)
    assert score > 0


def test_compute_deep_root_cause_format():
    row = {"pnl_pct": -0.012, "mfe_pct": 0.005, "mae_pct": -0.015,
           "event_strength": "medium", "market_confirmation": "no",
           "asset": "BTC", "action": "BUY"}
    ctx = rd.compute_kline_context(db.get_connection(), "BTC", 1700000000000)
    pat, _ = rd.classify_kline_pattern(ctx)
    root, lesson = rd.compute_deep_root_cause(row, ctx, pat, 0.5, 0.0, -0.3)
    assert root.startswith("FACT:")
    assert "CAUSE:" in root
    assert lesson.startswith("LESSON:")


def test_enrich_reflection_end_to_end():
    conn = db.get_connection()
    conn.execute("INSERT OR REPLACE INTO ai_decisions (id, news_id, sentiment_score, suggested_action, reasoning, target_asset, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (-990002, -990001, 0.5, "BUY", "test", "BTC", 1700000000000))
    conn.execute("""INSERT OR REPLACE INTO trade_reflections_v2
        (id, decision_id, asset, action, pnl_pct, mfe_pct, mae_pct,
         layer, sub_layer, signal_name, ontology_signal,
         root_cause, lesson, enrich_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (-990002, -990002, "BTC", "BUY", -0.012, 0.005, -0.015,
         "event_logic", "causal_chain", "test_v230_sig",
         "event_logic/causal_chain/test_v230_sig",
         "test root", "test lesson", "pending"))
    conn.commit()
    conn.close()

    out = rd.enrich_reflection(-990002, write=True)
    assert out["ok"] is True
    assert out["enrich_status"] == "ok"
    for f in ("kline_at_decision", "kline_pattern", "cross_asset_sync_score",
              "front_run_score", "luck_vs_signal_score", "market_regime_detail",
              "deep_root_cause", "deep_lesson"):
        assert f in out, f"missing field {f}"

    conn = db.get_connection()
    row = conn.execute("SELECT enrich_status, kline_pattern, luck_vs_signal_score FROM trade_reflections_v2 WHERE id = -990002").fetchone()
    assert row[0] == "ok"
    conn.close()


def test_meta_loop_classifies_invalidated():
    conn = db.get_connection()
    conn.execute("DELETE FROM trade_reflections_v2 WHERE signal_name = 'test_meta_invalidated'")
    conn.execute("DELETE FROM reflection_cycles WHERE parent_signal = 'test_meta_invalidated'")
    today = datetime.datetime.utcnow()
    for i in range(5):
        ts = (today - datetime.timedelta(days=2)).isoformat() + "Z"
        conn.execute("""INSERT INTO trade_reflections_v2
            (decision_id, asset, action, pnl_pct, layer, sub_layer, signal_name,
             ontology_signal, luck_vs_signal_score, enrich_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (-900000 - i, "BTC", "BUY", -0.02, "event_logic", "causal_chain",
             "test_meta_invalidated", "event_logic/causal_chain/test_meta_invalidated",
             0.0, "ok", ts))
    for i in range(5):
        ts = (today - datetime.timedelta(days=15)).isoformat() + "Z"
        conn.execute("""INSERT INTO trade_reflections_v2
            (decision_id, asset, action, pnl_pct, layer, sub_layer, signal_name,
             ontology_signal, luck_vs_signal_score, enrich_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (-800000 - i, "BTC", "BUY", 0.02, "event_logic", "causal_chain",
             "test_meta_invalidated", "event_logic/causal_chain/test_meta_invalidated",
             0.0, "ok", ts))
    conn.commit()
    conn.close()

    out = mr.run_meta_loop(window_days=7, compare_days=14, max_level=2)
    assert out["written_cycles"] >= 1
    assert "invalidated" in out["by_finding_kind"]
    assert "invalidated_confirmed" in out["by_finding_kind"]


def test_meta_loop_classifies_stable_skill():
    conn = db.get_connection()
    conn.execute("DELETE FROM trade_reflections_v2 WHERE signal_name = 'test_meta_skill'")
    conn.execute("DELETE FROM reflection_cycles WHERE parent_signal = 'test_meta_skill'")
    today = datetime.datetime.utcnow()
    for i in range(25):
        ts = (today - datetime.timedelta(days=2)).isoformat() + "Z"
        conn.execute("""INSERT INTO trade_reflections_v2
            (decision_id, asset, action, pnl_pct, layer, sub_layer, signal_name,
             ontology_signal, luck_vs_signal_score, enrich_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (-700000 - i, "BTC", "BUY", 0.02, "event_logic", "causal_chain",
             "test_meta_skill", "event_logic/causal_chain/test_meta_skill",
             0.6, "ok", ts))
    for i in range(20):
        pnl = 0.02 if i < 14 else -0.01
        ts = (today - datetime.timedelta(days=15)).isoformat() + "Z"
        conn.execute("""INSERT INTO trade_reflections_v2
            (decision_id, asset, action, pnl_pct, layer, sub_layer, signal_name,
             ontology_signal, luck_vs_signal_score, enrich_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (-600000 - i, "BTC", "BUY", pnl, "event_logic", "causal_chain",
             "test_meta_skill", "event_logic/causal_chain/test_meta_skill",
             0.5, "ok", ts))
    conn.commit()
    conn.close()
    out = mr.run_meta_loop(window_days=7, compare_days=14, max_level=2)
    assert "stable_skill" in out["by_finding_kind"]


def test_meta_loop_classifies_unstable_luck():
    conn = db.get_connection()
    conn.execute("DELETE FROM trade_reflections_v2 WHERE signal_name = 'test_meta_luck'")
    conn.execute("DELETE FROM reflection_cycles WHERE parent_signal = 'test_meta_luck'")
    today = datetime.datetime.utcnow()
    # 当前窗口: 25 个 WIN + luck=-0.6 (高 WR + 极负 luck)
    for i in range(25):
        ts = (today - datetime.timedelta(days=2)).isoformat() + "Z"
        conn.execute("""INSERT INTO trade_reflections_v2
            (decision_id, asset, action, pnl_pct, layer, sub_layer, signal_name,
             ontology_signal, luck_vs_signal_score, enrich_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (-500000 - i, "BTC", "BUY", 0.02, "event_logic", "causal_chain",
             "test_meta_luck", "event_logic/causal_chain/test_meta_luck",
             -0.6, "ok", ts))
    # 历史 baseline: WR=0.5 (避免被分类为 invalidated/refined)
    for i in range(20):
        pnl = 0.02 if i < 10 else -0.02
        ts = (today - datetime.timedelta(days=15)).isoformat() + "Z"
        conn.execute("""INSERT INTO trade_reflections_v2
            (decision_id, asset, action, pnl_pct, layer, sub_layer, signal_name,
             ontology_signal, luck_vs_signal_score, enrich_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (-490000 - i, "BTC", "BUY", pnl, "event_logic", "causal_chain",
             "test_meta_luck", "event_logic/causal_chain/test_meta_luck",
             -0.4, "ok", ts))
    conn.commit()
    conn.close()
    out = mr.run_meta_loop(window_days=7, compare_days=14, max_level=2)
    # 该信号应被分类为 unstable_luck
    assert "unstable_luck" in out["by_finding_kind"], f"expected unstable_luck in {out}"
