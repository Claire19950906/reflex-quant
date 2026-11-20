#!/usr/bin/env python3
"""v489：A股/港股多期历史 → TTM 趋势 + 盈利位置；只读入，不改方向分。"""

import sqlite3

import pytest

import cn_depth
import cn_fundamentals as cf


def _a_row(date, label, rev, np_, eps, roe, gm=90.0):
    return {
        "SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
        "SECURITY_NAME_ABBR": "贵州茅台", "REPORTDATE": date, "DATEMMDD": label,
        "TOTAL_OPERATE_INCOME": rev, "PARENT_NETPROFIT": np_, "BASIC_EPS": eps,
        "WEIGHTAVG_ROE": roe, "XSMLL": gm, "BPS": 20.0,
        "YSTZ": 10.0, "SJLTZ": 15.0,
    }


A_ROWS = [
    _a_row("2026-09-30", "三季报", 90.0, 45.0, 3.0, 24.0),
    _a_row("2026-06-30", "半年报", 60.0, 30.0, 2.0, 16.0),
    _a_row("2026-03-31", "一季报", 30.0, 15.0, 1.0, 8.0),
    _a_row("2025-12-31", "年报", 100.0, 50.0, 4.0, 32.0),
    _a_row("2025-09-30", "三季报", 80.0, 40.0, 3.2, 24.0),
    _a_row("2025-06-30", "半年报", 55.0, 28.0, 1.8, 16.0),
    _a_row("2024-12-31", "年报", 95.0, 47.0, 3.7, 30.0),
    _a_row("2024-09-30", "三季报", 75.0, 37.0, 3.0, 22.0),
]


def _hk_row(date, label, rev, np_, eps, roe, gm=56.0):
    return {
        "SECUCODE": "00700.HK", "SECURITY_NAME_ABBR": "腾讯控股",
        "REPORT_DATE": date, "REPORT_TYPE": label,
        "OPERATE_INCOME": rev, "HOLDER_PROFIT": np_, "EPS_TTM": eps,
        "ROE_AVG": roe, "ROE_YEARLY": roe * 4 / 3, "GROSS_PROFIT_RATIO": gm,
        "BPS": 100.0, "DEBT_ASSET_RATIO": 40.0,
        "OPERATE_INCOME_YOY": 9.0, "HOLDER_PROFIT_YOY": 7.0,
    }


HK_ROWS = [
    _hk_row("2026-09-30", "2026年三季报", 608.579, 177.727, 5.0, 15.0),
    _hk_row("2026-06-30", "2026年中报", 401.243, 114.115, 3.2, 10.0),
    _hk_row("2026-03-31", "2026年一季报", 196.458, 58.093, 1.6, 5.0),
    _hk_row("2025-12-31", "2025年年报", 751.766, 224.842, 6.3, 21.0),
    _hk_row("2025-09-30", "2025年三季报", 557.395, 166.582, 4.7, 15.0),
    _hk_row("2025-06-30", "2025年中报", 364.526, 103.449, 2.9, 10.0),
]


# ── A股：每期独立还原 TTM，不用未来年报回填过去 ─────────────────────────
def test_a_history_ttm_per_period():
    records = cf.parse_a_share_history(A_ROWS, price=100.0)
    assert len(records) == 8
    latest = records[0]
    # 90 + 100 − 80 = 110；45 + 50 − 40 = 55；3.0 + 4.0 − 3.2 = 3.8
    assert latest["revenue_ttm"] == pytest.approx(110.0)
    assert latest["net_profit_ttm"] == pytest.approx(55.0)
    assert latest["eps_ttm"] == pytest.approx(3.8)
    assert latest["pe_ttm"] == pytest.approx(26.32)
    # 历史期不能用今天的价格算 PE，否则是假的
    assert records[1]["price"] is None
    assert records[1]["pe_ttm"] is None
    # 最早的三季报缺 2023 年报/三季报，TTM 必须是 None，不许用相邻季凑
    oldest = [r for r in records if r["report_date"] == "2024-09-30"][0]
    assert oldest["revenue_ttm"] is None


# ── 港股：累计口径还原，且年份前缀不能挡住同期匹配 ───────────────────────
def test_hk_history_ttm_and_year_prefix():
    records = cf.parse_hk_history(HK_ROWS, price=400.0)
    latest = records[0]
    # 608.579 + 751.766 − 557.395 = 802.95
    assert latest["revenue_ttm"] == pytest.approx(802.95, abs=0.01)
    assert latest["net_profit_ttm"] == pytest.approx(235.987, abs=0.01)
    assert latest["eps_ttm"] == pytest.approx(5.0)
    assert latest["pe_ttm"] == pytest.approx(80.0)
    # 中报 TTM 也要还原：401.243 + 751.766 − 364.526
    assert records[1]["revenue_ttm"] == pytest.approx(788.483, abs=0.01)


