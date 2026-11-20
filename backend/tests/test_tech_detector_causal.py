# -*- coding: utf-8 -*-
"""tests/test_tech_detector_causal.py — v450 因果与技术信号打分口径回归。

背景：旧版 detector 的转折点用未来 3 根 K 线确认，并把 ts 回填到枢轴那根，
导致 9671/18548 条信号带 uses_future=1，只能整批剔除；同时所有信号 score
被加成封顶到 0.95，分不出强弱。

本测试锁定三件事：
  1. detect_signals 不再产出 uses_future=1 的行；
  2. 转折点的 ts 必须晚于枢轴（lag_bars ≥ reversal_pivot_window），不能回填；
  3. 分数必须有分布（不是全 0.95），且等级与分数单调对应。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tech_detector as td


def _bars(n: int = 400) -> list[dict]:
    """造一段有涨有跌、有放量、有拐点的合成日线。"""
    bars = []
    price = 100.0
    base_ts = 1_600_000_000
    for i in range(n):
        # 低频正弦 + 局部急涨急跌，制造枢轴与反转
        drift = math.sin(i / 18.0) * 0.9 + math.sin(i / 5.0) * 0.35
        if i % 37 == 0:
            drift += 2.6
        if i % 41 == 0:
            drift -= 2.6
        price *= 1 + drift / 100.0
        high = price * (1 + 0.012)
        low = price * (1 - 0.012)
        vol = 1000 + (4500 if i % 29 == 0 else 0)
        bars.append({
            "asset": "TEST", "ts": base_ts + i * 86400,
            "open": price, "high": high, "low": low, "close": price, "volume": vol,
        })
    return bars


def test_no_future_function_by_default():
    sigs = td.detect_signals(_bars())
    assert sigs, "合成序列应至少产出一条信号"
    assert all(s["uses_future"] is False for s in sigs)
    assert td.CONFIG["enable_reversal_future"] is False


def test_reversal_is_confirmed_not_backfilled():
    sigs = [s for s in td.detect_signals(_bars()) if s["trigger_type"] == "reversal"]
    assert sigs, "合成序列应产出转折点"
    pw = int(td.CONFIG["reversal_pivot_window"])
    for s in sigs:
        hit = next(h for h in s["signals"] if h["k"].startswith("reversal_"))
        info = hit["v"]
        assert info["lag_bars"] >= pw, "转折点最早只能在枢轴确认后发出"
        assert s["ts"] > info["pivot_ts"], "信号时间不得回填到枢轴"
        # 确认那根不能早于枢轴 + pw
        assert s["ts"] - info["pivot_ts"] >= pw * 86400


def test_scores_are_distributed():
    sigs = td.detect_signals(_bars())
    scores = {s["score"] for s in sigs}
    assert len(scores) >= 3, f"分数退化成常数：{scores}"
    assert max(scores) < 0.99 + 1e-9
    for s in sigs:
        g = s.get("grade") or td.grade_of(s["score"])
        if s["score"] >= 0.78:
            assert g == "A"
        elif s["score"] >= 0.58:
            assert g == "B"
        else:
            assert g == "C"


def test_event_families_dedupe_correlated_evidence():
    """pct_1d 与 pct_3d 同属 price 族，只能算 1 个独立证据，不能互相凑数。"""
    hits = [
        {"k": "pct_1d", "w": 1.0, "role": "event"},
        {"k": "pct_3d", "w": 1.0, "role": "event"},
        {"k": "supertrend_bull", "w": 0.6, "role": "context"},
    ]
    score, families, total = td._score_from_hits(hits)
    assert families == 1
    assert total == 1.0
    assert td.CONFIG["min_event_families"] >= 2
    assert 0 < score < 0.58
