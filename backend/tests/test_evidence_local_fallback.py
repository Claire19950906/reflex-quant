"""v344 回归：无 K 线 fetcher 的资产必须能从本地库拿到硬数据证据。

背景（实测）：
    quote_sources.route("WTI") → {'market': 'domestic_spot', 'symbol': None}
    旧代码在这里直接 return ""，导致 WTI **永远没有 hard_data_evidence**，
    market_confirmation 恒为 unknown —— 而库里其实有 9,229 根 WTI 分钟线。

注意：conftest 会把 DB 隔离到测试库（TRIDENT_DB_PATH），
所以这里必须自己建表插数据，不能假设有真实行情。
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import db
from engine import ai_worker as aw

_SCHEMA = """CREATE TABLE IF NOT EXISTS ohlcv_intraday (
    asset TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL,
    close REAL, volume REAL, trades INTEGER, closed INTEGER, source TEXT)"""


def _seed(asset, rows):
    conn = sqlite3.connect(db.get_db_path())
    try:
        conn.execute(_SCHEMA)
        for r in rows:
            conn.execute(
                "INSERT INTO ohlcv_intraday VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
        conn.commit()
    finally:
        conn.close()


def _bar(asset, ts, o, h, l, c, v=1.0):
    return (asset, "1m", ts, o, h, l, c, v, 3, 1, "test")


def test_local_evidence_for_asset_without_symbol():
    now = int(time.time())
    _seed("WTI", [_bar("WTI", now - 60, 88.10, 88.30, 88.05, 88.28)]
          + [_bar("WTI", now - 60 * i, 88.0 + i * 0.01, 88.3, 87.9,
                  88.2 + i * 0.01) for i in range(2, 40)])
    ev = aw._build_evidence_from_local("WTI")
    assert ev, "WTI（route 无 symbol）应能从本地库拿到证据"
    assert ev.startswith("WTI")
    assert "close=88.2800" in ev, f"证据应含最新收盘价，实际: {ev[:80]}"


def test_unknown_asset_returns_empty():
    assert aw._build_evidence_from_local("NO_SUCH_ASSET_XYZ") == ""


def test_frozen_bar_is_flagged():
    """冻结报价（high==low）不能当成真实盘面，必须打"仅作记录"。"""
    now = int(time.time())
    _seed("FROZENX", [_bar("FROZENX", now - 60, 10.0, 10.0, 10.0, 10.0)])
    ev = aw._build_evidence_from_local("FROZENX")
    assert ev, "有数据时应返回证据"
    assert "数据质量提示" in ev, (
        "冻结/过期报价必须带数据质量提示，否则 0.00% 会被模型读成真实盘面")
