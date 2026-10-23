# -*- coding: utf-8 -*-
"""v226+ /api/discovery/cycle-analysis/{asset} endpoint 测试。"""
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


@pytest.fixture
def kb_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test_cycle_endpoint.db"
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
    """)
    conn.commit()
    conn.close()
    return str(db_path)


def _seed_uptrend(conn, symbol="BTC", n=60, base=100.0, step=1.0):
    for i in range(n):
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, close) VALUES (?, ?, ?, ?)",
            (symbol, "1d", int(time.time()) - (n - i) * 86400, base + step * i),
        )


def _seed_4h_uptrend(conn, symbol="BTC", n=60, base=100.0, step=0.2):
    for i in range(n):
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, close) VALUES (?, ?, ?, ?)",
            (symbol, "4h", int(time.time()) - (n - i) * 4 * 3600, base + step * i),
        )


def test_endpoint_returns_full_dict_with_data(kb_db):
    """K 线充足 → 返回完整字段 + 调 cycle_analyst。"""
    conn = sqlite3.connect(kb_db)
    try:
        _seed_uptrend(conn, "BTC", n=60)
        _seed_4h_uptrend(conn, "BTC", n=60)
        conn.commit()
    finally:
        conn.close()

    from fastapi.testclient import TestClient
    import api_server
    client = TestClient(api_server.app)
    r = client.get("/api/discovery/cycle-analysis/BTC")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["asset"] == "BTC"
    assert body["kline_count"] == 60
    # 关键字段都来自 cycle_analyst
    for k in ("phase", "phase_cn", "confidence", "rsi", "hurst",
              "trend_pct", "cycle_len_days", "reversal_prob", "reversal_dir",
              "reversal_days_to", "reversal_votes", "mtf_alignment",
              "mtf_score", "mtf_dirs", "hint"):
        assert k in body
    # 上行 → trend_pct > 0
    assert body["trend_pct"] > 0
    # 1d + 4h 都上行 → mtf_dirs 包含 daily 和 4h
    assert "daily" in body["mtf_dirs"]
    assert "4h" in body["mtf_dirs"]


def test_endpoint_insufficient_data(kb_db):
    """K 线 < 10 根 → ok=False, error=insufficient_data。"""
    from fastapi.testclient import TestClient
    import api_server
    client = TestClient(api_server.app)
    r = client.get("/api/discovery/cycle-analysis/UNKNOWN")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "insufficient_data"


def test_endpoint_no_4h_works(kb_db):
    """只有 1d → mtf_dirs 只有 daily。"""
    conn = sqlite3.connect(kb_db)
    try:
        _seed_uptrend(conn, "ETH", n=30)
        conn.commit()
    finally:
        conn.close()

    from fastapi.testclient import TestClient
    import api_server
    client = TestClient(api_server.app)
    r = client.get("/api/discovery/cycle-analysis/ETH")
    body = r.json()
    assert body["ok"] is True
    assert "daily" in body["mtf_dirs"]
    assert "4h" not in body["mtf_dirs"]