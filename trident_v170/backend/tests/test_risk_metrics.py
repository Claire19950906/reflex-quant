"""v176 — Portfolio 风险指标测试（drawdown / sharpe / winrate）。"""
from __future__ import annotations

import math
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import risk_metrics as rm



def test_drawdown_empty_returns_zero():
    """空序列：所有指标=0。"""
    d = rm.compute_drawdown([])
    assert d == {
        "max_drawdown": 0.0,
        "current_drawdown": 0.0,
        "peak_pnl": 0.0,
        "trough_pnl": 0.0,
    }


def test_drawdown_all_positive_no_drawdown():
    """全部盈利 → max_drawdown=0。"""
    d = rm.compute_drawdown([0.02, 0.01, 0.03, 0.015])
    assert d["max_drawdown"] == 0.0
    assert d["current_drawdown"] == 0.0
    assert abs(d["peak_pnl"] - 0.075) < 1e-6


def test_drawdown_peak_then_drop():
    """先涨 +5% 后跌 -7% → max_drawdown=7%。"""
    d = rm.compute_drawdown([0.05, -0.07])
    assert abs(d["max_drawdown"] - 0.07) < 1e-6
    assert abs(d["current_drawdown"] - 0.07) < 1e-6
    assert abs(d["peak_pnl"] - 0.05) < 1e-6


def test_drawdown_recovery_from_initial_loss():
    """首笔 -10% 后涨 +5%, +5% → 从 -0.10 涨到 0 中无回撤（peak 跟着涨）。"""
    d = rm.compute_drawdown([-0.10, 0.05, 0.05])
    assert d["max_drawdown"] == 0.0
    assert d["current_drawdown"] == 0.0


def test_drawdown_current_vs_max_can_differ():
    """中段回撤后新高 → current_drawdown=0, max_drawdown>0。"""
    d = rm.compute_drawdown([0.05, -0.03, 0.05])
    assert abs(d["max_drawdown"] - 0.03) < 1e-6
    assert d["current_drawdown"] == 0.0


def test_drawdown_typical_realistic():
    """cum 0→50→20→100→70→80：max_dd=30，current_dd=20。"""
    pnl = [0.50, -0.30, 0.80, -0.30, 0.10]
    d = rm.compute_drawdown(pnl)
    assert abs(d["max_drawdown"] - 0.30) < 1e-6
    assert abs(d["current_drawdown"] - 0.20) < 1e-6


def test_sharpe_empty_returns_neg_one():
    """空 → -1（标记无意义）。"""
    assert rm.compute_sharpe([]) == -1.0


def test_sharpe_single_trade_returns_neg_one():
    """单笔无法计算 std → -1。"""
    assert rm.compute_sharpe([0.05]) == -1.0


def test_sharpe_identical_returns_returns_neg_one():
    """全部相等 pnl → std=0 → -1（避免除零）。"""
    assert rm.compute_sharpe([0.02, 0.02, 0.02, 0.02]) == -1.0


def test_sharpe_simple_known_value():
    """手算 [0.02, -0.02]：mean=0 → sharpe=0。"""
    assert abs(rm.compute_sharpe([0.02, -0.02])) < 1e-6


def test_sharpe_all_positive_positive_value():
    """全正且波动小 → sharpe > 0。"""
    s = rm.compute_sharpe([0.01, 0.012, 0.008, 0.011])
    assert s > 0


def test_sharpe_all_negative_negative_value():
    """全负 → sharpe < 0。"""
    s = rm.compute_sharpe([-0.01, -0.012, -0.008, -0.011])
    assert s < 0


def test_sharpe_periods_per_year_scales():
    """periods_per_year 越大 → sharpe 越大（年化放大）。"""
    short = rm.compute_sharpe([0.02, -0.01, 0.015], periods_per_year=252.0)
    long_ = rm.compute_sharpe([0.02, -0.01, 0.015], periods_per_year=365.0)
    assert abs(long_ / short - math.sqrt(365.0 / 252.0)) < 0.01


def test_sharpe_risk_free_subtracts():
    """risk_free_per_period 从均值中扣除 → sharpe 变小。"""
    s_no_rf = rm.compute_sharpe([0.03, 0.04, 0.02, 0.05])
    s_with_rf = rm.compute_sharpe([0.03, 0.04, 0.02, 0.05], risk_free_per_period=0.03)
    assert s_with_rf < s_no_rf

    """cum 0→50→20→100→70→80：max_dd=30，current_dd=20。"""
    pnl = [0.50, -0.30, 0.80, -0.30, 0.10]
