# -*- coding: utf-8 -*-
"""v425：资金费率影子账本的记账口径测试。

最要紧的一条：**只向前记账**。用户口径是"中断后以恢复时刻为新起点，不补停机期堆积的历史"，
所以 activated_at 之前的极端点一条都不能进账本——这条如果写错，账本会凭空多出几十笔历史样本，
看起来"样本够了"，其实全是回填。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import funding_shadow as fs


def _series(vals, start=1_000_000, step=28_800):
    return [(start + i * step, float(v)) for i, v in enumerate(vals)]


def test_pick_signals_ignores_history_before_activation():
    # 前 45 个点里有大量极端值（如果补记，就会冒出一堆信号）
    vals = [0.0, 1.0, 2.0] * 14 + [9.0, 9.0]
    act = 1_000_000 + len(vals) * 28_800        # 激活点设在所有历史之后
    assert fs.pick_signals(_series(vals), act) == []


def test_pick_signals_only_new_points():
    vals = [0.0, 1.0, 2.0] * 14
    series = _series(vals)
    act = series[-1][0] + 1                      # 激活之后还没有新点
    assert fs.pick_signals(series, act) == []
    # 激活之后来一个极端高值 → 应该出一条做空信号
    s2 = series + [(series[-1][0] + 28_800, 9.0)]
    sig = fs.pick_signals(s2, act)
    assert len(sig) == 1 and sig[0]["direction"] == "SELL" and sig[0]["side"] == "high"


def test_pick_signals_low_side():
    # 默认只做"高拥挤→做空"这一侧（这是 v424 唯一有三重检验迹象的方向）。
    # 低侧要显式开 limit_side="both" 才会出信号。
    vals = [0.0, 1.0, 2.0] * 14
    series = _series(vals)
    act = series[-1][0] + 1
    s2 = series + [(series[-1][0] + 28_800, -9.0)]
    assert fs.pick_signals(s2, act) == []                       # 默认不记低侧
    sig = fs.pick_signals(s2, act, limit_side="both")
    assert len(sig) == 1 and sig[0]["direction"] == "BUY"


def test_settle_pnl_sign_conventions():
    # 做空：价格跌 1% → 毛赚约 1.01%，扣 30bps 后 0.71%，算命中
    st = fs.settle_pnl("SELL", 100.0, 99.0, 30.0)
    assert abs(st["gross"] - 1.0101) < 1e-3
    assert abs(st["net"] - 0.7101) < 1e-3
    assert st["hit"] == 1
    # 做空：价格涨 1% → 亏，不算命中
    st2 = fs.settle_pnl("SELL", 100.0, 101.0, 30.0)
    assert st2["hit"] == 0 and st2["net"] < 0
    # 做多方向对称
    st3 = fs.settle_pnl("BUY", 100.0, 101.0, 30.0)
    assert abs(st3["gross"] - 1.0) < 1e-9 and st3["hit"] == 1
    # 成本必须是从毛里减，而不是加
    assert st3["net"] == round(st3["gross"] - 0.3, 4)


def test_settle_pnl_rejects_bad_input():
    assert fs.settle_pnl("HOLD", 100.0, 101.0, 30.0) is None
    assert fs.settle_pnl("BUY", 0, 101.0, 30.0) is None
    assert fs.settle_pnl("SELL", 100.0, None, 30.0) is None
