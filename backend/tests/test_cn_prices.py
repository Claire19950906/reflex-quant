# -*- coding: utf-8 -*-
"""tests/test_cn_prices.py — 国内直连行情源 (v324)

全部离线: 只测纯解析与兜底接线, 不发真实网络请求。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cn_prices as cp


# ── _to_float ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("178.67", 178.67), ("85.27", 85.27), ("0", None), ("-1.5", None),
    ("", None), ("-", None), ("null", None), ("abc", None), (" 12.5 ", 12.5),
])
def test_to_float(raw, expected):
    assert cp._to_float(raw) == expected


# ── 腾讯解析 ──────────────────────────────────────────────────────────────
def test_parse_tencent_tilde_format():
    """指数/美股/港股: `~` 分隔, 现价索引 3。"""
    txt = 'v_usNVDA="200~英伟达~NVDA.OQ~178.67~186.64~173.07~462719328~0~0~180.50"'
    assert cp.parse_tencent(txt)["usNVDA"] == pytest.approx(178.67)


def test_parse_tencent_futures_comma_format():
    """期货 hf_*: `,` 分隔, 现价索引 0。"""
    txt = 'v_hf_CL="85.27,-1.82,85.31,85.32,86.93,84.83,16:03:02,86.85"'
    assert cp.parse_tencent(txt)["hf_CL"] == pytest.approx(85.27)


def test_parse_tencent_multiple_symbols():
    txt = ('v_hf_CL="85.27,-1.82,85.31";'
           'v_hf_GC="4123.18,0.21,4123.30";'
           'v_sh000300="1~沪深300~000300~4059.11~4056.11~4089.05~150385451"')
    out = cp.parse_tencent(txt)
    assert out["hf_CL"] == pytest.approx(85.27)
    assert out["hf_GC"] == pytest.approx(4123.18)
    assert out["sh000300"] == pytest.approx(4059.11)


def test_parse_tencent_ignores_garbage():
    assert cp.parse_tencent("") == {}
    assert cp.parse_tencent("random text") == {}
    assert cp.parse_tencent('v_x=""') == {}
    assert cp.parse_tencent('v_x="abc~def~ghi~nan"') == {}


def test_parse_tencent_zero_price_dropped():
    """价格为 0 视为无效 (停牌/无数据), 丢弃而不是返回 0。"""
    assert cp.parse_tencent('v_x="1~a~b~0~c~d"') == {}


# ── 新浪解析 ──────────────────────────────────────────────────────────────
def test_parse_sina_a_share_index():
    """A股指数: 名称,今开,昨收,现价,... → 索引 3。"""
    txt = ('var hq_str_sh000001="上证指数,3682.0369,3660.7917,3663.7235,3683.7262,'
           '3660.7167,0,0,404865177,673951893240,0,0,2026-11-10,15:35:57,00,";')
    assert cp.parse_sina(txt)["sh000001"] == pytest.approx(3663.7235)


def test_parse_sina_us_stock():
    """美股 gb_*: 名称,现价,涨跌幅,时间,... → 索引 1。"""
    txt = ('var hq_str_gb_nvda="英伟达,178.6700,-4.27,2026-11-10 09:37:22,'
           '-7.9700,173.0650,179.9200,170.2400,243.3700,164.0200,462719289";')
    assert cp.parse_sina(txt)["gb_nvda"] == pytest.approx(178.67)


def test_parse_sina_futures():
    """期货 hf_*: 现价,买价,卖价,... → 索引 0。"""
    txt = 'var hq_str_hf_CL="85.289,,85.330,85.340,86.930,84.830,16:05:26,86.850";'
    assert cp.parse_sina(txt)["hf_CL"] == pytest.approx(85.289)

def test_parse_sina_hk_stock():
    """港股 rt_*: 代码,名称,今开,昨收,最高,最低,现价 → 索引 6。"""
    txt = ('var hq_str_rt_hkHSI="HSI,恒生指数,24621.130,24448.951,24675.530,'
           '24471.500,24522.990,74.040,0.300";')
    assert cp.parse_sina(txt)["rt_hkHSI"] == pytest.approx(24522.990)


def test_parse_sina_multiple():
    txt = ('var hq_str_sh000300="沪深300,4089.0521,4056.1081,4059.1105,4090.3793";'
           'var hq_str_hf_GC="4124.025,,4124.600,4124.900";')
    out = cp.parse_sina(txt)
    assert out["sh000300"] == pytest.approx(4059.1105)
    assert out["hf_GC"] == pytest.approx(4124.025)


def test_parse_sina_ignores_garbage():
    assert cp.parse_sina("") == {}
    assert cp.parse_sina("nothing here") == {}
    assert cp.parse_sina('var hq_str_x=""') == {}


# ── 资产映射 ──────────────────────────────────────────────────────────────
def test_asset_symbols_known():
    assert cp.asset_symbols("NVDA") == ("usNVDA", "gb_nvda")
    assert cp.asset_symbols("shcomp") == ("sh000001", "sh000001")
    assert cp.asset_symbols("XAU")[0] == "hf_GC"


def test_asset_symbols_unknown_returns_none_pair():
    assert cp.asset_symbols("NOT_A_THING") == (None, None)
    assert cp.asset_symbols("") == (None, None)


def test_asset_label():
    assert cp.asset_label("XAU") == "黄金"
    assert cp.asset_label("NVDA") == "NVDA"


# ── 前收价解析 (v324: 多源实测锁定) ───────────────────────────────────────
def test_parse_tencent_pairs_us_stock():
    """腾讯 ~ 载荷: parts[3]=现价, parts[4]=昨收。"""
    txt = 'v_usNVDA="200~英伟达~NVDA.OQ~178.67~186.64~173.07~462719328~0~0~180.50"'
    price, prev = cp.parse_tencent_pairs(txt)["usNVDA"]
    assert price == pytest.approx(178.67)
    assert prev == pytest.approx(186.64)


def test_parse_tencent_pairs_futures_prev_is_index7():
    """腾讯 hf_ 期货: parts[1]=**涨跌幅%**, parts[7]=昨收 (不是 parts[1])。

    实测载荷: 4090.72 / -0.58 / ... / 4114.50(昨收) / 4149.00(开盘)
    校验: (4090.72-4114.50)/4114.50 = -0.578% ≈ parts[1]
    """
    txt = ('v_hf_GC="4090.72,-0.58,4091.00,4091.20,4156.20,4088.20,18:10:00,'
           '4114.50,4149.00,0,1,2,2026-11-10,纽约黄金"')
    price, prev = cp.parse_tencent_pairs(txt)["hf_GC"]
    assert price == pytest.approx(4090.72)
    assert prev == pytest.approx(4114.50)
    # 涨跌幅% 与前收自洽
    assert (price - prev) / prev * 100.0 == pytest.approx(-0.58, abs=0.01)


def test_parse_tencent_pairs_futures_oil():
    """腾讯 hf_CL: 现价 84.44, 涨跌幅 -2.78%, 昨收 86.85。"""
    txt = ('v_hf_CL="84.44,-2.78,84.50,84.52,86.93,83.88,18:10:03,'
           '86.85,86.43,0,6,3,2026-11-10,纽约原油"')
    price, prev = cp.parse_tencent_pairs(txt)["hf_CL"]
    assert price == pytest.approx(84.44)
    assert prev == pytest.approx(86.85)
    assert (price - prev) / prev * 100.0 == pytest.approx(-2.78, abs=0.01)


def test_parse_sina_pairs_us_stock_derives_prev():
    """新浪 gb_: parts[1]=现价, parts[4]=涨跌额 → 昨收 = 现价 - 涨跌额。"""
    txt = ('var hq_str_gb_nvda="英伟达,178.6700,-4.27,2026-11-10 09:37:22,'
           '-7.9700,173.0650,179.9200,170.2400,243.3700,164.0200,462719289";')
    price, prev = cp.parse_sina_pairs(txt)["gb_nvda"]
    assert price == pytest.approx(178.67)
    assert prev == pytest.approx(186.64)


def test_parse_sina_pairs_futures_prev_is_index7():
    """新浪 hf_: parts[6]=时间串, parts[7]=昨收 (曾误取 parts[6] → 前收恒 None)。"""
    txt = 'var hq_str_hf_CL="85.289,,85.330,85.340,86.930,84.830,16:05:26,86.850";'
    price, prev = cp.parse_sina_pairs(txt)["hf_CL"]
    assert price == pytest.approx(85.289)
    assert prev == pytest.approx(86.850)


def test_parse_sina_pairs_a_share_index():
    """新浪 sh/sz: parts[3]=现价, parts[2]=昨收。"""
    txt = ('var hq_str_sh000001="上证指数,3682.0369,3660.7917,3663.7235,3683.7262,'
           '3660.7167,0,0,404865177,673951893240,0,0,2026-11-10,15:35:57,00,";')
    price, prev = cp.parse_sina_pairs(txt)["sh000001"]
    assert price == pytest.approx(3663.7235)
    assert prev == pytest.approx(3660.7917)


def test_parse_sina_pairs_hk_stock():
    """新浪 rt_ 港股: parts[6]=现价, parts[3]=昨收。"""
    txt = ('var hq_str_rt_hkHSI="HSI,恒生指数,24621.130,24448.951,24675.530,'
           '24471.500,24522.990,74.040,0.300";')
    price, prev = cp.parse_sina_pairs(txt)["rt_hkHSI"]
    assert price == pytest.approx(24522.990)
    assert prev == pytest.approx(24448.951)


def test_parse_pairs_missing_prev_is_none():
    """字段不足时前收为 None, 不是 0 也不是现价。"""
    txt = 'v_hf_X="85.29"'
    price, prev = cp.parse_tencent_pairs(txt)["hf_X"]
    assert price == pytest.approx(85.29)
    assert prev is None


def test_asset_symbols_mu_alias():
    """US_HYNIX 是 MU 的美股口径 (见 feed_redund_policy 注释)。"""
    assert cp.asset_symbols("MU") == ("usMU", "gb_mu")
    assert cp.asset_symbols("US_HYNIX") == ("usMU", "gb_mu")


# ── K 线 (腾讯) ───────────────────────────────────────────────────────────
def test_kline_period_mapping():
    assert cp.kline_period("1d") == "day"
    assert cp.kline_period("1wk") == "week"
    assert cp.kline_period("1mo") == "month"
    assert cp.kline_period("60m") is None      # 腾讯不提供分钟线
    assert cp.kline_period("5m") is None
    assert cp.kline_period("") is None


def test_kline_symbols_us_needs_exchange_suffix():
    """美股必须带交易所后缀, 不带只返回 2 根。"""
    assert cp._kline_symbols("usNVDA") == ["usNVDA.OQ", "usNVDA.N"]
    assert cp._kline_symbols("sh000001") == ["sh000001"]
    assert cp._kline_symbols("hkHSI") == ["hkHSI"]


def test_parse_tencent_kline_field_order():
    """腾讯行格式是 [日期, **开, 收, 高, 低**, 量] —— 不是 OHLC 顺序!"""
    raw = ('{"code":0,"data":{"sh000001":{"day":['
           '["2026-11-09","3605.650","3599.020","3660.210","3595.260","497973940.000"],'
           '["2026-11-10","3682.040","3663.720","3683.730","3660.720","404865177.000"]'
           ']}}}')
    rows = cp.parse_tencent_kline(raw, "day")
    assert len(rows) == 2
    ts, o, h, lo, c, v = rows[-1]
    assert o == pytest.approx(3682.040)   # 开
    assert c == pytest.approx(3663.720)   # 收
    assert h == pytest.approx(3683.730)   # 高
    assert lo == pytest.approx(3660.720)  # 低
    assert v == pytest.approx(404865177.0)
    # ts 是本地时区当日 00:00 (与项目内 Naver/KRX 源同口径)
    import datetime
    assert datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") == "2026-11-10"


def test_parse_tencent_kline_sorted_ascending():
    raw = ('{"code":0,"data":{"sh000001":{"day":['
           '["2026-11-10","3682.040","3663.720","3683.730","3660.720","1.000"],'
           '["2026-11-09","3605.650","3599.020","3660.210","3595.260","2.000"]'
           ']}}}')
    rows = cp.parse_tencent_kline(raw, "day")
    assert rows[0][0] < rows[1][0], "必须按时间升序"


def test_parse_tencent_kline_qfq_key():
    """前复权时 key 可能是 qfqday —— 必须能识别。"""
    raw = ('{"code":0,"data":{"sh000001":{"qfqday":['
           '["2026-11-10","3682.040","3663.720","3683.730","3660.720","1.000"]'
           ']}}}')
    rows = cp.parse_tencent_kline(raw, "day")
    assert len(rows) == 1
    assert rows[0][4] == pytest.approx(3663.720)


def test_parse_tencent_kline_garbage():
    assert cp.parse_tencent_kline("", "day") == []
    assert cp.parse_tencent_kline("not json", "day") == []
    assert cp.parse_tencent_kline('{"code":0,"msg":"param error","data":{}}', "day") == []
    assert cp.parse_tencent_kline('{"data":{"x":{"day":[["2026-11-10","0","0","0","0","0"]]}}}',
                                  "day") == [], "全 0 的行必须丢弃"



# ── 接入回归: yfinance 失败时必须落到国内源 ────────────────────────────────
def test_stock_prices_falls_back_to_cn_source(monkeypatch):
    """v324: Yahoo/Polygon 全失败 → 国内源兜底 (无代理环境的关键保障)。"""
    import stock_prices

    monkeypatch.setattr(stock_prices, "_polygon_prev_close", lambda s: None)
    monkeypatch.setattr(stock_prices, "_live_cache", {})
    monkeypatch.setattr(stock_prices, "_cache_lock", threading.Lock())

    class _YF:
        @staticmethod
        def Ticker(s):
            class _T:
                fast_info = type("F", (), {"last_price": None})()
            return _T()

    monkeypatch.setitem(sys.modules, "yfinance", _YF)

    seen = {}

    def _fake_fetch(asset, timeout=None, use_cache=True):
        seen["asset"] = asset
        return 123.45

    import cn_prices
    monkeypatch.setattr(cn_prices, "fetch_price", _fake_fetch)

    price = stock_prices.current_price_for_symbol("NVDA")
    assert price == pytest.approx(123.45)
    assert seen["asset"] == "NVDA"


def test_asia_prices_falls_back_to_cn_source(monkeypatch):
    """v324: yfinance 失败 → A股指数改走国内源。"""
    import asia_prices

    monkeypatch.setattr(asia_prices, "_price_cache", {})
    monkeypatch.setattr(asia_prices, "_cache_lock", threading.Lock())

    class _YF:
        @staticmethod
        def Ticker(s):
            raise RuntimeError("no network")

    monkeypatch.setitem(sys.modules, "yfinance", _YF)

    import cn_prices
    monkeypatch.setattr(cn_prices, "fetch_price",
                        lambda asset, timeout=None: 3663.72 if asset == "SHCOMP" else None)

    price = asia_prices.fetch_price("000001.SS")
    assert price == pytest.approx(3663.72)


def test_asia_cn_asset_mapping():
    import asia_prices
    assert asia_prices._cn_asset("000001.SS") == "SHCOMP"
    assert asia_prices._cn_asset("399001.SZ") == "SZCOMP"
    assert asia_prices._cn_asset("000300.SS") == "CSI300"
    assert asia_prices._cn_asset("000660.KS") is None   # 韩股无国内源
