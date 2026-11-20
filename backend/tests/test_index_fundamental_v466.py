"""v466 — 股指估值分量（SPX / CAPE）回归测试。

钉死四件事：
  1. 贵就是空：CAPE 处于 30 年高位 → 负分；低位 → 正分（用分位，不是绝对 CAPE）。
  2. 股债性价比：盈利收益率−10Y 越高（股票相对债券越便宜）→ 越偏多。
  3. **同一个信用利差，对股指和对国债符号相反**：走阔＝利空股指、利多国债。
  4. 单一族不发分：只有 CAPE 没有利率/信用 → 判不可得；NDX/沪深300 明确 unsupported。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import alt_fundamental as af
import fundamental_factor

_SCHEMA = """
CREATE TABLE macro_snapshots (
    ts INTEGER, category TEXT, source TEXT, metric_key TEXT,
    value REAL, unit TEXT, status TEXT, payload TEXT
);
CREATE TABLE external_indicators (
    id INTEGER PRIMARY KEY, asset TEXT, indicator TEXT, value REAL,
    ts INTEGER, source TEXT, regime_tags TEXT, meta TEXT, ingested_at INTEGER
);
"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    return conn


def _seed(conn, key, values, *, step_days=1, category="fred") -> None:
    now = int(time.time())
    n = len(values)
    for i, v in enumerate(values):
        ts = now - (n - 1 - i) * step_days * 86400
        conn.execute(
            "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ts, category, "test", key, float(v), "", "ok", "{}"))


def _ramp(start, end, n=120) -> list:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def _spx_db(*, cape_end=38.6, dgs10=5.1, hy_start=3.3, hy_end=3.43) -> sqlite3.Connection:
    conn = _db()
    _seed(conn, "spx.cape", _ramp(16.0, cape_end, 120), step_days=30, category="cape")
    _seed(conn, "dgs10", [dgs10] * 120, step_days=30)   # 与 CAPE 同频，才能按月对齐
    _seed(conn, "hy_oas", _ramp(hy_start, hy_end, 60))
    return conn


# ─────────── 1. 贵=空 / 便宜=多 ───────────

def test_expensive_cape_is_bearish():
    out = af.build("SPX", conn=_spx_db(cape_end=44.0), use_cache=False)
    assert out["available"] is True
    assert out["components"]["cape"]["score"] < 0
    assert out["score"] < 0


def test_cheap_cape_is_bullish():
    """CAPE 从 44 一路回落到 16（历史低位）+ 股债性价比高 → 必须为正。"""
    conn = _db()
    _seed(conn, "spx.cape", _ramp(44.0, 16.0, 120), step_days=30, category="cape")
    _seed(conn, "dgs10", [3.0] * 120, step_days=30)   # 利率掉到 3%，盈利收益率相对更高
    _seed(conn, "hy_oas", _ramp(4.0, 3.0, 60))  # 利差收窄 = 风险偏好好
    out = af.build("SPX", conn=conn, use_cache=False)
    assert out["available"] is True
    assert out["components"]["cape"]["score"] > 0
    assert out["components"]["erp"]["score"] > 0
    assert out["components"]["credit"]["score"] > 0
    assert out["score"] > 0


def test_erp_high_is_bullish():
    """跌下来的股票 + 低利率 → 盈利收益率−10Y 处在高位 → ERP 分量更偏多。

    注意分位对"整体平移"不敏感（全序列一起加一个常数，末位分位不变），
    所以必须让两个库的**形状**不同：一个末位便宜、一个末位昂贵。
    """
    lo = _db()   # 末位 CAPE 掉到 20（便宜）+ 利率 3%
    _seed(lo, "spx.cape", [40.0] * 119 + [20.0], step_days=30, category="cape")
    _seed(lo, "dgs10", [3.0] * 120, step_days=30)
    hi = _db()   # 末位 CAPE 冲到 40（贵）+ 利率 6%
    _seed(hi, "spx.cape", [20.0] * 119 + [40.0], step_days=30, category="cape")
    _seed(hi, "dgs10", [6.0] * 120, step_days=30)
    a = af.build("SPX", conn=lo, use_cache=False)["components"]["erp"]["score"]
    b = af.build("SPX", conn=hi, use_cache=False)["components"]["erp"]["score"]
    assert a > b


# ─────────── 2. 同一个变量，两个市场符号相反 ───────────

def test_credit_spread_sign_is_opposite_for_index_and_bond():
    """HY 利差走阔：对国债是避险买盘（正），对股指是风险偏好恶化（负）。"""
    wid = _ramp(3.0, 4.5, 60)
    idx = af.build("SPX", conn=_spx_db(hy_start=3.0, hy_end=4.5), use_cache=False)
    conn = _db()
    _seed(conn, "hy_oas", wid)
    _seed(conn, "tips10y", _ramp(2.0, 2.5, 60))
    _seed(conn, "dgs10", _ramp(4.5, 5.1, 60))
    _seed(conn, "dgs30", _ramp(5.0, 5.46, 60))
    bond = af.build("UST", conn=conn, use_cache=False)
    assert idx["components"]["credit"]["score"] < 0
    assert bond["components"]["credit"]["score"] > 0


# ─────────── 3. 门槛与明确不支持 ───────────

def test_cape_alone_is_not_enough():
    conn = _db()
    _seed(conn, "spx.cape", _ramp(16.0, 38.6, 120), step_days=30, category="cape")
    out = af.build("SPX", conn=conn, use_cache=False)
    assert out["available"] is False
    assert out["reason"] == "insufficient"


def test_ndx_proxy_is_labeled_and_hsi_stays_unsupported():
    """v467: NDX 允许用 SPX CAPE 代理，但必须在 evidence/gaps 里写明"代理"，不能装成自己的估值。
    沪深300 仍然没有同口径数据，必须明确 unsupported。"""
    import sqlite3
    from datetime import datetime, timedelta
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE macro_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, category TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', metric_key TEXT NOT NULL, value REAL,
        unit TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ok',
        payload TEXT NOT NULL DEFAULT '{}')""")
    base = datetime(2021, 1, 1)
    conn.executemany(
        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(int((base + timedelta(days=30 * i)).timestamp()), "valuation", "synth",
          "spx.cape", 20.0 + i * 0.3, "x", "ok", "{}") for i in range(60)])
    conn.executemany(
        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(int((base + timedelta(days=30 * i)).timestamp()), "rates", "synth",
          "dgs10", 3.5 + i * 0.01, "%", "ok", "{}") for i in range(60)])
    conn.commit()
    ndx = af.build("NDX", conn=conn, use_cache=False)
    assert ndx["available"] is True
    blob = " ".join(ndx["evidence"]) + " " + " ".join(ndx["gaps"])
    assert "代理" in blob
    assert any("NDX 自身估值" in g for g in ndx["gaps"])
    # v467: 沪深300 已接入（中证指数官方 PE + EPS 代理），这里改为验证"仍未接入"的恒指
    for symbol in ("HSI", "VIX"):
        out = af.build(symbol, use_cache=False)
        assert out["available"] is False, symbol
        assert out["reason"] == "unsupported", symbol


def test_alias_routing():
    assert af.canonical("标普500") == "SPX"
    assert af.canonical("SP500") == "SPX"
    assert af.canonical("GSPC") == "SPX"
    out = fundamental_factor.build("SPX", use_cache=False)
    assert out.get("kind") == "alt"
    assert out.get("reason") != "no_data"
