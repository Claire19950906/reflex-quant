"""v342: 国内可达行情兜底源（curl）单元测试。

只测解析与映射，不联网：把 market_data_fallback._curl_text / _json_from_curl
替换成固定字符串，验证新浪期货 JSONP 与 Binance klines 的解析口径。
"""

import json
import time

import pytest

from market import market_data_fallback as mdf


SINA_SAMPLE = (
    "/*<script>location.href='//sina.com';</script>*/\n"
    "var _GC=(["
    '{"date":"2026-11-13","open":"3850.1","high":"3900.5","low":"3840.0",'
    '"close":"3881.7","volume":"0","position":"0"},'
    '{"date":"2026-11-14","open":"3881.7","high":"3927.2","low":"3871.1",'
    '"close":"3887.6","volume":"0","position":"0"}'
    "]);"
)


def test_sina_parse_returns_ohlc_rows(monkeypatch):
    monkeypatch.setattr(mdf, "_curl_text", lambda *a, **k: SINA_SAMPLE)
    rows = mdf.fetch_sina_futures_daily("XAU", keep_days=400)
    assert len(rows) == 2
    ts, o, h, l, c, v = rows[-1]
    assert (o, h, l, c) == (3881.7, 3927.2, 3871.1, 3887.6)
    assert isinstance(ts, int) and v == 0


def test_sina_unmapped_asset_returns_empty(monkeypatch):
    monkeypatch.setattr(mdf, "_curl_text", lambda *a, **k: SINA_SAMPLE)
    assert mdf.fetch_sina_futures_daily("BTC") == []


def test_binance_parse_ms_to_seconds(monkeypatch):
    now_ms = int(time.time() * 1000)
    payload = [
        [now_ms - 86400_000, "100.0", "110.0", "90.0", "105.0", "12345", 0, "0", 7, "0", "0", "0"],
        [now_ms, "105.0", "111.0", "95.0", "108.0", "22345", 0, "0", 9, "0", "0", "0"],
    ]
    monkeypatch.setattr(mdf, "_json_from_curl", lambda *a, **k: payload)
    rows = mdf.fetch_binance_daily("ETH", keep_days=5)
    assert len(rows) == 2
    ts, o, h, l, c, v = rows[-1]
    assert ts == now_ms // 1000
    assert (o, h, l, c, v) == (105.0, 111.0, 95.0, 108.0, 22345)


def test_binance_retries_once_then_succeeds(monkeypatch):
    calls = {"n": 0}
    payload = [[int(time.time() * 1000), "1", "2", "0.5", "1.5", "10", 0, "0", 0, "0", "0", "0"]]

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("curl rc=56 reset")
        return payload

    monkeypatch.setattr(mdf, "_json_from_curl", flaky)
    rows = mdf.fetch_binance_daily("BTC", keep_days=5)
    assert calls["n"] == 2
    assert len(rows) == 1
