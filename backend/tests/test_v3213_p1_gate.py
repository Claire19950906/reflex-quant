#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v3213_p1_gate.py — v321.3 P1 闸门校准 / 资金流上下文 / 代理配置 (全离线)
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PY = os.path.join(SRC, "src_python")
if SRC_PY not in sys.path:
    sys.path.insert(0, SRC_PY)


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY, target_asset TEXT, suggested_action TEXT,
        sentiment_score REAL, impact_score REAL, event_strength TEXT,
        forward_move_pct REAL, ts INTEGER, created_at TEXT)""")
    conn.execute("""CREATE TABLE gate_calibration (
        asset TEXT, action TEXT, n_samples INTEGER, n_hits INTEGER,
        hit_rate REAL, base_rate REAL, lift_pp REAL, avg_abs_move REAL,
        window_days INTEGER, computed_at INTEGER, applied INTEGER, note TEXT,
        PRIMARY KEY(asset, action))""")
    return conn


def _seed(conn, asset, action, n, up, id0=0):
    for i in range(n):
        conn.execute("INSERT INTO ai_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                     (id0 + i, asset, action, 0.5, 0.5, "high",
                      1.0 if i < up else -1.0, 1800000000, ""))


def _fake_calib(dg, table):
    """monkeypatch _gate_calibration —— 直接改缓存会被 TTL 检查绕过。"""
    dg._gate_calibration = lambda: table


# ── 1. 校准计算 ─────────────────────────────────────────────────────────────
def test_calibrate_groups_by_asset_and_action():
    """分组 hit_rate vs **全局同方向** base_rate —— 这才是有意义的对比。

    base 来自所有 BUY 样本的市场上涨比例; hit 来自某个 (资产, 方向) 分组。
    所以同一方向下不同资产的 hit 可以一个高于 base、一个低于 base。
    """
    import calibrate_gate as cg
    conn = _mk_db()
    # BTC BUY: 10 涨 20 跌 (命中 33%) | ETH BUY: 20 涨 10 跌 (命中 67%)
    # → 全局 BUY base = 30/60 = 50%
    _seed(conn, "BTC", "BUY", 30, 10, id0=0)
    _seed(conn, "ETH", "BUY", 30, 20, id0=100)
    conn.commit()
    st = cg.compute(conn, window_days=3650)
    rows = {(r["asset"], r["action"]): r for r in st["rows"]}
    assert ("BTC", "BUY") in rows and ("ETH", "BUY") in rows
    assert rows[("BTC", "BUY")]["lift_pp"] < 0, "BTC BUY 应低于基准"
    assert rows[("ETH", "BUY")]["lift_pp"] > 0, "ETH BUY 应高于基准"
    assert rows[("BTC", "BUY")]["n_samples"] == 30


def test_calibrate_skips_small_samples():
    """样本不足的分组**不写表** —— 不用小样本硬调门槛。"""
    import calibrate_gate as cg
    conn = _mk_db()
    _seed(conn, "BTC", "BUY", 5, 4)
    conn.commit()
    assert cg.compute(conn, window_days=3650)["rows"] == []


def test_significance_gate_blocks_unproven_lift():
    """lift 不显著 → applied 必须是 0 (不改交易门槛)。"""
    import calibrate_gate as cg
    conn = _mk_db()
    _seed(conn, "BTC", "BUY", 30, 18)
    conn.commit()
    st = cg.compute(conn, window_days=3650)
    assert st["rows"], "样本够应该有结果"
    cg.persist(conn, st["rows"], apply=True)
    got = conn.execute("SELECT applied FROM gate_calibration").fetchone()[0]
    assert got == 0, "统计不显著时不允许 applied=1"


def test_force_apply_sets_applied_and_marks_note():
    """--force-apply 是**用户的显式决策** → applied=1 且留痕。"""
    import calibrate_gate as cg
    conn = _mk_db()
    _seed(conn, "BTC", "BUY", 30, 18)
    conn.commit()
    st = cg.compute(conn, window_days=3650)
    cg.persist(conn, st["rows"], apply=True, force=True)
    row = conn.execute("SELECT applied, note FROM gate_calibration").fetchone()
    assert row["applied"] == 1
    assert "强制采信" in row["note"]


# ── 2. 闸门按分组调整门槛 ────────────────────────────────────────────────────
def test_calibrated_thresholds_relaxes_positive_lift():
    """正增量分组 → 门槛放宽 (闸门不该拦好信号)。"""
    import decision_guard as dg
    _fake_calib(dg, {})
    b0, s0, _ = dg._calibrated_thresholds("XAU", "BUY")
    _fake_calib(dg, {
        ("XAU", "BUY"): {"n_samples": 200, "hit_rate": 0.6, "base_rate": 0.5,
                         "lift_pp": 10.0, "note": "正"}})
    b1, s1, info = dg._calibrated_thresholds("XAU", "BUY")
    assert info is not None
    assert b1 < b0, "正增量的 BUY 门槛应被放宽"


def test_calibrated_thresholds_tightens_negative_lift():
    import decision_guard as dg
    _fake_calib(dg, {})
    b0, _, _ = dg._calibrated_thresholds("BTC", "BUY")
    _fake_calib(dg, {
        ("BTC", "BUY"): {"n_samples": 200, "hit_rate": 0.35, "base_rate": 0.5,
                         "lift_pp": -15.0, "note": "负"}})
    b1, s1, info = dg._calibrated_thresholds("BTC", "BUY")
    assert info is not None
    assert b1 > b0, "负增量的 BUY 门槛应被收紧"


def test_calibrated_thresholds_default_when_no_record():
    """没有记录 → 用默认门槛, 不猜。"""
    import decision_guard as dg
    _fake_calib(dg, {})
    b, s, info = dg._calibrated_thresholds("XYZ", "BUY")
    assert info is None
    assert b == dg._direction_scores()["buy_min"]


# ── 3. 代理配置 ──────────────────────────────────────────────────────────────
def test_proxy_config_reads_env():
    """代理必须能从环境变量读出来并写回 os.environ。"""
    import importlib
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
    try:
        import config
        importlib.reload(config)
        assert config.PROXY_ENABLED is True
        assert config.HTTPS_PROXY == "http://127.0.0.1:7890"
        assert config.apply_proxy_env() is True
        assert os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:7890"
    finally:
        os.environ.pop("HTTPS_PROXY", None)
        importlib.reload(config)


def test_proxy_disabled_when_unset():
    import importlib
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(k, None)
    import config
    importlib.reload(config)
    assert config.PROXY_ENABLED is False
    assert config.apply_proxy_env() is False


# ── 4. 归因端点必须返回资金流/挂单量 (需求 #6) ──────────────────────────────
def test_attribution_endpoint_includes_flow_context():
    """归因端点必须挂上库内真实资金流/挂单量 (外网不可达时的兜底)。"""
    path = os.path.join(SRC_PY, "bridge_server.py")
    text = open(path, encoding="utf-8").read()
    assert "_attribution_flow_context" in text, "必须有库内资金流上下文"
    for key in ("opt_wall", "opt_call_put_oi", "taker_buy_sell", "agg.liquidation"):
        assert key in text, f"资金流上下文必须包含 {key}"
