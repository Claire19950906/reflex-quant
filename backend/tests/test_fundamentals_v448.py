"""v448 基本面能力提升 —— 单元测试（全部离线：不联网、不碰生产库）。

覆盖五件事：
  1. TTM EPS 序列与「盈利位置 / 周期度」判型（PE 陷阱修正的输入）；
  2. 强周期见顶/见底时估值与盈利修正各折半，平稳型不动；
  3. 亏损股专用口径（营收/毛利率/现金流），三表不足时单因子封顶 0.6；
  4. PB 倒推序列（市值÷净资产）的单位换算、防前视与「市值恒定」守卫；
  5. 财报窗口折半 + 影子账本的 A/B 还原与判定门槛。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import equity_fundamentals as ef
import fundamental_factor as ff
import fundamental_shadow as fs


# ── 1) TTM 序列与判型 ─────────────────────────────────────────────────────
def _events(eps_list):
    """把 [eps] 变成按季度递进的财报事件（每季 +1 天，够用不掺假）。"""
    return [{"ticker": "T", "event_date": f"20{20 + i // 4}-{(i % 4) * 3 + 1:02d}-28",
             "eps_actual": e, "eps_estimated": None} for i, e in enumerate(eps_list)]


def test_ttm_series_needs_four_quarters_and_dedupes_dates():
    ev = [{"ticker": "T", "event_date": "2025-03-31", "eps_actual": 1.0},
          {"ticker": "T", "event_date": "2025-03-31", "eps_actual": 9.9},   # 同日重复
          {"ticker": "T", "event_date": "2025-06-30", "eps_actual": 2.0},
          {"ticker": "T", "event_date": "2025-09-30", "eps_actual": 3.0}]
    assert ff.ttm_eps_series(ev) == []          # 只有 3 个季度，拼不出 TTM
    ev.append({"ticker": "T", "event_date": "2025-12-31", "eps_actual": 4.0})
    series = ff.ttm_eps_series(ev)
    assert len(series) == 1
    assert series[0][1] == pytest.approx(18.9)   # 同日重复只留最后一条（9.9）→ 9.9+2+3+4
    assert series[0][1] != pytest.approx(12.9)   # 若把同日两条都算进去就会是 12.9


def test_cycle_profile_mu_is_strong_peak_aapl_is_steady_peak():
    mu = [(f"d{i}", v) for i, v in enumerate(
        [4.01, 5.15, 6.44, 8.29, 11.28, 21.92, 45.12, 75.28])]
    prof = ff.cycle_profile(mu)
    assert prof["regime"] == "strong_cyclical"
    assert prof["zone"] == "peak"
    assert prof["cyclicity"] >= 1.5
    assert prof["position"] == pytest.approx(1.0)

    aapl = [(f"d{i}", v) for i, v in enumerate(
        [6.30, 6.42, 6.59, 7.47, 7.91, 8.27, 8.72, 8.92])]
    prof2 = ff.cycle_profile(aapl)
    assert prof2["regime"] == "steady"          # 波动度低 = 成长股，不被惩罚
    assert prof2["position"] == pytest.approx(1.0)


def test_cycle_profile_requires_eight_points():
    assert ff.cycle_profile([("d0", 1.0), ("d1", 2.0)]) is None


# ── 2) 折半规则 ───────────────────────────────────────────────────────────
def test_cycle_fold_halves_valuation_and_revision_on_strong_peak():
    prof = {"regime": "strong_cyclical", "zone": "peak", "position": 1.0,
            "cyclicity": 4.08, "points": 8, "min": 4.01, "max": 75.28}
    ev: list[str] = []
    val, rev, flags = ff._cycle_fold(0.982, 0.88, prof, ev)
    assert val == pytest.approx(0.491)
    assert rev == pytest.approx(0.44)
    assert flags == ["peak_earnings"]
    assert any("强周期见顶" in line for line in ev)


def test_cycle_fold_leaves_steady_grower_untouched():
    prof = {"regime": "steady", "zone": "peak", "position": 1.0,
            "cyclicity": 0.30, "points": 8, "min": 6.3, "max": 8.92}
    ev: list[str] = []
    val, rev, flags = ff._cycle_fold(-0.86, 0.45, prof, ev)
    assert (val, rev, flags) == (-0.86, 0.45, [])
    assert ev == []


def test_cycle_fold_trough_marks_trough_earnings():
    prof = {"regime": "strong_cyclical", "zone": "trough", "position": 0.0,
            "cyclicity": 5.98, "points": 8, "min": -2.05, "max": 1.38}
    ev: list[str] = []
    _val, _rev, flags = ff._cycle_fold(0.3, -0.6, prof, ev)
    assert flags == ["trough_earnings"]


# ── 3) 亏损股口径 ─────────────────────────────────────────────────────────
def _quarters(n=8):
    """COIN 式：营收下滑、毛利率改善；没有现金流列（None 表示源不提供）。"""
    out = []
    rev = [1.216, 1.220, 1.413, 1.781, 1.869, 1.497, 2.034, 2.272]   # 最新在前
    gp = [r * 0.84 for r in rev]
    for i in range(n):
        out.append({"period_end": f"2026-{12 - i * 3:02d}-30" if i < 4 else f"2025-{12 - (i - 4) * 3:02d}-30",
                    "revenue": rev[i] * 1e9,
                    "gross_profit": gp[i] * 1e9,
                    "operating_cash_flow": None,
                    "net_income": -1e8})
    return out


def test_loss_maker_uses_business_readouts_not_loss_size():
    out = ff.loss_maker_readout(_quarters(), eps_yoy=-135.0)
    assert out["available"] is True
    assert set(out["components"]) >= {"rev_yoy", "gm_trend", "loss_score"}
    # 亏损幅度（-135%）没有被当成主尺子：分数由营收/毛利率决定
    assert out["components"]["loss_score"] != pytest.approx(-1.0)
    assert any("营收同比" in line for line in out["evidence"])


def test_loss_maker_fallback_is_capped_so_it_does_not_saturate():
    """MARA：三表不足 → 单因子兜底，且封顶 ±0.6（不再回到 −1.0 饱和）。"""
    out = ff.loss_maker_readout([], eps_yoy=-214.3)
    assert out["available"] is True
    assert out["score"] == pytest.approx(-0.6)
    assert out["flags"] == ["loss_eps_only"]


def test_loss_maker_unavailable_without_any_readout():
    out = ff.loss_maker_readout([], eps_yoy=None)
    assert out["available"] is False


# ── 4) PB 倒推 ────────────────────────────────────────────────────────────
def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE macro_snapshots(
        id INTEGER PRIMARY KEY, ts INTEGER, category TEXT, source TEXT,
        metric_key TEXT, value REAL, unit TEXT, status TEXT, payload TEXT)""")
    conn.execute("""CREATE TABLE equity_fundamentals(
        id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT, fiscal_year TEXT, period TEXT,
        revenue REAL, gross_profit REAL, operating_income REAL, net_income REAL,
        eps_diluted REAL, total_assets REAL, total_liabilities REAL, total_equity REAL,
        operating_cash_flow REAL, capex REAL, source TEXT, fetched_at INTEGER, payload TEXT)""")
    return conn


