# -*- coding: utf-8 -*-
"""v449：单押注检测 + 正期望停单的口径测试。

锁三件事：
  - 同簇同向必须合并成一注（13 笔不能当 13 个证据）；
  - 相关性只认正相关 ≥0.70，负相关是分散不是同注；
  - 正期望必须同时满足 净>0、n≥30、t≥2，t 在 1~2 只能"观察"。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import bets as bm
import expectancy_policy as ep


def _cell(horizon, avg, n=94, t=2.5):
    return {
        "horizon": horizon,
        "net": {"n": n, "avg": avg, "t_stat": t, "win_rate": 0.5},
        "gross": {"n": n, "avg": avg, "t_stat": t},
        "cost_to_move": 0.1,
        "verdict": "",
    }


def _rep(cells, by_asset=None, entries=94):
    return {"cells": cells, "by_asset": by_asset or [], "entries": entries}


# ── 相关性聚簇 ──────────────────────────────────────────
def test_clusters_group_high_positive_corr():
    corr = {
        "assets": ["BTC", "ETH", "SOL", "WTI", "XAU"],
        "matrix": {
            "BTC": {"BTC": 1.0, "ETH": 0.934, "SOL": 0.856, "WTI": -0.251, "XAU": 0.031},
            "ETH": {"BTC": 0.934, "ETH": 1.0, "SOL": 0.887, "WTI": -0.277, "XAU": -0.007},
            "SOL": {"BTC": 0.856, "ETH": 0.887, "SOL": 1.0, "WTI": -0.254, "XAU": 0.016},
            "WTI": {"BTC": -0.251, "ETH": -0.277, "SOL": -0.254, "WTI": 1.0, "XAU": 0.071},
            "XAU": {"BTC": 0.031, "ETH": -0.007, "SOL": 0.016, "WTI": 0.071, "XAU": 1.0},
        },
    }
    groups = bm.clusters_from_matrix(corr)
    as_sets = [set(g) for g in groups]
    assert {"BTC", "ETH", "SOL"} in as_sets
    assert {"WTI"} in as_sets
    assert {"XAU"} in as_sets
    # 负相关不能被并进来
    assert not any("WTI" in g and "BTC" in g for g in as_sets)


def test_clusters_threshold_is_hard():
    corr = {"assets": ["A", "B"], "matrix": {"A": {"A": 1, "B": 0.69},
                                             "B": {"A": 0.69, "B": 1}}}
    groups = bm.clusters_from_matrix(corr, threshold=0.70)
    assert len(groups) == 2


# ── 仓位 → 注 ────────────────────────────────────────────
def _pos(i, asset, action, entry=100.0, pnl=0.0, size=100.0):
    return {"id": i, "asset": asset, "action": action, "entry_price": entry,
            "size_usdt": size, "pnl_pct": pnl}


def test_build_bets_merges_same_cluster_same_direction():
    clusters = [["BTC", "ETH", "SOL"], ["WTI"], ["XAU"]]
    positions = [
        _pos(1, "XAU", "SELL"), _pos(2, "XAU", "SELL"), _pos(3, "XAU", "SELL"),
        _pos(4, "WTI", "SELL"), _pos(5, "WTI", "SELL"),
        _pos(6, "WTI", "BUY"), _pos(7, "WTI", "BUY"),
        _pos(8, "BTC", "SELL"), _pos(9, "ETH", "SELL"),
    ]
    rep = bm.build_bets(positions, clusters)
    assert len(rep["bets"]) == 4  # XAU空 / WTI空 / WTI多 / BTC+ETH空
    assert rep["opposing_assets"] == ["WTI"]
    assert any("同簇同向" in w for w in rep["warnings"])
    assert any("同时有多/空" in w for w in rep["warnings"])


def test_build_bets_concentration_warning():
    clusters = [["XAU"]]
    positions = [_pos(i, "XAU", "SELL") for i in range(7)] + [_pos(9, "WTI", "BUY")]
    rep = bm.build_bets(positions, clusters)
    assert any("集中度偏高" in w for w in rep["warnings"])


# ── 同向闸 ──────────────────────────────────────────────
def test_confirm_ok_direction_and_threshold():
    assert ep._confirm_ok("BUY", 100.2, 100.0, 0.10) is True
    assert ep._confirm_ok("BUY", 100.05, 100.0, 0.10) is False
    assert ep._confirm_ok("SELL", 99.8, 100.0, 0.10) is True
    assert ep._confirm_ok("SELL", 99.95, 100.0, 0.10) is False
    assert ep._confirm_ok("BUY", 100.0, 0, 0.10) is False


# ── 停单 / 观察 / 开火 ──────────────────────────────────
def test_cohort_halt_when_all_negative():
    rep = ep._cohort(_rep([_cell("1h", -0.2, t=-3.0)]), 30)
    assert rep["allow"] is False
    assert rep["action"] == "停单"


def test_cohort_weak_when_t_below_two():
    rep = ep._cohort(_rep([_cell("1d", 0.45, t=1.14)]), 30)
    assert rep["allow"] is False
    assert rep["weak"] is True
    assert rep["action"] == "仅观察仓"


def test_cohort_allow_when_t_above_two():
    rep = ep._cohort(_rep([_cell("20m", 0.07, t=2.12)]), 2)
    assert rep["allow"] is True
    assert rep["action"] == "可开火"
    assert rep["recommended_hold"] == "20m"


def test_cohort_small_sample_never_allows():
    rep = ep._cohort(_rep([_cell("1d", 1.0, n=12, t=5.0)]), 0)
    assert rep["allow"] is False
