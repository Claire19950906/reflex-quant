# -*- coding: utf-8 -*-
"""v491 交易规则层：时段 / 整股 / 裸空。"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import time

import pytest

import account_ledger as al
import market_rules as mr
import trade_preflight as pf
from trident2 import store as t2store


def _ts(y, m, d, hh, mm, tz):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())


@pytest.fixture()
def conn(monkeypatch):
    monkeypatch.setenv("PAPER_INITIAL_EQUITY_USDT", "10000")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "100")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t2store.ensure_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, price REAL, ts INTEGER)")
    al.rebuild(c, apply=True)
    yield c
    c.close()


def _tick(conn, asset, price, ts=None):
    conn.execute("INSERT INTO market_ticks (symbol, price, ts) VALUES (?,?,?)",
                 (asset, price, int(ts or time.time())))
    conn.commit()


def test_classify_assets():
    assert mr.classify("BTC") == "crypto"
    assert mr.classify("XAU") == "perp"
    assert mr.classify("NVDA") == "us_stock"
    assert mr.classify("SKHYNIX") == "kr_stock"
    assert mr.classify("WTI") == "commodity"


def test_us_session_open_and_closed():
    t_open = _ts(2026, 11, 19, 10, 30, mr.ET)
    t_close = _ts(2026, 11, 19, 20, 0, mr.ET)
    t_weekend = _ts(2026, 11, 21, 10, 30, mr.ET)
    assert mr.session_open("NVDA", t_open)[0] is True
    assert mr.session_open("NVDA", t_close)[0] is False
    assert mr.session_open("NVDA", t_weekend)[0] is False


def test_crypto_always_open():
    for t in (0, _ts(2026, 11, 21, 3, 0, mr.ET)):
        assert mr.session_open("BTC", t)[0] is True


def test_short_only_for_derivatives():
    assert mr.allows_short("NVDA")[0] is False
    assert mr.allows_short("BTC")[0] is True


def test_round_lot_for_stocks():
    assert mr.round_qty("NVDA", 3.7) == 3.0
    assert mr.round_qty("NVDA", 0.4) == 0.0
    assert mr.round_qty("BTC", 0.12345) == 0.12345


def test_preflight_blocks_after_hours_stock(conn):
    t_close = _ts(2026, 11, 19, 20, 0, mr.ET)
    _tick(conn, "NVDA", 150.0, ts=t_close)
    out = pf.preflight(conn, "NVDA", "BUY", 1000.0, now=t_close)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_MARKET_CLOSED" for d in out["denies"])


def test_preflight_blocks_naked_short_stock(conn):
    t_open = _ts(2026, 11, 19, 10, 30, mr.ET)
    _tick(conn, "NVDA", 150.0, ts=t_open)
    out = pf.preflight(conn, "NVDA", "SELL", 1000.0, now=t_open)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_SHORT_FORBIDDEN" for d in out["denies"])


def test_preflight_blocks_less_than_one_share(conn):
    t_open = _ts(2026, 11, 19, 10, 30, mr.ET)
    _tick(conn, "NVDA", 150.0, ts=t_open)
    out = pf.preflight(conn, "NVDA", "BUY", 100.0, now=t_open)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_LOT_SIZE" for d in out["denies"])


def test_preflight_allows_stock_intraday(conn):
    t_open = _ts(2026, 11, 19, 10, 30, mr.ET)
    _tick(conn, "NVDA", 150.0, ts=t_open)
    out = pf.preflight(conn, "NVDA", "BUY", 1000.0, now=t_open)
    assert out["ok"] is True, out["denies"]


# ── v492 资产簇 & 连亏熔断 ─────────────────────────────────────────────
def _open(conn, asset, side, qty, entry, now=None):
    pid = t2store.insert_position(conn, asset=asset, side=side, qty=qty,
                                  entry_price=entry, entry_ts=int(now or time.time()))
    al.book_entry_fee(conn, pid)
    return pid


def test_cluster_blocks_same_bet(conn, monkeypatch):
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "300")
    monkeypatch.setenv("MAX_CLUSTER_WEIGHT_PCT", "80")
    _tick(conn, "BTC", 64000.0)
    _tick(conn, "ETH", 1800.0)
    _open(conn, "BTC", "long", 6000 / 64000.0, 64000.0)
    al.revalue_equity(conn)
    out = pf.preflight(conn, "ETH", "BUY", 3000.0)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_CLUSTER" for d in out["denies"])


def test_cluster_allows_different_bucket(conn, monkeypatch):
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "300")
    monkeypatch.setenv("MAX_CLUSTER_WEIGHT_PCT", "80")
    _tick(conn, "BTC", 64000.0)
    _tick(conn, "XAU", 3730.0)
    _open(conn, "BTC", "long", 6000 / 64000.0, 64000.0)
    al.revalue_equity(conn)
    out = pf.preflight(conn, "XAU", "BUY", 3000.0)
    assert out["ok"] is True, out["denies"]


def _close_loss(conn, asset, pnl, closed_ts):
    pid = _open(conn, asset, "long", 0.01, 100.0)
    conn.execute("UPDATE t2_positions SET status='closed', realized_pnl=?, closed_ts=? WHERE id=?",
                 (pnl, int(closed_ts), pid))
    conn.commit()


def test_loss_streak_blocks_new_entry(conn, monkeypatch):
    monkeypatch.setenv("CONSECUTIVE_LOSS_LIMIT", "3")
    monkeypatch.setenv("CONSECUTIVE_LOSS_PAUSE_MIN", "60")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "300")
    now = int(time.time())
    for i, ts in enumerate((now - 600, now - 900, now - 1200)):
        _close_loss(conn, "BTC", -10.0, ts)
    _tick(conn, "ETH", 1800.0, ts=now)
    out = pf.preflight(conn, "ETH", "BUY", 500.0, now=now)
    assert any(d["code"] == "PREFLIGHT_LOSS_STREAK" for d in out["denies"])


def test_loss_streak_expires(conn, monkeypatch):
    monkeypatch.setenv("CONSECUTIVE_LOSS_LIMIT", "3")
    monkeypatch.setenv("CONSECUTIVE_LOSS_PAUSE_MIN", "60")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "300")
    now = int(time.time())
    for ts in (now - 7200, now - 7500, now - 7800):
        _close_loss(conn, "BTC", -10.0, ts)
    al.rebuild(conn, apply=True)
    _tick(conn, "ETH", 1800.0, ts=now)
    out = pf.preflight(conn, "ETH", "BUY", 500.0, now=now)
    assert out["ok"] is True, out["denies"]


def test_loss_streak_does_not_block_closing(conn, monkeypatch):
    monkeypatch.setenv("CONSECUTIVE_LOSS_LIMIT", "3")
    monkeypatch.setenv("CONSECUTIVE_LOSS_PAUSE_MIN", "60")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "300")
    now = int(time.time())
    for ts in (now - 600, now - 900, now - 1200):
        _close_loss(conn, "BTC", -10.0, ts)
    al.rebuild(conn, apply=True)
    _tick(conn, "XAU", 3730.0, ts=now)
    _open(conn, "XAU", "long", 1.0, 3730.0)
    al.revalue_equity(conn)
    out = pf.preflight(conn, "XAU", "SELL", 1000.0, now=now)
    assert out["ok"] is True, out["denies"]


def test_health_flags_only_open_markets(tmp_path, monkeypatch):
    """收盘品种的旧 tick 不该算过期；24/7 的旧 tick 必须报。"""
    import trading_health as th
    path = str(tmp_path / "health.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    t2store.ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, price REAL, ts INTEGER)")
    # 钉死在 UTC 12:00（北京 20:00）：美股/A 股都收盘，只有 BTC 该更新。
    now = int(dt.datetime(2026, 11, 20, 12, 0, 0,
                          tzinfo=dt.timezone.utc).timestamp())
    conn.execute("INSERT INTO market_ticks VALUES ('NVDA', 150.0, ?)", (now - 5000,))
    conn.execute("INSERT INTO market_ticks VALUES ('002241', 10.0, ?)", (now - 5000,))
    conn.execute("INSERT INTO market_ticks VALUES ('BTC', 64000.0, ?)", (now - 1000,))
    conn.commit()
    conn.close()
    monkeypatch.setenv("HEALTH_TICK_STALE_SEC", "300")
    # 时钟必须钉死在这个时刻：美股与 A 股都收盘、只有 24/7 品种在交易。
    # 真实教训：全量套件在北京时间 04:5x 跑过一次，此时美股正开着，
    # NVDA 的旧 tick 理应收进 stale_open_markets —— 用例红得毫无道理，
    # 因为它其实是在断言"现在是白天收盘时段"。
    frozen = now
    import market_rules as _mr
    orig_session_open = _mr.session_open
    monkeypatch.setattr(_mr, "session_open",
                        lambda asset, ts=None: orig_session_open(asset, frozen))
    monkeypatch.setattr(th.time, "time", lambda: frozen)
    saved = dict(os.environ)
    try:
        h = th.build(path)      # 会读 .env 并写 os.environ；测试后必须还原，否则污染后续用例
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert any(x.startswith("BTC(") for x in h["stale_open_markets"])
    assert not any(x.startswith("NVDA(") for x in h["stale_open_markets"])
    assert not any(x.startswith("002241(") for x in h["stale_open_markets"])
