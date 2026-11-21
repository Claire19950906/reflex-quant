#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v535：K 线源适配层从 api_server 拆出后的行为测试。

拆出去（`kline_sources.py`）的是外部行情适配：币安/Bybit/OKX/Bitstamp/Yahoo/金十，
只做 HTTP + 解析 + 短期 TTL，不碰决策链。这里钉三件事：

① **拆完还是一份实现**：`api._fetch_binance_klines is ks._fetch_binance_klines`，
   缓存 dict 也必须是同一个对象 —— 否则两份 5s 缓存各存各的，同一根 K 线会被拉两次。
② **纯函数口径**：1m → 15m/1h 聚合的开高低收量口径（桶内首开、末收、极值、量累加）。
③ **失败就返回 None / []**：上游拿不到时不造数据，由调用方降级。
"""
import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import kline_sources as ks  # noqa: E402


def _bar(t, o, h, l, c, v=10):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ── ① 只有一份实现（拆完不能变成两份）──────────────────────────────
def test_api_server_reexports_the_same_function_objects():
    import api_server as api

    for name in ("_fetch_binance_klines", "_fetch_yahoo_history_bars",
                 "_jin10_klines_bars", "_aggregate_bars", "_bybit_paged",
                 "_okx_klines_bars", "_bitstamp_klines_bars"):
        assert getattr(api, name) is getattr(ks, name), name


def test_api_server_shares_the_kline_cache_object():
    import api_server as api

    # 同一个 dict：两个模块各清各的缓存会让"5s 内只拉一次"失效
    assert api._BINANCE_KLINE_CACHE is ks._BINANCE_KLINE_CACHE
    assert api._BINANCE_KLINE_TTL_S == ks._BINANCE_KLINE_TTL_S


def test_paged_history_stays_beside_store_klines():
    """分页拉取要落库（_store_klines 在 api_server），所以它留在原模块。"""
    import api_server as api

    assert not hasattr(ks, "_paged_history")
    assert callable(api._paged_history)


# ── ② 聚合口径 ───────────────────────────────────────────────────
def test_aggregate_bars_buckets_by_period_with_ohlc_semantics():
    bars = [
        _bar(0, 100, 101, 99, 100.5, 5),
        _bar(60, 100.5, 102, 100, 101, 6),
        _bar(120, 101, 103, 97, 98, 7),
        _bar(900, 98, 99, 96, 97.5, 8),     # 下一个 15m 桶
    ]
    agg = ks._aggregate_bars(bars, 900)
    assert len(agg) == 2
    first, second = agg
    assert first["time"] == 0
    assert first["open"] == 100 and first["close"] == 98      # 首开、末收
    assert first["high"] == 103 and first["low"] == 97         # 极值
    assert first["volume"] == 18                               # 量累加
    assert second["time"] == 900 and second["volume"] == 8


def test_aggregate_bars_keeps_the_incomplete_last_bucket():
    # 最后一根桶没走完也要保留：与真实行情一致，缺了它图上会少一段
    bars = [_bar(3600, 1, 2, 1, 2), _bar(3720, 2, 3, 2, 3)]
    agg = ks._aggregate_bars(bars, 3600)
    assert len(agg) == 1 and agg[0]["time"] == 3600
    assert agg[0]["close"] == 3 and agg[0]["high"] == 3


def test_aggregate_bars_handles_gaps_and_empty():
    assert ks._aggregate_bars([], 900) == []
    # 中间缺桶：不补齐、不插空 bar（缺就是缺，不许造）
    agg = ks._aggregate_bars([_bar(0, 1, 1, 1, 1), _bar(1800, 2, 2, 2, 2)], 900)
    assert [a["time"] for a in agg] == [0, 1800]


def test_aggregate_bars_tolerates_missing_volume():
    bars = [{"time": 0, "open": 1, "high": 1, "low": 1, "close": 1},
            {"time": 60, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 2}]
    assert ks._aggregate_bars(bars, 900)[0]["volume"] == 2


# ── ③ 拿不到就降级，不造数据 ─────────────────────────────────────
def test_binance_klines_returns_none_for_unroutable_asset():
    ks._BINANCE_KLINE_CACHE.clear()
    assert ks._fetch_binance_klines("UNKNOWN_XYZ", "1h") is None


def test_binance_klines_maps_interval_to_yahoo_params(monkeypatch):
    """前端周期 → Yahoo 周期/根数：1h/4h 都走 60m（4h 少取根数），1d 走 1d。"""
    ks._BINANCE_KLINE_CACHE.clear()
    seen = []

    def _yahoo(asset, interval, limit):
        seen.append((asset, interval, limit))
        return [_bar(0, 120, 121, 119, 120.5)]

    monkeypatch.setattr(ks, "_fetch_yahoo_history_bars", _yahoo)
    out_1h = ks._fetch_binance_klines("US_HYNIX", "1h")
    out_4h = ks._fetch_binance_klines("US_HYNIX", "4h")
    out_1d = ks._fetch_binance_klines("US_HYNIX", "1d")
    assert out_1h and out_4h and out_1d
    assert seen == [("US_HYNIX", "60m", 48), ("US_HYNIX", "60m", 36), ("US_HYNIX", "1d", 120)]


def test_binance_klines_returns_none_when_every_source_fails(monkeypatch):
    ks._BINANCE_KLINE_CACHE.clear()
    monkeypatch.setattr(ks, "_fetch_yahoo_history_bars", lambda a, i, l: [])
    monkeypatch.setattr(ks, "_binance_fapi_klines", lambda s, i: None)
    monkeypatch.setattr(ks, "_binance_spot_klines", lambda s, i: None)
    monkeypatch.setattr(ks, "_bybit_klines_bars", lambda s, i: None)
    monkeypatch.setattr(ks, "_bitstamp_klines_bars", lambda s, i: None)
    assert ks._fetch_binance_klines("US_HYNIX", "1h") is None


def test_paged_history_guards_bad_input():
    import api_server as api

    assert api._paged_history("BTC", "1h", 0) == []          # 目标根数 <= 0 直接空
    api._PAGED_HISTORY_CACHE.clear()
    out = api._paged_history("UNKNOWN_XYZ", "1h", 100)       # 无路由 → 空，不造数据
    assert out == []
