#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v502：门卫校准的行为测试（review/gate_calibrator 从 0% 起步）。

这块是"门卫该拦多少"的算法本身。它错的方式很隐蔽：
小样本 3 条全中就被当成 100% 命中率，门卫开始狂放行 —— 所以
贝叶斯收缩（SHRINK_N=20）必须被钉住，这是整个校准的地基。
"""
import datetime
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

from review import gate_calibrator as gc


# ── 收缩：小样本不许吹牛 ──────────────────────────────────────
def test_prob_shrinks_tiny_perfect_sample():
    # 3 条全中，直接算就是 100%；收缩后必须显著低于 1，也不能低于 0.5
    p = gc._prob(3, 3)
    assert 0.5 < p < 0.65       # (3 + 10) / (3 + 20) ≈ 0.565


def test_prob_approaches_real_rate_with_big_sample():
    assert gc._prob(700, 1000) == pytest.approx((700 + 10) / 1020)
    assert gc._prob(700, 1000) > 0.65


def test_prob_zero_sample_is_half():
    assert gc._prob(0, 0) == pytest.approx(0.5)


def test_prob_is_monotonic_in_hits():
    assert gc._prob(0, 20) < gc._prob(10, 20) < gc._prob(20, 20)


# ── 训练：三层格子 ────────────────────────────────────────────
def _row(asset, direction, confirm, hit, did=1, conf=0.0, ts=0):
    return {"decision_id": did, "ts": ts, "asset": asset, "direction": direction,
            "confidence": conf, "hit": hit, "confirm": confirm}


def test_train_counts_cells_and_fallbacks():
    rows = [_row("BTC", "BUY", 1, 1, did=1), _row("BTC", "BUY", 1, 0, did=2),
            _row("ETH", "SELL", 0, 1, did=3)]
    m = gc.train(rows)
    assert m["cells"][("BTC", "BUY", 1)] == [1, 2]
    assert m["asset_dir"][("BTC", "BUY")] == [1, 2]
    assert m["asset_only"]["ETH"] == [1, 1]
    assert m["glob"] == [2, 3]


def test_train_empty_rows_does_not_crash():
    m = gc.train([])
    assert m["cells"] == {} and m["glob"] == [0, 0]


# ── 打分：优先细格子，样本不够就降级 ───────────────────────────
def test_score_uses_cell_when_enough_samples():
    rows = [_row("BTC", "BUY", 1, 1, did=i) for i in range(8)]
    m = gc.train(rows)
    s = gc.score(_row("BTC", "BUY", 1, 1), m)
    assert s["basis"].startswith("格 ")
    assert s["n"] == 8


def test_score_falls_back_to_asset_direction_when_cell_is_thin():
    rows = [_row("BTC", "BUY", 1, 1, did=i) for i in range(3)]        # 细格子只有 3 条
    rows += [_row("BTC", "BUY", 0, 1, did=100 + i) for i in range(10)]  # 品种方向够 13 条
    m = gc.train(rows)
    s = gc.score(_row("BTC", "BUY", 1, 1), m)
    assert s["basis"].startswith("品种方向")


def test_score_falls_back_to_global_for_unknown_asset():
    m = gc.train([_row("BTC", "BUY", 1, 1, did=i) for i in range(10)])
    s = gc.score(_row("SOL", "SELL", None, 1), m)
    assert s["basis"].startswith("全局")
    assert s["p"] == pytest.approx(gc._prob(10, 10), abs=1e-4)


def test_score_is_transparent_about_basis():
    # 不黑箱：每条分数都要说明它是靠哪一层算出来的
    m = gc.train([_row("BTC", "BUY", 1, 1, did=i) for i in range(20)])
    s = gc.score(_row("BTC", "BUY", 1, 1), m)
    assert set(s) == {"p", "basis", "n"}
    assert "n=" in s["basis"]


# ── 决策前 30 分钟是否已同向 ───────────────────────────────────
@pytest.fixture()
def ticks():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE market_ticks(symbol TEXT, ts INTEGER, price REAL)")
    yield c
    c.close()


def _tick(c, symbol, ts, price):
    c.execute("INSERT INTO market_ticks VALUES(?,?,?)", (symbol, ts, price))


def test_pre_move_align_detects_same_direction(ticks):
    _tick(ticks, "BTC", 1000, 100.0)
    _tick(ticks, "BTC", 2800, 101.0)      # 决策前 1800 秒涨 1%
    assert gc.pre_move_align(ticks, "BTC", 2800, "BUY") == 1
    assert gc.pre_move_align(ticks, "BTC", 2800, "SELL") == 0


def test_pre_move_align_flat_is_zero_not_none(ticks):
    _tick(ticks, "BTC", 1000, 100.0)
    _tick(ticks, "BTC", 2800, 100.0)
    assert gc.pre_move_align(ticks, "BTC", 2800, "BUY") == 0


def test_pre_move_align_none_when_no_history(ticks):
    _tick(ticks, "BTC", 1600, 100.0)      # 只有当下这一笔，没有 30 分钟前
    assert gc.pre_move_align(ticks, "BTC", 1600, "BUY") is None


def test_pre_move_align_none_for_unknown_symbol(ticks):
    assert gc.pre_move_align(ticks, "SOL", 1600, "BUY") is None


# ── 样本装载：只要已结算、只要窗口内 ───────────────────────────
@pytest.fixture()
def ledger():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE shadow_ledger(decision_id INTEGER PRIMARY KEY, decided_ts INTEGER,
                                   asset TEXT, direction TEXT, confidence REAL, hit INTEGER);
        CREATE TABLE market_ticks(symbol TEXT, ts INTEGER, price REAL);
    """)
    yield c
    c.close()


