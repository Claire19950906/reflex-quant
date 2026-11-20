# -*- coding: utf-8 -*-
"""v471 ETH 链上维度（TVL + 手续费）口径测试。

ETH 之前只有场内资金面，这三条测试钉住"链上维度真的接上了、符号没接反、品种之间不串味"。
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
    n = max(len(v) for v in series.values())
    base = datetime.now() - timedelta(days=n + 2)
    rows = []
    for key, vals in series.items():
        for i, v in enumerate(vals):
            rows.append((int((base + timedelta(days=i)).timestamp()), "test", "synth",
                         key, v, "", "ok", "{}"))
    conn.executemany(
        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
        " VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_eth_has_onchain_components():
    conn = _db({"eth.tvl": [3e10 + i * 2e8 for i in range(80)],
                "eth.fees": [3e5 + i * 1e3 for i in range(80)]})
    out = af.build("ETH", conn=conn, use_cache=False)
    assert out["components"].get("tvl") is not None
    assert out["components"].get("fees") is not None


def test_eth_tvl_growth_is_bullish():
    conn = _db({"eth.tvl": [3e10 + i * 4e8 for i in range(80)]})   # 每天 +1.3%
    out = af.build("ETH", conn=conn, use_cache=False)
    assert out["components"]["tvl"]["score"] > 0.5


def test_eth_fee_contraction_is_bearish():
    conn = _db({"eth.fees": [8e5 - i * 2e3 for i in range(80)]})
    out = af.build("ETH", conn=conn, use_cache=False)
    assert out["components"]["fees"]["score"] < -0.3


def test_eth_without_onchain_is_thin():
    """只有场内三条的老口径，仍须能出分（不能因为新增分量把老路径搞崩）。"""
    conn = _db({"eth.funding": [0.01] * 80})
    out = af.build("ETH", conn=conn, use_cache=False)
    assert out["components"].get("tvl") is None
    assert any("TVL 样本不足" in g for g in out["gaps"])


def test_eth_keys_do_not_leak_into_btc_or_sol():
    """ETH 的链上数据不能被 BTC/SOL 蹭到。"""
    conn = _db({"eth.tvl": [3e10] * 80, "eth.fees": [5e5] * 80})
    for symbol in ("BTC", "SOL"):
        out = af.build(symbol, conn=conn, use_cache=False)
        tvl = out["components"].get("tvl")
        assert tvl is None, symbol


def test_eth_gap_still_names_staking():
    out = af.build("ETH", use_cache=False)
    assert any("质押" in g for g in (out["gaps"] or []))
