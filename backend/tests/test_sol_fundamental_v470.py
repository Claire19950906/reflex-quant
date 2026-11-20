# -*- coding: utf-8 -*-
"""v470 SOL 非公司基本面的口径测试。

SOL 的独立性来自三族：链上 TVL（资本）、链上手续费（活动）、期货持仓（拥挤度）。
符号都写死在这里：TVL/手续费扩张=偏多，持仓越拥挤越脆弱=反向。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import alt_fundamental as af


def _db(series):
    """series: {metric_key: [values...]}，按天铺，最新一天贴近今天（别被 _HIST_DAYS 裁掉）。"""
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


def test_sol_available_with_three_families():
    conn = _db({
        "sol.tvl": [1e9 + i * 5e6 for i in range(80)],
        "sol.fees": [5e5 + i * 2e3 for i in range(80)],
        "sol.cot_noncomm_net": [1000.0 + i for i in range(80)],
    })
    out = af.build("SOL", conn=conn, use_cache=False)
    assert out["available"] is True
    assert {"tvl", "fees", "cot"} <= set(out["components"])


def test_tvl_and_fees_expansion_is_bullish():
    conn = _db({
        "sol.tvl": [1e9 + i * 20e6 for i in range(80)],   # 每天 +2%
        "sol.fees": [5e5 + i * 1.5e4 for i in range(80)],  # 每天 +3%
    })
    out = af.build("SOL", conn=conn, use_cache=False)
    assert out["components"]["tvl"]["score"] > 0.5
    assert out["components"]["fees"]["score"] > 0.3


def test_tvl_and_fees_contraction_is_bearish():
    conn = _db({
        "sol.tvl": [1e9 - i * 15e6 for i in range(80)],
        "sol.fees": [5e5 - i * 1.2e4 for i in range(80)],
    })
    out = af.build("SOL", conn=conn, use_cache=False)
    assert out["components"]["tvl"]["score"] < -0.5
    assert out["components"]["fees"]["score"] < -0.3


def test_crowded_cot_leans_bearish():
    """净多从低位一路刷到历史最高 → 拥挤度最高 → 分量必须是负。"""
    conn = _db({"sol.cot_noncomm_net": [float(i) for i in range(80)]})
    out = af.build("SOL", conn=conn, use_cache=False)
    assert out["components"]["cot"]["score"] < -0.9


def test_single_family_still_insufficient():
    """只有 TVL 一条，缺另一族 → 不发分（v460 的硬门槛仍生效）。"""
    conn = _db({"sol.tvl": [1e9 + i * 5e6 for i in range(80)]})
    out = af.build("SOL", conn=conn, use_cache=False)
    assert out["available"] is False
    assert out["reason"] == "insufficient"


def test_alias_routes_solana():
    assert af.canonical("SOLANA") == "SOL"
    assert af.canonical("solusdt") == "SOL"
    assert af.canonical("索拉纳") == "SOL"


def test_sol_reports_honest_gaps():
    """没接的东西必须写在缺口里，不许沉默。"""
    out = af.build("SOL", use_cache=False)
    blob = " ".join(out["gaps"]) if out["gaps"] else ""
    if "insufficient" in out["detail"] or out["reason"] == "insufficient":
        blob = " ".join(out.get("gaps") or [])
    assert any("活跃地址" in g or "质押率" in g for g in (out["gaps"] or []))
