# -*- coding: utf-8 -*-
"""v463：执行层组合闸 + 开仓止损的口径测试。"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import execution_guards as eg


@pytest.fixture(autouse=True)
def _pin_guard_caps(monkeypatch):
    """把组合闸的上限钉在代码默认值（单品种 50% / 总杠杆 100%）。

    生产 `backend/.env` 现在把上限放到 60% / 200%（真实账户口径），conftest 会把
    .env 读进来，于是这些用 1 万权益做的口径用例全部算不出"撞上限"：
    200% 杠杆下 9984 已用 + 5000 新单 = 14984 < 20000，闸门当然放行。
    口径测试要的是"上限生效时怎么算"，不该被部署参数带着漂。
    """
    monkeypatch.setenv("MAX_ASSET_WEIGHT_PCT", "50")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "100")
    monkeypatch.delenv("ALLOW_HEDGE", raising=False)
    monkeypatch.setenv("DUP_ENTRY_GUARD", "enforce")


def _db(equity=10000.0):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    p = Path(f.name)
    c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE t2_account(id INTEGER PRIMARY KEY, cash REAL, equity REAL);
    CREATE TABLE t2_positions(id INTEGER PRIMARY KEY, asset TEXT, side TEXT, qty REAL,
                              entry_price REAL, status TEXT);
    """)
    c.execute("INSERT INTO t2_account VALUES(1,?,?)", (equity, equity))
    c.commit()
    c.close()
    return p


def _pos(p, asset, side, qty, entry, status="open", pid=1):
    c = sqlite3.connect(p)
    c.execute("INSERT INTO t2_positions VALUES(?,?,?,?,?,?)",
              (pid, asset, side, qty, entry, status))
    c.commit()
    c.close()


def test_duplicate_same_asset_same_side_rejected():
    p = _db()
    _pos(p, "ETH", "short", 2.8, 1780)
    d = eg.check_new_entry(sqlite3.connect(p), "ETH", "SELL", 5000)
    assert d["allow"] is False
    assert "duplicate_open" in d["reason"]
    p.unlink()


def test_opposite_side_rejected_by_default():
    p = _db()
    _pos(p, "ETH", "long", 2.8, 1780)
    d = eg.check_new_entry(sqlite3.connect(p), "ETH", "SELL", 3000)
    assert d["allow"] is False
    assert "opposite_open" in d["reason"]
    p.unlink()


def test_allow_hedge_switch_can_shrink_to_asset_cap():
    p = _db()
    _pos(p, "ETH", "long", 2.0, 1500)  # 3000 名义
    old = os.environ.get("ALLOW_HEDGE")
    os.environ["ALLOW_HEDGE"] = "1"
    try:
        d = eg.check_new_entry(sqlite3.connect(p), "ETH", "SELL", 5000)
        assert d["allow"] is True
        assert d["size_usdt"] == 2000  # 权益 50% = 5000，已有 3000
    finally:
        if old is None:
            os.environ.pop("ALLOW_HEDGE", None)
        else:
            os.environ["ALLOW_HEDGE"] = old
        p.unlink()


def test_different_asset_under_caps_allowed():
    p = _db()
    _pos(p, "XAU", "short", 0.5, 3000)  # 1500 名义
    d = eg.check_new_entry(sqlite3.connect(p), "BTC", "BUY", 5000)
    assert d["allow"] is True
    assert d["size_usdt"] == 5000
    p.unlink()


def test_gross_leverage_blocks_new_entry():
    p = _db()
    _pos(p, "XAU", "short", 1.0, 5000, pid=1)
    _pos(p, "ETH", "short", 2.8, 1780, pid=2)
    d = eg.check_new_entry(sqlite3.connect(p), "BTC", "BUY", 5000)
    assert d["allow"] is False
    assert "cap_exceeded" in d["reason"]
    p.unlink()


def test_shadow_mode_allows_but_warns():
    p = _db()
    _pos(p, "ETH", "short", 2.8, 1780)
    old = os.environ.get("DUP_ENTRY_GUARD")
    os.environ["DUP_ENTRY_GUARD"] = "shadow"
    try:
        d = eg.check_new_entry(sqlite3.connect(p), "ETH", "SELL", 5000)
        assert d["allow"] is True
        assert any("shadow" in w for w in d["warnings"])
    finally:
        if old is None:
            os.environ.pop("DUP_ENTRY_GUARD", None)
        else:
            os.environ["DUP_ENTRY_GUARD"] = old
        p.unlink()


def test_auto_stop_short_above_long_below():
    assert eg.auto_stop_price(100, "SELL") == 102.0
    assert eg.auto_stop_price(100, "BUY") == 98.0
    old = os.environ.get("AUTO_STOP_PCT")
    os.environ["AUTO_STOP_PCT"] = "1.5"
    try:
        assert eg.auto_stop_price(200, "short") == 203.0
    finally:
        if old is None:
            os.environ.pop("AUTO_STOP_PCT", None)
        else:
            os.environ["AUTO_STOP_PCT"] = old


def test_auto_stop_can_be_disabled():
    old = os.environ.get("AUTO_STOP_ENABLED")
    os.environ["AUTO_STOP_ENABLED"] = "0"
    try:
        assert eg.auto_stop_price(100, "SELL") is None
    finally:
        if old is None:
            os.environ.pop("AUTO_STOP_ENABLED", None)
        else:
            os.environ["AUTO_STOP_ENABLED"] = old
