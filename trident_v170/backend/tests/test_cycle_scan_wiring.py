# -*- coding: utf-8 -*-
"""v226+ cycle_scan_handler 接 cycle_analyst 的 wire-in 测试。"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import discovery as dc


@pytest.fixture
def kb_db(monkeypatch, tmp_path):
    """隔离 SQLite + 建 kline_bars / factor_snapshots 表。"""
    db_path = tmp_path / "test_cycle_scan.db"
    monkeypatch.setenv("TRIDENT_DB_PATH", str(db_path))
    monkeypatch.setattr(db, "get_db_path", lambda: str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kline_bars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            ts INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            UNIQUE(symbol, timeframe, ts)
        );
        CREATE TABLE IF NOT EXISTS factor_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            final_score REAL,
            ts INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return str(db_path)


def _seed_uptrend(conn, symbol="BTC", n=60, base=100.0, step=1.5):
    for i in range(n):
        close = base + step * i
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, close) VALUES (?, ?, ?, ?)",
            (symbol, "1d", int(time.time()) - (n - i) * 86400, close),
        )


def _seed_downtrend(conn, symbol="ETH", n=60, base=3000.0, step=-50.0):
    for i in range(n):
        close = base + step * i
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, close) VALUES (?, ?, ?, ?)",
            (symbol, "1d", int(time.time()) - (n - i) * 86400, close),
        )


def test_cycle_scan_uses_detect_phase_for_uptrend(kb_db):
    conn = sqlite3.connect(kb_db)
    try:
        _seed_uptrend(conn)
        conn.commit()
    finally:
        conn.close()
    out = asyncio.run(dc.cycle_scan_handler())
    assert out["ok"] is True
    rows = out["rows"]
    assert len(rows) >= 1
    btc = next((r for r in rows if r["asset"] == "BTC"), None)
    assert btc is not None
    # 单调上行 → trend_pct > 0; 阶段可能 markup/ranging/distribution (RSI 超买)
    assert btc["trend_pct"] > 0
    assert btc["cycle"] in ("markup", "distribution", "ranging")
    assert btc["cycle_cn"] in ("拉升", "派发", "震荡")
    for k in ("phase_confidence", "hurst", "trend_pct", "cycle_len_days",
              "reversal_prob", "reversal_dir", "mtf_alignment"):
        assert k in btc


def test_cycle_scan_handles_downtrend(kb_db):
    conn = sqlite3.connect(kb_db)
    try:
        _seed_downtrend(conn)
        conn.commit()
    finally:
        conn.close()
    out = asyncio.run(dc.cycle_scan_handler())
    rows = out["rows"]
    eth = next((r for r in rows if r["asset"] == "ETH"), None)
    assert eth is not None
    assert eth["cycle"] in ("markdown", "distribution", "accumulation", "ranging")
    assert eth["trend_pct"] < 0


def test_cycle_scan_fallback_to_factor_snapshots_when_kline_empty(kb_db):
    conn = sqlite3.connect(kb_db)
    try:
        now = int(time.time())
        conn.executemany(
            "INSERT INTO factor_snapshots (asset, final_score, ts) VALUES (?, ?, ?)",
            [
                ("BTC", 0.50, now - 7 * 86400),
                ("BTC", 0.55, now - 6 * 86400),
                ("BTC", 0.60, now - 5 * 86400),
                ("BTC", 0.58, now - 4 * 86400),
                ("BTC", 0.62, now - 3 * 86400),
                ("BTC", 0.65, now - 2 * 86400),
                ("BTC", 0.70, now - 1 * 86400),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    out = asyncio.run(dc.cycle_scan_handler())
    assert out["fallback"] is True
    assert len(out["rows"]) == 1
    assert out["rows"][0]["cycle"] == "markup"
    assert "fallback" in out["rows"][0]["hint"]


def test_phase_cn_translates():
    assert dc._phase_cn("accumulation") == "吸筹"
    assert dc._phase_cn("markup") == "拉升"
    assert dc._phase_cn("distribution") == "派发"
    assert dc._phase_cn("markdown") == "下跌"
    assert dc._phase_cn("ranging") == "震荡"
    assert dc._phase_cn("unknown") == "未知"
    assert dc._phase_cn("garbage") == "garbage"


def test_phase_hint_distribution_with_high_prob():
    phase = {"phase": "distribution"}
    rev = {"prob": 0.7, "direction": "down"}
    h = dc._phase_hint(phase, rev)
    assert "派发中" in h
    assert "70%" in h


def test_phase_hint_markup_low_risk():
    phase = {"phase": "markup"}
    rev = {"prob": 0.2, "direction": "neutral"}
    h = dc._phase_hint(phase, rev)
    assert "顺势做多" in h


def test_cycle_scan_mtf_with_4h_data(kb_db):
    conn = sqlite3.connect(kb_db)
    try:
        _seed_uptrend(conn, symbol="BTC", n=60, base=100.0, step=1.0)
        for i in range(60):
            conn.execute(
                "INSERT INTO kline_bars (symbol, timeframe, ts, close) VALUES (?, ?, ?, ?)",
                ("BTC", "4h", int(time.time()) - (60 - i) * 4 * 3600, 100.0 + 0.2 * i),
            )
        conn.commit()
    finally:
        conn.close()
    out = asyncio.run(dc.cycle_scan_handler())
    rows = out["rows"]
    btc = next((r for r in rows if r["asset"] == "BTC"), None)
    assert btc is not None
    # 1d step=1.0, 4h step=0.2 → 都是 markup (上行)
    assert btc["mtf_dirs"].get("daily") in ("markup", "distribution")
    assert btc["mtf_dirs"].get("4h") in ("markup", "distribution", "ranging")
    assert btc["mtf_alignment"] in ("aligned", "mixed")


def test_cycle_scan_returns_empty_when_db_has_nothing(kb_db):
    out = asyncio.run(dc.cycle_scan_handler())
    assert out["ok"] is True
    assert out["rows"] == []
    # 两表都空 → 走到 fallback 路径但 fallback 内 SELECT 也空 → rows 还是 []
    assert out["fallback"] is True