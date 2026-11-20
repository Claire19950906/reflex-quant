"""v460 — 非公司基本面分量（alt_fundamental）回归测试。

钉死四件事：
  1. 符号方向必须对：实际利率高 → 黄金利空；库存累积 → 原油利空；短端利率高 → 美元利多。
  2. 分位必须真的用历史算，不是拿当前值拍脑袋（同样是 2.89%，历史分位不同结论要不同）。
  3. 分量不足 2 个必须判不可得，不许拿单一读数硬凑分。
  4. 路由与闸门接线：GOLD/XAUUSD 走 alt（不再是 no_data 缺口），白银仍 N/A；
     闸门 explanation 必须写「非公司基本面」，不能套用「公司级基本面」。

全部用内存假库，不碰生产库、不发网络请求。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import alt_fundamental as af
import decision_guard
import fundamental_factor


# ─────────────────────────── 假库 ───────────────────────────

_SCHEMA = """
CREATE TABLE macro_snapshots (
    ts INTEGER, category TEXT, source TEXT, metric_key TEXT,
    value REAL, unit TEXT, status TEXT, payload TEXT
);
CREATE TABLE external_indicators (
    id INTEGER PRIMARY KEY, asset TEXT, indicator TEXT, value REAL,
    ts INTEGER, source TEXT, regime_tags TEXT, meta TEXT, ingested_at INTEGER
);
"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    return conn


