# -*- coding: utf-8 -*-
"""v472 分歧度（dispersion）：同一分数，"全体轻微偏多"和"一半极多一半极空"必须能分开。

口径：
  disp = sqrt( Σ w_i (s_i - score)^2 / Σ w_i )   —— 加权标准差，0=完全同向
  conf = max(0, 1 - disp/1.5)                    —— 可信度，用于 decision_guard 打折
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


def test_all_same_direction_low_dispersion_high_confidence():
    # 三条一起涨：tvl / fees 上升，COT 掉到低位（反向=看多），全同向
    conn = _db({"sol.tvl": [1e9 + i * 12e6 for i in range(90)],
                "sol.fees": [5e5 + i * 1.2e4 for i in range(90)],
                "sol.cot_noncomm_net": [float(5000 - i) for i in range(90)]})
    out = af.build("SOL", conn=conn, use_cache=False)
    assert out["dispersion_level"] == "低", out["dispersion"]
    assert out["confidence"] > 0.8
    assert out["bear_weight"] == 0.0


def test_opposing_components_high_dispersion():
    """一边极多一边极空：分数接近 0，但分歧度必须高、双方权重都要在场。"""
    conn = _db({"sol.tvl": [1e9 + i * 20e6 for i in range(90)],      # 极多
                "sol.fees": [2e6 - i * 1.6e4 for i in range(90)]})    # 极空（30 日 -24%）
    out = af.build("SOL", conn=conn, use_cache=False)
    assert out["dispersion_level"] == "高", out["dispersion"]
    assert out["bull_weight"] > 0.2 and out["bear_weight"] > 0.2
    assert out["confidence"] < 0.6


def test_dispersion_zero_when_identical_scores():
    """两个分量同权重、同分数 → 方差为 0，分歧度必须是 0、可信度 1。"""
    conn = _db({"sol.tvl": [1e9] * 90, "sol.fees": [5e5] * 90})
    out = af.build("SOL", conn=conn, use_cache=False)
    # 两条都是 0 分（无变化）→ 加权分 0，方差 0
    assert out["dispersion"] == 0.0
    assert out["confidence"] == 1.0


def test_confidence_monotonic_in_dispersion():
    """分歧越大，可信度越低（不能出现"更分歧反而更可信"）。"""
    a = af.build("SOL", conn=_db({"sol.tvl": [1e9 + i * 20e6 for i in range(90)],
                                  "sol.fees": [2e6 - i * 1.6e4 for i in range(90)]}),
                 use_cache=False)
    b = af.build("SOL", conn=_db({"sol.tvl": [1e9 + i * 20e6 for i in range(90)],
                                  "sol.fees": [2e6 + i * 2.2e3 for i in range(90)]}),
                 use_cache=False)
    assert a["dispersion"] > b["dispersion"]
    assert a["confidence"] < b["confidence"]


def test_unsupported_has_null_dispersion():
    """不支持/不足的品种也要有 dispersion 键（None），下游统一读，不用 hasattr 分支。"""
    out = af.build("PLATINUM", use_cache=False)
    assert out["dispersion"] is None
    assert out["confidence"] is None


def test_detail_carries_dispersion():
    """只读 detail 的人也必须看到分歧度，不能只看到一个数字。"""
    out = af.build("BTC", use_cache=False)
    if out["available"]:
        assert "分歧度" in out["detail"]
        assert "可信度" in out["detail"]
