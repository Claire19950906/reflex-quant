# -*- coding: utf-8 -*-
"""v322: 方向判别力守卫的回归单测。

背景（2026-11-07 实测）
----------------------
因果链的方向命中率（超额收益口径）实测 **48.3%** —— **低于随机 50%**。
若照旧把"方向猜对"记进 credit，等于**拿噪声改权重** → 越跑越歪。

守卫规则：族（或全局）的方向判别力 ≤ 0.50 时，该族链的 credit
**只保留幅度部分**，方向部分不给分。

credit 拆解（精确等价）：
    HIT     = dir_hit*0.5 + mag_hit*0.5
    HIT_WEAK= dir_hit*0.5 + mag_hit*0
    守卫触发 → credit = mag_hit*0.5

⚠️ 既有 test_causal_weight_feedback.py 用的内存库**没有方向列**，
   所以守卫在那套 fixture 下完全不生效（代码会容错降级）。
   本文件用**带方向列**的 schema 专门覆盖守卫。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

from feeds import causal_weight_feedback as cwf


def _mem_db(chains, outcomes):
    """带**方向列**的内存库。

    chains:  [(id, asset, direction, weight)]
    outcomes:[(chain_id, status, pred_dir, direction_hit, magnitude_hit)]
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE causal_chains (
            id INTEGER PRIMARY KEY, name TEXT, asset TEXT,
            expected_direction TEXT DEFAULT 'BOTH', weight REAL DEFAULT 1.0)""")
    conn.execute(
        """CREATE TABLE causal_chain_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT,
            status TEXT, predicted_direction TEXT DEFAULT 'BOTH',
            direction_hit INTEGER, magnitude_hit INTEGER)""")
    for cid, asset, direction, weight in chains:
        conn.execute(
            "INSERT INTO causal_chains (id, name, asset, expected_direction, weight)"
            " VALUES (?,?,?,?,?)",
            (cid, "chain-%s" % cid, asset, direction, weight))
    for row in outcomes:
        cid, status = row[0], row[1]
        pred = row[2] if len(row) > 2 else "BOTH"
        dh = row[3] if len(row) > 3 else None
        mh = row[4] if len(row) > 4 else None
        conn.execute(
            "INSERT INTO causal_chain_outcomes"
            " (chain_id, status, predicted_direction, direction_hit, magnitude_hit)"
            " VALUES (?,?,?,?,?)", (str(cid), status, pred, dh, mh))
    conn.commit()
    return conn


def test_stats_reads_direction_columns():
    """有方向列时，dir_n / dir_hit / dir_mag_hit 要正确统计。"""
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)],
                   outcomes=[(1, "HIT", "UP", 1, 1),
                             (1, "MISS", "UP", 0, 0)])
    try:
        s = cwf._chain_stats(conn)
        assert s[1]["dir_n"] == 2
        assert s[1]["dir_hit"] == 1
        assert s[1]["dir_mag_hit"] == 1
        assert s[1]["credit"] == 1.0
    finally:
        conn.close()


def test_guard_strips_direction_credit_when_uninformative():
    """判别力低于随机 → 方向部分不给分，只留幅度部分。"""
    # 10 个有方向的 outcome，方向只对 3 个 → 30% < 50% → 守卫触发
    outcomes = [(1, "HIT", "UP", 1, 1) if i < 3 else
                (1, "MISS", "UP", 0, 0) for i in range(10)]
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=outcomes)
    try:
        stats = cwf._chain_stats(conn)
        meta = cwf._chain_meta(conn)
        before = stats[1]["credit"]
        stats2, fam_rate, global_rate, guarded = cwf._apply_direction_guard(
            stats, meta)
        # 方向部分被剥掉：3 个方向命中 × 0.5 = 1.5 被扣掉
        assert guarded, "判别力不足的族必须被守卫"
        assert stats2[1]["credit"] < before
        # 只剩幅度命中 × 0.5
        assert abs(stats2[1]["credit"] - 3 * 0.5) < 1e-9
    finally:
        conn.close()


def test_guard_keeps_direction_credit_when_informative():
    """判别力高于随机 → 方向部分照常计分。"""
    # 10 个 outcome，方向对 8 个 → 80% > 50% → 不守卫
    outcomes = [(1, "HIT", "UP", 1, 1) if i < 8 else
                (1, "MISS", "UP", 0, 0) for i in range(10)]
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=outcomes)
    try:
        stats = cwf._chain_stats(conn)
        meta = cwf._chain_meta(conn)
        before = stats[1]["credit"]
        stats2, fam_rate, global_rate, guarded = cwf._apply_direction_guard(
            stats, meta)
        assert not guarded, "判别力好的族不该被守卫"
        assert abs(stats2[1]["credit"] - before) < 1e-9
    finally:
        conn.close()


def test_guard_is_noop_without_direction_columns():
    """没有方向列的老库 → 守卫不生效，credit 保持旧口径。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE causal_chain_outcomes (id INTEGER PRIMARY KEY,"
        " chain_id TEXT, status TEXT)")
    conn.execute("INSERT INTO causal_chain_outcomes (chain_id, status)"
                 " VALUES ('1','HIT')")
    conn.commit()
    try:
        s = cwf._chain_stats(conn)
        assert s[1]["credit"] == 1.0
        assert s[1]["dir_n"] == 0
    finally:
        conn.close()


