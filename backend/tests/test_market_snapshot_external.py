# -*- coding: utf-8 -*-
"""v226 P2-1 market_snapshot._collect_external_indicators 测试。"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import market_snapshot as ms


@pytest.fixture
def ms_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test_ms_external.db"
    monkeypatch.setenv("TRIDENT_DB_PATH", str(db_path))
    monkeypatch.setattr(db, "get_db_path", lambda: str(db_path))
    # market_snapshot 内部用 db.get_connection()，需 monkeypatch 该函数返回带 row_factory 的 conn
    class _Conn:
        def __init__(self, raw):
            self.raw = raw
            self.raw.row_factory = sqlite3.Row
        def execute(self, *a, **kw):
            return self.raw.execute(*a, **kw)
        def close(self):
            self.raw.close()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.close()
    raw = sqlite3.connect(str(db_path))
    raw.executescript("""
        CREATE TABLE IF NOT EXISTS external_indicators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            indicator TEXT NOT NULL,
            value REAL NOT NULL,
            ts INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            regime_tags TEXT NOT NULL DEFAULT '[]',
            meta TEXT NOT NULL DEFAULT '{}',
            ingested_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
        );
    """)
    raw.commit()
    monkeypatch.setattr(db, "get_connection", lambda: _Conn(raw))
    return str(db_path)


def _seed_uptrend(conn, asset="BTC", symbol="BTC-USD", n=60, base=100.0, step=1.5):
    for i in range(n):
        conn.execute(
            "INSERT INTO external_indicators (asset, indicator, value, ts) "
            "VALUES (?, ?, ?, ?)",
            (asset, symbol, base + step * i,
             int(time.time()) - (n - i) * 86400),
        )


def test_collect_external_no_data(ms_db):
    """external_indicators 空 → has_data=False。"""
    r = ms._collect_external_indicators("BTC")
    assert r["has_data"] is False
    assert r["trend_factor_7d_pct"] is None
    assert r["source"] is None


def test_collect_external_unsupported_asset(ms_db):
    """XAU/WTI 不在覆盖列表 → has_data=False。"""
    r = ms._collect_external_indicators("XAU")
    assert r["has_data"] is False
    assert r["source"] is None


def test_collect_external_uptrend_7d(ms_db):
    """上行 → trend_factor_7d_pct > 0。"""
    conn = sqlite3.connect(ms_db)
    try:
        _seed_uptrend(conn, asset="BTC", symbol="BTC-USD", n=60,
                       base=100.0, step=1.5)
        conn.commit()
    finally:
        conn.close()
    r = ms._collect_external_indicators("BTC")
    assert r["has_data"] is True
    assert r["source"] == "BTC-USD"
    assert r["n_bars"] == 60
    assert r["trend_factor_7d_pct"] is not None
    assert r["trend_factor_7d_pct"] > 0


def test_collect_external_downtrend_7d(ms_db):
    """下行 → trend_factor_7d_pct < 0。"""
    conn = sqlite3.connect(ms_db)
    try:
        for i in range(60):
            conn.execute(
                "INSERT INTO external_indicators (asset, indicator, value, ts) "
                "VALUES (?, ?, ?, ?)",
                ("ETH", "ETH-USD", 3000.0 - 50.0 * i,
                 int(time.time()) - (60 - i) * 86400),
            )
        conn.commit()
    finally:
        conn.close()
    r = ms._collect_external_indicators("ETH")
    assert r["has_data"] is True
    assert r["source"] == "ETH-USD"
    assert r["trend_factor_7d_pct"] < 0


def test_collect_external_insufficient_bars(ms_db):
    """< 8 根 → has_data=False, n_bars < 8。"""
    conn = sqlite3.connect(ms_db)
    try:
        for i in range(3):
            conn.execute(
                "INSERT INTO external_indicators (asset, indicator, value, ts) "
                "VALUES (?, ?, ?, ?)",
                ("BTC", "BTC-USD", 100.0 + i, int(time.time()) - (3 - i) * 86400),
            )
        conn.commit()
    finally:
        conn.close()
    r = ms._collect_external_indicators("BTC")
    assert r["has_data"] is False
    assert r["n_bars"] == 3


def test_collect_external_30d_factor(ms_db):
    """30d 因子只在 n >= 31 时计算。"""
    conn = sqlite3.connect(ms_db)
    try:
        _seed_uptrend(conn, asset="BTC", symbol="BTC-USD", n=60)
        conn.commit()
    finally:
        conn.close()
    r = ms._collect_external_indicators("BTC")
    assert r["trend_factor_30d_pct"] is not None
    # 30 天单调上行 → 30d > 7d
    assert r["trend_factor_30d_pct"] > r["trend_factor_7d_pct"]