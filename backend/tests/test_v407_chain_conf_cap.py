# -*- coding: utf-8 -*-
"""v407 · 因果链置信度口径用例

锁三件事（都是 2026-11-17 实测出来的真问题）：
  1. BOTH 链没有方向主张 → 它的"命中率"是幅度口径，不许把 confidence 抬到 0.95；
  2. 无样本 / 样本 <5 的链不许保留 1.0 假置信（旧库 450 条无样本链全是 1.0）；
  3. 有方向的链用 Beta(2,2) 后验均值，幂等（重复跑不会指数衰减）。
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import chain_steps_verify as csv_mod


def _stat(n=10, hit=10, moved=10, miss=0):
    return {"n": n, "hit": hit, "miss": miss, "moved": moved,
            "sources": Counter({"news_events": n})}


def test_both_direction_confidence_is_capped():
    chain = {"id": 1, "expected_direction": "BOTH", "confidence": 1.0}
    _steps, patch = csv_mod.build_steps(chain, _stat(n=10, hit=10))
    assert patch["confidence"] <= csv_mod.CONF_BOTH_MAX
    assert patch["confidence"] <= 0.55


def test_directional_confidence_uses_posterior_mean():
    chain = {"id": 2, "expected_direction": "UP", "confidence": 1.0}
    _steps, patch = csv_mod.build_steps(chain, _stat(n=5, hit=5))
    # Beta(2,2): (5+2)/(5+4) = 0.778，不是点估计 1.0 → 0.95
    assert 0.7 <= patch["confidence"] <= 0.82


def test_directional_confidence_is_idempotent():
    chain = {"id": 3, "expected_direction": "DOWN"}
    _, p1 = csv_mod.build_steps({**chain, "confidence": 1.0}, _stat(n=20, hit=16))
    _, p2 = csv_mod.build_steps({**chain, "confidence": p1["confidence"]}, _stat(n=20, hit=16))
    assert abs(p1["confidence"] - p2["confidence"]) < 1e-9


def test_no_sample_confidence_is_not_one():
    chain = {"id": 4, "expected_direction": "UP", "confidence": 1.0}
    _steps, patch = csv_mod.build_steps(chain, None)
    assert patch["no_data"] is True
    assert patch["confidence"] <= csv_mod.CONF_UNVERIFIED_MAX


def test_thin_sample_confidence_is_not_one():
    chain = {"id": 5, "expected_direction": "UP", "confidence": 1.0}
    _steps, patch = csv_mod.build_steps(chain, _stat(n=2, hit=2))
    assert patch["confidence"] <= csv_mod.CONF_UNVERIFIED_MAX
