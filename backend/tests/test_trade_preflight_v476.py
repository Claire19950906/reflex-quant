# -*- coding: utf-8 -*-
"""v476 交易前置闸：账本/行情/持仓数/杠杆/run 唯一。"""
from __future__ import annotations

import sqlite3
import time

import pytest

import account_ledger as al
import trade_preflight as pf
from trident2 import store as t2store

pytestmark = pytest.mark.smoke  # 快速门禁集


@pytest.fixture()
def conn(monkeypatch):
    monkeypatch.setenv("PAPER_INITIAL_EQUITY_USDT", "10000")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "100")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
    monkeypatch.setenv("PREFLIGHT_CRYPTO_MAX_AGE_SEC", "120")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t2store.ensure_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, price REAL, ts INTEGER)")
    # 账本先对平，避免 R3 干扰单项断言
    al.rebuild(c, apply=True)
    yield c
    c.close()


def _tick(conn, asset, price, age_s=0):
    conn.execute("INSERT INTO market_ticks (symbol, price, ts) VALUES (?,?,?)",
                 (asset, price, int(time.time()) - age_s))
    conn.commit()


def _open(conn, asset, side, qty, entry):
    pid = t2store.insert_position(conn, asset=asset, side=side, qty=qty,
                                  entry_price=entry, entry_ts=int(time.time()))
    al.book_entry_fee(conn, pid)
    return pid


def test_preflight_passes_when_clean(conn):
    _tick(conn, "XAU", 3737.0)
    out = pf.preflight(conn, "XAU", "BUY", 1000.0)
    assert out["ok"] is True, out["denies"]


def test_preflight_denies_stale_mark(conn):
    _tick(conn, "XAU", 3737.0, age_s=600)
    out = pf.preflight(conn, "XAU", "BUY", 1000.0)
    assert out["ok"] is False
    assert {d["code"] for d in out["denies"]} == {"PREFLIGHT_MARK"}


def test_preflight_denies_when_no_mark(conn):
    out = pf.preflight(conn, "XAU", "BUY", 1000.0)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_MARK" for d in out["denies"])


def test_preflight_denies_when_equity_not_marked(conn):
    # 打开一笔大幅浮亏持仓但故意不重估账户 → R3
    _open(conn, "BTC", "long", 0.1, 60000.0)
    _tick(conn, "BTC", 61000.0)
    conn.execute("UPDATE t2_account SET equity=10000 WHERE id=1")
    conn.commit()
    out = pf.preflight(conn, "BTC", "BUY", 100.0)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_EQUITY" for d in out["denies"])


def test_preflight_denies_leverage_breach(conn):
    _tick(conn, "BTC", 60000.0)
    _open(conn, "BTC", "long", 0.16, 60000.0)  # 9,600 名义 ≈ 96% 权益
    out = pf.preflight(conn, "BTC", "BUY", 1000.0)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_LEVERAGE" for d in out["denies"])


def test_preflight_denies_max_open(conn, monkeypatch):
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    _tick(conn, "XAU", 3737.0)
    _open(conn, "XAU", "long", 0.1, 3737.0)
    out = pf.preflight(conn, "XAU", "BUY", 100.0)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_MAX_OPEN" for d in out["denies"])


def test_preflight_denies_duplicate_runs(conn):
    _tick(conn, "XAU", 3737.0)
    conn.execute("CREATE TABLE paper_trading_runs (id INTEGER PRIMARY KEY, status TEXT, "
                 "strategy_id INTEGER, spec_version TEXT, started_at TEXT)")
    conn.execute("INSERT INTO paper_trading_runs VALUES (1,'RUNNING',1,'a','x'),"
                 "(2,'RUNNING',1,'a','y')")
    conn.commit()
    out = pf.preflight(conn, "XAU", "BUY", 100.0)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_RUNS" for d in out["denies"])
