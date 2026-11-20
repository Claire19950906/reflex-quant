# -*- coding: utf-8 -*-
"""v479 数据源补强回归：WTI 兜底、商品/美股多源优先级、报价覆盖。"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from feeds import quote_sources as qs
from feeds import domestic_quotes as dq


def test_wti_without_okx_symbol_still_falls_back_to_yahoo(monkeypatch):
    """WTI 在 ASSET_ROUTE 里 symbol=None（OKX 无原油永续）。

    修复前这里 `if not sym: return None` 直接早退，Yahoo `CL=F` 永远走不到——
    表现就是"原油没数据"。修复后必须真的去问 Yahoo。
    """
    assert qs.route("WTI")["symbol"] is None
    seen = {}

    def _fake_yahoo(sym, **kw):
        seen["sym"] = sym
        return [[1.0, 80.0, 81.0, 79.0, 80.5, 10.0]]

    monkeypatch.setattr(qs, "fetch_yahoo_candles", _fake_yahoo)
    rows = qs.fetch_asset_candles_with_yahoo_fallback("WTI", "1H", 5)
    assert rows and seen["sym"] == "CL=F"


def test_daily_multisource_commodity_uses_sina_first(monkeypatch):
    """商品日线优先新浪外盘（国内直连 ~1s），不先等 Yahoo 8s。"""
    monkeypatch.setattr(dq, "sina_daily_klines",
                        lambda sym, bars=12: [[1.7e9, 80, 81, 79, 80.5, 100]])
    rows = qs.fetch_daily_multisource("WTI", interval="1d", limit=5)
    assert rows and rows[0][0] == 1.7e12          # 秒 → 毫秒，与 Yahoo 口径统一
    assert rows[0][4] == 80.5


def test_daily_multisource_us_stock_falls_back_to_tencent(monkeypatch):
    monkeypatch.setattr(qs, "fetch_yahoo_candles", lambda *a, **kw: None)
    monkeypatch.setattr(qs, "_tencent_us_daily",
                        lambda sym, limit=None: [[1.7e12, 100, 101, 99, 100.5, 1.0]])
    rows = qs.fetch_daily_multisource("TSLA", interval="1d", limit=5)
    assert rows and rows[-1][4] == 100.5


def test_intraday_never_fakes_daily(monkeypatch):
    """要 1h 线时腾讯日线不能顶替（周期不对，宁缺毋滥）。"""
    monkeypatch.setattr(qs, "fetch_yahoo_candles", lambda *a, **kw: None)
    monkeypatch.setattr(qs, "_tencent_us_daily",
                        lambda sym, limit=None: [[1.7e12, 100, 101, 99, 100.5, 1.0]])
    assert qs.fetch_daily_multisource("TSLA", interval="60m", limit=5) is None


def test_quote_coverage_includes_new_us_tickers():
    for a in ("TSLA", "AAPL", "MSTR", "MSFT", "GOOGL", "COIN", "PLTR",
              "MU", "US_HYNIX", "SMCI", "MARA", "RIOT"):
        assert a in dq._TENCENT_CODES, f"{a} 缺国内报价源"
    for a in ("WTI", "OIL", "XAU", "GOLD", "XAG", "BTC"):
        assert a in dq._SINA_CODES, f"{a} 缺新浪外盘源"


def test_parse_sina_hf_crude_line():
    line = ('var hq_str_hf_CL="82.77,-0.35,82.80,82.78,83.10,82.20,'
            '18:30:11,83.06,82.90,0,2,3,2026-11-19,纽约原油,0";')
    row = dq._parse_sina_hf(line)
    assert row and row["price"] == pytest.approx(82.77)
    assert row["prev_close"] == pytest.approx(83.06)