def test_hk_ttm_none_when_prior_missing():
    only_latest = [HK_ROWS[0]]
    records = cf.parse_hk_history(only_latest)
    assert records[0]["revenue_ttm"] is None
    assert records[0]["net_profit_ttm"] is None


# ── 深度读数：趋势、位置、护栏 ───────────────────────────────────────────
def _history(n=12, rev_growth=1.0, last_gm=60.0, prior_gm=58.0, roe_last=20.0, roe_prior=18.0):
    rows = []
    labels = ["一季报", "半年报", "三季报", "年报"]
    for i in range(n):
        year = 2023 + i // 4
        label = labels[i % 4]
        rev = 80.0 * rev_growth + i * 2
        np_ = 10.0 + i * 2
        gm = last_gm if i == n - 1 else (prior_gm if i == n - 5 else 59.0)
        roe = roe_last if i == n - 1 else (roe_prior if i == n - 5 else 19.0)
        rows.append({
            "report_date": f"{year}-{['03-31','06-30','09-30','12-31'][i % 4]}",
            "period_label": label, "revenue_ttm": rev, "net_profit_ttm": np_,
            "eps_ttm": np_ / 10.0, "gross_margin": gm, "roe": roe,
            "debt_asset_ratio": 0.4,
        })
    return list(reversed(rows))  # 与 read_history 一致：最新在前


def test_depth_trend_and_position():
    out = cn_depth.readout(_history())
    assert out["available"] is True
    assert out["points"] == 12
    assert out["revenue_ttm_yoy"] is not None
    assert out["gross_margin_delta_pp"] == pytest.approx(2.0)
    assert out["roe_delta_pp"] == pytest.approx(2.0)
    assert 0.0 <= out["earnings_position"] <= 1.0
    assert out["flags"] == []          # 改善不奖励、不打空头标签
    assert "无季度历史" not in " ".join(out["gaps"])


def test_depth_flags_only_worse():
    rows = _history()
    rows[0]["revenue_ttm"] = 60.0     # 同比约 −25%
    rows[0]["net_profit_ttm"] = 6.0   # 同比约 −50%
    rows[0]["gross_margin"] = 55.0    # −3pp
    rows[0]["roe"] = 12.0             # −6pp
    rows[0]["debt_asset_ratio"] = 0.9
    out = cn_depth.readout(rows)
    assert set(out["flags"]) >= {"rev_down", "np_down", "gm_down", "roe_down", "high_leverage"}


def test_depth_short_history_writes_gap():
    out = cn_depth.readout(_history(n=2))
    assert out["available"] is True
    assert any("趋势样本不足" in g for g in out["gaps"])
    assert out["earnings_position"] is None


# ── 落库 / 读库 / 零改分 ─────────────────────────────────────────────────
def _conn_with_history():
    conn = sqlite3.connect(":memory:")
    cf.ensure_tables(conn)
    cf.persist_history(conn, cf.parse_a_share_history(A_ROWS, price=100.0))
    return conn


def test_persist_and_read_history_roundtrip():
    conn = _conn_with_history()
    hist = cf.read_history(conn, "600519.SH", "A")
    assert len(hist) == 8
    assert hist[0]["report_date"] == "2026-09-30"
    assert hist[0]["revenue_ttm"] == pytest.approx(110.0)
    assert cf.read_one(conn, "600519.SH", "A")["report_date"] == "2026-09-30"


def test_build_reads_depth_without_changing_score(monkeypatch):
    latest = cf.parse_a_share_history(A_ROWS, price=100.0)[0]

    def fake_read_one(*a, **k):
        rec = dict(latest)
        rec["fetched_at"] = 1
        return rec

    monkeypatch.setattr(cf, "read_one", fake_read_one)
    monkeypatch.setattr(cf, "pe_percentile", lambda *a, **k: {})

    empty = sqlite3.connect(":memory:")
    cf.ensure_tables(empty)
    base = cf.build("600519.SH", "A", conn=empty)

    with_hist = _conn_with_history()
    got = cf.build("600519.SH", "A", conn=with_hist)

    assert got["score"] == base["score"]                      # 只读入，不改分
    assert got["components"]["depth_revenue_ttm_yoy"] is not None
    assert got["components"]["depth_points"] == 8
    assert "无季度历史" not in " ".join(got["gaps"])