def test_pb_history_converts_billions_and_respects_publication_date():
    conn = _memdb()
    now = int(time.time())
    # 三个交易日，市值 100/110/120（十亿美元）
    for i, cap in enumerate((100.0, 110.0, 120.0)):
        conn.execute("""INSERT INTO macro_snapshots(ts,metric_key,value,payload)
                        VALUES(?,?,?,?)""",
                     (now - (3 - i) * 86400, "eq.mktcap", cap, '{"label": "XX"}'))
    # 净资产 50（十亿量级美元）；period_end 早于所有快照 → 可用
    conn.execute("""INSERT INTO equity_fundamentals(ticker,period_end,total_equity)
                    VALUES('XX','2024-12-31',50e9)""")
    hist = ef.pb_history("XX", days=365, conn=conn)
    assert len(hist) == 3
    assert hist[-1][1] == pytest.approx(120e9 / 50e9)      # 2.4，不是 1e-9
    conn.close()


def test_pb_history_skips_when_balance_sheet_not_yet_public():
    conn = _memdb()
    now = int(time.time())
    conn.execute("""INSERT INTO macro_snapshots(ts,metric_key,value,payload)
                    VALUES(?,?,?,?)""", (now - 86400, "eq.mktcap", 100.0, '{"label": "YY"}'))
    # 财报期末在快照之后 → 用尚未公布的季报解释历史市值 = 前视，必须跳过
    conn.execute("""INSERT INTO equity_fundamentals(ticker,period_end,total_equity)
                    VALUES('YY',?,40e9)""",
                 (time.strftime("%Y-%m-%d", time.localtime(now + 86400)),))
    assert ef.pb_history("YY", days=365, conn=conn) == []
    conn.close()


