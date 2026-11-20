# -*- coding: utf-8 -*-
"""v473 影子仓位按分歧度打折。

三件事必须钉住：
  1. 公司侧老路径（confidence=None）行为**一点都不能变** —— 不能借影子账偷偷改公司票的仓位；
  2. 高分歧（低可信度）必须真的把乘数压下来，且压在 [0.70, 1.20] 里；
  3. 落库要能看见"这个仓位是按共识给的还是按吵架给的"。
"""
import json
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import fundamental_shadow as fs


def _guard(score, weight=0.08, available=True):
    return {"factors": {"fundamental": {"available": available, "score": score,
                                        "weight": weight, "explanation": "test"}},
            "thresholds": {"buy_min": 0.35, "sell_min": -0.35}}


def test_company_path_unchanged_without_confidence():
    """无 confidence 时与 v456 完全一致：1 + 0.20×score，截 [0.80, 1.20]。"""
    assert fs.sizing_multiplier(0.5, True) == 1.10
    assert fs.sizing_multiplier(-0.5, True) == 0.90
    assert fs.sizing_multiplier(1.0, True) == 1.20
    assert fs.sizing_multiplier(-1.0, True) == 0.80


def test_full_confidence_equals_base():
    """可信度 1 = 不打折，和公司侧口径对齐。"""
    assert fs.sizing_multiplier(0.5, True, 1.0) == fs.sizing_multiplier(0.5, True)


def test_low_confidence_shrinks_position():
    base = fs.sizing_multiplier(0.5, True)
    low = fs.sizing_multiplier(0.5, True, 0.41)      # USDJPY 实测可信度
    assert low < base
    assert round(base - low, 4) > 0.10, (base, low)


def test_clamped_into_band():
    """极端组合也要留在 [0.70, 1.20]，不能出现负数或 2 倍仓。"""
    for score in (-1.0, -0.5, 0.0, 0.5, 1.0):
        for conf in (0.0, 0.3, 0.7, 1.0):
            mult = fs.sizing_multiplier(score, True, conf)
            assert 0.70 <= mult <= 1.20, (score, conf, mult)


def test_unavailable_stays_one():
    assert fs.sizing_multiplier(0.9, False, 0.1) == 1.0
    assert fs.sizing_multiplier(None, True, 0.1) == 1.0


def test_snapshot_stores_dispersion():
    conn = sqlite3.connect(":memory:")
    comp = {"ticker": "BTC", "market": "ALT", "detail": "BTC 非公司基本面 +0.07",
            "dispersion": 0.643, "dispersion_level": "高", "confidence": 0.571}
    fs.record(conn, 1001, None, "BTC", _guard(0.07), "HOLD", components=comp)
    row = conn.execute(
        """SELECT dispersion, dispersion_level, confidence, sizing_mult
           FROM decision_fundamental_snapshot WHERE decision_id=1001""").fetchone()
    assert row[0] == 0.643 and row[1] == "高" and abs(row[2] - 0.571) < 1e-9
    # 0.014 → 1.0028；× (0.75 + 0.25×0.571)=0.8928 → ≈0.8955
    assert 0.85 < row[3] < 0.95, row[3]


def test_snapshot_company_row_has_null_dispersion():
    """公司侧没有分歧度：列必须是 NULL，且乘数与 v456 一致。"""
    conn = sqlite3.connect(":memory:")
    fs.record(conn, 1002, None, "AAPL", _guard(0.6), "BUY", components={"ticker": "AAPL"})
    row = conn.execute(
        """SELECT dispersion, dispersion_level, confidence, sizing_mult
           FROM decision_fundamental_snapshot WHERE decision_id=1002""").fetchone()
    assert row[0] is None and row[1] is None and row[2] is None
    assert row[3] == 1.12, row[3]


def test_components_json_keeps_dispersion_for_audit():
    """components 整份存 JSON，离散度字段也要在里面（复盘可复算）。"""
    conn = sqlite3.connect(":memory:")
    comp = {"ticker": "SOL", "dispersion": 0.47, "dispersion_level": "中", "confidence": 0.687}
    fs.record(conn, 1003, None, "SOL", _guard(-0.75), "SELL", components=comp)
    blob = conn.execute("SELECT components FROM decision_fundamental_snapshot"
                        " WHERE decision_id=1003").fetchone()[0]
    assert json.loads(blob)["dispersion"] == 0.47
