"""XAU 行情源诚实兜底回归测试。

覆盖：
  - _xau_xag_spot_fallback 不再合成扁平假 K 线（返回 None，诚实标 unavailable）
  - fetch_okx_candles 在 OKX 失败时不返回值、不落合成 bar
"""

import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import quote_sources  # noqa: E402


def test_spot_fallback_disabled_returns_none():
    assert quote_sources._xau_xag_spot_fallback("XAU-USDT-SWAP", "1H", 96) is None
    assert quote_sources._xau_xag_spot_fallback("XAG-USDT-SWAP", "1m", 50) is None


def test_fetch_okx_candles_xau_no_synthetic(monkeypatch):
    class _DummyStatus:
        def report(self, *a, **k):
            pass

    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "_okx_get", lambda *a, **k: None)
    rows = quote_sources.fetch_okx_candles("XAU-USDT-SWAP", "1H", 96)
    # OKX 失败 → 诚实返回 None，绝不合成扁平假 bar
    assert rows is None