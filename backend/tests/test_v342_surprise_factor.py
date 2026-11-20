"""v342 — 预期差因子（surprise_factor 回归测试）

钉死的六件事：
  1. 数值解析：%、K/M/B、括号负数、N/A 一律 None（不猜）
  2. 归一：按「预期 vs 前值」的典型变化折算，极端值夹到 ±1
  3. 极性表只给口径清楚的指标；不认识的指标 → None（不给意见）
  4. 事件匹配：只认窗口内、标题关键词命中的事件
  5. 冲突判定：CPI 超预期 + BUY = 冲突；CPI 低于预期 + BUY = 不冲突
  6. 闸门只减分不改向：冲突且幅度够大 → final_score 幅度变小、记 news_vs_surprise
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import decision_guard
import surprise_factor as su


# ─────────────────────── 1. 数值解析 ───────────────────────

def test_parse_number_variants():
    assert su.parse_number("2.5%") == 2.5
    assert su.parse_number("150K") == 150000
    assert su.parse_number("1.2M") == 1200000
    assert su.parse_number("(1.3)") == -1.3
    assert su.parse_number("1,234") == 1234
    assert su.parse_number("3.4万") == 34000
    for bad in ("N/A", "—", "", None, "abc"):
        assert su.parse_number(bad) is None


# ─────────────────────── 2. 归一与极性 ───────────────────────

def test_norm_uses_forecast_vs_previous_scale():
    out = su.compute(actual=3.5, forecast=3.0, previous=2.5, indicator="CPI", asset="BTC")
    assert out["available"] is True
    assert out["deviation"] == 0.5
    assert out["norm"] == 1.0          # 0.5 / |3.0-2.5| = 1.0（夹到上限）
    assert out["polarity"] == -1       # CPI 超预期 → 风险资产偏空


def test_norm_is_clamped():
    out = su.compute(actual=10, forecast=1, previous=1, indicator="CPI", asset="BTC")
    assert -1.0 <= out["norm"] <= 1.0


def test_missing_actual_or_forecast_is_unavailable():
    out = su.compute(actual="N/A", forecast=3.0, indicator="CPI", asset="BTC")
    assert out["available"] is False
    out2 = su.compute(actual=3.1, forecast=None, indicator="CPI", asset="BTC")
    assert out2["available"] is False


def test_polarity_table_and_unknown_indicator():
    assert su.polarity("美国CPI年率", "BTC") == -1
    assert su.polarity("美国失业率", "BTC") == +1
    assert su.polarity("美国CPI年率", "USD") == +1
    assert su.polarity("某不知名指标", "BTC") is None
    assert su.polarity("美国CPI年率", "WTI") is None     # 没登记资产类别 → 不给意见


# ─────────────────────── 3. 事件匹配 ───────────────────────

def _econ_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE econ_events (country TEXT, title TEXT, ts INTEGER,
                                     impact TEXT, actual TEXT, forecast TEXT,
                                     previous TEXT, fetched_at INTEGER,
                                     PRIMARY KEY (country, title, ts));"""
    )
    conn.execute("INSERT INTO econ_events VALUES (?,?,?,?,?,?,?,?)",
                 ("US", "美国CPI年率", 1791840000, "high", "3.5", "3.0", "2.5", 0))
    return conn


def test_find_event_and_enrich():
    conn = _econ_db()
    ev = su.find_event(conn, when_ts=1791840300, title="美国CPI年率公布", asset="BTC")
    assert ev is not None and ev["title"] == "美国CPI年率"
    row = {"news_title": "美国CPI年率公布", "target_asset": "BTC",
           "news_ts": 1791840300}
    out = su.enrich(conn, row)
    assert out["available"] is True and out["polarity"] == -1
    assert out["event"]["country"] == "US"


def test_enrich_returns_unavailable_when_no_event():
    conn = _econ_db()
    out = su.enrich(conn, {"news_title": "某公司财报", "target_asset": "AAPL",
                           "news_ts": 1791840300})
    assert out["available"] is False


