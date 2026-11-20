"""v454 基本面补洞 —— 单元测试（全部离线：不联网、不碰生产库）。

覆盖五件事：
  1. SEC companyfacts 解析（同期末去重取最新申报、缺失用 Assets−Liabilities 派生）；
  2. 「已知日」防前视：季末之后、申报日之前，净资产不进 PB 分母；
  3. 股本×收盘 推导市值（历史股本前向填充、缺股本不插值、当日兜底不用来回填）；
  4. PB 序列把推导市值并进分子，样本数当场够门槛；
  5. 同业可比（相对中位数、样本不足拒绝结论）与宏观传导（符号、幅度、数据门槛）；
  6. 影子账本的适用性标记、+10 天主判据与"决策前最后一根收盘"基准价。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import equity_fundamentals as ef
import equity_mktcap as em
import fundamental_factor as ff
import fundamental_shadow as fs
import macro_equity_regime as mr
import peer_valuation as pv
import sec_fundamentals as sec


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
    conn.execute("""CREATE TABLE ohlcv_daily(
        asset TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,
        volume REAL, source TEXT)""")
    return conn


# ── 1) SEC 解析 ───────────────────────────────────────────────────────────
def test_sec_parse_prefers_latest_filing_for_same_period():
    eq = [{"end": "2026-09-30", "filed": "2026-10-20", "val": 100.0},
          {"end": "2026-09-30", "filed": "2026-11-05", "val": 120.0}]   # 重述
    rows = sec.parse_balance(eq)
    assert len(rows) == 1
    assert rows[0]["total_equity"] == 120.0          # 取 filed 最新，不取先到的
    assert rows[0]["filed"] == "2026-11-05"


def test_sec_parse_derives_equity_from_assets_minus_liabilities():
    assets = [{"end": "2026-06-30", "filed": "2026-07-30", "val": 500.0}]
    liabs = [{"end": "2026-06-30", "filed": "2026-07-30", "val": 300.0}]
    rows = sec.parse_balance([], assets, liabs)
    assert rows[0]["total_equity"] == 200.0
    assert rows[0]["derived"] == "assets-liabilities"


def test_sec_parse_keeps_missing_equity_missing():
    """两头都缺就让它缺 —— 不插值、不猜（PB 少一只好过给错的分位）。"""
    assert sec.parse_balance([], [], []) == []
    rows = sec.parse_balance([{"end": "2026-06-30", "filed": "2026-07-30", "val": 100.0}],
                             [], [])
    assert rows[0]["total_equity"] == 100.0


def test_equity_known_date_uses_filing_date_not_period_end():
    assert sec.equity_known_date("2026-09-30", "2026-11-03") == "2026-11-03"
    assert sec.equity_known_date("2026-09-30", "") == "2026-09-30"
    # 申报日早于季末（异常源）→ 取更晚的那个，绝不会提前
    assert sec.equity_known_date("2026-09-30", "2026-09-01") == "2026-09-30"


# ── 2) 已知日防前视 ───────────────────────────────────────────────────────
def test_pb_history_ignores_balance_until_filed():
    """季末 3/31、申报 5/01 → 4/15 的快照不能用这期净资产（否则是前视）。"""
    conn = _memdb()
    now = int(time.time())
    for i, cap in enumerate((100.0, 110.0, 120.0)):
        day = time.strftime("%Y-%m-%d", time.localtime(now - (20 - i) * 86400))
        conn.execute("""INSERT INTO macro_snapshots(ts,metric_key,value,payload)
                        VALUES(?,?,?,?)""",
                     (now - (20 - i) * 86400, "eq.mktcap", cap,
                      '{"label": "FF"}'))
    # 上一期净资产（早就公布）确保有分母
    conn.execute("""INSERT INTO equity_fundamentals(ticker,period_end,total_equity,payload)
                    VALUES('FF','2025-12-31',40e9,'{"filed": "2026-02-01"}')""")
    # 本期：季末 2026-09-30，申报 2026-11-20（晚于全部快照）
    conn.execute("""INSERT INTO equity_fundamentals(ticker,period_end,total_equity,payload)
                    VALUES('FF','2026-09-30',500e9,'{"filed": "2026-11-20"}')""")
    hist = ef.pb_history("FF", days=365, conn=conn)
    conn.close()
    assert hist, "应该有 3 个点"
    # 用旧分母 40e9，而不是新的 500e9
    assert hist[-1][1] == pytest.approx(120e9 / 40e9)


def test_balance_rows_reads_filed_from_payload():
    conn = _memdb()
    conn.execute("""INSERT INTO equity_fundamentals(ticker,period_end,total_equity,payload)
                    VALUES('GG','2026-09-30',10e9,'{"filed": "2026-11-03", "derived": null}')""")
    rows = ef._balance_rows(conn, "GG")
    conn.close()
    # v456：必须同时留 known_date 与 period_end，否则"旧报表晚申报"会把最新一期挤掉
    assert rows == [("2026-11-03", "2026-09-30", 10e9, None, None)]


# ── v456：净资产取"期末最新"，不是"申报日最新" ────────────────────────────
def test_equity_asof_ignores_older_period_reported_later():
    """SEC 会把往年对比期塞进后来的申报（META 2025-06-30 出现在 2026-10-22 的 10-Q 里）。

    按"已知日最新者胜"就会用 195B 那张旧表 → PB 从 5.81 虚涨到 8.11（实测偏差 +39.9%）。
    """
    rows = [
        ("2026-10-22", "2025-06-30", 195.1e9, None, None),   # 旧期，晚申报
        ("2026-09-30", "2026-09-30", 272.4e9, None, None),   # 新期，早已知
    ]
    equity, basis = ef._equity_asof(rows, "2026-11-18")
    assert equity == pytest.approx(272.4e9)
    assert basis == "reported"


def test_equity_asof_skips_empty_row_at_newest_period():
    """期末最新那行恰好没有净资产时，不能挡住有数的旧行（AAPL/NVDA 实测踩到）。"""
    rows = [
        ("2026-09-30", "2026-09-30", None, None, None),
        ("2026-11-03", "2026-09-26", 112e9, None, None),
    ]
    equity, _basis = ef._equity_asof(rows, "2026-11-18")
    assert equity == pytest.approx(112e9)


def test_equity_asof_still_respects_known_date():
    """今天还没申报的季度不许用（防前视这条不能被上面两条修掉）。"""
    rows = [("2026-12-01", "2026-09-30", 500e9, None, None),
            ("2026-06-30", "2026-06-30", 100e9, None, None)]
    equity, _basis = ef._equity_asof(rows, "2026-11-18")
    assert equity == pytest.approx(100e9)


# ── 3) 股本×收盘 ─────────────────────────────────────────────────────────
def test_shares_asof_forward_fills_without_looking_ahead():
    series = [(1000, 10.0), (2000, 12.0)]
    assert em.shares_asof(series, 999) is None          # 还没有任何公布
    assert em.shares_asof(series, 1500) == 10.0         # 用上一期，不用下一期
    assert em.shares_asof(series, 2500) == 12.0


def test_derived_series_multiplies_shares_by_close(monkeypatch):
    conn = _memdb()
    monkeypatch.setattr(em, "shares_series", lambda t, days=420: [(1000, 10.0), (3000, 20.0)])
    monkeypatch.setattr(em, "closes",
                        lambda c, t, days=365: [(1000, 5.0), (2000, 6.0), (3000, 7.0)])
    series = em.derived_series(conn, "ZZ", days=365)
    conn.close()
    assert [v for _, v, _ in series] == [50.0, 60.0, 140.0]    # 第三点已用新股本 20
    assert series[-1][2].startswith("shares×")


def test_derived_series_falls_back_to_one_point_without_history(monkeypatch):
    """历史股本拿不到：只给"今天"一个点，绝不拿当前股本去回填一年。"""
    conn = _memdb()
    monkeypatch.setattr(em, "shares_series", lambda t, days=420: [])
    monkeypatch.setattr(em, "current_shares", lambda t: 30.0)
    monkeypatch.setattr(em, "closes", lambda c, t, days=365: [(1000, 5.0), (2000, 8.0)])
    series = em.derived_series(conn, "WW", days=365)
    conn.close()
    assert len(series) == 1
    assert series[0][1] == pytest.approx(240.0)
    assert series[0][2] == "shares_current×close"


# ── 4) PB 分子并流 ───────────────────────────────────────────────────────
def test_pb_history_unions_derived_market_cap(monkeypatch):
    conn = _memdb()
    now = int(time.time())
    conn.execute("""INSERT INTO equity_fundamentals(ticker,period_end,total_equity,payload)
                    VALUES('HH','2025-06-30',50e9,'{"filed": "2025-08-01"}')""")
    fake = [(int((now - (10 - i) * 86400) // 86400) * 86400, 100e9 + i * 1e9)
            for i in range(10)]
    monkeypatch.setattr(ef, "_derived_caps",
                        lambda c, t, days: {int(ts) // 86400: v for ts, v in fake})
    hist = ef.pb_history("HH", days=365, conn=conn)
    conn.close()
    assert len(hist) == 10                       # 快照为 0，全靠推导通道
    assert hist[0][1] == pytest.approx(100e9 / 50e9)


# ── 5) 同业可比 ───────────────────────────────────────────────────────────
def test_relative_score_sign_and_saturation():
    assert pv.relative_score(10.0, [20.0, 22.0]) == pytest.approx(1.0)   # 便宜>50%→饱和
    assert pv.relative_score(20.0, [10.0, 10.0]) == pytest.approx(-1.0)
    assert pv.relative_score(20.0, [20.0, 20.0]) == pytest.approx(0.0)
    assert pv.relative_score(None, [20.0]) is None
    assert pv.relative_score(20.0, []) is None


def test_peer_build_refuses_on_thin_sample(monkeypatch):
    conn = _memdb()
    monkeypatch.setattr(pv, "_latest_metric",
                        lambda c, t, k: {"AA": 10.0, "BB": 20.0}.get(t))
    out = pv.build("AA", conn=conn)
    conn.close()
    assert out["available"] is False            # 只有 1 只同业 → 不给结论


def test_peer_build_blends_pe_and_pb(monkeypatch):
    conn = _memdb()
    table = {("AA", "eq.pe"): 10.0, ("AA", "eq.pb"): 2.0,
             ("BB", "eq.pe"): 20.0, ("BB", "eq.pb"): 4.0,
             ("CC", "eq.pe"): 20.0, ("CC", "eq.pb"): 4.0}
    monkeypatch.setattr(pv, "_latest_metric", lambda c, t, k: table.get((t, k)))
    monkeypatch.setattr(pv, "SECTORS", {"test": ("AA", "BB", "CC")}, raising=False)
    out = pv.build("AA", conn=conn)
    conn.close()
    assert out["available"] is True
    assert out["components"]["rel_pe"] == pytest.approx(1.0)      # 便宜 50% → +1
    assert out["components"]["rel_pb"] == pytest.approx(1.0)
    assert out["score"] == pytest.approx(1.0)


# ── 6) 宏观传导 ───────────────────────────────────────────────────────────
def test_macro_change_needs_enough_days():
    assert mr._change([(i * 86400, float(i)) for i in range(5)]) is None
    series = [(i * 86400, float(i)) for i in range(25)]
    assert mr._change(series, window=20) == pytest.approx(20.0)   # 末-第21个


def test_macro_tilt_signs_rate_up_hurts_duration(monkeypatch):
    conn = _memdb()
    now = int(time.time())
    # 10Y 20 日内 +0.30pp（利率压力满分）
    for i in range(30):
        conn.execute("""INSERT INTO macro_snapshots(ts,metric_key,value,payload) VALUES(?,?,?,?)""",
                     (now - (30 - i) * 86400, "dgs10", 4.0 + (0.01 * i if i >= 10 else 0.0), "{}"))
    reg = mr.build(conn, days=90)
    conn.close()
    assert reg["available"] is True
    assert reg["drivers"]["rate"] == pytest.approx(1.0, abs=0.05)
    assert reg["tilt"]["ai_growth"] < 0          # 利率上行 → 长久期负
    assert abs(reg["tilt"]["ai_growth"]) <= 1.0


def test_macro_tilt_needs_ten_days():
    conn = _memdb()
    now = int(time.time())
    for i in range(5):
        conn.execute("""INSERT INTO macro_snapshots(ts,metric_key,value,payload) VALUES(?,?,?,?)""",
                     (now - (5 - i) * 86400, "dgs10", 4.0, "{}"))
    reg = mr.build(conn, days=90)
    conn.close()
    assert reg["available"] is False


# ── 7) 因子接线 ───────────────────────────────────────────────────────────
def _pack(eps_yoy=10.0):
    return {"summary": {"T": {"ttm": {"eps_diluted": 2.0, "net_income": 1e9},
                              "yoy": {"eps_diluted": eps_yoy},
                              "gross_margin": 40.0, "net_margin": 10.0}},
            "earnings": [], "statements": {"T": []}, "asof": int(time.time())}


def test_peer_blend_uses_thirty_percent(monkeypatch):
    monkeypatch.setattr(ff.ef, "db_pack", lambda *a, **k: _pack())
    monkeypatch.setattr(ff, "_pe_readout", lambda *a, **k: {
        "pe": 20.0, "pe_position": 50.0, "pe_samples": 80, "pe_samples_ok": True,
        "pe_p25": 18.0, "pe_p75": 22.0})
    monkeypatch.setattr(ff, "_pb_readout", lambda *a, **k: {})
    monkeypatch.setattr(pv, "build", lambda *a, **k: {
        "available": True, "score": 1.0, "sector": "test",
        "detail": "test 同业 3 只", "evidence": ["PE 10 vs 20"],
        "components": {}})
    monkeypatch.setattr(mr, "cached_build", lambda *a, **k: {"available": False})
    out = ff.build("T", conn=None, use_cache=False)
    # 自身分位 50% → 0；同业 +1 → 0.7×0 + 0.3×1 = 0.3
    assert out["components"]["valuation_percentile"] == pytest.approx(0.3)
    assert out["components"]["peer_rel_score"] == pytest.approx(1.0)


def test_macro_tilt_is_capped(monkeypatch):
    monkeypatch.setattr(ff.ef, "db_pack", lambda *a, **k: _pack())
    monkeypatch.setattr(ff, "_pe_readout", lambda *a, **k: {
        "pe": 20.0, "pe_position": 50.0, "pe_samples": 80, "pe_samples_ok": True,
        "pe_p25": 18.0, "pe_p75": 22.0})
    monkeypatch.setattr(ff, "_pb_readout", lambda *a, **k: {})
    monkeypatch.setattr(pv, "build", lambda *a, **k: {"available": False, "detail": "无"})
    monkeypatch.setattr(mr, "cached_build", lambda *a, **k: {"available": True})
    monkeypatch.setattr(mr, "tilt_for", lambda *a, **k: {
        "available": True, "tilt": 1.0, "sector": "test", "evidence": []})
    base = ff.build("T", conn=None, use_cache=False)
    monkeypatch.setattr(mr, "tilt_for", lambda *a, **k: {
        "available": True, "tilt": -1.0, "sector": "test", "evidence": []})
    down = ff.build("T", conn=None, use_cache=False)
    assert base["score"] - down["score"] == pytest.approx(2 * ff._MACRO_TILT_CAP, abs=1e-3)


# ── 8) 影子账本 ───────────────────────────────────────────────────────────
def _shadow(conn, **kw):
    fs.ensure_table(conn)
    row = {"decision_id": 1, "ts": int(time.time()) - 15 * 86400, "asset": "AA",
           "ticker": "AA", "action": "BUY", "action_with": "BUY",
           "action_without": "HOLD", "applicable": 1, "fund_available": 1}
    row.update(kw)
    conn.execute(
        """INSERT INTO fundamental_shadow
           (decision_id, ts, asset, ticker, action, action_with, action_without,
            applicable, fund_available, settled)
           VALUES (?,?,?,?,?,?,?,?,?,0)""",
        (row["decision_id"], row["ts"], row["asset"], row["ticker"], row["action"],
         row["action_with"], row["action_without"], row["applicable"],
         row["fund_available"]))
    sel = f"{int(row['ts'])}".encode()


def test_shadow_skips_not_applicable_rows(monkeypatch):
    conn = _memdb()
    fs.ensure_table(conn)
    conn.execute("""INSERT INTO fundamental_shadow
        (decision_id, ts, asset, ticker, action, applicable, fund_available, settled)
        VALUES (1, ?, 'XAU', NULL, 'HOLD', 0, 0, 0)""", (int(time.time()) - 15 * 86400,))
    fs.evaluate(conn, days=30)
    settled = conn.execute("SELECT settled FROM fundamental_shadow").fetchone()[0]
    conn.close()
    assert settled == -1          # 不适用 → 退出样本池，但留痕


def test_shadow_settles_at_ten_day_primary_horizon():
    conn = _memdb()
    t0 = int(time.time()) - 20 * 86400
    conn.execute("""INSERT INTO ohlcv_daily(asset,ts,close) VALUES('AA',?,100.0)""",
                 (t0 - 3600,))                        # 决策前最后一根
    conn.execute("""INSERT INTO ohlcv_daily(asset,ts,close) VALUES('AA',?,110.0)""",
                 (t0 + 10 * 86400,))
    _shadow(conn, ts=t0)
    fs.evaluate(conn, days=40)
    row = conn.execute("""SELECT settled, ret_10d, ret_1d, hit_with, hit_without
                          FROM fundamental_shadow""").fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] == pytest.approx(10.0)
    assert row[2] is None            # 只有 10 天那根，1 天窗口不该结算出数
    assert row[3] == 1               # BUY + 正收益 → 命中
    assert row[4] is None            # HOLD 不算命中


def test_shadow_summary_counts_applicable_separately():
    conn = _memdb()
    now = int(time.time()) - 20 * 86400
    conn.execute("""INSERT INTO ohlcv_daily(asset,ts,close) VALUES('AA',?,100.0)""", (now - 60,))
    conn.execute("""INSERT INTO ohlcv_daily(asset,ts,close) VALUES('AA',?,90.0)""",
                 (now + 10 * 86400,))
    _shadow(conn, ts=now, action_with="BUY", action_without="BUY")
    conn.execute("""INSERT INTO fundamental_shadow(decision_id, ts, asset, action, settled)
                    VALUES (2, ?, 'XAU', 'HOLD', 0)""", (now,))
    fs.evaluate(conn, days=40)
    stats = fs.summarize(conn, days=40)
    conn.close()
    assert stats["samples"] == 2
    assert stats["applicable"] == 1
    assert stats["not_applicable"] == 1
    assert stats["primary_horizon"] == "10d"
    assert stats["verdict"]["code"] == "insufficient"