def test_pb_history_refuses_constant_market_cap():
    """MSTR 实测 46 天市值一个值不变 → 分位是噪音，宁可没有。"""
    conn = _memdb()
    now = int(time.time())
    for i in range(4):
        conn.execute("""INSERT INTO macro_snapshots(ts,metric_key,value,payload)
                        VALUES(?,?,?,?)""",
                     (now - (4 - i) * 86400, "eq.mktcap", 63.804117, '{"label": "ZZ"}'))
    conn.execute("""INSERT INTO equity_fundamentals(ticker,period_end,total_equity)
                    VALUES('ZZ','2024-12-31',68e9)""")
    assert ef.pb_history("ZZ", days=365, conn=conn) == []
    conn.close()


def test_equity_asof_derives_equity_and_labels_it():
    rows = [("2025-12-31", None, 100e9, 60e9)]
    equity, basis = ef._equity_asof(rows, "2026-03-01")
    assert equity == pytest.approx(40e9)
    assert basis.startswith("derived")


# ── 5) 财报日历 / 窗口 / 影子账本 ──────────────────────────────────────────
def test_parse_av_calendar_filters_window_and_keeps_estimate():
    text = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\n"
            "NVDA,NVIDIA,2026-11-25,2026-10-31,1.23,USD\n"
            "OLD,OldCo,2020-01-01,2019-12-31,0.10,USD\n")
    evs = ef.parse_av_calendar(text, days_back=7, days_fwd=90)
    assert [e["ticker"] for e in evs] == ["NVDA"]
    assert evs[0]["eps_estimated"] == pytest.approx(1.23)
    assert evs[0]["source"] == "alphavantage"


def test_earnings_window_halves_score(monkeypatch):
    """财报前 2 天：分数折半 + earnings_window 标签（数据随时重算）。"""
    soon = time.strftime("%Y-%m-%d", time.localtime(time.time() + 2 * 86400))
    eps = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0]
    events = _events(eps)
    events.append({"ticker": "T", "event_date": soon, "eps_actual": None,
                   "eps_estimated": 2.2})
    for i, ev in enumerate(events[:-1]):
        ev["ticker"] = "T"
    pack = {
        "summary": {"T": {"ttm": {"eps_diluted": 2.0, "net_income": 1e9},
                          "yoy": {"eps_diluted": 10.0},
                          "gross_margin": 40.0, "net_margin": 10.0}},
        "earnings": events, "statements": {"T": []}, "asof": int(time.time()),
    }
    monkeypatch.setattr(ff.ef, "db_pack", lambda *a, **k: pack)
    monkeypatch.setattr(ff, "_pe_readout", lambda *a, **k: {
        "pe": 20.0, "pe_position": 50.0, "pe_samples": 80, "pe_samples_ok": True,
        "pe_p25": 18.0, "pe_p75": 22.0})
    monkeypatch.setattr(ff, "_pb_readout", lambda *a, **k: {})
    out = ff.build("T", conn=None, use_cache=False)
    # 同一份数据、去掉"未来财报"这条 → 基线分
    pack_no_window = dict(pack)
    pack_no_window["earnings"] = [e for e in events if e.get("eps_actual") is not None]
    monkeypatch.setattr(ff.ef, "db_pack", lambda *a, **k: pack_no_window)
    base = ff.build("T", conn=None, use_cache=False)
    assert "earnings_window" in out["flags"]
    assert out["earnings_in_days"] == 2
    assert out["score"] == pytest.approx(base["score"] * 0.5, abs=1e-3)
    assert any("财报窗口" in line for line in out["evidence"])