def test_plan_reports_direction_discrimination():
    """plan 里要如实暴露方向判别力（供审计/验收读取）。"""
    outcomes = [(1, "HIT", "UP", 1, 1) if i < 3 else
                (1, "MISS", "UP", 0, 0) for i in range(10)]
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=outcomes)
    try:
        plan = cwf.plan_updates(conn)
        dd = plan.get("direction_discrimination")
        assert dd is not None, "plan 必须报告方向判别力"
        assert dd["min_required"] == cwf.DIRECTION_MIN_DISCRIMINATION
        assert 0.0 <= dd["global_rate"] <= 1.0
    finally:
        conn.close()


# ─────────────────── 滞回（hysteresis）───────────────────

def test_hysteresis_prevents_flapping(tmp_path, monkeypatch):
    """判别力在门槛附近时，守卫状态**保持上次**，不因噪声翻转。

    为什么需要：实测全局判别力 48.3%，离 50% 只有 1.66 个百分点 ——
    样本多几十条就可能翻过门槛 → 守卫反复开关 → 权重来回跳。
    """
    state_file = tmp_path / "guard_state.json"
    monkeypatch.setattr(cwf, "_GUARD_STATE_FILE", state_file)

    # 判别力 48% → 落在滞回区间 [45%, 55%] → 保持上次状态
    def _mk(rate_n, rate_total=100):
        outs = []
        for i in range(rate_total):
            if i < rate_n:
                outs.append((1, "HIT", "UP", 1, 1))
            else:
                outs.append((1, "MISS", "UP", 0, 0))
        return outs

    # 第一轮：48% → 滞回区 → 默认按"守卫"处理（prev 为空 → True）
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=_mk(48))
    try:
        stats = cwf._chain_stats(conn)
        meta = cwf._chain_meta(conn)
        _, _, _, guarded = cwf._apply_direction_guard(stats, meta)
        assert guarded, "首次落在滞回区应按守卫处理（保守）"
    finally:
        conn.close()

    # 状态已落盘 → 下一轮 52%（仍在滞回区）→ 保持守卫
    assert state_file.exists(), "守卫状态必须落盘（跨轮次）"
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=_mk(52))
    try:
        stats = cwf._chain_stats(conn)
        meta = cwf._chain_meta(conn)
        _, _, _, guarded = cwf._apply_direction_guard(stats, meta)
        assert guarded, "滞回区内必须保持上次的守卫状态（不抖）"
    finally:
        conn.close()

    # 超过解除门槛 55% → 解除守卫
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=_mk(60))
    try:
        stats = cwf._chain_stats(conn)
        meta = cwf._chain_meta(conn)
        _, _, _, guarded = cwf._apply_direction_guard(stats, meta)
        assert not guarded, "超过解除门槛应解除守卫"
    finally:
        conn.close()


def test_hysteresis_releases_only_above_release_threshold(tmp_path, monkeypatch):
    """滞回区上沿之下不得解除守卫。"""
    state_file = tmp_path / "guard_state.json"
    monkeypatch.setattr(cwf, "_GUARD_STATE_FILE", state_file)
    state_file.write_text(json.dumps({"XAU|UP": True}), encoding="utf-8")

    outs = [(1, "HIT", "UP", 1, 1) if i < 54 else (1, "MISS", "UP", 0, 0)
            for i in range(100)]
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=outs)
    try:
        stats = cwf._chain_stats(conn)
        meta = cwf._chain_meta(conn)
        _, _, _, guarded = cwf._apply_direction_guard(stats, meta)
        assert guarded, "54% < 解除门槛 55% → 不得解除"
    finally:
        conn.close()
