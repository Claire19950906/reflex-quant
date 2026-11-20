#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v329_chain_steps.py — 因果链步骤化 + 纠正写回 (v329) 的口径测试
==================================================================
守四件事:
  1. 结构: 每条链都产出 ≥3 步, 每步必须含 step/mechanism/proxy/threshold/window/verdict
     (frozen_ruler_full 的铁律6 判据, 缺一个字段就判"结构缺失");
  2. 没样本不乱判: 0 条 outcomes → 三步全"不可分离", 且不升版本号;
  3. 样本不够不下结论: n<5 时步3 一律"不可分离";
  4. 纠正边界: 命中率 ≤45% 且 n≥8 才纠下线; 置信度夹在 [0.2, 0.95]。
  · 纯离线 (:memory: 造数据), 不碰生产库。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import chain_steps_verify as csv

STEP_KEYS = ["step", "mechanism", "proxy", "threshold", "window", "verdict"]


def _chain(**kw):
    base = {"id": 1, "cause": "测试链", "expected_direction": "UP",
            "confidence": 0.5, "enabled": 1, "active": 1, "version": 1}
    base.update(kw)
    return base


def _stat(n, hit, moved=None, mag_n=0, mag_hit=0, src="news_events", pdir="UP"):
    from collections import Counter
    return {"n": n, "hit": hit, "miss": n - hit, "mag_n": mag_n, "mag_hit": mag_hit,
            "moved": n if moved is None else moved,
            "sources": Counter({src: n}), "pdir": Counter({pdir: n})}


def test_every_step_has_required_keys():
    steps, _ = csv.build_steps(_chain(), _stat(10, 8))
    assert len(steps) >= 3
    for st in steps:
        for k in STEP_KEYS:
            assert k in st and str(st[k]).strip(), f"缺字段 {k}"


def test_no_samples_marks_all_indistinguishable():
    steps, patch = csv.build_steps(_chain(), None)
    assert [s["verdict"] for s in steps] == ["不可分离"] * 3
    assert patch["no_data"] is True
    assert "steps_json" in patch


def test_no_samples_does_not_bump_version():
    _steps, patch = csv.build_steps(_chain(), None)
    assert "version_inc" not in patch


def test_high_hit_rate_verifies_step3():
    steps, patch = csv.build_steps(_chain(expected_direction="UP"), _stat(10, 8))
    assert steps[2]["verdict"] == "验证"
    assert patch["enabled"] is None  # 验证不等于复活/下线


def test_low_hit_rate_with_enough_samples_triggers_correction():
    steps, patch = csv.build_steps(_chain(expected_direction="UP"), _stat(10, 4))
    assert steps[2]["verdict"] == "否证"
    assert patch["enabled"] == 0
    assert "步3否证" in patch["invalidation"]


def test_low_hit_rate_but_too_few_samples_does_not_correct():
    steps, patch = csv.build_steps(_chain(expected_direction="UP"), _stat(4, 1))
    assert steps[2]["verdict"] == "不可分离"
    assert patch.get("enabled") is None


def test_both_direction_is_never_judged():
    steps, patch = csv.build_steps(_chain(expected_direction="BOTH"), _stat(20, 15))
    assert steps[2]["verdict"] == "不可分离"
    assert "不可判" in steps[2]["evidence"]


def test_step2_falsified_when_price_barely_moves():
    steps, _ = csv.build_steps(_chain(), _stat(10, 7, moved=1))
    assert steps[1]["verdict"] == "否证"


def test_step2_verified_when_most_samples_move():
    steps, _ = csv.build_steps(_chain(), _stat(10, 7, moved=9))
    assert steps[1]["verdict"] == "验证"


def test_confidence_is_clamped():
    steps, patch = csv.build_steps(_chain(confidence=0.99), _stat(10, 10))
    assert csv.CONF_LO <= patch["confidence"] <= csv.CONF_HI
    steps2, patch2 = csv.build_steps(_chain(confidence=0.01), _stat(10, 0))
    assert csv.CONF_LO <= patch2["confidence"] <= csv.CONF_HI


def test_hit_and_miss_counts_written_back():
    _steps, patch = csv.build_steps(_chain(), _stat(10, 6))
    assert patch["hit_count"] == 6
    assert patch["miss_count"] == 4


def test_steps_json_roundtrips_and_is_chinese_readable():
    steps, patch = csv.build_steps(_chain(), _stat(10, 8))
    parsed = json.loads(patch["steps_json"])
    assert isinstance(parsed, list) and len(parsed) == 3
    assert parsed[0]["step"] == "事件触发"
    assert "验证" in patch["steps_json"] or "否证" in patch["steps_json"]


def test_load_stats_ignores_unevaluated_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE causal_chain_outcomes (chain_id INTEGER, direction_hit, "
                 "magnitude_hit, actual_pct_5d REAL, trigger_source TEXT, predicted_direction TEXT)")
    conn.executemany("INSERT INTO causal_chain_outcomes VALUES (?,?,?,?,?,?)", [
        (7, 1, 1, 2.0, "news_events", "UP"),
        (7, 0, 0, -0.1, "news_events", "UP"),
        (7, None, None, None, "news_events", "UP"),   # 未评估: 不计
    ])
    st = csv.load_stats(conn)
    assert st[7]["n"] == 2 and st[7]["hit"] == 1
    assert st[7]["moved"] == 1          # 只有 +2.0% 那条越过 0.5%