def test_shadow_ab_reconstructs_two_scores():
    guard = {
        "factors": {
            "news_sentiment": {"score": 1.0, "weight": 0.32, "available": True},
            "structure": {"score": -0.5, "weight": 0.15, "available": True},
            "fundamental": {"score": 0.9, "weight": 0.08, "available": True},
        },
        "thresholds": {"buy_min": 0.35, "sell_min": -0.35},
    }
    ab = fs.compute_ab(guard)
    # 含： (1.0*0.32 + -0.5*0.15 + 0.9*0.08) / 0.55 = 0.297/0.55 ≈ 0.54
    assert ab["dir_with"] == pytest.approx(0.576, abs=0.01)
    # 不含： (0.32 - 0.075) / 0.47 ≈ 0.521
    assert ab["dir_without"] == pytest.approx(0.521, abs=0.01)
    assert ab["fund_available"] == 1


def test_shadow_verdict_refuses_to_conclude_on_small_samples():
    v = fs._verdict(3, 3, [1, 1, 1], [0, 0, 0])
    assert v["code"] == "insufficient"


def test_shadow_record_and_evaluate_roundtrip(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE ohlcv_daily(id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER, close REAL)""")
    now = int(time.time())
    for i, px in enumerate((100.0, 105.0, 110.0, 120.0)):
        conn.execute("INSERT INTO ohlcv_daily(asset,ts,close) VALUES('NVDA',?,?)",
                     (now - (4 - i) * 86400, px))
    guard = {"factors": {"news_sentiment": {"score": 1.0, "weight": 0.32, "available": True},
                         "fundamental": {"score": 0.5, "weight": 0.08, "available": True}},
             "thresholds": {"buy_min": 0.35, "sell_min": -0.35}}
    ts = now - 4 * 86400      # 第 0 根 K 线所在日 → +3 天正好落在最后一根
    fs.record(conn, decision_id=1, news_id=9, asset="NVDA", guard=guard, action="BUY",
              flags=["peak_earnings"], components={"ticker": "NVDA"})
    row = conn.execute("SELECT dir_with, dir_without, fund_score FROM fundamental_shadow").fetchone()
    assert row[0] == pytest.approx(0.9) and row[2] == pytest.approx(0.5)
    conn.execute("UPDATE fundamental_shadow SET ts=?", (ts,))
    updated = fs.evaluate(conn, days=30, now_ts=now)
    assert updated == 1
    row = conn.execute("SELECT settled, hit_with, hit_without, ret_3d FROM fundamental_shadow").fetchone()
    assert row[0] == 1 and row[1] == 1            # BUY 且三天后上涨 → 命中
    assert row[3] == pytest.approx(20.0, abs=0.5)   # 100 → 120
    conn.close()

def test_earnings_window_covers_the_three_days_after(monkeypatch):
    """刚公布（−1 天）也必须折半 —— 实测 NVDA 事件日期早一天，旧逻辑整根线断掉。"""
    past = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    events = _events([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0])
    events.append({"ticker": "T", "event_date": past, "eps_actual": None,
                   "eps_estimated": 2.3})
    pack = {"summary": {"T": {"ttm": {"eps_diluted": 2.0, "net_income": 1e9},
                              "yoy": {"eps_diluted": 10.0},
                              "gross_margin": 40.0, "net_margin": 10.0}},
            "earnings": events, "statements": {"T": []}, "asof": int(time.time())}
    monkeypatch.setattr(ff.ef, "db_pack", lambda *a, **k: pack)
    monkeypatch.setattr(ff, "_pe_readout", lambda *a, **k: {
        "pe": 20.0, "pe_position": 100.0, "pe_samples": 80, "pe_samples_ok": True,
        "pe_p25": 18.0, "pe_p75": 22.0})
    monkeypatch.setattr(ff, "_pb_readout", lambda *a, **k: {})
    out = ff.build("T", conn=None, use_cache=False)
    assert out["earnings_in_days"] == -1
    assert "earnings_window" in out["flags"]
    assert any("已过 1 天" in line for line in out["evidence"])


def test_parse_yf_calendar_only_keeps_window_and_midpoint_estimate():
    import datetime as dt
    today = "2026-11-18"
    cal = {"Earnings Date": [dt.date(2026, 11, 18), dt.date(2027, 6, 1)],
           "Earnings High": 3.117, "Earnings Low": 2.44}
    evs = ef.parse_yf_calendar(cal, "NVDA", days_back=7, days_fwd=90, today=today)
    assert len(evs) == 1                       # 明年 6 月那条在窗口外
    assert evs[0]["event_date"] == "2026-11-18"
    assert evs[0]["eps_estimated"] == pytest.approx(2.7785, abs=1e-4)
    assert evs[0]["source"] == "yfinance"


def test_calendar_uncovered_uses_watchlist(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "EQUITY_TICKERS", ["NVDA", "MU", "AAPL"], raising=False)
    got = ef._calendar_uncovered([{"ticker": "AAPL", "event_date": "2027-02-04"}])
    assert got == {"NVDA", "MU"}

# ── 6) 盈利含金量（v449）──────────────────────────────────────────────────
def _stmt(ni, ocf, rev=1000.0):
    return [{"period_end": f"2026-{12 - i * 3:02d}-30", "net_income": ni,
             "operating_cash_flow": ocf, "revenue": rev} for i in range(4)]


def test_cash_quality_flags_low_conversion():
    out = ff.cash_quality(_stmt(100.0, 40.0))
    assert out["available"] is True and out["ratio"] == pytest.approx(0.4)
    assert out["discount"] == 0.5 and out["flags"] == ["low_cash_quality"]


def test_cash_quality_good_conversion_is_not_rewarded():
    out = ff.cash_quality(_stmt(100.0, 120.0))
    assert out["available"] is True
    assert out["discount"] == 1.0 and out["flags"] == []      # 只判"差"，不奖励"好"


def test_cash_quality_unavailable_when_ocf_missing_or_ni_tiny():
    assert ff.cash_quality(_stmt(100.0, None))["available"] is False        # COIN 口径
    assert ff.cash_quality(_stmt(1.0, 500.0, rev=1000.0))["available"] is False  # 净利<1%营收→分母失真
    assert ff.cash_quality(_stmt(100.0, 900.0))["available"] is False       # 比值>3，多为营运资本波动


def test_build_halves_revision_when_cash_conversion_is_poor(monkeypatch):
    """净利 +10% 但现金流只有四成 → 盈利修正折半，标 low_cash_quality。"""
    eps = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0]
    pack = {
        "summary": {"T": {"ttm": {"eps_diluted": 2.0, "net_income": 100e6},
                          "yoy": {"eps_diluted": 10.0},
                          "gross_margin": 40.0, "net_margin": 10.0}},
        "earnings": _events(eps),
        "statements": {"T": _stmt(100e6, 40e6, rev=1000e6)},
        "asof": int(time.time()),
    }
    monkeypatch.setattr(ff.ef, "db_pack", lambda *a, **k: pack)
    monkeypatch.setattr(ff, "_pe_readout", lambda *a, **k: {
        "pe": 20.0, "pe_position": 50.0, "pe_samples": 80, "pe_samples_ok": True,
        "pe_p25": 18.0, "pb_p25": 0, "pe_p75": 22.0})
    monkeypatch.setattr(ff, "_pb_readout", lambda *a, **k: {})
    out = ff.build("T", conn=None, use_cache=False)
    assert out["components"]["ocf_ni_ratio"] == pytest.approx(0.4)
    assert "low_cash_quality" in out["flags"]
    assert out["components"]["earnings_revision_used"] == pytest.approx(
        out["components"]["earnings_revision"] * 0.5, abs=1e-6)

# ── 7) 票池铺满：yfinance 兜底 + 同季同比 + 缺估值降音量（v452）───────────────
def _frames():
    import pandas as pd
    cols = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
    inc = pd.DataFrame(
        [[1000.0, 900.0, 800.0, 700.0, 500.0],
         [500.0, 450.0, 400.0, 350.0, 250.0],
         [300.0, 280.0, 250.0, 220.0, 150.0],
         [110.0, 100.0, 90.0, 80.0, 55.0],
         [2.0, 1.8, 1.6, 1.4, 1.0]],
        index=["Total Revenue", "Gross Profit", "Operating Income", "Net Income", "Diluted EPS"],
        columns=cols)
    bal = pd.DataFrame(
        [[5000.0, 4800.0, 4600.0, 4400.0, None],
         [3000.0, 2900.0, 2800.0, 2700.0, None],
         [2000.0, 1900.0, 1800.0, 1700.0, None]],
        index=["Total Assets", "Total Liabilities Net Minority Interest", "Stockholders Equity"],
        columns=cols)
    cf = pd.DataFrame(
        [[200.0, 180.0, 160.0, 140.0, 100.0], [-50.0, -45.0, -40.0, -35.0, -30.0]],
        index=["Operating Cash Flow", "Capital Expenditure"], columns=cols)
    return inc, bal, cf


def test_parse_yf_statements_maps_fields_and_labels():
    inc, bal, cf = _frames()
    rows = ef.parse_yf_statements("tst", inc, bal, cf, limit=8)
    assert len(rows) == 5
    first = rows[0]
    assert first["period_end"] == "2026-09-30" and first["source"] == "yfinance"
    assert first["revenue"] == 1000.0 and first["net_income"] == 110.0
    assert first["total_equity"] == 2000.0 and first["operating_cash_flow"] == 200.0
    assert first["period"] == "Q3" and first["fiscal_year"] == "2026"
    assert rows[-1]["total_equity"] is None       # NaN 不写 0，留空


def test_dedupe_keeps_richer_source_without_double_counting():
    inc, bal, cf = _frames()
    yf_rows = ef.parse_yf_statements("tst", inc, bal, cf)
    av_rows = [{"period_end": "2026-09-30", "revenue": 1000.0, "net_income": 110.0,
                "gross_profit": None, "total_equity": None, "operating_cash_flow": None,
                "source": "alphavantage"}]
    merged = ef._dedupe_rows(av_rows + yf_rows)
    assert len(merged) == 5                                   # 同一季只留一条
    assert merged[0]["source"] == "yfinance"                   # 非空字段更多的优先
    assert merged[0]["total_equity"] == 2000.0


def test_stmt_growth_quarter_yoy_and_negative_base():
    rows = [{"period_end": f"2026-{m:02d}-30", "revenue": 1000.0, "net_income": 100.0,
             "eps_diluted": 2.0} for m in (9, 6, 3)]
    rows.append({"period_end": "2025-09-30", "revenue": 800.0, "net_income": -50.0,
                 "eps_diluted": 1.0})
    out = ff.stmt_growth(rows)
    assert out["revenue"] == pytest.approx(25.0)
    assert out["eps_diluted"] == pytest.approx(100.0)
    assert out["net_income"] is None            # 负基期不算同比


def test_build_uses_quarter_yoy_when_no_eps_events(monkeypatch):
    """新票没有 events 历史 → 用三表同季同比，不该再是 score=0/available=False。"""
    inc, bal, cf = _frames()
    rows = ef.parse_yf_statements("NEW", inc, bal, cf)
    pack = {"summary": {"NEW": {"ttm": {"eps_diluted": 6.0, "net_income": 380e6},
                                 "yoy": {}}},
            "earnings": [], "statements": {"NEW": rows}, "asof": int(time.time())}
    monkeypatch.setattr(ff.ef, "db_pack", lambda *a, **k: pack)
    monkeypatch.setattr(ff, "_pe_readout", lambda *a, **k: {})
    monkeypatch.setattr(ff, "_pb_readout", lambda *a, **k: {})
    out = ff.build("NEW", conn=None, use_cache=False)
    assert out["available"] is True
    assert out["components"]["growth_basis"] == "quarter_yoy_eps"
    assert "earnings_only" in out["flags"]                    # 无估值 → 降音量
    assert out["score"] == pytest.approx(out["components"]["earnings_revision_used"] * 0.7,
                                        abs=1e-4)
