# -*- coding: utf-8 -*-
"""v398 洞察样本池口径回归 (纯离线, 不碰生产库)。

钉死三件事:
  1) S10 只统计 A/B 交易错因, C 类技术错误不进分母, 且被排除条数写进 detail;
  2) S12/S15 的降级决策移出分母, 降级率由 S16 单独度量;
  3) insight_pool 的两个纯函数在缺列/缺 id 时保守放行, 不悄悄停掉写入链。

跑法: pytest backend/tests/test_v398_insight_pool.py -q
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_scorecard as cs
import insight_pool as ip


def _metric(mid):
    return [m for m in cs.METRICS if m.id == mid][0]


def _reflections_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE trade_reflections_v2("
                 "dimension TEXT, subcategory TEXT)")
    conn.executemany("INSERT INTO trade_reflections_v2 VALUES (?,?)", rows)
    return conn


def _decisions_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ai_decisions("
                 "id INTEGER, reasoning_path TEXT, impact_score REAL, "
                 "analysis_degraded INTEGER, quarantined_at REAL)")
    conn.executemany("INSERT INTO ai_decisions VALUES (?,?,?,?,?)", rows)
    return conn


# ── S10: 技术错误不进错因分母 ─────────────────────────────────────────────
def test_s10_excludes_technical_errors_from_denominator():
    conn = _reflections_conn([
        ("A", "market_regime_misread"),
        ("A", "market_regime_misread"),
        ("A", "pattern_recognition"),
        ("B", "news_direction_inverted"),
        # 一次抓取失败批量写入的技术错误: 不应把分母撑大, 也不应成为"最大类"
        ("C", "data_feed_timeout"),
        ("C", "data_feed_timeout"),
        ("C", "data_feed_timeout"),
        ("C", "data_feed_timeout"),
        ("C", "data_feed_timeout"),
        ("C", "data_feed_timeout"),
        ("C", "signal_delay"),
    ])
    res = cs.evaluate_metric(conn, _metric("S10"))
    assert res.n == 4                     # 只有 A+B
    assert abs(res.value - 50.0) < 0.1    # max(A/regime=2)/4
    assert "排除 C 类技术错误 7 条" in res.detail


def test_s10_handles_empty_business_pool():
    conn = _reflections_conn([("C", "data_feed_timeout")] * 5)
    res = cs.evaluate_metric(conn, _metric("S10"))
    assert res.n == 0
    assert res.value is None
    assert "排除 C 类技术错误 5 条" in res.detail


# ── S12 / S15: 降级决策不进分母 ───────────────────────────────────────────
def test_s12_excludes_degraded_decisions():
    real = "[总框架] XAU 实际盘面：美元走弱 + 避险资金流入\n[总结] 看多"
    fallback = "[总框架] 目标资产 XAU；事件不直接命中本系统核心监控品种"
    conn = _decisions_conn([
        (1, real, 3.0, 0, 0),
        (2, real, 3.0, 0, 0),
        (3, fallback, 3.0, 0, 0),
        (4, fallback, 3.0, 0, 0),
        # 降级决策: 没有真分析, 但"没有"不等于"答错", 不进分母
        (5, fallback, 5.0, 1, 0),
        (6, fallback, 5.0, 1, 0),
        (7, real, 5.0, 1, 0),
    ])
    res = cs.evaluate_metric(conn, _metric("S12"))
    assert res.n == 4                     # 非降级 4 条
    assert abs(res.value - 50.0) < 0.1    # 2/4 有真分析


def test_s15_excludes_degraded_decisions():
    conn = _decisions_conn([
        (1, "x", 4.0, 0, 0),
        (2, "x", 5.0, 0, 0),
        (3, "x", 5.0, 1, 0),
        (4, "x", 8.0, 1, 0),
    ])
    res = cs.evaluate_metric(conn, _metric("S15"))
    assert res.n == 2                     # 非降级 2 条
    assert abs(res.value - 50.0) < 0.1    # 1 条实质化


def test_s16_counts_degraded_share():
    rows = [(i, "x", 5.0, 1, 0) for i in range(1, 301)]
    rows += [(i, "x", 5.0, 0, 0) for i in range(301, 401)]
    conn = _decisions_conn(rows)
    res = cs.evaluate_metric(conn, _metric("S16"))
    assert res.n == 400
    assert abs(res.value - 75.0) < 0.1
    # 越低越好: 75% 远高于毕业线 20% → 未达线
    assert res.status == cs.ST_BELOW


# ── insight_pool 纯函数 ──────────────────────────────────────────────────
def test_is_degraded_decision_true_false_and_missing():
    conn = _decisions_conn([(1, "x", 5.0, 1, 0), (2, "x", 5.0, 0, 0)])
    assert ip.is_degraded_decision(conn, 1) is True
    assert ip.is_degraded_decision(conn, 2) is False
    assert ip.is_degraded_decision(conn, None) is False
    assert ip.is_degraded_decision(conn, 999) is False


def test_is_degraded_decision_degrades_open_when_column_missing():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ai_decisions(id INTEGER)")
    conn.execute("INSERT INTO ai_decisions VALUES (1)")
    assert ip.is_degraded_decision(conn, 1) is False


def test_reflection_pool_stats_splits_pools():
    conn = _reflections_conn([
        ("A", "market_regime_misread"), ("A", "market_regime_misread"),
        ("A", "pattern_recognition"),
        ("B", "news_direction_inverted"),
        ("C", "data_feed_timeout"), ("C", "data_feed_timeout"),
        ("", ""), ("Z", "whatever"),
    ])
    stats = ip.reflection_pool_stats(conn)
    assert stats["business_n"] == 4
    assert stats["technical_n"] == 2
    assert stats["other_n"] == 2
    assert stats["top_sub"] == "market_regime_misread"
    assert stats["top_n"] == 2
    assert abs(stats["top_share"] - 50.0) < 0.1


def test_s17_counts_reflections_with_error_codes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE trade_reflections_v2(decision_id INTEGER, "
                 "dimension TEXT, subcategory TEXT)")
    conn.execute("CREATE TABLE decision_errors(decision_id INTEGER, error_code TEXT)")
    conn.executemany("INSERT INTO trade_reflections_v2 VALUES (?,?,?)", [
        (1, "A", "pattern_recognition"),
        (2, "B", "cause_effect_reversed"),
        (3, "A", "psychological_anchor"),
        (4, "C", "data_feed_timeout"),      # 技术故障不进分母
    ])
    conn.executemany("INSERT INTO decision_errors VALUES (?,?)", [
        (1, "E008"), (1, "E009"),
        (2, "E004"),
    ])
    res = cs.evaluate_metric(conn, _metric("S17"))
    assert res.n == 3                     # 只有 A/B 三行
    assert abs(res.value - 66.7) < 0.2    # 2/3 有错误码
    assert res.status == cs.ST_THIN       # n=3 低于 gate 200


def test_registry_ids_unique_after_v398():
    ids = [m.id for m in cs.METRICS]
    assert len(ids) == len(set(ids))
    assert "S16" in ids
    assert "S17" in ids
