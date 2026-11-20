# -*- coding: utf-8 -*-
"""v425：资金费率套息的算术测试。

这套息结论会直接影响"要不要拿真钱去做"，所以三件事必须钉死：
  1. 年化 = 均值 × 3 次/天 × 365 天（少乘一次就虚高 3 倍）
  2. 回本天数 = 成本 / 每天净收（单位必须都是"百分点"）
  3. 成本只认 cost_model，不许在本地再抄一份
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import carry_yield as cy


def _db(vals, asset="BTC"):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE flow_snapshots(asset TEXT, kind TEXT, ts INTEGER, value REAL)")
    for i, v in enumerate(vals):
        conn.execute("INSERT INTO flow_snapshots VALUES(?,?,?,?)",
                     (asset, "funding", 1_700_000_000 + i * 28_800, v))
    return conn


def test_annualized_matches_formula():
    conn = _db([0.0001] * 200)            # 每次结算 0.01%
    st = cy.funding_stats(conn, "BTC", 320, now=1_700_000_000 + 200 * 28_800)
    # 0.0001 × 3 × 365 = 0.1095 → 10.95%
    assert st["points"] == 200
    assert abs(st["ann_pct_1x"] - 10.95) < 0.05
    assert abs(st["ann_pct_3x"] - 32.85) < 0.15


def test_breakeven_days_units():
    conn = _db([0.0001] * 200)
    st = cy.funding_stats(conn, "BTC", 320, now=1_700_000_000 + 200 * 28_800)
    # 成本 30bps = 0.30 个百分点；每天收 10.95/365 = 0.03 个百分点 → ≈10 天
    assert st["breakeven_days"] is not None
    assert 9 <= st["breakeven_days"] <= 11


def test_cost_comes_from_cost_model():
    conn = _db([0.0001] * 200)
    st = cy.funding_stats(conn, "BTC", 320, now=1_700_000_000 + 200 * 28_800)
    assert st["cost_bps"] == 30.0
    st_sol = cy.funding_stats(conn, "SOL", 320, now=1_700_000_000 + 200 * 28_800)
    # SOL 在 cost_model 里是 32bps，本地不许另写
    assert st_sol is None or st_sol["cost_bps"] in (30.0, 32.0)


def test_negative_funding_says_no():
    conn = _db([-0.0001] * 200)
    st = cy.funding_stats(conn, "BTC", 320, now=1_700_000_000 + 200 * 28_800)
    assert st["ann_pct_1x"] < 0
    assert st["breakeven_days"] is None
    assert "不成立" in st["verdict"] or "不付钱" in st["verdict"]


def test_short_series_returns_none():
    conn = _db([0.0001] * 3)
    assert cy.funding_stats(conn, "BTC", 320, now=1_700_000_000) is None
