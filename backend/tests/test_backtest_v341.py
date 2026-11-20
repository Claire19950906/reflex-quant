# -*- coding: utf-8 -*-
"""v341 回测跑道：规则统计口径不能漂。

跑通口径比"跑出漂亮数字"重要 —— 这几个测试钉的是统计定义本身。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import run_backtest as rb


def _mk(closes):
    return [(1000 + i * 86400, c) for i, c in enumerate(closes)]


def test_forward_return_is_close_to_close():
    # 10 → 11 → 12：第 0 根到第 2 根 = +20%
    prices = _mk([10, 11, 12])
    assert rb._ret([c for _, c in prices], 0, 2) == 20.0


def test_forward_return_out_of_range_is_none():
    assert rb._ret([1.0, 2.0], 0, 5) is None


def test_rule_counts_only_signals_with_forward_window():
    # 最后 horizon 根不可能有前瞻收益，不能被算进样本
    # 第 3 根 +10%、第 4 根 +4.5% 都触发；最后一根(i=6)没有 2 日前瞻，必须被剔掉
    prices = _mk([10, 10, 10, 11, 11.5, 12, 12])
    r = rb.backtest_rule(prices, thresh=3.0, horizon=2)
    assert r["ok"] is True
    assert r["n"] == 2


def test_winrate_and_edge_use_same_sample():
    prices = _mk([10, 10, 11, 11.6, 11.7])          # 第 2 根 +10%，之后 2 日 +6.36%
    r = rb.backtest_rule(prices, thresh=5.0, horizon=2)
    assert r["n"] == 1
    assert r["winrate"] == 1.0                      # 唯一样本为正
    assert r["avg_pct"] > 0
    # edge = 信号均值 − 全样本基线，两者都基于同一前瞻口径
    assert abs((r["avg_pct"] - r["base_avg_pct"]) - r["edge_pct"]) <= 0.02


def test_too_few_bars_is_gated():
    # run() 对不足 GATE_MIN_BARS 的品种直接跳过，不给结论
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    for i in range(5):
        conn.execute("INSERT INTO ohlcv_daily VALUES ('TINY', ?, ?)", (1000 + i, 10 + i))
    out = rb.run(conn, ["TINY"], 3.0, 5, False)
    assert out and "skipped" in out[0]


# ── 阈值选优（pick_best）：判定口径不能漂 ────────────────────────────────
def _row(thresh, edge, n, winrate=0.5):
    return {"ok": True, "thresh": thresh, "edge_pct": edge, "n": n, "winrate": winrate}


def test_pick_best_prefers_positive_edge_with_enough_samples():
    rows = [_row(2, -0.5, 900), _row(5, 0.9, 120), _row(8, 2.1, 10)]  # 8% 样本太少
    pk = rb.pick_best(rows, min_sample=30)
    assert pk["verdict"] == "use"
    assert pk["thresh"] == 5          # 8% 超额最高但 n=10 < 30，必须被样本下限挡掉


def test_pick_best_blocks_when_best_edge_is_negative():
    pk = rb.pick_best([_row(2, -0.6, 600), _row(5, -1.2, 100)], min_sample=30)
    assert pk["verdict"] == "blocked"


def test_pick_best_marks_thin_edge_as_weak():
    # 正超额但低于 MIN_EDGE，扣成本不剩 → 不能算"可用"
    pk = rb.pick_best([_row(2, 0.05, 800), _row(3, 0.01, 400)], min_sample=30)
    assert pk["verdict"] == "weak"


def test_pick_best_insufficient_when_no_threshold_has_samples():
    pk = rb.pick_best([_row(5, 3.0, 4), _row(8, 5.0, 2)], min_sample=30)
    assert pk["verdict"] == "insufficient"


# ── 成本口径：净超额才是可交易的那部分 ────────────────────────────────
def test_round_trip_cost_is_twelve_bps():
    # 4bps 手续费 + 2bps 滑点，一开一平 = 12bps = 0.12%
    assert round(rb.round_trip_cost_pct(), 4) == 0.12


def test_backtest_reports_net_edge_after_cost():
    prices = _mk([10, 10, 11, 11.6, 11.7])
    r = rb.backtest_rule(prices, thresh=5.0, horizon=2)
    assert r["net_edge_pct"] == round(r["edge_pct"] - rb.round_trip_cost_pct(), 2)


def test_pick_best_blocks_when_net_edge_is_negative_although_gross_positive():
    # 毛超额 +0.05%，扣 0.12% 成本后为负 → 必须是 blocked，不能再标"薄利"
    rows = [{"ok": True, "thresh": 3, "edge_pct": 0.05, "net_edge_pct": -0.07,
             "n": 500, "winrate": 0.53}]
    pk = rb.pick_best(rows, min_sample=30)
    assert pk["verdict"] == "blocked"
    assert pk["net_edge_pct"] == -0.07
