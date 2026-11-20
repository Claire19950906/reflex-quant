# -*- coding: utf-8 -*-
"""v475 可信度门槛闸：低可信度的分数不足以开启新仓位。

两个机制必须分开：
  - 权重打折（v472/v475）= "这个分数算多少"；
  - 门槛收紧（v475）      = "多确定才准开仓"。
这组测试钉住后者的三条硬约束：只收紧不放宽、封顶 0.10、无可信度时逐位不变。
"""
from __future__ import annotations

import json
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import decision_guard


def _row(fundamental=None) -> dict:
    ctx = json.dumps({"assets": {"COIN": {
        "status": "ok", "change_24h_pct": 1.0, "change_24h_str": "+1.00%",
        "funding_rate_pct": 0.01, "funding_rate_str": "+0.0100%",
        "basis_bps": None, "oi_change_pct": None,
        "stats_7d": {"trend": "Unknown", "atr_pct": 0.0, "atr_str": "N/A"},
    }}})
    row = {"sentiment_score": 0.0, "target_asset": "COIN", "decision_context": ctx}
    if fundamental is not None:
        row["fundamental"] = fundamental
    return row


def _fund(conf, score=0.07, kind=None):
    d = {"available": True, "score": score, "ticker": "COIN", "detail": "x",
         "components": {"path": "profitable"}, "evidence": [], "flags": []}
    if conf is not None:
        d.update({"dispersion": 0.84, "dispersion_level": "高", "confidence": conf,
                  "bull_weight": 0.25, "bear_weight": 0.6})
    if kind:
        d["kind"] = kind
    return d


def test_penalty_monotonic_and_capped():
    assert decision_guard._confidence_penalty(None) == 0.0
    assert decision_guard._confidence_penalty(1.0) == 0.0
    assert decision_guard._confidence_penalty(0.44) == 0.056
    assert decision_guard._confidence_penalty(0.0) == decision_guard._CONF_GATE_CAP


def test_low_confidence_raises_entry_threshold():
    out = decision_guard.evaluate_decision(_row(_fund(0.44)))
    th = out["thresholds"]
    assert th["confidence_penalty"] == 0.056
    assert th["buy_min_effective"] == round(th["buy_min_calibrated"] + 0.056, 4)
    assert th["sell_min_effective"] == round(th["sell_min_calibrated"] + 0.056, 4)
    assert out["confidence_gate"]["penalty"] == 0.056


def test_company_weight_now_also_discounted():
    """v475：权重打折不再只对 alt 生效 —— 公司票高分歧同样降权。"""
    out = decision_guard.evaluate_decision(_row(_fund(0.44)))
    w = out["factors"]["fundamental"]["weight"]
    assert w < decision_guard.DIRECTIONAL_WEIGHTS["fundamental"], w
    # 0.08 × (0.6 + 0.4×0.44) = 0.06208
    assert abs(w - 0.0621) < 0.001, w


def test_no_confidence_is_bit_identical():
    """老数据（无分歧度）必须逐位保持 v321 纯校准行为。"""
    out = decision_guard.evaluate_decision(_row(_fund(None)))
    th = out["thresholds"]
    assert th["confidence_penalty"] == 0.0
    assert th["buy_min_effective"] == th["buy_min_calibrated"]
    assert out["confidence_gate"] is None
    assert out["factors"]["fundamental"]["weight"] == decision_guard.DIRECTIONAL_WEIGHTS["fundamental"]


def test_alt_row_still_discounted():
    """alt 侧原有行为不能被这次重构改掉。"""
    out = decision_guard.evaluate_decision(_row(_fund(0.4, kind="alt")))
    w = out["factors"]["fundamental"]["weight"]
    assert abs(w - 0.08 * (0.6 + 0.4 * 0.4)) < 0.001, w
    assert out["confidence_gate"]["penalty"] == 0.06
