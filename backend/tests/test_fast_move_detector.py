# -*- coding: utf-8 -*-
"""test_fast_move_detector.py — 快涨/快跌检测 + 市场上下文的回归测试。

覆盖三个曾经真实踩过的坑:
  1. tick → 1m bar 聚合必须产出**秒级**时间戳 (用分钟桶会让 span 算错,
     写出 1970 年的时间戳);
  2. 窗口必须按真实时间跨度算, 且做 bar 连续性校验 (数据稀疏时不许
     用残缺窗口冒充 1 小时);
  3. kline_bars.ts 是**毫秒**, 拿秒去比会静默变成"成交量永远 unavailable"。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import fast_move_detector as fmd


def _conn_with_ticks(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE market_ticks (id INTEGER PRIMARY KEY, symbol TEXT, "
                 "ts INTEGER, price REAL, change24h REAL, source TEXT, source_count INT)")
    conn.executemany(
        "INSERT INTO market_ticks (symbol, ts, price, change24h, source, source_count) "
        "VALUES (?,?,?,?,?,?)", rows)
    return conn


def _series(pcts_per_min, start_ts=1_700_000_000):
    """按每分钟 pct 变化构造一条 1m bar 序列。"""
    out = []
    px = 100.0
    for i, p in enumerate(pcts_per_min):
        px = px * (1.0 + p / 100.0)
        out.append((start_ts + i * 60, round(px, 6)))
    return out


# ── 1. tick → 1m bar ──────────────────────────────────────────
def test_load_tick_bars_returns_second_aligned_timestamps():
    """回归: 曾经用 ts//60 当时间戳 → 下游 span 变成"分钟差"当秒用。"""
    # 1700000000 // 60 = 28333333, 分钟桶 28333333 覆盖 [1700000000, 1700000019]
    conn = _conn_with_ticks([
        ("BTC", 1_700_000_000, 100.0, 0.0, "x", 1),   # 同一分钟的第 0 秒
        ("BTC", 1_700_000_010, 101.0, 0.0, "x", 1),   # 同一分钟的第 10 秒
        ("BTC", 1_700_000_060, 102.0, 0.0, "x", 1),   # 下一分钟
    ])
    bars = fmd.load_tick_bars(conn, "BTC", 0)
    assert len(bars) == 2
    for ts, _px in bars:
        assert ts > 1_000_000_000, "时间戳退化成分钟桶/秒级以下"
        assert ts % 60 == 0, "时间戳未做 60 秒对齐"
    assert bars[0][1] == 101.0        # 每根 bar 取分钟内最后一笔
    assert bars[1][1] == 102.0
    conn.close()


def test_load_tick_bars_drops_non_positive_price():
    conn = _conn_with_ticks([
        ("BTC", 1_700_000_030, 0.0, 0.0, "x", 1),     # 量纲错误
        ("BTC", 1_700_000_040, -5.0, 0.0, "x", 1),    # 负价
        ("BTC", 1_700_000_090, 100.0, 0.0, "x", 1),
    ])
    bars = fmd.load_tick_bars(conn, "BTC", 0)
    assert len(bars) == 1
    assert bars[0][1] == 100.0
    conn.close()


# ── 2. fast move 判定 ─────────────────────────────────────────
def test_detect_fast_moves_flags_large_one_hour_move():
    # 20 分钟 × 0.15%/分钟 ≈ 3% 累计, 超过 1.5% 阈值
    series = _series([0.0] * 60 + [0.15] * 20)
    moves = fmd.detect_fast_moves(series)
    assert moves, "1 小时累计涨幅超过阈值却没检出"
    m = moves[-1]
    assert m["move_kind"] == "fast_rise"
    assert m["duration_min"] == 60
    assert m["move_pct"] > 1.5


def test_detect_fast_moves_ignores_small_move():
    series = _series([0.01] * 120)                  # 波动很小
    assert fmd.detect_fast_moves(series) == []


