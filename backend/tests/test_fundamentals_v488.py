"""v488 基本面深度读数 —— 单元测试（全部离线：不联网、不碰生产库）。

覆盖：
  1. 同一财季跨源期末日漂移（AV 月末 vs SEC 申报口径）必须对齐成一季；
  2. 相邻季度不能被误合并；
  3. 同季同比在日期漂 1 天时仍能匹配；
  4. 生意质量四项读数的算法与"只判差"护栏；
  5. capex 正负号跨源不一致时取绝对值；
  6. 口径失真（公允价值/财库资产）不外抛成正常比率；
  7. 公司侧与 A股/港股侧都输出 gaps，深度读数不进方向分。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import cn_fundamentals as cf
import company_depth as cd
import equity_fundamentals as ef
import fundamental_factor as ff


# ── 1) 跨源同季对齐 ──────────────────────────────────────────────────────
def _quarter_rows():
    """4 个财季，每季两条：AV 有利润表/CFF、SEC 有资产/负债，期末日差 3~4 天。"""
    pairs = [("2025-12-31", "2025-12-28"), ("2026-03-31", "2026-03-28"),
             ("2026-06-30", "2026-06-27"), ("2026-09-30", "2026-09-26")]
    rows = []
    for av_end, sec_end in pairs:
        rows.append({"period_end": av_end, "source": "alphavantage",
                     "revenue": 1000.0, "gross_profit": 400.0,
                     "operating_income": 200.0, "net_income": 100.0,
                     "operating_cash_flow": 150.0, "capex": 50.0})
        rows.append({"period_end": sec_end, "source": "sec",
                     "total_assets": 5000.0, "total_liabilities": 2000.0,
                     "total_equity": 3000.0})
    return rows


def test_dedupe_aligns_same_quarter_across_sources_and_ttm_works():
    merged = ef._dedupe_rows(_quarter_rows())
    assert len(merged) == 4                                  # 4 季，不是 8 条
    assert all(r["revenue"] == 1000.0 for r in merged)        # 利润表来自 AV
    assert all(r["total_assets"] == 5000.0 for r in merged)   # 资产负债表来自 SEC
    summary = ef.summarize(merged)
    assert summary["ttm"]["revenue"] == pytest.approx(4000.0)
    assert summary["gross_margin"] == pytest.approx(40.0)


def test_dedupe_does_not_merge_adjacent_quarters():
    merged = ef._dedupe_rows([
        {"period_end": "2026-09-30", "source": "alphavantage", "revenue": 1000.0},
        {"period_end": "2026-06-30", "source": "alphavantage", "revenue": 900.0},
    ])
    assert len(merged) == 2


def test_fiscal_key_beats_date_when_period_known():
    merged = ef._dedupe_rows([
        {"period_end": "2026-09-30", "fiscal_year": "2026", "period": "Q3",
         "source": "yfinance", "revenue": 1000.0},
        {"period_end": "2026-08-31", "fiscal_year": "2026", "period": "Q3",
         "source": "alphavantage", "net_income": 100.0},
    ])
    assert len(merged) == 1
    assert merged[0]["revenue"] == 1000.0 and merged[0]["net_income"] == 100.0


# ── 2) 同季同比的日期漂移 ────────────────────────────────────────────────
def test_stmt_growth_matches_prior_year_with_date_drift():
    rows = [
        {"period_end": "2026-09-26", "revenue": 1000.0, "net_income": 100.0,
         "eps_diluted": 2.0},
        {"period_end": "2026-06-27", "revenue": 950.0, "net_income": 90.0,
         "eps_diluted": 1.8},
        {"period_end": "2025-09-27", "revenue": 800.0, "net_income": 80.0,
         "eps_diluted": 1.0},
    ]
    out = ff.stmt_growth(rows)
    assert out["revenue"] == pytest.approx(25.0)
    assert out["eps_diluted"] == pytest.approx(100.0)
    assert out["cur_period"] == "2026-09-26"
    assert out["base_period"] == "2025-09-27"


# ── 3) 生意质量四项 ──────────────────────────────────────────────────────
def _eight_quarters(gp_latest: float, op_latest: float,
                    gp_prior: float = 400.0, op_prior: float = 200.0,
                    ocf_latest: float = 50.0, capex_latest: float = 150.0,
                    liab: float = 300.0):
    periods = ["2026-09-28", "2026-06-28", "2026-03-28", "2025-12-28",
               "2025-09-28", "2025-06-28", "2025-03-28", "2024-12-28"]
    rows = []
    for i, period_end in enumerate(periods):
        latest = i < 4
        rows.append({
            "period_end": period_end,
            "revenue": 1000.0,
            "gross_profit": gp_latest if latest else gp_prior,
            "operating_income": op_latest if latest else op_prior,
            "net_income": 100.0,
            "operating_cash_flow": ocf_latest if latest else 200.0,
            "capex": capex_latest if latest else 50.0,
        })
    # 资产负债表并进最新一期（readout 要求 rows 已按财季对齐）
    rows[0].update({"total_assets": 1000.0, "total_liabilities": liab,
                    "total_equity": 1000.0 - liab})
    return rows


def test_depth_flags_deterioration_only_when_worse():
    rows = _eight_quarters(gp_latest=300.0, op_latest=100.0, liab=900.0)
    out = cd.readout(rows, {"revenue": -5.0, "eps_diluted": 10.0})
    assert out["available"] is True
    assert out["gross_margin_ttm"] == pytest.approx(30.0)
    assert out["gm_delta_pp"] == pytest.approx(-10.0)
    assert out["opm_delta_pp"] == pytest.approx(-10.0)
    assert out["fcf_margin_ttm"] == pytest.approx(-10.0)
    assert out["leverage"] == pytest.approx(0.9)
    assert {"gm_down", "opm_down", "negative_fcf", "high_leverage",
            "eps_without_revenue"} <= set(out["flags"])
    assert any("分部" in g for g in out["gaps"])


def test_depth_does_not_reward_improvement():
    rows = _eight_quarters(gp_latest=450.0, op_latest=250.0,
                           ocf_latest=300.0, capex_latest=50.0, liab=300.0)
    out = cd.readout(rows, {"revenue": 20.0, "eps_diluted": 30.0})
    assert out["gm_delta_pp"] == pytest.approx(5.0)
    assert out["opm_delta_pp"] == pytest.approx(5.0)
    assert out["flags"] == []          # 只判差，好转不产生正标签


def test_depth_capex_sign_is_normalized():
    pos = _eight_quarters(gp_latest=400.0, op_latest=200.0,
                          ocf_latest=100.0, capex_latest=50.0)
    neg = _eight_quarters(gp_latest=400.0, op_latest=200.0,
                          ocf_latest=100.0, capex_latest=-50.0)
    assert cd.readout(pos)["fcf_margin_ttm"] == pytest.approx(5.0)
    assert cd.readout(neg)["fcf_margin_ttm"] == pytest.approx(5.0)


def test_depth_sanity_cap_drops_meaningless_ratios():
    rows = _eight_quarters(gp_latest=400.0, op_latest=2500.0,
                           ocf_latest=10.0, capex_latest=0.0)
    out = cd.readout(rows)
    assert out["operating_margin_ttm"] is None
    assert out["opm_delta_pp"] is None          # 失真时趋势也不能留
    assert any("口径失真" in g for g in out["gaps"])
    # FCF 10% 仍在正常范围，不应被顺手丢掉
    assert out["fcf_margin_ttm"] == pytest.approx(1.0)


# ── 4) gaps 与"深度不进方向分" ───────────────────────────────────────────
def test_factor_includes_depth_but_score_unchanged_by_depth(monkeypatch):
    rows = _eight_quarters(gp_latest=300.0, op_latest=100.0, liab=900.0)
    summary = {"ttm": {"eps_diluted": 4.0, "net_income": 400.0},
               "yoy": {"eps_diluted": 10.0, "revenue": -5.0},
               "gross_margin": 30.0}
    pack = {"summary": {"TST": summary}, "statements": {"TST": rows},
            "earnings": [], "asof": 1}
    monkeypatch.setattr(ff.ef, "db_pack", lambda *a, **k: pack)
    monkeypatch.setattr(ff, "_pe_readout", lambda *a, **k: {})
    monkeypatch.setattr(ff, "_pb_readout", lambda *a, **k: {})
    monkeypatch.setattr(ff, "_surprise_pct", lambda *a, **k: None)
    import peer_valuation
    monkeypatch.setattr(peer_valuation, "build",
                        lambda *a, **k: {"available": False, "detail": "no peer"})
    import macro_equity_regime
    monkeypatch.setattr(macro_equity_regime, "tilt_for",
                        lambda *a, **k: {"available": False})
    out = ff.build("TST", use_cache=False)
    assert out["components"]["depth_gross_margin_ttm"] == pytest.approx(30.0)
    assert out["components"]["depth_flags"]
    assert out["depth"]["flags"]
    assert out["gaps"]

    # 深度读数不参与打分：把 readout 换成空读数，分数必须逐位不变
    monkeypatch.setattr(cd, "readout", lambda *a, **k: {
        "available": False, "flags": [], "evidence": [],
        "gaps": ["生意质量读数不可用"]})
    out2 = ff.build("TST", use_cache=False)
    assert out2["score"] == out["score"]


def test_cn_fundamentals_outputs_gaps(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(cf, "read_one", lambda *a, **k: {
        "ticker": "600000.SH", "name": "测试", "report_date": "2026-09-30",
        "revenue_yoy": 10.0, "net_profit_yoy": 20.0, "roe_annualized": 12.0,
        "eps_ttm": 1.0, "fetched_at": 1,
    })
    monkeypatch.setattr(cf, "pe_percentile", lambda *a, **k: {})
    out = cf.build("600000.SH", "A", conn=conn)
    # v489：无历史时多一条「无季度历史」，其余 5 条边界不变
    assert len(out["gaps"]) >= 5
    assert any("无季度历史" in g for g in out["gaps"])
    assert any("三表" in g for g in out["gaps"])
    assert any("财报日历" in g for g in out["gaps"])
