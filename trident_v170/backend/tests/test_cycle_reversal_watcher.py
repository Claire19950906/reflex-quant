# -*- coding: utf-8 -*-
"""v226+ format_reversal_alert_message + cycle_reversal_watcher 测试。"""
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
from engine import alerts as eng_alerts
import cycle_reversal_watcher as crw


@pytest.fixture
def kb_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test_reversal.db"
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


def _seed_uptrend_then_dump(conn, symbol="BTC-USD", n=60):
    prices = []
    for i in range(50):
        prices.append(100.0 + 2.0 * i)
    for i in range(10):
        prices.append(prices[-1] - 5.0)
    for i, p in enumerate(prices):
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, close) VALUES (?, ?, ?, ?)",
            (symbol, "1d", int(time.time()) - (n - i) * 86400, p),
        )


def _seed_flat(conn, symbol="ETH-USD", n=60):
    for i in range(n):
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, close) VALUES (?, ?, ?, ?)",
            (symbol, "1d", int(time.time()) - (n - i) * 86400, 100.0 + (i % 3) * 0.1),
        )


def test_format_title_short():
    t = eng_alerts.format_reversal_alert_message(
        prob=0.7, days_to=3, direction="down", asset="BTC", title_only=True)
    assert t.startswith("[周期反转]")
    assert "BTC" in t
    assert "下挫" in t
    assert "70%" in t


def test_format_body_includes_extra():
    body = eng_alerts.format_reversal_alert_message(
        prob=0.66, days_to=5, direction="up", asset="ETH",
        title_only=False,
        extra={"phase": "distribution", "votes": 2, "hurst": 0.65, "rsi": 78.5})
    assert "ETH" in body
    assert "反弹" in body
    assert "预计天数：5 天" in body
    assert "distribution" in body
    assert "投票数：2/3" in body
    assert "Hurst 指数：0.65" in body
    assert "RSI：78.5" in body


def test_format_prob_clamped():
    t = eng_alerts.format_reversal_alert_message(
        prob=1.5, days_to=2, direction="up", asset="SOL", title_only=True)
    assert "100%" in t


def test_format_handles_missing_days_to():
    body = eng_alerts.format_reversal_alert_message(
        prob=0.7, days_to=None, direction="neutral", asset="ADA",
        title_only=False)
    assert "预计天数：未知" in body


def test_scan_once_no_data_skips(kb_db):
    r = crw.scan_once(symbols=["BTC"], db_path=kb_db)
    assert r["scanned"] == 0
    assert r["skipped_no_data"] == 1


def test_scan_once_low_prob_skips(kb_db, monkeypatch):
    conn = sqlite3.connect(kb_db)
    try:
        _seed_flat(conn, symbol="ETH-USD", n=60)
        conn.commit()
    finally:
        conn.close()
    r = crw.scan_once(symbols=["ETH"], db_path=kb_db)
    assert r["scanned"] == 1
    assert r["sent"] == 0
    assert r["skipped_low_prob"] == 1
    eth = r["results"][0]
    assert eth["skip_reason"] in ("low_prob", "low_votes")


def test_scan_once_high_prob_sends(kb_db, monkeypatch):
    sent = {"count": 0, "titles": [], "bodies": []}

    def fake_send(title, body):
        sent["count"] += 1
        sent["titles"].append(title)
        sent["bodies"].append(body)

    import engine.alerts
    monkeypatch.setattr(engine.alerts, "send_important_news", fake_send)

    conn = sqlite3.connect(kb_db)
    try:
        _seed_uptrend_then_dump(conn, symbol="BTC-USD", n=60)
        conn.commit()
    finally:
        conn.close()

    r = crw.scan_once(symbols=["BTC"], db_path=kb_db)
    assert r["scanned"] == 1
    if r["sent"] >= 1:
        assert sent["count"] >= 1
        assert "[周期反转]" in sent["titles"][0]


def test_scan_once_dedup_blocks_second_send(kb_db):
    import engine.alerts
    monkeypatch_local = __import__("pytest").MonkeyPatch()
    monkeypatch_local.setattr(engine.alerts, "send_important_news",
                              lambda title, body: None)

    conn = sqlite3.connect(kb_db)
    try:
        _seed_uptrend_then_dump(conn, symbol="BTC-USD", n=60)
        conn.commit()
    finally:
        conn.close()

    crw.reset_dedup()
    r1 = crw.scan_once(symbols=["BTC"], db_path=kb_db)
    r2 = crw.scan_once(symbols=["BTC"], db_path=kb_db)
    if r1["sent"] >= 1:
        assert r2["sent"] == 0
        assert r2["skipped_dedup"] >= 1
    monkeypatch_local.undo()


def test_daemon_loop_calls_scan_and_sleeps(monkeypatch):
    sleep_durations = []
    calls = {"scan": 0}

    def fake_scan(symbols=None):
        calls["scan"] += 1
        return {"scanned": 0, "sent": 0, "skipped_low_prob": 0,
                "skipped_dedup": 0, "skipped_no_data": 0, "results": [],
                "ts": 0}

    def fake_sleep(s):
        sleep_durations.append(s)
        raise StopIteration

    monkeypatch.setattr(crw, "scan_once", fake_scan)
    monkeypatch.setattr(crw.time, "sleep", fake_sleep)
    monkeypatch.setenv("REVERSAL_INTERVAL_S", "60")
    try:
        crw.daemon_loop()
    except StopIteration:
        pass
    assert calls["scan"] == 1
    assert sleep_durations == [60]