def test_detect_fast_moves_rejects_sparse_series():
    """数据稀疏 (bar 太少) 时不许用残缺窗口冒充 1 小时。"""
    series = _series([1.0] * 6)                     # 只有 6 根 bar
    assert fmd.detect_fast_moves(series) == []


def test_detect_fast_moves_rejects_implausible_pct():
    """量纲跳变 (>90%) 是数据污染, 不是市场异动。"""
    series = [(1_700_000_000 + i * 60, 100.0) for i in range(60)]
    series += [(1_700_000_000 + (60 + i) * 60, 1.0) for i in range(60)]
    assert fmd.detect_fast_moves(series) == []


def test_detect_fast_moves_span_is_seconds():
    """窗口跨度必须是**秒** —— 曾经的分钟桶 bug 让 continuity 算出 39.18。"""
    series = _series([0.0] * 30 + [0.08] * 31)
    moves = fmd.detect_fast_moves(series)
    assert moves
    m = moves[-1]
    assert m["_span_sec"] >= fmd.FAST_WINDOW_MIN * 60 * fmd.MIN_SPAN_RATIO
    assert m["_expected_bars"] <= 62          # 1h 窗口约 61 根, 不是几千根
    assert m["_continuity"] <= 1.0


# ── 3. 市场上下文 ─────────────────────────────────────────────
def test_classify_market_context_flags_volume_surge():
    ctx = {"regime": "volatile_low", "vol_ratio": 2.5, "change24h_pct": 3.0}
    out = fmd.classify_market_context(ctx)
    assert out["market_phase"] == "accumulation_or_markup"
    assert any("放大" in r for r in out["reasons"])


def test_classify_market_context_flags_distribution():
    ctx = {"regime": "volatile_low", "vol_ratio": 2.5, "change24h_pct": -3.0}
    out = fmd.classify_market_context(ctx)
    assert out["market_phase"] == "distribution_or_dump"


def test_classify_market_context_marks_high_leverage():
    ctx = {"regime": "unknown", "vol_ratio": 1.0, "change24h_pct": 0.5,
           "liq_total_usd": 120_000_000}
    out = fmd.classify_market_context(ctx)
    assert out["lever_risk"] == "high"
    assert any("杠杆" in r for r in out["reasons"])


def test_classify_market_context_handles_missing_fields():
    out = fmd.classify_market_context({})
    assert out["market_phase"] == "unclassified"
    assert out["lever_risk"] == "unknown"
    assert out["reasons"] == []


def test_volume_context_handles_millisecond_ts():
    """回归: kline_bars.ts 是毫秒, 拿秒去比会永远查不到 → 成交量永远 unavailable。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE kline_bars (id INTEGER PRIMARY KEY, symbol TEXT, "
                 "timeframe TEXT, ts INTEGER, open REAL, high REAL, low REAL, "
                 "close REAL, volume REAL)")
    base_ms = 1_700_000_000_000
    conn.executemany(
        "INSERT INTO kline_bars (symbol, timeframe, ts, volume) VALUES (?,?,?,?)",
        [("BTC", "1h", base_ms - i * 3_600_000, 100.0 + i) for i in range(20)],
    )
    ctx = fmd._volume_context(conn, "BTC", base_ms // 1000)
    assert ctx["available"] is True
    assert ctx["vol_ratio"] is not None and ctx["vol_ratio"] > 0
    assert ctx["timeframe"] == "1h"
    conn.close()


def test_latest_regime_is_point_in_time():
    """回归: 回填历史时不能取"当前最新"的 regime (未来信息泄漏)。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE market_style_snapshot (id INTEGER PRIMARY KEY, "
                 "asset TEXT, regime TEXT, layer1_alerts TEXT, layer2_alerts TEXT, "
                 "layer3_alerts TEXT, ts INTEGER)")
    conn.executemany(
        "INSERT INTO market_style_snapshot (asset, regime, ts) VALUES (?,?,?)",
        [("BTC", "old_regime", 1_700_000_000), ("BTC", "new_regime", 1_800_000_000)],
    )
    past = fmd._latest_regime(conn, "BTC", 1_700_000_100)
    assert past["regime"] == "old_regime"
    latest = fmd._latest_regime(conn, "BTC")
    assert latest["regime"] == "new_regime"
    conn.close()


