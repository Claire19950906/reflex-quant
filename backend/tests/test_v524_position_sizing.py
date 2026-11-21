# -*- coding: utf-8 -*-
"""仓位分层（confidence-tier sizing）契约测试。

这一层把"信心"换成"金额"。两条不能破的规矩：
  · 缺信心不能变成"不许建仓"——缺字段走 1.0 透明兜底，不阻断
  · 任何信号都不许把名义金额放大到失控——clamp 必须真的夹住
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import position_sizing as ps  # noqa: E402


def test_select_multiplier_tiers_and_out_of_range():
    assert ps._select_multiplier(None) == 1.0
    assert ps._select_multiplier(0.1) == 0.5      # low
    assert ps._select_multiplier(0.3) == 1.0      # 阈值含边界
    assert ps._select_multiplier(0.69) == 1.0
    assert ps._select_multiplier(0.7) == 1.5      # high
    assert ps._select_multiplier(1.0) == 1.5
    # 越界与脏值 → 1.0，不许猜
    assert ps._select_multiplier(-0.1) == 1.0
    assert ps._select_multiplier(1.2) == 1.0
    assert ps._select_multiplier("bad") == 1.0


def test_compute_notional_scales_and_labels_tier():
    scaled, meta = ps.compute_notional({"evidence_confidence": 0.8}, 1000.0)
    assert scaled == 1500.0
    assert meta["tier"] == "high" and meta["reason"] == "tier_match"
    scaled_low, meta_low = ps.compute_notional({"evidence_confidence": 0.1}, 1000.0)
    assert scaled_low == 500.0 and meta_low["tier"] == "low"


def test_compute_notional_missing_confidence_is_transparent():
    # 缺字段不许阻断建仓：base 原样，tier=fallback
    scaled, meta = ps.compute_notional({}, 800.0)
    assert scaled == 800.0
    assert meta["tier"] == "fallback"
    assert meta["reason"] == "missing_confidence"


def test_compute_notional_clamp_really_clamps(monkeypatch):
    monkeypatch.setattr(ps, "POSITION_SIZING_TIERS", {0.0: 0.1, 0.9: 3.0})
    monkeypatch.setattr(ps, "POSITION_SIZING_MAX_MULT", 2.0)
    monkeypatch.setattr(ps, "POSITION_SIZING_MIN_MULT", 0.25)
    scaled, meta = ps.compute_notional({"evidence_confidence": 0.95}, 1000.0)
    assert scaled == 2000.0                 # 3.0 被夹到 2.0
    assert meta["tier"] == "clamped" and meta["reason"] == "clamp_applied"
    assert meta["raw_multiplier"] == 3.0


def test_compute_notional_kill_switch_bypasses_whole_layer(monkeypatch):
    monkeypatch.setattr(ps, "POSITION_SIZING_ENABLED", False)
    scaled, meta = ps.compute_notional({"evidence_confidence": 0.9}, 1000.0)
    assert scaled == 1000.0
    assert meta["reason"] == "disabled" and meta["enabled"] is False
