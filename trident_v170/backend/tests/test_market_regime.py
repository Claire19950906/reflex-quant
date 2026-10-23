"""market_regime.Price/fund divergence 单元测试。"""
from market_regime import detect_price_fund_divergence


def test_bull_negative_funding_is_bearish():
    r = detect_price_fund_divergence(trend="Strong Bull", funding_rate_pct=-0.08)
    assert r["divergence"] == "bearish"
    assert r["strength"] > 0.5


def test_bear_positive_funding_is_bullish():
    r = detect_price_fund_divergence(trend="Bear", funding_rate_pct=0.05)
    assert r["divergence"] == "bullish"
    assert r["strength"] > 0.0


def test_aligned_is_neutral():
    assert detect_price_fund_divergence(trend="Bull", funding_rate_pct=0.02)["divergence"] is None
    assert detect_price_fund_divergence(trend="Bear", funding_rate_pct=-0.02)["divergence"] is None


def test_ranging_is_neutral():
    assert detect_price_fund_divergence(trend="Ranging", funding_rate_pct=-0.02)["divergence"] is None


def test_basis_reinforces():
    r = detect_price_fund_divergence(trend="Bull", funding_rate_pct=-0.03, basis_bps=-5.0)
    assert r["divergence"] == "bearish"
    assert len(r["signals"]) >= 2


def test_oi_reinforces_bullish():
    r = detect_price_fund_divergence(trend="Bear", funding_rate_pct=0.02, oi_change_pct=3.0)
    assert r["divergence"] == "bullish"
    assert len(r["signals"]) >= 2