# -*- coding: utf-8 -*-
"""v406 — 整点"进行中 K 线"不再被误判成冻结报价。

背景（2026-11-17 06:45 实测）：XAU 87 条 + WTI 97 条决策的硬数据状态是
`missing:feed_stale`，但本地 1m 线是新鲜的。根因是整点刚过时最新一根 1m 线
只含一笔成交，open=high=low=close，被 quality_notes 判成"冻结报价" →
is_usable False → 转网络（拿不到）→ 证据为空。

验收：
1. 当前小时只有 1~2 根且无振幅 → 回退到最近一根走完的 K 线，证据可用；
2. 整个小时都无振幅 → 仍判冻结，绝不把冻住的数当盘面；
3. 不在当前小时的无振幅行 → 保持原样（+过期提示）。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import hard_data_block as hdb


def _bar(ts, o, h, l, c, v=0.0):
    return (ts, o, h, l, c, v)


NOW = 1_794_866_400 + 5 * 60        # 某一小时的第 5 分钟
HOUR = int(NOW) // 3600 * 3600


def test_forming_bar_falls_back_to_last_closed():
    rows = [
        _bar(HOUR + 60, 3856.24, 3856.24, 3856.24, 3856.24, 71),   # 本小时第 1 根，四价全等
        _bar(HOUR - 60, 3855.85, 3858.66, 3848.94, 3855.85, 6302),  # 上一小时最后一根
        _bar(HOUR - 120, 3851.49, 3861.6, 3851.08, 3861.49, 10608),
    ]
    sel = hdb.select_bar(rows, NOW)
    assert sel[0] == HOUR - 60, "应回退到最近一根走完的 K 线"
    block = hdb.build_block("XAU", sel, rows[1:], now_ts=NOW)
    assert "不作方向依据" not in block
    assert hdb.is_usable(block)


def test_truly_frozen_hour_still_flagged():
    rows = [_bar(HOUR + i * 60, 10.0, 10.0, 10.0, 10.0, 0) for i in range(3)]
    sel = hdb.select_bar(rows, NOW)
    assert sel == rows[0], "整小时无振幅 = 真冻结，不许回退"
    block = hdb.build_block("NVDA", sel, rows, now_ts=NOW)
    assert "该K线无振幅（冻结报价）" in block
    assert not hdb.is_usable(block)


def test_non_flat_top_is_kept():
    rows = [
        _bar(HOUR + 60, 100.0, 101.0, 99.0, 100.5, 20),
        _bar(HOUR, 99.0, 100.0, 98.0, 99.5, 30),
    ]
    assert hdb.select_bar(rows, NOW) == rows[0]


def test_old_flat_bar_keeps_its_warning():
    old = HOUR - 48 * 3600
    rows = [_bar(old, 10.0, 10.0, 10.0, 10.0, 0),
            _bar(old - 60, 10.0, 11.0, 9.9, 10.5, 5)]
    sel = hdb.select_bar(rows, NOW)
    assert sel == rows[0], "不在当前小时的冻结行不能被回退洗白"
    block = hdb.build_block("WTI", sel, rows, now_ts=NOW)
    assert "不作方向依据" in block


def test_empty_rows():
    assert hdb.select_bar([], NOW) is None
