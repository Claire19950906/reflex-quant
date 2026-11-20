# -*- coding: utf-8 -*-
"""v406 · 链先验的"证据闸门"用例

锁三件事：
  1. 小样本不许否决交易（2/2 全中的点估计是 100%，Wilson 下界只有 34%）；
  2. 证据不足的链不产生方向（UNKNOWN），不是"随便挑一条说 UP"；
  3. BOTH 链的方向命中不参与证据（它只主张"会动"，混进来会把命中率灌到 100%）。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import chain_prior as cp


def test_wilson_lower_small_sample_is_weak():
    assert cp._wilson_lower(2, 2) < 0.5          # 2/2 的置信下界远低于 0.5
    assert cp._wilson_lower(0, 0) == 0.0


def test_eligible_requires_samples_and_lower_bound():
    assert cp._eligible({"n_outcomes": 10, "hit_rate": 1.0, "wilson_lower": 0.72}) is False
    assert cp._eligible({"n_outcomes": 60, "hit_rate": 0.70, "wilson_lower": 0.58}) is True
    # 只给 hit_rate/n 的老调用方：内部补算下界。样本门槛先挡人：
    # n=10 全中也不行（样本不够），n=12 全中的下界≈0.76 才过线。
    assert cp._eligible({"n_outcomes": 10, "hit_rate": 1.0}) is False
    assert cp._eligible({"n_outcomes": 12, "hit_rate": 1.0}) is True
    assert cp._eligible({"n_outcomes": 4, "hit_rate": 1.0}) is False


def test_aggregate_weak_when_samples_thin():
    """v407 口径：证据不足不再一律 UNKNOWN，而是 UP_WEAK —— 明确标注"不确定"，
    但仍然让决策侧看见（旧口径下决策侧永远读不到任何链）。"""
    thin = [{"direction": "UP", "n_outcomes": 10, "hit_rate": 1.0, "wilson_lower": 0.72}]
    assert cp.aggregate_direction(thin) == "UP_WEAK"
    solid = [{"direction": "DOWN", "n_outcomes": 80, "hit_rate": 0.65, "wilson_lower": 0.55}]
    assert cp.aggregate_direction(solid) == "DOWN"
    # strong 优先：只要有一条 strong，就出正式方向，不被 weak 稀释
    mixed = solid + thin
    assert cp.aggregate_direction(mixed) == "DOWN"
    # 两条 weak 方向相反 → WEAK_MIXED（不可判，也不许当方向用）
    mixed_weak = [dict(thin[0]), {"direction": "DOWN", "n_outcomes": 5, "hit_rate": 1.0}]
    assert cp.aggregate_direction(mixed_weak) == "WEAK_MIXED"


def test_tier_of_three_levels():
    base = {"direction": "UP", "hit_rate": 1.0}
    assert cp.tier_of({**base, "n_outcomes": 12, "wilson_lower": 0.76}) == "strong"
    assert cp.tier_of({**base, "n_outcomes": 10, "wilson_lower": 0.72}) == "weak"
    assert cp.tier_of({**base, "n_outcomes": 2}) == "none"


def test_weak_never_vetoes():
    """v407：weak 档只做标注，永远不许否决真实交易。"""
    weak = {"direction": "DOWN", "n_outcomes": 6, "hit_rate": 1.0, "posterior": 0.9,
            "wilson_lower": 0.45, "chain_id": "9", "name": "w"}
    assert cp.conflict_veto([weak], "BUY") is None


def test_conflict_veto_never_fires_on_thin_evidence():
    p = {"direction": "UP", "n_outcomes": 3, "hit_rate": 1.0, "posterior": 1.0,
         "wilson_lower": 0.3, "chain_id": "1", "name": "n"}
    assert cp.conflict_veto([p], "SELL") is None
    p2 = dict(p, n_outcomes=80, hit_rate=0.75, wilson_lower=0.65)
    assert cp.conflict_veto([p2], "SELL") is not None


def test_evidence_excludes_both_chains():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE causal_chains (id TEXT, expected_direction TEXT)")
    conn.execute("CREATE TABLE causal_chain_outcomes (chain_id TEXT, status TEXT)")
    conn.executemany("INSERT INTO causal_chains VALUES (?,?)",
                     [("1", "UP"), ("2", "BOTH")])
    # 链 1：3 中 2；链 2（BOTH）：4 中 4 —— 后者必须完全不计
    conn.executemany("INSERT INTO causal_chain_outcomes VALUES (?,?)",
                     [("1", "HIT"), ("1", "HIT"), ("1", "MISS")] +
                     [("2", "HIT")] * 4)
    ev = cp._evidence_from_outcomes(conn, ["1", "2"])
    assert ev["1"] == (2, 3)
    assert "2" not in ev
