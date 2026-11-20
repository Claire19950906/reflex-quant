#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v341: 品种级阈值政策 + 加密口径（无拆股 / 500% 上限）。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import asset_threshold_policy as atp
import backfill_critical_moves as b


def _ramp(n=400, start=100.0, step=0.6):
    return [(1700000000 + i * 86400, start + i * step) for i in range(n)]


# ---------- has_splits ----------
def test_crypto_has_no_splits():
    for a in ("BTC", "ETH", "DOGE", "XRP", "SOL", "BTC-USD", "doge-usdt"):
        assert b.has_splits(a) is False, a


def test_equity_has_splits():
    for a in ("AAPL", "NVDA", "TSLA", "XAU", "WTI", "SPX"):
        assert b.has_splits(a) is True, a


# ---------- sanitize ----------
def test_crypto_level_jump_not_rescaled():
    prices = [(1, 0.18), (2, 0.19), (3, 0.3655), (4, 0.37), (5, 0.38)]
    out = b.sanitize_price_series(prices, asset="DOGE")
    assert out == prices  # 真实 2 倍行情，不许归平


def test_equity_level_jump_is_rescaled():
    prices = [(1, 100.0), (2, 100.5), (3, 25.0), (4, 25.2), (5, 25.4)]
    out = b.sanitize_price_series(prices, asset="AAPL")
    # 归平的目标是"跳变处连续"，不是把历史价换成今天的绝对价
    assert abs(out[1][1] - out[2][1]) < 1e-6
    assert out[0][1] < 100.0


def test_spike_still_removed_for_crypto():
    prices = [(1, 10.0), (2, 10.2), (3, 99.0), (4, 10.1), (5, 10.3)]
    out = b.sanitize_price_series(prices, asset="BTC")
    assert 99.0 not in [v for _, v in out]
    assert len(out) == 4


def test_long_gap_growth_is_not_split():
    """AAPL 实测：32.20→91.13 跨 808 天，是几年真实涨幅，不许改写前段历史。"""
    day = 86400
    prices = [(1000, 32.2), (1000 + 808 * day, 91.13), (1000 + 809 * day, 91.5)]
    out = b.sanitize_price_series(prices, asset="AAPL")
    assert out == prices


def test_adjacent_split_still_adjusted():
    day = 86400
    prices = [(1000, 645.57), (1000 + 3 * day, 93.70), (1000 + 4 * day, 94.0)]
    out = b.sanitize_price_series(prices, asset="AAPL")
    assert abs(out[0][1] - 93.70) < 1e-6         # 跳变前一根被归平到新水平 → 连续
    assert out[1][1] == 93.70 and out[2][1] == 94.0   # 拆股后的真实价格不许动


# ---------- detect_moves ----------
def test_higher_thresh_yields_fewer_or_equal_moves():
    prices = _ramp()
    base = b.detect_moves(prices, scale=1.0, asset="BTC")
    tight = b.detect_moves(prices, scale=1.0, thresh_1d=8.0, asset="BTC")
    assert len(tight) <= len(base)


def test_crypto_cap_allows_double():
    prices = [(1700000000, 0.1), (1700086400, 0.22)]  # +120% 单日
    moves = b.detect_moves(prices, scale=0.67, asset="DOGE")
    assert [m for m in moves if m["move_pct"] > 100], "加密 +120% 不该被当污染丢掉"


def test_equity_cap_still_blocks_absurd():
    prices = [(1700000000, 1.0), (1700086400, 5.0)]  # +400% 对股票不合理
    assert b.detect_moves(prices, scale=0.67, asset="AAPL") == []


# ---------- policy ----------
def test_policy_none_without_map(tmp_path, monkeypatch):
    atp.clear_cache()
    monkeypatch.setattr(atp, "MAP_GLOB", str(tmp_path / "none_*.json"))
    monkeypatch.setenv("TRIDENT_THRESH_POLICY", "on")
    assert atp.load_map() is None
    assert atp.policy("BTC") is None


def test_policy_reads_verdict_and_thresh(tmp_path, monkeypatch):
    atp.clear_cache()
    f = tmp_path / "threshold_map_test.json"
    f.write_text(json.dumps({"assets": {
        "BTC": {"pick": {"verdict": "use", "thresh": 5, "net_edge_pct": 0.64,
                         "winrate": 51.2, "n": 900}},
        "ETH": {"pick": {"verdict": "insufficient"}},
    }}), encoding="utf-8")
    monkeypatch.setattr(atp, "MAP_GLOB", str(tmp_path / "threshold_map_*.json"))
    monkeypatch.setenv("TRIDENT_THRESH_POLICY", "on")
    p = atp.policy("btc-usd")
    assert p and p["thresh"] == 5.0 and p["verdict"] == "use"
    assert atp.policy("ETH") is None          # 样本不足 → 不干预
    assert atp.policy("ZZZ_UNKNOWN") is None  # 不在矩阵里 → 不干预


def test_policy_kill_switch(tmp_path, monkeypatch):
    atp.clear_cache()
    (tmp_path / "threshold_map_k.json").write_text(
        json.dumps({"assets": {"BTC": {"pick": {"verdict": "use", "thresh": 5}}}}),
        encoding="utf-8")
    monkeypatch.setattr(atp, "MAP_GLOB", str(tmp_path / "threshold_map_*.json"))
    monkeypatch.setenv("TRIDENT_THRESH_POLICY", "off")
    assert atp.policy("BTC") is None


def test_describe_handles_missing(monkeypatch, tmp_path):
    atp.clear_cache()
    monkeypatch.setattr(atp, "MAP_GLOB", str(tmp_path / "x_*.json"))
    assert "无政策" in atp.describe("BTC")
