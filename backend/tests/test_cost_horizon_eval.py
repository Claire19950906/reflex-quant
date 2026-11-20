# -*- coding: utf-8 -*-
"""v423：成本–持有期评估的口径测试。

这些断言的价值在于"锁口径"：
  - 毛收益必须按方向算（SELL 价格跌才算赚），不能一律按多头；
  - 持有期没走完必须跳过，不能拿更早的价冒充；
  - 中间空洞超过 2 个周期必须跳过；
  - 样本 <30 一律"不下结论"（这是本仓库最容易犯的毛病）。
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import cost_horizon_eval as ce


def test_gross_pct_direction():
    assert ce.gross_pct("BUY", 100, 101) == pytest.approx(1.0)
    assert ce.gross_pct("LONG", 100, 101) == pytest.approx(1.0)
    assert ce.gross_pct("SELL", 100, 99) == pytest.approx((100 / 99 - 1) * 100)
    assert ce.gross_pct("SELL", 100, 101) < 0
    assert ce.gross_pct("HOLD", 100, 101) is None      # 不认识的方向不许当多头
    assert ce.gross_pct("BUY", 0, 101) is None
    assert ce.gross_pct("BUY", 100, None) is None


def test_exit_at_tail_is_none():
    bars = [(0, 100.0), (300, 101.0), (600, 102.0)]
    # 目标时刻在数据尾部之后 → 持有期还没走完，不能返回最后一根
    assert ce.exit_at(bars, 900, 300) is None
    # 目标时刻已超出最后一根 K 线 → 也算"还没走完"，不许拿 600 的价冒充 700
    assert ce.exit_at(bars, 700, 300) is None
    # 正常命中：目标落在已收盘的 K 线上
    assert ce.exit_at(bars, 600, 300) == 102.0
    bars2 = [(0, 100.0), (300, 101.0), (600, 102.0), (900, 103.0)]
    assert ce.exit_at(bars2, 700, 300) == 102.0


def test_exit_at_gap_is_none():
    bars = [(0, 100.0), (300, 101.0), (600, 103.0), (3000, 110.0)]
    # 目标 2500，最近一根是 600 → 空洞 1900s > 2*900 → 跳过
    assert ce.exit_at(bars, 2500, 900) is None
    # 目标 2500 之后才有 3000 的 K 线 → 2500 处仍是空洞，跳过
    assert ce.exit_at(bars, 2500, 900) is None
    # 目标正好落在 3000 那根上才可用
    assert ce.exit_at(bars, 3000, 900) == 110.0


def test_exit_at_empty():
    assert ce.exit_at([], 100, 60) is None


def test_stats_basic():
    s = ce.stats([1.0, -0.5, 2.0, -1.0, 0.0])
    assert s["n"] == 5
    assert abs(s["avg"] - 0.3) < 1e-9
    assert s["win_rate"] == 0.4                     # 0 算不赢
    assert s["profit_factor"] == round(3.0 / 1.5, 3)
    assert s["best"] == 2.0 and s["worst"] == -1.0
    assert ce.stats([])["n"] == 0
    assert ce.stats([x for x in [None]])["n"] == 0


def test_verdict_needs_sample_and_significance():
    small = {"n": 5, "avg": 5.0, "t_stat": 99.0}
    assert "样本不足" in ce.verdict(small)
    assert "显著" in ce.verdict({"n": 100, "avg": 0.5, "t_stat": 2.4})
    assert "不显著" in ce.verdict({"n": 100, "avg": 0.5, "t_stat": 0.4})
    assert "亏钱" in ce.verdict({"n": 100, "avg": -0.3, "t_stat": -5.0})


def _bars_from(prices, start=1_000_000, step=300):
    return [(start + i * step, float(p)) for i, p in enumerate(prices)]


def test_evaluate_matrix_and_costs():
    entries = [
        {"asset": "BTC", "action": "BUY", "entry_ts": 1_000_000, "entry_price": 100.0},
        {"asset": "BTC", "action": "SELL", "entry_ts": 1_000_000, "entry_price": 100.0},
    ]
    # 价格一路上行到 103：BUY 赚 3%，SELL 亏约 2.91%
    bars = {"BTC": {tf: _bars_from([100, 100.5, 101, 102, 103]) for _l, _s, tf in ce.HORIZONS}}
    rep = ce.evaluate(entries, bars)
    cells = {(c["horizon"], c["cost_bps"]): c for c in rep["cells"]}
    h20 = cells[("20m", 0)]
    assert h20["net"]["n"] == 2
    # 毛均值 = (3% + (100/103-1)*100) / 2
    expect = (3.0 + (100 / 103 - 1) * 100) / 2
    assert abs(h20["gross"]["avg"] - expect) < 1e-3
    # 成本必须是从毛里减掉的，不能加
    h30 = cells[("20m", 30)]
    assert h30["net"]["avg"] == round(expect - 0.30, 4)
    assert h30["profitable"] is False
    # 只有 20m 这个持有期有足够长的 K 线，所以只该出 20m × 成本档 的格子
    assert {c["horizon"] for c in rep["cells"]} == {"20m"}
    assert len(rep["cells"]) == len(ce.COST_BPS_TIERS)

    # K 线给到 3 天以后，则每个持有期都应出格子
    long_bars = [(1_000_000 + i * 300, 100.0 + (i % 7) * 0.1) for i in range(900)]
    rep_long = ce.evaluate(entries, {"BTC": {tf: long_bars for _l, _s, tf in ce.HORIZONS}})
    assert len(rep_long["cells"]) == len(ce.HORIZONS) * len(ce.COST_BPS_TIERS)


def test_evaluate_skips_when_bars_short():
    entries = [{"asset": "BTC", "action": "BUY", "entry_ts": 1_000_000, "entry_price": 100.0}]
    bars = {"BTC": {"5m": _bars_from([100, 101])}}   # 只有 2 根，20m 之后没数据
    rep = ce.evaluate(entries, bars)
    assert rep["entries"] == 1
    assert rep["cells"] == []


def test_cost_to_move_ratio():
    # 平均波动 1%，成本 0.30% → 成本只占 0.3 倍波动（能覆盖）
    entries = [{"asset": "BTC", "action": "BUY", "entry_ts": 1_000_000, "entry_price": 100.0},
               {"asset": "BTC", "action": "SELL", "entry_ts": 1_000_000, "entry_price": 100.0}]
    bars = {"BTC": {tf: _bars_from([100, 101.5, 100, 100]) for _l, _s, tf in ce.HORIZONS}}
    rep = ce.evaluate(entries, bars)
    for c in rep["cells"]:
        if c["cost_bps"] == 30 and c["avg_abs_move"]:
            assert abs(c["cost_to_move"] - round(0.30 / c["avg_abs_move"], 3)) < 1e-9