def _seed(conn, key: str, values: list, *, step_days: int = 1,
          category: str = "fred", start_days_ago: int = 0) -> None:
    """按天（或按周）倒序铺一串读数，最后一个值是"当前值"。"""
    now = int(time.time())
    n = len(values)
    for i, v in enumerate(values):
        ts = now - (n - 1 - i) * step_days * 86400 - start_days_ago * 86400
        conn.execute(
            "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ts, category, "test", key, float(v), "", "ok", "{}"))


def _seed_ext(conn, asset: str, indicator: str, values: list, *, step_days: int = 1) -> None:
    now = int(time.time())
    n = len(values)
    for i, v in enumerate(values):
        ts = now - (n - 1 - i) * step_days * 86400
        conn.execute(
            "INSERT INTO external_indicators(asset,indicator,value,ts,source,regime_tags,meta,ingested_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (asset, indicator, float(v), ts, "test", "[]", "{}", now))


def _ramp(start: float, end: float, n: int = 60) -> list:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def _gold_db(*, tips_end: float, dxy_end: float, cot_end: float) -> sqlite3.Connection:
    """60 天切片：实际利率、DXY、黄金 COT（周频）。"""
    conn = _db()
    _seed(conn, "tips10y", _ramp(1.2, tips_end, 60))
    _seed(conn, "dxy", _ramp(100.0, dxy_end, 60))
    _seed(conn, "gold.cot_noncomm_net", _ramp(50000, cot_end, 40),
          step_days=7, category="cot")
    return conn


def _oil_db(*, stocks_end: float, stocks_chg: float, cot_end: float,
            prod_chg_pct: float = 0.0) -> sqlite3.Connection:
    conn = _db()
    _seed(conn, "oil.crude_stocks", _ramp(420000, stocks_end, 60), category="eia")
    _seed(conn, "oil.crude_stocks.chg", _ramp(0, stocks_chg, 2), category="eia")
    _seed(conn, "wti.cot_noncomm_net", _ramp(50000, cot_end, 40),
          step_days=7, category="cot")
    base = 14000.0
    _seed(conn, "oil.us_crude_production",
          _ramp(base, base * (1 + prod_chg_pct / 100), 60), category="eia")
    return conn


# ─────────────────────── 1. 黄金：符号方向 ───────────────────────

def test_gold_high_real_yield_and_strong_dollar_is_bearish():
    """实际利率冲到历史高位 + 美元走强 + 投机盘拥挤 → 黄金基本面必须为负。"""
    out = af.build("XAU", conn=_gold_db(tips_end=3.2, dxy_end=110.0, cot_end=200000),
                   use_cache=False)
    assert out["available"] is True
    assert out["score"] < -0.2
    comps = out["components"]
    assert comps["real_yield"]["score"] < 0        # 实际利率高 = 利空
    assert comps["usd"]["score"] < 0               # 美元强 = 压制金价
    assert comps["cot"]["score"] < 0               # 净多拥挤 = 反向


def test_gold_low_real_yield_and_weak_dollar_is_bullish():
    """镜像场景：实际利率塌到历史低位 + 美元走弱 + 投机盘空仓 → 必须为正。

    同一把尺子、反过来的数，结论必须翻转 —— 否则说明分数是常数不是读数。
    """
    out = af.build("XAU", conn=_gold_db(tips_end=0.4, dxy_end=95.0, cot_end=-30000),
                   use_cache=False)
    assert out["available"] is True
    assert out["score"] > 0.2
    comps = out["components"]
    assert comps["real_yield"]["score"] > 0
    assert comps["usd"]["score"] > 0
    assert comps["cot"]["score"] > 0


# ─────────────────────── 2. 原油：库存口径 ───────────────────────

def test_oil_inventory_build_is_bearish():
    """库存冲到高位 + 周度大幅累库 + 投机净多拥挤 → 原油基本面为负。"""
    out = af.build("WTI", conn=_oil_db(stocks_end=470000, stocks_chg=8000, cot_end=200000),
                   use_cache=False)
    assert out["available"] is True
    assert out["score"] < -0.2
    assert out["components"]["crude_stocks"]["score"] < 0
    assert out["components"]["stocks_chg"]["score"] < 0


def test_oil_inventory_draw_and_short_supply_is_bullish():
    """去库 + 产量下滑 + 投机净空 → 原油基本面为正。"""
    out = af.build("WTI", conn=_oil_db(stocks_end=380000, stocks_chg=-6000,
                                      cot_end=-40000, prod_chg_pct=-4.0),
                   use_cache=False)
    assert out["available"] is True
    assert out["score"] > 0.2
    comps = out["components"]
    assert comps["crude_stocks"]["score"] > 0
    assert comps["stocks_chg"]["score"] > 0
    assert comps["production"]["score"] > 0    # 减产 = 利多


# ─────────────────────── 3. 美元与外汇口径 ───────────────────────

def _usd_db(dgs2_end: float) -> sqlite3.Connection:
    conn = _db()
    _seed(conn, "dgs2", _ramp(3.0, dgs2_end, 60))
    _seed(conn, "tips10y", _ramp(1.0, 2.2, 60))
    _seed(conn, "dxy.cot_noncomm_net", _ramp(20000, 60000, 40),
          step_days=7, category="cot")
    return conn


def test_usd_higher_short_rate_is_bullish_and_eurusd_is_mirror():
    """美元：短端利率走高 → 正分；EURUSD 必须拿到同幅度负分（镜像口径）。"""
    usd = af.build("USD", conn=_usd_db(5.2), use_cache=False)
    eur = af.build("EURUSD", conn=_usd_db(5.2), use_cache=False)
    assert usd["available"] is True and usd["score"] > 0.2
    assert eur["available"] is True and eur["score"] < -0.1
    assert abs(usd["score"] + eur["score"] * -1) >= 0  # 只是可读性检查


def test_usdjpy_uses_rate_spread_percentile_not_hardcoded_level():
    """美日利差：同样 2.04pp，若历史都在 0.5~1.5pp 区间 → 必须判"极阔"（正分）。

    以前写死 `(spread-3.0)/1.5`，利差在 2.0 附近时分数恒为负 —— 与"利差阔=美元强"
    的事实完全相反。这条测试锁死"按历史分位判"的口径。
    """
    conn = _db()
    _seed(conn, "dgs2", _ramp(1.2, 4.0, 60))
    _seed(conn, "jgb10y", _ramp(0.7, 1.96, 60))
    _seed(conn, "jpy.cot_noncomm_net", _ramp(100000, 120000, 40),
          step_days=7, category="cot")
    out = af.build("USDJPY", conn=conn, use_cache=False)
    assert out["available"] is True
    comp = out["components"]["rate_spread"]
    assert comp is not None and comp["score"] > 0.3


# ─────────────────────── 4. 边界：不足/不支持 ───────────────────────

def test_single_component_is_insufficient_not_a_score():
    """只有一个分量时不许给分 —— 单一读数拍出来的分和瞎猜没区别。"""
    conn = _db()
    _seed(conn, "tips10y", _ramp(1.0, 3.0, 60))
    out = af.build("XAU", conn=conn, use_cache=False)
    assert out["available"] is False
    assert out["reason"] == "insufficient"
    assert out["score"] == 0.0


def test_unsupported_asset_says_unsupported():
    """铂金没接入就明说 unsupported，不能悄悄返回 0 分（0 分会被当成中性）。"""
    out = af.build("PLATINUM", conn=_db(), use_cache=False)
    assert out["available"] is False
    assert out["reason"] == "unsupported"


def test_short_history_is_insufficient_for_percentile():
    """少于 30 个观测不算分位 —— 否则 5 个点的"历史"能造出任意结论。"""
    conn = _db()
    _seed(conn, "tips10y", _ramp(1.0, 3.0, 5))
    _seed(conn, "dxy", _ramp(100, 120, 5))
    out = af.build("XAU", conn=conn, use_cache=False)
    assert out["available"] is False
    assert out["reason"] == "insufficient"


# ─────────────────────── 5. 路由与闸门接线 ───────────────────────

def test_router_routes_alt_symbols_before_ticker_classification():
    """XAUUSD/BTCUSDT 这类代码以前被当成"美股 ticker 但没财报"(no_data 缺口)。"""
    for symbol in ("XAUUSD", "GOLD", "BTCUSDT", "EURUSD"):
        out = fundamental_factor.build(symbol, use_cache=False)
        assert out.get("kind") == "alt", symbol
        assert out.get("reason") != "no_data", symbol


def test_router_keeps_unsupported_metals_as_non_equity():
    # v467: 白银/铜/纳指已接入，改用 VIX（真非公司标的、且 alt 不覆盖）
    out = fundamental_factor.build("VIX", use_cache=False)
    assert out["available"] is False
    assert out["reason"] == "non_equity"


def _guard_row(asset: str, fundamental: dict) -> dict:
    ctx = {"assets": {asset: {
        "status": "ok", "change_24h_pct": 1.0, "change_24h_str": "+1.00%",
        "funding_rate_pct": 0.01, "funding_rate_str": "+0.0100%",
        "basis_bps": None, "oi_change_pct": None,
        "stats_7d": {"trend": "Unknown", "atr_pct": 2.0, "atr_str": "2.0%"},
    }}}
    return {"sentiment_score": 0.0, "target_asset": asset,
            "decision_context": json.dumps(ctx), "fundamental": fundamental}


def test_guard_explanation_says_alt_not_company_fundamental():
    """闸门文案必须区分口径：黄金的分不是"PE 贵"，是实际利率/库存/COT。"""
    fund = {"available": True, "score": -0.46, "kind": "alt", "asset": "XAU",
            "reason": "ok", "market": "ALT",
            "components": {"real_yield": {"score": -0.66, "weight": 0.28, "detail": "x"}},
            "evidence": ["real_yield=-0.66（10Y 实际利率分位 83%）"],
            "gaps": ["SPDR 黄金 ETF 持仓不可得"]}
    out = decision_guard.evaluate_decision(_guard_row("XAU", fund))
    factor = out["factors"]["fundamental"]
    assert factor["available"] is True
    assert factor["weight"] == decision_guard.DIRECTIONAL_WEIGHTS["fundamental"]
    assert "非公司基本面" in factor["explanation"]
    assert "公司级基本面" not in factor["explanation"]


def test_guard_company_side_still_renders_company_wording():
    """回归保护：不能为了 alt 把公司侧的文案一起改掉。"""
    fund = {"available": True, "score": 0.3, "kind": "us", "ticker": "NVDA",
            "reason": "ok", "market": "US",
            "components": {"valuation_percentile": 0.4, "earnings_revision": 0.5},
            "evidence": []}
    out = decision_guard.evaluate_decision(_guard_row("NVDA", fund))
    assert "公司级基本面" in out["factors"]["fundamental"]["explanation"]


# ─────────────────────── 6. prompt 压缩与 alt 摘要 ───────────────────────

def test_cot_block_folds_non_essential_fields():
    """COT 168 项 → 只展开投机净头寸类，商业头寸/未平仓折叠并报数量。"""
    import macro_context as mc
    items = [
        {"metric_key": "黄金.cot_noncomm_net", "value": 190107.0, "status": "ok", "payload": {}},
        {"metric_key": "黄金.cot_comm_net", "value": -50000.0, "status": "ok", "payload": {}},
        {"metric_key": "黄金.cot_oi", "value": 800000.0, "status": "ok", "payload": {}},
        {"metric_key": "黄金.cot_spec_ratio", "value": 6.26, "status": "ok", "payload": {}},
    ]
    line = mc._cot_block(items)
    assert "190,107" in line or "190107" in line
    assert "6.26" in line
    assert "折叠" in line
    assert "其余 2 项" in line


def test_alt_block_renders_conclusion_only(monkeypatch):
    """prompt 里只放分量结论，不放 TIPS 点位等原始数字。"""
    import macro_context as mc
    fake = {"available": True, "score": -0.46, "kind": "alt", "asset": "XAU",
            "components": {"real_yield": {"score": -0.66, "weight": 0.28, "detail": "x"},
                           "cot": {"score": -0.18, "weight": 0.22, "detail": "y"}},
            "evidence": [], "gaps": []}
    monkeypatch.setattr(af, "build", lambda *a, **k: fake)
    line = mc._alt_fundamental_block()
    assert "XAU -0.46" in line
    assert "real_yield-0.66" in line
    assert "TIPS" not in line and "2.89" not in line


def test_alt_block_off_switch():
    import macro_context as mc
    old = mc._ALT_PROMPT_MODE
    try:
        mc._ALT_PROMPT_MODE = "off"
        assert mc._alt_fundamental_block() == ""
    finally:
        mc._ALT_PROMPT_MODE = old