def test_crowd_context_is_point_in_time():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE crowd_topic_daily (topic_key TEXT, day TEXT, "
                 "display_name TEXT, asset_class TEXT, sentiment TEXT, "
                 "influence_tier TEXT)")
    conn.executemany(
        "INSERT INTO crowd_topic_daily (topic_key, day, sentiment, influence_tier) "
        "VALUES (?,?,?,?)",
        [("k1", "2026-10-01", "看多/有方向", "高影响"),
         ("k2", "2026-11-01", "看空/有方向", "高影响")],
    )
    # 1790924400 = 2026-10-01 01:00 UTC (在 day='2026-10-01' 之后, '2026-11-01' 之前)
    past = fmd._crowd_context(conn, "BTC", 1_790_924_400)
    assert past["sentiment"] == "看多/有方向"
    conn.close()


# ── 4. 幂等性 ─────────────────────────────────────────────────
def _build_db(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE market_ticks (id INTEGER PRIMARY KEY, symbol TEXT, "
                 "ts INTEGER, price REAL, change24h REAL, source TEXT, source_count INT)")
    conn.execute("CREATE TABLE critical_price_moves (id INTEGER PRIMARY KEY, "
                 "asset TEXT, ts INTEGER, start_price REAL, peak_price REAL, "
                 "end_price REAL, move_pct REAL, move_kind TEXT, duration_min INT, "
                 "algo_score REAL, context_json TEXT)")
    conn.execute("CREATE TABLE kline_bars (id INTEGER PRIMARY KEY, symbol TEXT, "
                 "timeframe TEXT, ts INTEGER, volume REAL)")
    now = 1_700_000_000
    conn.executemany(
        "INSERT INTO market_ticks (symbol, ts, price, change24h, source, source_count) "
        "VALUES (?,?,?,?,?,?)",
        [("BTC", now + i * 60, 100.0 * (1 + i * 0.0005), 0.0, "x", 1)
         for i in range(120)])
    conn.executemany(
        "INSERT INTO kline_bars (symbol, timeframe, ts, volume) VALUES (?,?,?,?)",
        [("BTC", "1h", (now + i * 3600) * 1000, 100.0) for i in range(20)])
    conn.commit()
    conn.close()


def test_run_is_idempotent(tmp_path):
    """重复跑不能把行数翻倍 (critical_price_moves 没有 UNIQUE 约束)。"""
    db = tmp_path / "t.db"
    _build_db(db)

    def _run():
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        try:
            return fmd.run(c, dry_run=False, lookback_hours=10 ** 6)
        finally:
            c.close()

    _run()
    chk = sqlite3.connect(str(db))
    n1 = chk.execute("SELECT COUNT(*) FROM critical_price_moves").fetchone()[0]
    res2 = _run()
    n2 = chk.execute("SELECT COUNT(*) FROM critical_price_moves").fetchone()[0]
    chk.close()
    assert n1 == n2, "重复运行让行数翻倍: %d → %d" % (n1, n2)
    assert res2["inserted"] == 0


def test_run_writes_json_context(tmp_path):
    """写出的 context_json 必须可解析, 且带关键字段。"""
    db = tmp_path / "ctx.db"
    _build_db(db)
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    fmd.run(c, dry_run=False, lookback_hours=10 ** 6)
    row = c.execute("SELECT context_json FROM critical_price_moves LIMIT 1").fetchone()
    c.close()
    assert row is not None
    ctx = json.loads(row[0])
    for k in ("source", "market_phase", "reasons", "continuity"):
        assert k in ctx, "context_json 缺字段 %s" % k
    assert ctx["source"] == "market_ticks"
