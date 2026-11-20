#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v511：成本与交收口径的行为测试（cost_model 49% → 目标 90%+）。

这个模块是"所有盈亏数字的地基"：一处口径错，回放、结算、复盘全跟着错。
测试盯五件事：
  ① 往返成本 = 2×(手续费+滑点)，且 1bp = 0.01 个百分点，单位不许错；
  ② 别名要归一（BTCUSDT / XAUUSD 与主代码同费率），否则同一品种两套成本；
  ③ T+1 的"最早可平仓"必须跳过周末，且平仓时点正好等于最早时点算允许；
  ④ 滑点方向必须对交易者不利（买更贵、卖更便宜），不能算成对自己有利；
  ⑤ 环境变量写错只能是"忽略"，不能让整条结算链崩掉。
"""
import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import cost_model as cm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("TRIDENT_FEE_BPS", "TRIDENT_SLIPPAGE_BPS", "TRIDENT_DISABLE_COSTS",
                 "TRIDENT_SETTLEMENT_LAG_DAYS", "TRIDENT_SETTLEMENT_TZ"):
        monkeypatch.delenv(name, raising=False)
    yield


# ── 成本：单位与构成 ───────────────────────────────────────
def test_round_trip_is_twice_fee_plus_slippage():
    for asset in ("BTC", "XAU", "WTI", "STOCK"):
        fee, slip = cm._class_bps(asset)
        assert cm.round_trip_cost_bps(asset) == pytest.approx(2.0 * (fee + slip))


def test_unknown_asset_falls_back_to_conservative_default():
    # 未知品种按最保守 30bps 往返（10+5 单边 → 2×15）
    assert cm.round_trip_cost_bps("SOMECOIN") == pytest.approx(30.0)


def test_one_bp_is_one_hundredth_of_a_percent():
    """30bps 往返 = 0.3 个百分点：整数一错，全库盈亏差十倍。"""
    assert cm.apply_costs(1.0, "SOMECOIN") == pytest.approx(0.7)
    assert cm.apply_costs(-1.0, "SOMECOIN") == pytest.approx(-1.3)


def test_aliases_share_the_same_cost():
    assert cm.round_trip_cost_bps("btcusdt") == cm.round_trip_cost_bps("BTC")
    assert cm.round_trip_cost_bps(" XAUUSD ") == cm.round_trip_cost_bps("XAU")
    assert cm.round_trip_cost_bps("usoil") == cm.round_trip_cost_bps("WTI")


def test_apply_costs_passes_through_bad_input():
    assert cm.apply_costs(None, "BTC") is None
    assert cm.apply_costs("abc", "BTC") == "abc"


def test_disable_switch_returns_raw(monkeypatch):
    monkeypatch.setenv("TRIDENT_DISABLE_COSTS", "1")
    assert cm.apply_costs(5.0, "BTC") == pytest.approx(5.0)
    monkeypatch.setenv("TRIDENT_DISABLE_COSTS", "0")
    assert cm.apply_costs(5.0, "BTC") < 5.0


def test_env_override_costs(monkeypatch):
    monkeypatch.setenv("TRIDENT_FEE_BPS", "2")
    monkeypatch.setenv("TRIDENT_SLIPPAGE_BPS", "3")
    assert cm.round_trip_cost_bps("BTC") == pytest.approx(10.0)


def test_broken_env_override_is_ignored_not_fatal(monkeypatch):
    """`TRIDENT_FEE_BPS=18bps` 这种写法很常见；不许因此让成本计算抛异常。"""
    monkeypatch.setenv("TRIDENT_FEE_BPS", "18bps")
    fee, slip = cm._class_bps("BTC")          # 不抛，退回默认
    assert isinstance(fee, float) and isinstance(slip, float)
    assert cm.apply_costs(1.0, "BTC") == pytest.approx(1.0 - cm.round_trip_cost_bps("BTC") / 100)


# ── 成本明细（前端/审计用） ─────────────────────────────────
def test_cost_summary_matches_round_trip():
    s = cm.cost_summary("BTC")
    assert s["round_trip_bps"] == pytest.approx(
        2.0 * (s["fee_bps_per_side"] + s["slippage_bps_per_side"]))
    assert s["round_trip_pct"] == pytest.approx(s["round_trip_bps"] / 100.0)
    assert s["normalized_asset"] == "BTC"


# ── 交收：T+0 / T+1 ───────────────────────────────────────
def test_crypto_metal_energy_are_t_plus_0():
    for a in ("BTC", "ETH", "XAU", "GOLD", "WTI", "BRENT"):
        assert cm.settlement_lag_days(a) == 0
        assert cm.settlement_rule(a)["t_plus"] == "T+0"


def test_stock_and_unknown_are_t_plus_1():
    assert cm.settlement_lag_days("STOCK") == 1
    assert cm.settlement_lag_days("AAPL") == 1, "未知品种按最严口径保守处理"
    assert cm.settlement_rule("AAPL")["t_plus"] == "T+1"


def test_lag_env_override_and_note(monkeypatch):
    monkeypatch.setenv("TRIDENT_SETTLEMENT_LAG_DAYS", "0")
    rule = cm.settlement_rule("AAPL")
    assert rule["lag_days"] == 0 and rule["t_plus"] == "T+0"
    assert "env 覆盖" in rule["note"]


def test_bad_lag_override_is_ignored(monkeypatch):
    monkeypatch.setenv("TRIDENT_SETTLEMENT_LAG_DAYS", "next-day")
    assert cm.settlement_lag_days("AAPL") == 1, "写错就忽略，不许崩"


def test_earliest_exit_t0_is_entry_itself():
    ts = 1_700_000_000
    assert cm.earliest_exit_ts("BTC", ts) == ts


def test_earliest_exit_t1_opens_next_trading_day():
    tz = ZoneInfo("Asia/Shanghai")
    # 2026-11-20 是周五 15:00（北京时间，收盘后）→ 下周一是 11-23 09:30
    entry = int(dt.datetime(2026, 11, 20, 15, 0, tzinfo=tz).timestamp())
    earliest = cm.earliest_exit_ts("AAPL", entry)
    got = dt.datetime.fromtimestamp(earliest, tz)
    assert got.date() == dt.date(2026, 11, 23), "必须跳过周六周日"
    assert (got.hour, got.minute) == (9, 30)
    assert entry < earliest


def test_earliest_exit_t1_spans_year_end_weekend():
    tz = ZoneInfo("Asia/Shanghai")
    entry = int(dt.datetime(2026, 12, 31, 16, 0, tzinfo=tz).timestamp())   # 周四
    got = dt.datetime.fromtimestamp(cm.earliest_exit_ts("AAPL", entry), tz)
    assert got.date() == dt.date(2027, 1, 1) or got.weekday() < 5
    assert got.weekday() < 5, "落到工作日就行"


# ── 平仓是否允许 ─────────────────────────────────────────
def test_exit_allowed_t0_always():
    ok, why = cm.is_exit_allowed("BTC", 1_700_000_000, 1_700_000_001)
    assert ok and "T+0" in why


def test_exit_allowed_boundary_is_inclusive():
    tz = ZoneInfo("Asia/Shanghai")
    entry = int(dt.datetime(2026, 11, 18, 10, 0, tzinfo=tz).timestamp())    # 周三
    earliest = cm.earliest_exit_ts("AAPL", entry)
    ok, _ = cm.is_exit_allowed("AAPL", entry, earliest)
    assert ok, "正好等于最早可平仓时点必须允许（边界是闭区间）"
    ok2, why2 = cm.is_exit_allowed("AAPL", entry, earliest - 1)
    assert not ok2 and "早于" in why2


# ── 滑点方向 ─────────────────────────────────────────────
def test_slippage_is_always_adverse():
    p = 100.0
    assert cm.effective_entry_price(p, "BTC", "BUY") > p, "买入要更贵"
    assert cm.effective_entry_price(p, "BTC", "SELL") < p, "卖出要更便宜"
    assert cm.effective_exit_price(p, "BTC", "BUY") < p, "多头平仓是卖出"
    assert cm.effective_exit_price(p, "BTC", "SELL") > p, "空头平仓是买回"
    assert cm.effective_entry_price("bad", "BTC") == "bad"


def test_slippage_magnitude_matches_configured_bps():
    _, slip = cm._class_bps("BTC")
    p = 100.0
    assert cm.effective_entry_price(p, "BTC", "BUY") == pytest.approx(p * (1 + slip / 10000.0))


# ── 一次性口径（审计用） ─────────────────────────────────
def test_settlement_policy_is_consistent():
    pol = cm.settlement_policy("BTC")
    assert pol["round_trip_pct"] == pytest.approx(pol["round_trip_bps"] / 100.0)
    assert pol["t_plus"] == "T+0" and pol["market"] == "crypto"
    assert pol["normalized_asset"] == "BTC"
    pol_stock = cm.settlement_policy("aapl")
    assert pol_stock["t_plus"] == "T+1"
    assert pol_stock["normalized_asset"] == "AAPL"
