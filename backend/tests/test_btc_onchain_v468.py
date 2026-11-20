# -*- coding: utf-8 -*-
"""v468 BTC 链上活跃度分量测试。

背景：加密缺"贵不贵"的估值锚（MVRV/已实现市值/交易所余额都要付费），
但"还有没有人在用"是免费的：blockchain.info 给日频活跃地址数、交易笔数。
符号是**显式假设**（活跃度扩张=偏多），测试要同时钉住方向和"缺数据不发分"。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import alt_fundamental as af


def _db(series):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE macro_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, category TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', metric_key TEXT NOT NULL, value REAL,
        unit TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ok',
        payload TEXT NOT NULL DEFAULT '{}')""")
    base = datetime(2026, 1, 1)
    rows = []
    for key, vals in series.items():
        for i, v in enumerate(vals):
            rows.append((int((base + timedelta(days=i)).timestamp()), "test", "synth",
                         key, v, "count", "ok", "{}"))
    conn.executemany(
        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
        " VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_btc_has_onchain_components_eth_does_not():
    """链上是 BTC 专属数据，ETH 不许蹭（ETH 的链上源没接）。"""
    conn = _db({"btc.onchain_addr": [400000.0 + i * 400 for i in range(90)],
                "btc.onchain_tx": [700000.0 + i * 900 for i in range(90)]})
    btc = af.build("BTC", conn=conn, use_cache=False)
    assert {"onchain_addr", "onchain_tx"} <= set(btc["components"])
    eth = af.build("ETH", conn=conn, use_cache=False)
    assert "onchain_addr" not in eth["components"]
    assert "onchain_tx" not in eth["components"]


def test_rising_activity_is_bullish():
    """活跃地址 30 日明显上升 → 该分量必须为正。"""
    conn = _db({"btc.onchain_addr": [400000.0 + i * 1200 for i in range(90)]})
    out = af.build("BTC", conn=conn, use_cache=False)
    assert out["components"]["onchain_addr"]["score"] > 0, out["components"]["onchain_addr"]


def test_falling_activity_is_bearish():
    conn = _db({"btc.onchain_addr": [600000.0 - i * 1200 for i in range(90)]})
    out = af.build("BTC", conn=conn, use_cache=False)
    assert out["components"]["onchain_addr"]["score"] < 0, out["components"]["onchain_addr"]


def test_missing_onchain_goes_to_gaps_not_zero():
    """没有链上数据时不许给 0 分充数，必须写进 gaps。"""
    conn = _db({"btc.funding": [0.01] * 90})
    out = af.build("BTC", conn=conn, use_cache=False)
    # 分量槽位在、但值是 None（下游按 None 过滤，不会当 0 分参与加权）
    assert out["components"].get("onchain_addr") is None
    assert any("活跃地址" in g for g in out["gaps"]), out["gaps"]


def test_mvrv_gap_still_declared():
    """链上活动已接入，但"贵不贵"的估值锚（MVRV/交易所余额）仍然没有，缺口必须还在。"""
    out = af.build("BTC", use_cache=False)
    assert any("MVRV" in g for g in out["gaps"]), out["gaps"]