def _ledger_row(c, did, ts, hit, asset="BTC", direction="BUY", conf=8.0):
    c.execute("INSERT INTO shadow_ledger VALUES(?,?,?,?,?,?)",
              (did, ts, asset, direction, conf, hit))


def test_load_samples_skips_unsettled(ledger):
    now = int(datetime.datetime.now().timestamp())
    _ledger_row(ledger, 1, now - 3600, 1)
    _ledger_row(ledger, 2, now - 3600, None)     # 未结算
    _tick(ledger, "BTC", now - 3600, 100.0)
    _tick(ledger, "BTC", now - 3600 - 1800, 99.0)
    rows = gc.load_samples(ledger, days=2)
    assert [r["decision_id"] for r in rows] == [1]
    assert rows[0]["confirm"] == 1               # 决策前 30 分钟已涨 → BUY 同向


def test_load_samples_respects_days_window(ledger):
    now = int(datetime.datetime.now().timestamp())
    _ledger_row(ledger, 1, now - 10 * 86400, 1)   # 10 天前，窗口外
    _ledger_row(ledger, 2, now - 3600, 1)
    rows = gc.load_samples(ledger, days=3)
    assert [r["decision_id"] for r in rows] == [2]


# ── 写回：只加列，不改历史语义 ────────────────────────────────
def test_write_scores_adds_columns_and_writes_back(ledger):
    now = int(datetime.datetime.now().timestamp())
    _ledger_row(ledger, 1, now - 3600, 1)
    rows = gc.load_samples(ledger, days=2)
    m = gc.train(rows)
    n = gc.write_scores(ledger, rows, m)
    assert n == 1
    cols = {r[1] for r in ledger.execute("PRAGMA table_info(shadow_ledger)")}
    assert {"gate_score", "gate_basis", "pre_confirm"} <= cols
    got = ledger.execute(
        "SELECT gate_score, gate_basis, pre_confirm FROM shadow_ledger WHERE decision_id=1"
    ).fetchone()
    assert got[0] is not None and got[1].startswith("全局")


def test_write_scores_is_idempotent(ledger):
    now = int(datetime.datetime.now().timestamp())
    _ledger_row(ledger, 1, now - 3600, 1)
    rows = gc.load_samples(ledger, days=2)
    m = gc.train(rows)
    gc.write_scores(ledger, rows, m)
    gc.write_scores(ledger, rows, m)
    assert ledger.execute("SELECT COUNT(*) FROM shadow_ledger").fetchone()[0] == 1


# ── 考试集报告：不崩、门槛越严覆盖率越低 ───────────────────────
def test_evaluate_prints_and_thresholds_shrink_coverage(capsys):
    rows = [_row("BTC", "BUY", 1, 1, did=i) for i in range(5)]
    rows += [_row("BTC", "BUY", 1, 0, did=10 + i) for i in range(5)]
    m = gc.train(rows)
    gc.evaluate(rows, m, thresholds=(0.50, 0.60))
    out = capsys.readouterr().out
    assert "考试集" in out
    covs = [line.split()[2] for line in out.splitlines() if "%" in line and "考试集" not in line]
    assert covs, "应打印每个门槛的覆盖率"


def test_evaluate_empty_is_silent_not_crash(capsys):
    gc.evaluate([], gc.train([]))
    assert "没有可评估样本" in capsys.readouterr().out
