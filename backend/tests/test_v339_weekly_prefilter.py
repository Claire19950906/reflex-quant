# -*- coding: utf-8 -*-
"""v339: weekly promotion 插入前预筛。

`weekly_insight_apply.apply_once` 过去是纯搬运：从 review_insights 的 rule 文本里
抽出 param_key 就 INSERT 一条 pending，不管这条建议在当前参数下能不能落地。
于是 `sizing.*`（与 settings 计量单位不等价）、`sell_min_score` 已在下限的"再降"、
`signal_threshold` 基线 0 的"再升"被原样灌进待审队列，攒成"点批准必失败"的假待办。
清退也没用 —— 下一周它又插一批。这里锁住"挡在插入之前"。
"""
import sqlite3

import pytest


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE review_insights (
        id INTEGER PRIMARY KEY, rule TEXT, asset TEXT, source_samples INTEGER,
        origin TEXT, kind INTEGER, active INTEGER, manual_deactivated INTEGER)""")
    conn.execute("""CREATE TABLE strategy_param_adjustments (
        id INTEGER PRIMARY KEY, asset TEXT, action TEXT, param_key TEXT,
        direction TEXT, magnitude REAL, sample_size INTEGER, avg_confidence REAL,
        evidence_summary TEXT, first_suggested_at TEXT, last_suggested_at TEXT,
        status TEXT, applied_strategy_version_id INTEGER,
        applied_setting_change TEXT, note TEXT, created_at TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE paper_trading_settings (
        id INTEGER PRIMARY KEY, buy_min_score REAL, sell_min_score REAL,
        tp1_mult REAL, tp2_mult REAL, tp1_ratio REAL, tp2_ratio REAL,
        gate_enabled INTEGER, tp_enabled INTEGER, market_structure_enabled INTEGER,
        news_trade_max_age_seconds INTEGER, started_at TEXT, active_run_id INTEGER,
        is_running INTEGER, tracks TEXT, updated_at TEXT)""")
    conn.execute("""INSERT INTO paper_trading_settings
        (id, buy_min_score, sell_min_score, tp1_mult, tp2_mult, tp1_ratio, tp2_ratio,
         gate_enabled, tp_enabled, market_structure_enabled, news_trade_max_age_seconds,
         is_running, tracks, updated_at)
        VALUES (1, 0.15, 0.1, 0.6, 1.2, 0.5, 0.5, 1, 1, 1, 2700, 1, '[]', datetime('now'))""")
    return conn


def _insight(conn, iid: int, rule: str):
    conn.execute(
        "INSERT INTO review_insights (id, rule, asset, source_samples, origin, kind,"
        " active, manual_deactivated) VALUES (?,?,?,?,?,?,?,?)",
        (iid, rule, "*", 30, "real", 1, 1, 0))


class TestWeeklyPrefilter:
    def test_sizing_advice_is_blocked_before_insert(self):
        from weekly_insight_apply import apply_once
        conn = _mk_db()
        _insight(conn, 1, "sizing.max_position_pct 应该下调")
        out = apply_once(conn, dry_run=True)
        assert out["inserted"] == [], "不可映射的 sizing.* 不该进 pending 队列"
        assert out["skipped_unlandable"], "被挡的建议要留痕，不能静默"

    def test_sell_min_score_at_floor_is_blocked(self):
        """sell_min_score 已在 0.1 下限，再"降 10%"会被钳回 0.1 —— 不该进队列。"""
        from weekly_insight_apply import apply_once
        conn = _mk_db()
        _insight(conn, 2, "[score.sell_min_score] 空头信号分门槛应下调")
        out = apply_once(conn, dry_run=True)
        assert all("sell_min_score" not in str(it[1]) for it in out["inserted"])

    def test_prefilter_keeps_reason_text(self):
        from weekly_insight_apply import apply_once
        conn = _mk_db()
        _insight(conn, 3, "sizing.stop_loss_pct 应该下调")
        out = apply_once(conn, dry_run=True)
        reasons = [str(it[2]) for it in out["skipped_unlandable"]]
        assert reasons, "必须记录被挡原因"
        assert any("back" not in r for r in reasons)
