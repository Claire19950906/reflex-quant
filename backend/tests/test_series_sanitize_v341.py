# -*- coding: utf-8 -*-
"""v341: 价格序列清洗 —— 尖刺 vs 拆股，必须分开处理。

真实数据里两种情况长得很像（都是一天掉 50%~90%），但处理方式相反：
  · 尖刺（脏数据）→ 该删，否则周围几十个窗口全变成假异动
  · 拆股（公司行为）→ 不能删，也不能当行情，只能把前面那段按比例归平
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import backfill_critical_moves as bcm

TS0 = 1_700_000_000


def _mk(closes):
    return [(TS0 + i * 86400, float(c)) for i, c in enumerate(closes)]


def _pct_jumps(prices):
    out = []
    for i in range(1, len(prices)):
        a, b = prices[i - 1][1], prices[i][1]
        if a > 0:
            out.append(abs((b - a) / a * 100.0))
    return out


def test_clean_series_is_untouched():
    closes = [100 + (i % 7) * 0.8 for i in range(60)]
    prices = _mk(closes)
    assert bcm.sanitize_price_series(prices, asset="X") == prices


def test_two_row_spike_is_dropped():
    """BTC 实测形状：中间两根错配价格（94900/94700），之后回到原水平。"""
    base = [35000 + (i % 5) * 60 for i in range(20)]
    closes = base[:10] + [94900.0, 94700.0] + [35406 + (i % 5) * 60 for i in range(10)]
    prices = _mk(closes)
    cleaned = bcm.sanitize_price_series(prices, asset="BTC")
    assert len(cleaned) == len(prices) - 2, "两根尖刺必须被剔除"
    assert all(34000 <= p[1] <= 40000 for p in cleaned), "剩下的必须是同一量纲"
    assert max(_pct_jumps(cleaned)) < 90


def test_split_level_shift_rescales_instead_of_dropping():
    """AAPL 7:1 形状：前面 645 一段，之后 93 一段 —— 不能删，要归平。"""
    pre = [640 + (i % 5) * 3 for i in range(20)]
    post = [93 + (i % 5) * 0.5 for i in range(20)]
    prices = _mk(pre + post)
    cleaned = bcm.sanitize_price_series(prices, asset="AAPL")
    assert len(cleaned) == len(prices), "拆股不能丢历史"
    assert max(_pct_jumps(cleaned)) < 20, f"归平后不应再有巨大跳变: {max(_pct_jumps(cleaned))}"
    # 归平后两端应落在同一水平（后段是新的量纲，前段被缩放到它附近）
    assert abs(cleaned[0][1] - cleaned[-1][1]) / cleaned[-1][1] < 0.2


def test_split_does_not_create_fake_slow_moves():
    """含拆股的序列，清洗后不应产出"+85% 慢涨"这种假异动。"""
    pre = [1200 + (i % 5) * 8 for i in range(30)]
    post = [120 + (i % 5) * 0.8 for i in range(30)]
    prices = _mk(pre + post)
    raw_moves = bcm.detect_moves(prices, scale=1.0)
    cleaned = bcm.sanitize_price_series(prices, asset="NVDA")
    moves = bcm.detect_moves(cleaned, scale=1.0)
    assert len(moves) == 0, f"归平后不该有异动, 实际 {len(moves)} 条"
    assert any(abs(m["move_pct"]) > 80 for m in raw_moves), "对照组：不清洗确实会出假异动"


def test_spike_does_not_waste_history():
    """尖刺只删那两根，不能把整段历史砍掉。"""
    base = [100 + (i % 4) for i in range(40)]
    closes = base[:20] + [10000.0] + base[20:]
    prices = _mk(closes)
    cleaned = bcm.sanitize_price_series(prices, asset="X")
    assert len(cleaned) == len(prices) - 1
