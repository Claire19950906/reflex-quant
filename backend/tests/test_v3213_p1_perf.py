#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v3213_p1_perf.py — v321.3 P1 性能/可见性回归 (全离线)

覆盖本轮验证中发现的两个**运行时**问题:
  1. market-regime 端点冷缓存 34.4s → 触发 20s 硬超时 → ok=false → 面板空白
     (根因: 端点有 stale 兜底, 但缓存从没被填充过 —— 因为算完不落库)
  2. 归因端点 SELECT 没取 ai_called/ai_explanation/ai_confidence
     → 需求 #6 的「双重验证」结果在归因页看不到
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # backend/
SRC_PY = os.path.join(SRC, "src_python")                                   # backend/src_python
if SRC_PY not in sys.path:
    sys.path.insert(0, SRC_PY)


# ── 1. market_regime 进程内 TTL 缓存 ─────────────────────────────────────────
def _mk_cot_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE cftc_cot_data (
        asset TEXT, report_date TEXT, open_interest REAL, change_oi REAL,
        noncomm_net REAL, comm_net REAL, spec_index REAL,
        pct_oi_noncomm_long REAL, pct_oi_noncomm_short REAL)""")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    conn.execute("CREATE TABLE crowd_topic_daily (topic_key TEXT, day TEXT, heat REAL)")
    conn.execute("""CREATE TABLE macro_snapshots (
        metric_key TEXT, ts INTEGER, value REAL, status TEXT)""")
    return conn


def test_detect_regimes_caches_in_process():
    """同一 since_date 的第二次调用必须命中缓存 (cached=True)。"""
    from market import market_regime as mr
    conn = _mk_cot_db()
    # 一条 COT 行 (够跑, 结果内容不重要)
    conn.execute("""INSERT INTO cftc_cot_data VALUES
        ('BTC','2026-11-01',100.0,5.0,10.0,-2.0,0.5,0.3,0.2)""")
    conn.commit()
    mr._REGIME_MEM_CACHE = {}
    mr._REGIME_MEM_CACHE_TS = 0.0
    r1 = mr.detect_regimes(conn, since_date="2024-01-01")
    r2 = mr.detect_regimes(conn, since_date="2024-01-01")
    assert r1.get("cached") is False
    assert r2.get("cached") is True
    assert "_since" not in r2, "内部键不能漏到响应里"
    assert r2.get("cache_age_sec") is not None


def test_detect_regimes_cache_keyed_by_since_date():
    """不同 since_date 不能串缓存。"""
    from market import market_regime as mr
    conn = _mk_cot_db()
    conn.execute("""INSERT INTO cftc_cot_data VALUES
        ('BTC','2026-11-01',100.0,5.0,10.0,-2.0,0.5,0.3,0.2)""")
    conn.commit()
    mr._REGIME_MEM_CACHE = {}
    mr._REGIME_MEM_CACHE_TS = 0.0
    mr.detect_regimes(conn, since_date="2024-01-01")
    r = mr.detect_regimes(conn, since_date="2025-01-01")
    assert r.get("cached") is False


def test_detect_regimes_uncached_still_works():
    """缓存包装不能改变底层行为。"""
    from market import market_regime as mr
    conn = _mk_cot_db()
    conn.execute("""INSERT INTO cftc_cot_data VALUES
        ('BTC','2026-11-01',100.0,5.0,10.0,-2.0,0.5,0.3,0.2)""")
    conn.commit()
    out = mr._detect_regimes_uncached(conn, since_date="2024-01-01")
    assert out.get("ok") is True
    assert "regimes" in out
    assert "funding" in out


# ── 2. 归因端点必须返回双重验证字段 ──────────────────────────────────────────
def test_attribution_endpoint_selects_ai_columns():
    """端点 SELECT 必须带 ai_called/ai_explanation/ai_confidence。

    这是需求 #6「双重验证自己检测一遍」的**页面可见性**保证 —— 少了这几列,
    归因页就只能看到规则引擎的结论, 看不到 AI 复核认不认。
    """
    path = os.path.join(SRC_PY, "bridge_server.py")
    text = open(path, encoding="utf-8").read()
    assert "ai_called, ai_explanation, ai_confidence, ai_error" in text, \
        "归因端点的 SELECT 必须取双重验证字段 (需求 #6)"
    # 端点必须把算完的结果落库, 否则超时后 stale 兜底永远是空的
    assert "_market_regime_cache_store(wconn, payload)" in text, \
        "超时后仍在跑的线程必须把结果落库, 否则兜底缓存永远为空 → 面板空白"
