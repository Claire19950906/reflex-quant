# -*- coding: utf-8 -*-
"""v227 P1-2/3/5: discovery.undervalued_detector + cross_asset_rankings + fed_watch 测试。"""
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
import discovery


@pytest.fixture
def disc_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test_disc.db"
    monkeypatch.setenv("TRIDENT_DB_PATH", str(db_path))
    monkeypatch.setattr(db, "get_db_path", lambda: str(db_path))

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
        CREATE TABLE IF NOT EXISTS raw_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            content TEXT,
            ts INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            anomaly_type TEXT,
            payload TEXT,
            ts INTEGER NOT NULL
        );
    """)
    raw.commit()
    monkeypatch.setattr(db, "get_connection", lambda: _Conn(raw))
    return str(db_path)


def _seed_series(conn, asset, indicator, values):
    now = int(time.time())
    for i, v in enumerate(values):
        ts = now - i * 86400
        conn.execute(
            "INSERT INTO external_indicators (asset, indicator, value, ts) VALUES (?,?,?,?)",
            (asset, indicator, v, ts),
        )


def test_undervalued_no_data(disc_db):
    r = asyncio.run(discovery.undervalued_detector_handler())
    assert r["ok"] is True
    assert r["items"] == []
    assert r["method"] == "heuristic"


def test_undervalued_hynix_severe_drawdown(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        # values[0] = latest = 82; values[29] = 30 days ago = 100 → change = -18%
        values = [round(82 + i * 0.62, 2) for i in range(30)]
        _seed_series(conn, "SKHYNIX", "000660.KS", values)
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.undervalued_detector_handler())
    items = {it["symbol"]: it for it in r["items"]}
    assert "SKHYNIX" in items
    h = items["SKHYNIX"]
    assert h["category"] == "memory_chip"
    assert h["change_30d_pct"] < -15
    assert h["undervalued_score"] > 0
    assert any("drawdown" in s for s in h["reasons"])


def test_undervalued_mu_us_peer(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        # latest=88, 30d ago=100 → -12%
        values = [round(88 + i * 0.4, 2) for i in range(30)]
        _seed_series(conn, "MU", "MU", values)
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.undervalued_detector_handler())
    items = {it["symbol"]: it for it in r["items"]}
    assert "MU" in items
    assert items["MU"]["category"] == "memory_chip"


def test_undervalued_btc_altcoin(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        # latest=54000, 30d ago=60000 → -10%
        values = [round(54000 + i * 200, 2) for i in range(30)]
        _seed_series(conn, "BTC", "BTC-USD", values)
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.undervalued_detector_handler())
    items = {it["symbol"]: it for it in r["items"]}
    assert "BTC" in items
    assert items["BTC"]["category"] == "altcoin"


def test_undervalued_news_surge(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        # latest=91, 30d ago=100 → -9%
        values = [round(91 + i * 0.3, 2) for i in range(30)]
        _seed_series(conn, "SOL", "SOL-USD", values)
        now = int(time.time())
        for i in range(8):
            conn.execute(
                "INSERT INTO raw_news (title, content, ts) VALUES (?,?,?)",
                ("SOL news", f"SOL drops {i}", now - i * 3600),
            )
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.undervalued_detector_handler())
    items = {it["symbol"]: it for it in r["items"]}
    assert "SOL" in items
    assert items["SOL"]["news_count_24h"] >= 3
    assert any("news_surge" in s for s in items["SOL"]["reasons"])


def test_undervalued_top_n_cap(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        for i in range(50):
            asset = f"TEST{i:03d}"
            values = [round(94 + j * 0.3, 2) for j in range(20)]
            _seed_series(conn, asset, asset + "-USD", values)
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.undervalued_detector_handler())
    assert len(r["items"]) <= 30


def test_rankings_empty(disc_db):
    r = asyncio.run(discovery.cross_asset_rankings_handler(timeframe="24h"))
    assert r["ok"] is True
    assert r["total_count"] == 0
    assert r["gainers"] == []
    assert r["losers"] == []
    assert r["crashes"] == []


def test_rankings_separates_gainers_losers(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        # BTC 上涨: latest=63500 (now), 7d ago=60000 → +5.8%
        _seed_series(conn, "BTC", "BTC-USD", [round(63500 - i * 500, 2) for i in range(7)])
        # ETH 下跌: latest=2300 (now), 7d ago=3000 → -23.3%
        _seed_series(conn, "ETH", "ETH-USD", [round(2300 + i * 100, 2) for i in range(7)])
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.cross_asset_rankings_handler(timeframe="7d", limit=5))
    assert r["total_count"] == 2
    gainers = {it["symbol"]: it for it in r["gainers"]}
    losers = {it["symbol"]: it for it in r["losers"]}
    assert "BTC" in gainers
    assert "ETH" in losers
    assert gainers["BTC"]["change_pct"] > 0
    assert losers["ETH"]["change_pct"] < 0


def test_rankings_crash_threshold(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        # DOGE 下跌: latest=0.07 (now), 7d ago=0.10 → -30%
        _seed_series(conn, "DOGE", "DOGE-USD", [round(0.07 + i * 0.005, 4) for i in range(7)])
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.cross_asset_rankings_handler(timeframe="7d", crash_threshold_pct=-10.0))
    crash_syms = {c["symbol"] for c in r["crashes"]}
    assert "DOGE" in crash_syms


def test_rankings_category_breakdown(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        # BTC 上: latest=60600, 7d ago=60000 → +1%
        _seed_series(conn, "BTC", "BTC-USD", [round(60600 - i * 100, 2) for i in range(7)])
        # XAU 下: latest=2295, 7d ago=2300 → -0.2%
        _seed_series(conn, "XAU", "XAU", [round(2295 + i * 5 * -1, 2) for i in range(7)])
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.cross_asset_rankings_handler(timeframe="7d"))
    assert "altcoin" in r["category_breakdown"]
    assert "safe_metal" in r["category_breakdown"]
    assert r["category_breakdown"]["altcoin"]["count"] == 1


def test_rankings_invalid_timeframe_falls_back(disc_db):
    r = asyncio.run(discovery.cross_asset_rankings_handler(timeframe="99h"))
    assert r["window_s"] == 86400


def test_rankings_excludes_macro_indicators(disc_db):
    conn = sqlite3.connect(disc_db)
    try:
        now = int(time.time())
        conn.execute(
            "INSERT INTO external_indicators (asset, indicator, value, ts) VALUES (?,?,?,?)",
            ("UST2Y", "UST2Y", 4.5, now),
        )
        conn.execute(
            "INSERT INTO external_indicators (asset, indicator, value, ts) VALUES (?,?,?,?)",
            ("UST2Y", "UST2Y", 4.0, now - 86400),
        )
        # BTC 上涨: latest=60600, 24h ago=60000 → +1%
        _seed_series(conn, "BTC", "BTC-USD", [round(60600 - i * 100, 2) for i in range(7)])
        conn.commit()
    finally:
        conn.close()
    r = asyncio.run(discovery.cross_asset_rankings_handler(timeframe="24h"))
    assert r["total_count"] == 1
    assert r["gainers"][0]["symbol"] == "BTC"


def test_fed_watch_structure(disc_db, monkeypatch):
    import fed_rate_expectation as fre

    def _fake():
        return {"ok": True, "dgs2_pct": 4.25, "dff_pct": 5.33,
                "ust5y_pct": 4.10, "ts": int(time.time()), "direction": "dovish"}

    monkeypatch.setattr(fre, "fetch_expectation", _fake)
    monkeypatch.setattr(fre, "_fetch_history_24h", lambda: {"points": []})
    r = asyncio.run(discovery.fed_watch_handler())
    assert r["ok"] is True
    assert len(r["sources"]) == 3
    src_names = [s["name"] for s in r["sources"]]
    assert "DGS2 (FRED)" in src_names
    assert r["derivation"]["direction"] == "dovish"
    assert r["derivation"]["implied"]["spread_bp"] == round((4.25 - 5.33) * 100, 1)
    assert r["derivation"]["implied"]["interpretation"] == "市场预期降息"
    assert "降息" in r["explain"] or "中性" in r["explain"]
    assert r["daemon_status"]["interval_s"] == 300


def test_fed_watch_hawkish_spread(disc_db, monkeypatch):
    import fed_rate_expectation as fre

    def _fake():
        return {"ok": True, "dgs2_pct": 5.50, "dff_pct": 5.33,
                "ust5y_pct": 5.40, "ts": int(time.time()), "direction": "hawkish"}

    monkeypatch.setattr(fre, "fetch_expectation", _fake)
    monkeypatch.setattr(fre, "_fetch_history_24h", lambda: {"points": []})
    r = asyncio.run(discovery.fed_watch_handler())
    assert r["derivation"]["implied"]["interpretation"] == "市场预期加息"
    assert "加息" in r["explain"]


def test_fed_watch_history_curve(disc_db, monkeypatch):
    import fed_rate_expectation as fre

    def _fake():
        return {"ok": True, "dgs2_pct": None, "dff_pct": None,
                "ust5y_pct": None, "ts": int(time.time()), "direction": "neutral"}

    pts = [{"ts": int(time.time()) - i * 600, "value": 4.2 + i * 0.01}
           for i in range(10)]
    monkeypatch.setattr(fre, "fetch_expectation", _fake)
    monkeypatch.setattr(fre, "_fetch_history_24h", lambda: {"points": pts})
    r = asyncio.run(discovery.fed_watch_handler())
    assert len(r["history_24h"]["ts"]) == 10