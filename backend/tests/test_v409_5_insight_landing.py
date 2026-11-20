# -*- coding: utf-8 -*-
"""v409.5: 洞察落地三条 —— 生成侧量化要求 / 验证失败出池 / 真实权益与仓位上限。

这里只测「纯逻辑」的部分（提示词与出池脚本是文本/脚本，靠干跑验证）：
  1. `sizing.max_position_pct` 从 unmappable 转正 → settings 桶；
  2. `market_context` 的权益优先取 `sim_equity_snapshots` 实时值，没有再退回配置初值；
  3. `_apply_param_change` 对 max_position_pct 的乘法与钳制。
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import insight_hypothesis as ih
import param_key_aliases as pka
import paper_trading as pt


def test_max_position_pct_is_mappable_to_settings():
    canon, bucket, why = pka.resolve_key("sizing.max_position_pct")
    assert canon == "max_position_pct"
    assert bucket == "settings"
    assert "settings" in why
    assert pka.is_mappable("sizing.max_position_pct") is True


def test_market_context_prefers_live_equity_snapshot():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sim_equity_snapshots(ts INTEGER, equity REAL,"
                 " realized_pnl REAL, n_settled INTEGER, n_open INTEGER,"
                 " peak_equity REAL, drawdown_pct REAL, note TEXT)")
    conn.execute("INSERT INTO sim_equity_snapshots (ts, equity) VALUES (1, 8000.0)")
    conn.execute("INSERT INTO sim_equity_snapshots (ts, equity) VALUES (2, 9997.57)")
    conn.execute("CREATE TABLE kline_snapshots(symbol TEXT, timeframe TEXT,"
                 " atr_pct REAL, ts INTEGER)")
    ctx = ih.market_context(conn, "", "")
    assert abs(ctx["equity_usdt"] - 9997.57) < 1e-6, "应取最新快照而不是初始值"


def test_market_context_falls_back_to_config_when_no_snapshot():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sim_equity_snapshots(ts INTEGER, equity REAL,"
                 " realized_pnl REAL, n_settled INTEGER, n_open INTEGER,"
                 " peak_equity REAL, drawdown_pct REAL, note TEXT)")
    conn.execute("CREATE TABLE kline_snapshots(symbol TEXT, timeframe TEXT,"
                 " atr_pct REAL, ts INTEGER)")
    ctx = ih.market_context(conn, "", "")
    assert ctx.get("equity_usdt", 0) > 0, "没有快照时必须退回配置初值，不能没有权益"


def test_apply_param_change_position_cap_is_multiplicative_and_clamped():
    # 无当前值时从 10% 起步，up 10% → 11%
    v = pt._apply_param_change("max_position_pct", "up", 0.10, None)
    assert abs(v - 0.11) < 1e-9
    # 上限钳在 1.0（不允许超过 100% 权益）
    v2 = pt._apply_param_change("max_position_pct", "up", 0.5, 0.9)
    assert v2 <= 1.0
    # 下限钳在 0.01
    v3 = pt._apply_param_change("max_position_pct", "down", 0.5, 0.02)
    assert v3 >= 0.01


def test_position_cap_is_in_settings_whitelist():
    assert "max_position_pct" in pt._V182_SETTINGS_KEYS


def _cap_db(pct, equity):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE paper_trading_settings(id INTEGER PRIMARY KEY, max_position_pct REAL)")
    c.execute("CREATE TABLE sim_equity_snapshots(ts INTEGER, equity REAL)")
    c.execute("INSERT INTO paper_trading_settings VALUES(1, ?)", (pct,))
    if equity:
        c.execute("INSERT INTO sim_equity_snapshots VALUES(1, ?)", (equity,))
    return c


def test_position_cap_behaviour():
    import quick_sim as qs
    # 未设置 → 原样（不改变既有行为）
    assert qs._apply_position_cap(_cap_db(None, 9997.57), 100) == 100
    # 0.5% × 9997.57 = 49.99 < 100 → 封顶
    assert abs(qs._apply_position_cap(_cap_db(0.005, 9997.57), 100) - 49.99) < 1e-6
    # 5% × 9997.57 = 499.9 > 100 → 不封顶
    assert qs._apply_position_cap(_cap_db(0.05, 9997.57), 100) == 100
    # 没有权益快照 → 原样
    assert qs._apply_position_cap(_cap_db(0.005, None), 100) == 100


def test_rescue_skips_insights_that_are_verdict_failed():
    """v409.5 实测踩到的坑：停用后会被『无样本救回』全量复活。"""
    import review.review_agent as ra
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE review_insights(id INTEGER PRIMARY KEY, rule TEXT, kind INTEGER,"
                 " active INTEGER, manual_deactivated INTEGER, verification_verdict TEXT)")
    conn.execute("INSERT INTO review_insights VALUES(1,'验否的','1',0,0,'failed')")
    conn.execute("INSERT INTO review_insights VALUES(2,'没验过的','1',0,0,NULL)")
    out = ra._rescue_unmeasured(conn)
    ids = {r["id"] for r in conn.execute("SELECT id FROM review_insights WHERE active=1")}
    assert 1 not in ids, "verdict=failed 的不许被『无样本』救回"
    assert 2 in ids, "没验证结论的仍应救回（不误伤）"
    assert len(out["rescued_no_sample"]) == 1


def test_contamination_veto_is_note_only_and_causal():
    """v409.6：E014/E020 不再 veto；且关联只认『洞察早于决策』。"""
    import review.reflection_feedback_config as rfc
    import verifier_feedback as vfb

    assert rfc.get_policy("E014")["veto_insights"] is False
    assert rfc.get_policy("E020")["veto_insights"] is False

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE review_insights(id INTEGER PRIMARY KEY, rule TEXT, asset TEXT,"
                 " created_at TEXT, active INTEGER)")
    conn.execute("CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY, created_at TEXT, target_asset TEXT)")
    conn.execute("CREATE TABLE decision_errors(id INTEGER PRIMARY KEY, decision_id INTEGER, error_code TEXT)")
    # 洞察 1：事故之前 1 小时出生（可能被带偏 → 应命中）
    conn.execute("INSERT INTO review_insights VALUES(1,'早生','XAU','2026-11-17 20:00:00',1)")
    # 洞察 2：事故之后出生（不可能带偏更早的决策 → 不应命中）
    conn.execute("INSERT INTO review_insights VALUES(2,'晚生','XAU','2026-11-17 22:30:00',1)")
    conn.execute("INSERT INTO ai_decisions VALUES(10,'2026-11-17T21:30:00+08:00','XAU')")
    conn.execute("INSERT INTO decision_errors VALUES(1,10,'E014')")

    from datetime import datetime
    hit = vfb._find_related_insights(10, "XAU", datetime(2026, 11, 17, 21, 30, 0), conn=conn)
    assert 1 in hit, "早于决策的洞察应被判为关联"
    assert 2 not in hit, "晚于决策的洞察不应被算作污染源"
