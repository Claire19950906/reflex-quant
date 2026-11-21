# -*- coding: utf-8 -*-
"""quick_sim 建仓/结算判定层的契约测试（全离线，不碰生产库）。

这层是"闸门"的原型：`gate_sell_signal` 决定 SELL 能不能进、
`_is_expired` 决定仓位还能不能用现价结算、`settle_figures` 决定毛→净的唯一口径。
三处历史事故都写在源码注释里，这里把结论固化成断言：
  · 过期仓拿现价结算 → 一笔 20 分钟的仓位被拖成 12742.9 分钟，收益放大到 ±50%
  · entry_ts 的 naive 字符串按本地时区解析 → 凭空平移 8 小时，正常仓位被误判过期
  · 止损/止盈只写毛值、到期只写净值 → 同一张表里的 pnl 不可比，反思层拿错标签
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import quick_sim as qs  # noqa: E402


# ── 窗口归一 ──────────────────────────────────────────────────────────────

def test_resolve_window_prefers_explicit_bounds():
    assert qs._resolve_window(None, 100, 200) == (100, 200)
    assert qs._resolve_window(None, 100, None) == (100, None)   # 开区间到当前
    assert qs._resolve_window(5, None, 300) == (None, 300)      # 开区间到起点
    assert qs._resolve_window(None, None, None) == (None, None)


def test_resolve_window_hours_is_relative_to_now_and_nonpositive_is_ignored():
    lo, hi = qs._resolve_window(2, None, None)
    assert hi is None
    assert abs(lo - (int(time.time()) - 7200)) <= 5
    assert qs._resolve_window(0, None, None) == (None, None)
    assert qs._resolve_window(-3, None, None) == (None, None)


# ── 新闻新鲜度 ────────────────────────────────────────────────────────────

def test_news_age_falls_back_to_ingest_ts_and_unknown_means_fresh():
    now = int(time.time())
    assert qs._news_age_seconds({}) is None
    assert qs._news_age_seconds({"news_ts": now - 600}) is not None
    assert abs(qs._news_age_seconds({"news_ts": now - 600}) - 600) < 10
    assert qs._news_age_seconds({"news_ts": "not-a-ts"}) is None
    # 拿不到时间 → 按新鲜放行（与主分析同口径，宁可放过不误伤）
    assert qs._news_fresh({}, 600) is True
    assert qs._news_fresh({"news_ts": now - 600}, 900) is True
    assert qs._news_fresh({"news_ts": now - 7200}, 2700) is False


# ── SELL 门槛 ─────────────────────────────────────────────────────────────

def test_sell_gate_buy_always_passes(monkeypatch):
    monkeypatch.setattr(qs.config, "SELL_GATE_ENABLED", True, raising=False)
    assert qs.gate_sell_signal({"action": "BUY", "event_strength": "high"}) == (True, "buy_passthrough")
    assert qs.gate_sell_signal({"action": "HOLD"}) == (True, "buy_passthrough")


def test_sell_gate_disabled_switch_passes_everything(monkeypatch):
    monkeypatch.setattr(qs.config, "SELL_GATE_ENABLED", False, raising=False)
    assert qs.gate_sell_signal({"action": "SELL", "asset": "BTC"}) == (True, "disabled")


def test_sell_gate_blocks_high_strength_and_listed_assets(monkeypatch):
    monkeypatch.setattr(qs.config, "SELL_GATE_ENABLED", True, raising=False)
    monkeypatch.setattr(qs.config, "SELL_GATE_BLOCK_HIGH_STRENGTH", True, raising=False)
    monkeypatch.setattr(qs.config, "SELL_GATE_BLOCKED_ASSETS", ("BTC", "WTI"), raising=False)
    assert qs.gate_sell_signal({"action": "SELL", "event_strength": "high"}) == (False, "high_strength")
    assert qs.gate_sell_signal({"action": "SELL", "asset": "btc"}) == (False, "asset_BTC")
    assert qs.gate_sell_signal({"action": "SELL", "asset": "XAU", "event_strength": "low"}) == (True, "passed")


# ── 时间归一：naive 字符串必须是 UTC ──────────────────────────────────────

def test_to_epoch_treats_naive_datetime_as_utc_not_local():
    # SQLite 的 datetime('now') 是 UTC。若按本地 +08:00 解析，会凭空差 8 小时。
    got = qs._to_epoch("2026-11-21T01:00:00")
    want = int(datetime(2026, 11, 21, 1, tzinfo=timezone.utc).timestamp())
    assert got == want
    assert got != int(datetime(2026, 11, 21, 1).astimezone().timestamp())


def test_to_epoch_accepts_numbers_strings_and_returns_zero_on_garbage():
    assert qs._to_epoch(None) == 0
    assert qs._to_epoch("") == 0
    assert qs._to_epoch("   ") == 0
    assert qs._to_epoch(1700000000) == 1700000000
    assert qs._to_epoch("1700000000") == 1700000000
    assert qs._to_epoch("garbage") == 0


def test_is_expired_needs_horizon_plus_grace_not_current_price_settlement():
    now = 1_700_000_000
    assert qs._is_expired({}, now) is False                       # 无 entry_ts 不判过期
    assert qs._is_expired({"entry_ts": now - 600, "horizon_minutes": 30}, now) is False
    # 600 秒前建仓、持有 30 分钟 + 60 分钟宽限 → 仍在窗口内
    assert qs._is_expired({"entry_ts": now - 7200, "horizon_minutes": 30}, now) is True
    assert qs._is_expired({"entry_ts": now - 600, "horizon_minutes": "bad"}, now) is False


# ── 毛→净唯一入口 ─────────────────────────────────────────────────────────

def test_settle_figures_is_the_single_gross_to_net_path():
    net, cost_bps, cost_pct = qs.settle_figures(2.0, "XAU", "BUY")
    assert cost_bps > 0 and cost_pct > 0
    assert abs((net + cost_pct) - 2.0) < 1e-6      # 净 = 毛 − 往返成本
    net2, _, _ = qs.settle_figures(1.0, "XAU", "BUY")
    assert net2 < 1.0


# ── 成本覆盖闸的"不猜"口径 ───────────────────────────────────────────────

def test_cost_edge_gate_is_off_switchable(monkeypatch):
    monkeypatch.setattr(qs.config, "QUICK_SIM_COST_EDGE_ENFORCE", False, raising=False)
    assert qs._cost_edge_allows(sqlite3.connect(":memory:"), "XAU", 60) == (True, "disabled", {})


def test_cost_edge_gate_does_not_block_without_samples(monkeypatch):
    # 没有样本时必须放行并标 edge_unverified —— 不许拿"没数据"当"不划算"
    monkeypatch.setattr(qs.config, "QUICK_SIM_COST_EDGE_ENFORCE", True, raising=False)
    conn = sqlite3.connect(":memory:")
    allowed, reason, meta = qs._cost_edge_allows(conn, "XAU", 60)
    assert allowed is True
    assert reason in ("edge_unverified", "zero_cost", "cost_model_unavailable")
    conn.close()