def test_find_event_respects_window():
    conn = _econ_db()
    ev = su.find_event(conn, when_ts=1791840000 + 5000, title="美国CPI年率公布",
                       asset="BTC", window_sec=1800)
    assert ev is None      # 超出窗口 → 不挂


# ─────────────────────── 4. 冲突判定 ───────────────────────

def test_conflict_logic():
    hot_cpi = su.compute(3.5, 3.0, 2.5, indicator="CPI", asset="BTC")
    assert su.conflict_with_action(hot_cpi, "BUY") is True       # 超预期 → 空 CPI → 反对追多
    assert su.conflict_with_action(hot_cpi, "SELL") is False
    cool_cpi = su.compute(2.5, 3.0, 3.4, indicator="CPI", asset="BTC")
    assert su.conflict_with_action(cool_cpi, "BUY") is False
    # 无极性 / 无动作 → 永不判冲突
    assert su.conflict_with_action({"available": True, "norm": 0.9, "polarity": None}, "BUY") is False
    assert su.conflict_with_action(hot_cpi, "HOLD") is False


# ─────────────────────── 5. 闸门减分 ───────────────────────

def _ctx(assets):
    return json.dumps({"assets": assets})


def _asset():
    return {"status": "ok", "change_24h_pct": 1.0, "change_24h_str": "+1.00%",
            "funding_rate_pct": 0.01, "funding_rate_str": "+0.0100%",
            "stats_7d": {"trend": "Ranging", "atr_pct": 1.0}}


def test_gate_damps_score_when_surprise_conflicts():
    base_row = {"sentiment_score": 0.6, "suggested_action": "BUY",
                "target_asset": "BTC", "decision_context": _ctx({"BTC": _asset()})}
    plain = decision_guard.evaluate_decision(dict(base_row))
    hot = su.compute(3.5, 3.0, 2.5, indicator="CPI", asset="BTC")
    with_surprise = decision_guard.evaluate_decision(dict(
        base_row, surprise=hot))
    assert abs(with_surprise["final_score"]) < abs(plain["final_score"])
    assert with_surprise["surprise"]["conflict"] is True
    assert with_surprise["pre_surprise_score"] == plain["final_score"]
    assert any(c["type"] == "news_vs_surprise" for c in with_surprise["contradictions"])


def test_gate_ignores_small_or_supporting_surprise():
    base_row = {"sentiment_score": 0.6, "suggested_action": "BUY",
                "target_asset": "BTC", "decision_context": _ctx({"BTC": _asset()})}
    plain = decision_guard.evaluate_decision(dict(base_row))
    tiny = su.compute(3.02, 3.0, 2.5, indicator="CPI", asset="BTC")   # norm≈0.04，很小
    assert abs(tiny["norm"]) < 0.35
    with_tiny = decision_guard.evaluate_decision(dict(base_row, surprise=tiny))
    assert with_tiny["final_score"] == plain["final_score"]           # 幅度不够 → 不减
    cool = su.compute(2.5, 3.0, 3.4, indicator="CPI", asset="BTC")    # 方向支持 BUY
    with_cool = decision_guard.evaluate_decision(dict(base_row, surprise=cool))
    assert with_cool["final_score"] == plain["final_score"]


def test_gate_without_surprise_is_unchanged():
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.6, "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _asset()}),
    })
    assert out["surprise"]["available"] is False
    assert out["pre_surprise_score"] == out["final_score"]


def test_enrich_by_news_id_reads_raw_news():
    conn = _econ_db()
    conn.executescript("CREATE TABLE raw_news (id INTEGER PRIMARY KEY, content TEXT, ts INTEGER);")
    conn.execute("INSERT INTO raw_news VALUES (1, '美国CPI年率公布 3.5%', 1791840300)")
    out = su.enrich_by_news_id(conn, 1, "BTC")
    assert out["available"] is True and out["polarity"] == -1
    missing = su.enrich_by_news_id(conn, 999, "BTC")
    assert missing["available"] is False
