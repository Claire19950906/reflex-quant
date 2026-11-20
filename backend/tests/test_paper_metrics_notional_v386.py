"""v386 口径统一回归：单笔盈亏必须用这笔仓位自己的名义金额。

事故背景 (2026-11-16)：
    · 引擎按 quick_sim_trades.notional_usdt=100 结算，账本累计 -4.31 USDT；
    · 前端 `_paper_metrics` 一律取 strategy_params.notional_usdt=5000，
      同一批交易显示成 -215.28 USDT、权益 9784.72；
    · 两个数字同时"正确"，用户无法判断哪个是真的 —— 对以"可审计"为卖点的
      系统来说，这是比亏钱更严重的问题。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import api_server


def _settled_signal(notional=None, strategy_notional=5000.0):
    sig = {
        "entry_price": 88.985,
        "action": "BUY",
        "settled": 1,
        "forward_pnl": -3.1713,
        "strategy_params": {"notional_usdt": strategy_notional},
    }
    if notional is not None:
        sig["notional_usdt"] = notional
    return sig


def test_paper_metrics_uses_trade_notional_not_strategy_notional():
    """仓位自己的名义金额优先 —— 账户口径与结算账本一致。"""
    out = api_server._paper_metrics(_settled_signal(notional=100.0), None)
    assert out["current_pnl_usdt"] == -3.1713   # 100 * -3.1713%


def test_paper_metrics_falls_back_to_strategy_notional_when_missing():
    """没有仓位级 notional 时（例如尚未开仓的信号），才退策略默认值。"""
    out = api_server._paper_metrics(_settled_signal(strategy_notional=5000.0), None)
    assert out["current_pnl_usdt"] == round(5000.0 * -3.1713 / 100, 4)


def test_paper_metrics_ignores_zero_trade_notional():
    """notional_usdt=0 视为缺省，不能把盈亏算成 0。"""
    out = api_server._paper_metrics(_settled_signal(notional=0.0), None)
    assert out["current_pnl_usdt"] == round(5000.0 * -3.1713 / 100, 4)
