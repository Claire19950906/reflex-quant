"""v403 基本面进决策链 —— 单元测试（全部离线，不碰网络、不碰生产库）。"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import decision_guard as dg
import equity_fundamentals as ef
import fundamental_factor as ff


# ── 1) 权重表：基本面这次真的有位置 ──────────────────────────────────────
def test_directional_weights_include_fundamental_and_sum_to_one():
    assert "fundamental" in dg.DIRECTIONAL_WEIGHTS
    assert dg.DIRECTIONAL_WEIGHTS["fundamental"] == 0.08
    assert dg.DIRECTIONAL_WEIGHTS["news_sentiment"] == 0.32
    assert abs(sum(dg.DIRECTIONAL_WEIGHTS.values()) - 1.0) < 1e-9


# ── 2) 闸门：可得时占权重，不可得时让回权重 ──────────────────────────────
def _row(**kw):
    base = {
        "sentiment_score": 0.5, "suggested_action": "buy", "target_asset": "NVDA",
        "market_confirmation": "unknown", "decision_context": "{}",
        "cluster_size": 1, "history_sample": {"total": 0, "wins": 0},
        "heat_score": 5.0, "impact_score": 5.0, "asset_impact": 5.0,
    }
    base.update(kw)
    return base


def test_guard_counts_fundamental_when_available():
    out = dg.evaluate_decision(_row(fundamental={
        "available": True, "score": 0.8, "ticker": "NVDA", "components": {}}))
    assert out["factors"]["fundamental"]["available"] is True
    assert abs(out["coverage"] - (0.32 + 0.08)) < 1e-9
    assert out["fundamental"]["available"] is True


def test_guard_drops_fundamental_weight_when_unavailable():
    out = dg.evaluate_decision(_row(fundamental={"available": False,
                                                 "detail": "BTC 非公司标的"}))
    assert out["factors"]["fundamental"]["available"] is False
    assert abs(out["coverage"] - 0.32) < 1e-9        # 取不到 ≠ 中性，权重让回
    assert out["fundamental"]["available"] is False


def test_guard_accepts_json_string_fundamental():
    out = dg.evaluate_decision(_row(fundamental='{"available": true, "score": 0.5}'))
    assert out["factors"]["fundamental"]["available"] is True


def test_fundamental_does_not_become_standalone_direction():
    """没有新闻锚点时，基本面不得单方面造出方向（锚点规则不破）。"""
    out = dg.evaluate_decision(_row(sentiment_score=0.0, suggested_action="hold",
                                    fundamental={"available": True, "score": 1.0,
                                                 "ticker": "NVDA"}))
    assert out["action"] == "HOLD"


# ── 3) 因子层：非公司标的直接判不可得，不硬套 PE ─────────────────────────
def test_factor_rejects_non_equity():
    # v460: BTC/XAU/WTI/DXY/ETH 现在由 alt_fundamental 提供非公司基本面分量，
    # 不再一律 N/A —— 这条测试改为验证"真没接入的品种"仍然明确不支持，
    # 且 ticker 恒为 None（避免被下游当成某只股票）。
    # v467: 银/铜/纳指已由 alt 口径接入，这里改用 alt 不覆盖的 VIX / HSI
    for asset in ("VIX", "HSI"):
        out = ff.build(asset)
        assert out.get("kind") == "non_equity", asset
        assert out["available"] is False
        assert out["ticker"] is None
    # 未接入的代码形态（会被 classify 认成美股 ticker）也不许悄悄拿到 alt 分
    for asset in ("NATGAS", "PLATINUM"):
        assert ff.build(asset, use_cache=False).get("kind") != "alt"


def test_factor_resolves_equity_ticker():
    assert ff.resolve_ticker("nvda") == "NVDA"
    assert ff.resolve_ticker("BTC") is None
    assert ff.resolve_ticker("") is None


def test_factor_degrades_when_no_statements(monkeypatch):
    monkeypatch.setattr(ef, "db_pack",
                        lambda *a, **kw: {"status": "unavailable", "summary": {},
                                          "earnings": [], "asof": 0, "tickers": ()})
    out = ff.build("NVDA", use_cache=False)
    assert out["available"] is False
    assert "无财报" in out["detail"]


def test_factor_uses_revenue_proxy_when_eps_yoy_missing(monkeypatch):
    fake = {
        "status": "ok", "asof": 1, "tickers": ("NVDA",),
        "summary": {"NVDA": {"ttm": {"revenue": 3.0e11, "net_income": 1.5e11,
                                     "eps_diluted": None, "operating_cash_flow": 1e11},
                             "yoy": {"revenue": 80.0}, "gross_margin": 74.0,
                             "net_margin": 50.0, "latest": {"period_end": "2026-07-31"},
                             "n_quarters": 8, "eps_source": "income_statement"}},
        "earnings": [{"ticker": "NVDA", "event_date": "2026-08-26",
                      "eps_actual": 1.1, "eps_estimated": 1.0}],
    }
    monkeypatch.setattr(ef, "db_pack", lambda *a, **kw: fake)
    monkeypatch.setattr(ff, "_pe_readout", lambda *a, **kw: {})
    out = ff.build("NVDA", use_cache=False)
    assert out["available"] is True
    # 营收 +80% 打到饱和 → 成长分 1.0；预期差 +10% → 修正分被拉高
    assert out["components"]["earnings_revision"] > 0.9
    assert any("营收同比" in e for e in out["evidence"])


# ── 4) 财报解析：TTM / 同比 / EPS 补源可追溯 ────────────────────────────
def _stmt(period_end, rev, net, eps=None, ocf=None, gp=None):
    return {"period_end": period_end, "fiscal_year": period_end[:4], "period": "Q",
            "revenue": rev, "gross_profit": gp, "operating_income": None,
            "net_income": net, "eps_diluted": eps, "total_assets": None,
            "total_liabilities": None, "total_equity": None,
            "operating_cash_flow": ocf, "capex": None, "source": "alphavantage"}


def test_summarize_ttm_and_yoy():
    rows = [_stmt(f"2026-{m:02d}-30", 1000.0, 200.0, 1.0, 150.0, 700.0)
            for m in (1, 4, 7, 10)]
    rows += [_stmt(f"2025-{m:02d}-30", 500.0, 100.0, 0.5, 75.0, 350.0)
             for m in (1, 4, 7, 10)]
    s = ef.summarize(rows)
    assert s["n_quarters"] == 8
    assert s["ttm"]["revenue"] == 4000.0
    assert s["yoy"]["revenue"] == 100.0
    assert s["yoy"]["eps_diluted"] == 100.0
    assert s["gross_margin"] == 70.0
    assert s["eps_source"] == "income_statement"


def test_summarize_falls_back_to_earnings_history_for_eps():
    rows = [_stmt(f"2026-{m:02d}-30", 1000.0, 200.0, None, 150.0, 700.0)
            for m in (1, 4, 7, 10)]
    hist = [{"event_date": f"2026-{m:02d}-30", "eps_actual": 1.25}
            for m in (1, 4, 7, 10)]
    s = ef.summarize(rows, eps_history=hist)
    assert s["ttm"]["eps_diluted"] == 5.0
    assert s["eps_source"] == "earnings_history"   # 补源必须标出来


def test_summarize_insufficient_quarters_yields_none():
    s = ef.summarize([_stmt("2026-07-31", 1000.0, 200.0, 1.0)])
    assert s["ttm"] == {} and s["yoy"] == {}


# ── 5) 层渲染：一票一行，8 条指标 ───────────────────────────────────────
def test_layer_items_one_line_per_ticker():
    pack = {
        "status": "ok", "asof": 1, "tickers": ("NVDA",),
        "summary": {"NVDA": {"ttm": {"revenue": 3.0e11, "eps_diluted": 7.0,
                                     "operating_cash_flow": 1.3e11},
                             "yoy": {"revenue": 83.0, "eps_diluted": 60.0},
                             "gross_margin": 74.6, "n_quarters": 8,
                             "latest": {"period_end": "2026-07-31"}}},
        "earnings": [{"ticker": "NVDA", "event_date": "2026-11-17",
                      "eps_estimated": 2.47, "eps_actual": None,
                      "revenue_estimated": 1.09e11, "revenue_actual": None}],
    }
    items = ef.layer_items(pack)
    assert len(items) == 8
    assert all(i["category"] == "fundamentals" for i in items)
    keys = {i["metric_key"] for i in items}
    assert "fund.revenue_ttm" in keys and "fund.next_earnings_days" in keys


def test_layer_items_unavailable_single_row():
    items = ef.layer_items({"status": "unavailable", "errors": ["income(fmp):402"]})
    assert len(items) == 1
    assert items[0]["metric_key"] == "fund.unavailable"
    assert items[0]["status"] == "unavailable"


# ── 6) 技术超跌的财务闸门（改名后不许再暗示"基本面没崩"）────────────────
def test_gate_marks_unverified_without_statements():
    import discovery
    g = discovery._fundamental_gate({"summary": {}}, "BTC")
    assert g["verified"] is False and g["ok"] is None
    assert "未核验" in g["note"]


def test_gate_blocks_loss_making_company():
    import discovery
    g = discovery._fundamental_gate({"summary": {"COIN": {
        "ttm": {"net_income": -1e9}, "yoy": {"revenue": -26.0}}}}, "COIN")
    assert g["verified"] is True and g["ok"] is False


def test_gate_passes_healthy_company():
    import discovery
    g = discovery._fundamental_gate({"summary": {"NVDA": {
        "ttm": {"net_income": 1e10}, "yoy": {"revenue": 30.0},
        "latest": {"period_end": "2026-07-31"}}}}, "NVDA")
    assert g["verified"] is True and g["ok"] is True
    assert g["period_end"] == "2026-07-31"