def test_winrate_empty_returns_zero():
    """空 → 全 0。"""
    w = rm.compute_winrate([])
    assert w == {
        "winrate": 0.0,
        "total_pnl": 0.0,
        "n_trades": 0,
        "n_wins": 0,
        "n_losses": 0,
        "n_breakeven": 0,
    }


def test_winrate_all_wins_100_percent():
    """全胜 → winrate=1.0。"""
    w = rm.compute_winrate([0.02, 0.01, 0.005, 0.03])
    assert w["winrate"] == 1.0
    assert w["n_wins"] == 4
    assert w["n_losses"] == 0
    assert abs(w["total_pnl"] - 0.065) < 1e-6


def test_winrate_all_losses_0_percent():
    """全败 → winrate=0.0。"""
    w = rm.compute_winrate([-0.02, -0.01, -0.005])
    assert w["winrate"] == 0.0
    assert w["n_wins"] == 0
    assert w["n_losses"] == 3


def test_winrate_breakeven_excluded_from_rate():
    """pnl=0 不计入分母：3 笔（2W/1BE）→ winrate=1.0。"""
    w = rm.compute_winrate([0.02, 0.01, 0.0])
    assert w["winrate"] == 1.0
    assert w["n_breakeven"] == 1
    assert w["n_trades"] == 3


def test_winrate_mixed_typical():
    """典型 6W / 4L → winrate=0.6。"""
    pnl = [+0.02]*6 + [-0.01]*4
    w = rm.compute_winrate(pnl)
    assert w["winrate"] == 0.6
    assert w["n_trades"] == 10
    assert w["n_wins"] == 6
    assert w["n_losses"] == 4

def test_portfolio_metrics_includes_all():
    """聚合接口必须含 drawdown / sharpe / winrate 三个子字段。"""
    m = rm.compute_portfolio_metrics([0.02, -0.01, 0.03, -0.005])
    assert "max_drawdown" in m
    assert "current_drawdown" in m
    assert "sharpe" in m
    assert "winrate" in m
    assert "total_pnl" in m
    assert "n_trades" in m


def test_portfolio_metrics_consistency():
    """聚合接口各子指标应与独立函数结果一致。"""
    pnl = [0.02, -0.01, 0.03, -0.005, 0.015, -0.02, 0.008]
    m = rm.compute_portfolio_metrics(pnl)
    assert m["max_drawdown"] == rm.compute_drawdown(pnl)["max_drawdown"]
    assert m["sharpe"] == rm.compute_sharpe(pnl)
    assert m["winrate"] == rm.compute_winrate(pnl)["winrate"]


def test_portfolio_metrics_empty_safe():
    """空序列 → 聚合接口仍可调用，sharpe=-1。"""
    m = rm.compute_portfolio_metrics([])
    assert m["max_drawdown"] == 0.0
    assert m["sharpe"] == -1.0
    assert m["winrate"] == 0.0
    assert m["n_trades"] == 0


def test_portfolio_metrics_typical_realistic():
    """真实场景：15 笔混合（10W / 4L / 1BE）→ winrate≈0.714。"""
    pnl = [+0.02]*10 + [-0.01]*4 + [0.0]
    m = rm.compute_portfolio_metrics(pnl)
    assert abs(m["winrate"] - 0.7142857) < 0.001
    assert abs(m["total_pnl"] - (10*0.02 - 4*0.01)) < 1e-6
    assert m["n_trades"] == 15
    assert m["n_breakeven"] == 1
def test_drawdown_handles_none_in_series():
    """序列中含 None 应视为 0（不抛错）。"""
    d = rm.compute_drawdown([0.02, None, 0.03])
    assert abs(d["peak_pnl"] - 0.05) < 1e-6
    assert d["max_drawdown"] == 0.0


def test_sharpe_handles_none_in_series():
    """序列中含 None 应视为 0（不抛错）。"""
    s = rm.compute_sharpe([0.02, None, 0.04])
    assert s > 0


def test_winrate_handles_none_in_series():
    """序列中含 None 应视为 0（计入 breakeven）。"""
    w = rm.compute_winrate([0.02, None, -0.01])
    assert w["n_trades"] == 3
    assert w["n_wins"] == 1
    assert w["n_losses"] == 1
    assert w["n_breakeven"] == 1

