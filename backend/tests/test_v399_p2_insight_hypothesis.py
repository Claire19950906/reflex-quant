# -*- coding: utf-8 -*-
"""v399 P2 回归：洞察→可测假设 的分类器 + S18/S19 两条指标。

覆盖点：
  1. 有参数键 + 明确方向 → 可执行（settings / strategy 两个桶都算）
  2. 没有参数键 → observe_only（不许编假设）
  3. 参数键存在但单位不等价（sizing.stop_loss_pct）→ observe_only，理由来自 aliases
  4. 有键但没说方向 → observe_only
  5. 观测窗口解析（天/周/默认）
  6. S18 无标的占比、S19 观察型占比的取数与 gate 行为
"""
from __future__ import annotations

import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src_python"))

import capability_scorecard as cs
import insight_hypothesis as ih


def _metric(mid):
    return [m for m in cs.METRICS if m.id == mid][0]


# ── 分类器 ────────────────────────────────────────────────────────────────

def test_actionable_settings_insight():
    c = ih.classify("[score.buy_min_score] BUY 在 0.4～0.5 区间胜率低，应降低该区间门槛",
                    asset="WTI", source_samples=40)
    assert c["observe_only"] == 0
    assert c["param_bucket"] == "settings"
    assert c["direction"] == -1
    assert "buy_min_score" in c["hypothesis"]


def test_actionable_strategy_insight():
    c = ih.classify("[sizing.holding_horizon_minutes] 消息冲击期应延长持仓窗口",
                    asset="XAU", source_samples=35)
    assert c["observe_only"] == 0
    assert c["param_bucket"] == "strategy"
    assert c["direction"] == 1


def test_no_param_key_is_observe_only():
    c = ih.classify("样本仅3笔，经验只作参考；积累至少30笔后再评估稳定性")
    assert c["observe_only"] == 1
    assert c["hypothesis"] == ""
    assert "参数键" in c["observe_reason"]


def test_unmappable_unit_mismatch_is_observe_only():
    c = ih.classify("[sizing.stop_loss_pct] 消息冲击初期波动放大，应收紧止损",
                    asset="WTI", source_samples=6)
    assert c["observe_only"] == 1
    assert "止损" in c["observe_reason"] or "不等价" in c["observe_reason"]


def test_missing_direction_is_observe_only():
    c = ih.classify("[score.sell_min_score] SELL 门槛看起来有点高")
    assert c["observe_only"] == 1
    assert "方向" in c["observe_reason"]


def test_window_parsing():
    assert ih.classify("[score.buy_min_score] 提高 3 天窗口内的胜率")["window_h"] == 72
    assert ih.classify("[score.buy_min_score] 提高 2 周窗口内的胜率")["window_h"] == 336
    assert ih.classify("[score.buy_min_score] 提高门槛")["window_h"] == ih.DEFAULT_WINDOW_H


def test_ensure_classified_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE review_insights(id INTEGER PRIMARY KEY, rule TEXT, "
                 "asset TEXT, source_samples INTEGER, active INTEGER, "
                 "insight_hypothesis TEXT DEFAULT '', insight_param_key TEXT DEFAULT '', "
                 "insight_param_bucket TEXT DEFAULT '', insight_direction INTEGER DEFAULT 0, "
                 "insight_window_h INTEGER DEFAULT 0, observe_only INTEGER DEFAULT 0, "
                 "observe_reason TEXT DEFAULT '', insight_classified_at TEXT DEFAULT '', "
                 "verification_verdict TEXT DEFAULT '')")
    conn.execute("INSERT INTO review_insights (id,rule,asset,source_samples,active) "
                 "VALUES (1,'[score.buy_min_score] 提高门槛','BTC',40,1)")
    conn.execute("INSERT INTO review_insights (id,rule,asset,source_samples,active) "
                 "VALUES (2,'样本仅3笔，只作参考','',3,1)")
    assert ih.ensure_classified(conn) == 2
    assert ih.ensure_classified(conn) == 0          # 幂等：已分类不再动
    stats = ih.pool_stats(conn)
    assert stats["active"] == 2 and stats["observe_only"] == 1 and stats["actionable"] == 1


# ── 指标 ──────────────────────────────────────────────────────────────────

def test_s18_no_target_share():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ai_decisions(id INTEGER, target_asset TEXT, "
                 "analysis_degraded_reason TEXT, asset_route TEXT)")
    conn.executemany("INSERT INTO ai_decisions VALUES (?,?,?,?)", [
        (1, "BTC", "", ""), (2, "ETH", "", ""), (3, "NONE", "target_none", ""),
        (4, "NONE", "server_fallback", ""),
    ])
    res = cs.evaluate_metric(conn, _metric("S18"))
    assert res.n == 4
    assert abs(res.value - 50.0) < 0.2
    assert res.status == cs.ST_THIN


def test_s19_observe_only_share():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE review_insights(active INTEGER, observe_only INTEGER, "
                 "insight_classified_at TEXT, verification_verdict TEXT)")
    conn.executemany("INSERT INTO review_insights VALUES (?,?,?,?)", [
        (1, 1, "x", ""), (1, 1, "x", ""), (1, 1, "x", ""),
        (1, 0, "x", "passed"), (0, 1, "x", ""),     # 停用的不进分母
    ])
    res = cs.evaluate_metric(conn, _metric("S19"))
    assert res.n == 4
    assert abs(res.value - 75.0) < 0.2
    assert res.status == cs.ST_THIN


def test_registry_has_s18_s19_unique():
    ids = [m.id for m in cs.METRICS]
    assert len(ids) == len(set(ids))
    assert "S18" in ids and "S19" in ids
