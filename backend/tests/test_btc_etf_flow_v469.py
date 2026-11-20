# -*- coding: utf-8 -*-
"""v469 BTC 现货 ETF 净流入口径测试。

ETF 净流入是"真金白银的增量资金"，不是交易所内部换手，方向最好讲：
20 日累计净流入的历史分位高 = 资金在进 = 偏多；分位低 = 在撤 = 偏空。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import alt_fundamental as af


def _db(vals):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE macro_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, category TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', metric_key TEXT NOT NULL, value REAL,
        unit TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ok',
        payload TEXT NOT NULL DEFAULT '{}')""")
    base = datetime.now() - timedelta(days=len(vals) + 2)   # 必须落在 _HIST_DAYS 窗口内
    conn.executemany(
        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(int((base + timedelta(days=i)).timestamp()), "etf_flow", "synth",
          "btc.etf_net_flow", v, "USD m", "ok", "{}") for i, v in enumerate(vals)])
    conn.commit()
    return conn


def test_etf_flow_component_exists():
    out = af.build("BTC", conn=_db([100.0] * 60), use_cache=False)
    assert out["components"].get("etf_flow") is not None


def test_heavy_inflow_is_bullish():
    """前面都是小额流出、最近 20 天转为大额流入 → 分位最高 → 必须正分。"""
    vals = [-50.0] * 60 + [300.0] * 20
    out = af.build("BTC", conn=_db(vals), use_cache=False)
    comp = out["components"]["etf_flow"]
    assert comp["score"] > 0.8, comp
    assert "净流入" in comp["detail"]


def test_heavy_outflow_is_bearish():
    vals = [50.0] * 60 + [-400.0] * 20
    out = af.build("BTC", conn=_db(vals), use_cache=False)
    assert out["components"]["etf_flow"]["score"] < -0.8, out["components"]["etf_flow"]


def test_short_series_does_not_score():
    """数据不足 30 个滚动窗口 → 不发分，写缺口。"""
    out = af.build("BTC", conn=_db([100.0] * 25), use_cache=False)
    assert out["components"].get("etf_flow") is None
    assert any("ETF 净流入样本不足" in g for g in out["gaps"]), out["gaps"]


def test_etf_gap_no_longer_claims_unavailable():
    """接通之后缺口里不许再写"ETF 不可得"，只保留 MVRV 这类真缺口。"""
    out = af.build("BTC", use_cache=False)
    assert not any("SOSOVALUE" in g for g in out["gaps"]), out["gaps"]
    assert any("MVRV" in g for g in out["gaps"]), out["gaps"]
