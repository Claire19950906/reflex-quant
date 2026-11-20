"""v488 — 影子打分（决策级、短窗口）回归。

为什么值得钉死：这条链路是"样本饥饿"的唯一解药 —— 不等 7 天持有期，
也不等开仓，直接用 K 线判 BUY/SELL 对不对。它错了不会报错，只会
静默给出漂亮但错误的胜率，所以四个口径必须写成测试：
  1. 涨了 → BUY 记 WIN，净收益=涨幅-成本
  2. 跌了对 SELL 才是 WIN（方向不能搞反）
  3. 没到期 / 无 K 线 → 一条都不产出（不拿未来数据）
  4. HOLD / NONE → 不参与；重复落库幂等
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import shadow_score


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, target_asset TEXT, "
        "suggested_action TEXT, ts INTEGER, code_fingerprint TEXT)"
    )
    conn.execute(
        "CREATE TABLE kline_bars (symbol TEXT, timeframe TEXT, ts INTEGER, close REAL)"
    )
    shadow_score.ensure_schema(conn)
    return conn


def _bar(conn, symbol, ts_sec, close, tf="1h"):
    conn.execute("INSERT INTO kline_bars VALUES (?,?,?,?)", (symbol, tf, ts_sec * 1000, close))


def _dec(conn, did, asset, action, ts, fp="testfp"):
    conn.execute("INSERT INTO ai_decisions VALUES (?,?,?,?,?)", (did, asset, action, ts, fp))


def test_buy_up_is_win_with_cost():
    conn = _db()
    now = int(time.time())
    ts = now - 3 * 3600
    _bar(conn, "BTC", ts - 60, 100.0)
    _bar(conn, "BTC", ts + 3600, 101.0)
    _dec(conn, 1, "BTC", "BUY", ts)
    rows = shadow_score.compute_scores(conn, now_ts=now)
    h1 = [r for r in rows if r["horizon_sec"] == 3600]
    assert h1, "1h 窗口应有结果"
    r = h1[0]
    assert r["verdict"] == "WIN"
    assert abs(r["net_move"] - (1.0 - shadow_score.SHADOW_COST_BPS / 100)) < 1e-6
    # 4h 未到期 → 不能出现
    assert not [x for x in rows if x["horizon_sec"] == 4 * 3600]


def test_sell_down_is_win():
    conn = _db()
    now = int(time.time())
    ts = now - 3 * 3600
    _bar(conn, "XAU", ts - 60, 2000.0)
    _bar(conn, "XAU", ts + 3600, 1980.0)
    _dec(conn, 2, "XAU", "SELL", ts)
    r = [x for x in shadow_score.compute_scores(conn, now_ts=now) if x["horizon_sec"] == 3600][0]
    assert r["verdict"] == "WIN"
    assert r["signed_move"] > 0


def test_hold_and_none_are_skipped():
    conn = _db()
    now = int(time.time())
    ts = now - 5 * 3600
    _bar(conn, "BTC", ts - 60, 100.0)
    _bar(conn, "BTC", ts + 3600, 101.0)
    _dec(conn, 3, "BTC", "HOLD", ts)
    _dec(conn, 4, "NONE", "BUY", ts)
    assert shadow_score.compute_scores(conn, now_ts=now) == []


def test_persist_idempotent():
    conn = _db()
    now = int(time.time())
    ts = now - 3 * 3600
    _bar(conn, "BTC", ts - 60, 100.0)
    _bar(conn, "BTC", ts + 3600, 101.0)
    _dec(conn, 5, "BTC", "BUY", ts)
    rows = shadow_score.compute_scores(conn, now_ts=now)
    assert shadow_score.persist(conn, rows) == len(rows)
    assert shadow_score.compute_scores(conn, now_ts=now) == []   # 已打过分 → 不重复算
    assert conn.execute("SELECT COUNT(*) FROM shadow_scores").fetchone()[0] == len(rows)


def test_no_kline_no_score():
    conn = _db()
    now = int(time.time())
    _dec(conn, 6, "ETH", "BUY", now - 3 * 3600)
    assert shadow_score.compute_scores(conn, now_ts=now) == []
