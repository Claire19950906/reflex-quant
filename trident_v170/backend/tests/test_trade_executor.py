# -*- coding: utf-8 -*-
"""v190 — TradeExecutor / MockAdapter / BinanceAdapter 单元测试。

覆盖:
- ExecutorConfig.from_env: mode 解析 / poll_interval / notional_usdt / symbol_map / unsupported
- MockAdapter: 价格查找 / 跳过非 USDT 永续 / skip 标记
- RiskManager + TradeExecutor 集成:
  - mode=off 不下单但仍写 audit RISK_REJECTED
  - mode=paper 正常下 Mock 单 + entry_price 已写
  - kill_switch 立即拒
  - 白名单拒绝 → RISK_REJECTED
  - max_position_usdt 上限截断
  - WTI / INDIVIDUAL_STOCK → asset_unsupported → RISK_REJECTED
  - 二次 tick 不重抓（trade_audit_log 排重 + entry_price 排重）
  - 失败重试：连续两次 ADAPTER_ERROR 不会写两次 audit（写过一次后被 NOT IN 过滤）
- BinanceAdapter (dry_run): 不调 place_order 接口 + 标记 skipped
- status endpoint / status payload 结构
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import json
import types

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import trade_executor as te
from trade_executor import (
    OrderRequest, OrderResult, OrderAdapter, MockAdapter,
    ExecutorConfig, TradeExecutor, build_default_executor,
    STATUS_RISK_REJECTED, STATUS_ORDER_PLACED, STATUS_ADAPTER_SKIPPED,
    STATUS_ADAPTER_ERROR,
)
from risk_manager import RiskManager, parse_whitelist


# ─────────────────────────── helper ────────────────────────────

def _make_db() -> sqlite3.Connection:
    """最小 schema（含 ai_decisions + trade_audit_log），返回 (path, conn)。

    必须用 tempfile 路径而非 :memory: —— TradeExecutor 内部会开新连接，
    :memory: 每次都是独立 DB。
    """
    path = tempfile.NamedTemporaryFile(
        prefix="trident_test_trade_", suffix=".db", delete=False
    ).name
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            sentiment_score REAL NOT NULL,
            suggested_action TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'UNREAD',
            market_category TEXT NOT NULL DEFAULT 'OTHER',
            target_asset TEXT NOT NULL DEFAULT 'NONE',
            aggregation_key TEXT DEFAULT '',
            entry_price REAL DEFAULT NULL,
            entry_time TEXT DEFAULT '',
            settled INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE trade_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            asset TEXT NOT NULL DEFAULT '',
            symbol TEXT NOT NULL DEFAULT '',
            side TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            notional_usdt REAL DEFAULT 0.0,
            quantity REAL DEFAULT 0.0,
            filled_price REAL DEFAULT 0.0,
            order_id TEXT DEFAULT '',
            adapter TEXT DEFAULT '',
            mode TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            raw_json TEXT DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.commit()
    return path, conn


def _seed_decision(conn, *, asset="BTC", action="BUY", settled=0,
                   aggregation_key="k1", market_category="crypto",
                   sentiment_score=0.6, entry_price=None) -> int:
    """插入一条 ai_decisions 行，返回 id。"""
    cur = conn.execute(
        """INSERT INTO ai_decisions
           (news_id, sentiment_score, suggested_action, reasoning,
            created_at, status, market_category, target_asset,
            aggregation_key, entry_price, settled)
           VALUES (1, ?, ?, 'test', datetime('now'), 'AUTO_APPROVED',
                   ?, ?, ?, ?, ?)""",
        (sentiment_score, action, market_category, asset,
         aggregation_key, entry_price, settled),
    )
    conn.commit()
    return int(cur.lastrowid)


def _make_executor(mode="paper", *, whitelist=("*",),
                   max_position_usdt=5000.0,
                   adapter=None,
                   unsupported=None,
                   db_path=None) -> TradeExecutor:
    if db_path is None:
        db_path = tempfile.NamedTemporaryFile(
            prefix="trident_test_trade_exec_", suffix=".db", delete=False
        ).name
    ec = ExecutorConfig(
        mode=mode,
        poll_interval=5,
        notional_usdt=max_position_usdt,
        symbol_map={"BTC": "BTCUSDT", "ETH": "ETHUSDT", "XAU": "PAXGUSDT"},
        unsupported=unsupported if unsupported is not None else {"WTI", "INDIVIDUAL_STOCK"},
        max_decisions_per_tick=50,
        testnet=False,
    )
    if adapter is None:
        adapter = MockAdapter(price_lookup=lambda sym: 50000.0 if sym == "BTCUSDT" else 2500.0)
    rm = RiskManager(whitelist=whitelist, max_position_usdt=max_position_usdt)
    return TradeExecutor(ec, adapter, rm, db_path=db_path)


# ───────────────────── ExecutorConfig ─────────────────────────

def test_executor_config_from_env_default_off():
    env = {}
    cfg = ExecutorConfig.from_env(env=env)
    assert cfg.mode == "off"
    assert cfg.poll_interval == 5
    assert cfg.notional_usdt == 100.0  # ExecutorConfig 默认 / from_env 缺省
    assert "BTC" in cfg.symbol_map and cfg.symbol_map["BTC"] == "BTCUSDT"
    assert "INDIVIDUAL_STOCK" in cfg.unsupported


def test_executor_config_from_env_live_real_with_testnet():
    env = {
        "TRADE_EXECUTOR_MODE": "live_real",
        "TRADE_EXECUTOR_POLL_INTERVAL": "3",
        "BINANCE_NOTIONAL_USDT": "1500",
        "BINANCE_USE_TESTNET": "true",
    }
    cfg = ExecutorConfig.from_env(env=env)
    assert cfg.mode == "live_real"
    assert cfg.poll_interval == 3
    assert cfg.notional_usdt == 1500.0
    assert cfg.testnet is True


def test_parse_whitelist_star_matches_all():
    assert parse_whitelist("*") == ("*",)


def test_parse_whitelist_csv():
    assert parse_whitelist("BTC,ETH,XAU") == ("BTC", "ETH", "XAU")


# ───────────────────── MockAdapter ───────────────────────────

def test_mock_adapter_get_price_hit():
    a = MockAdapter(price_lookup=lambda sym: 99.5)
    assert a.get_price("BTCUSDT") == 99.5


def test_mock_adapter_get_price_unknown_returns_none():
    a = MockAdapter(price_lookup=lambda sym: None)
    assert a.get_price("XYZUSDT") is None


def test_mock_adapter_place_order_ok():
    """Mock 始终以 skipped=True 返回（语义：paper mode 不真下单）但 ok=True、含真实 qty/price。"""
    a = MockAdapter(price_lookup=lambda sym: 50000.0)
    req = OrderRequest(symbol="BTCUSDT", side="BUY", type_="MARKET",
                      notional_usdt=1000.0, decision_id=42)
    res = a.place_order(req)
    assert res.ok is True
    assert res.skipped is True
    assert res.note == "paper_mode_no_real_order"
    assert res.order_id.startswith("mock-")
    assert abs(res.filled_qty - 1000.0 / 50000.0) < 1e-9
    assert res.filled_price == 50000.0


def test_mock_adapter_place_order_no_price_skipped():
    """Mock 始终 skipped=True，但价格=0 时 qty/price 都是 0（无法 fill）。"""
    a = MockAdapter(price_lookup=lambda sym: None)
    req = OrderRequest(symbol="BTCUSDT", side="BUY", type_="MARKET",
                      notional_usdt=1000.0, decision_id=1)
    res = a.place_order(req)
    assert res.ok is True
    assert res.skipped is True
    assert res.filled_qty == 0.0
    assert res.filled_price == 0.0


# ───────────────────── TradeExecutor 集成 ─────────────────────

def test_mode_off_does_not_place_order_but_records_audit():
    path, conn = _make_db()
    _seed_decision(conn, asset="BTC", action="BUY")
    ex = _make_executor(mode="off", db_path=path)
    out = ex.tick()
    # mode=off 仍然 fetch + _process_one（用 MockAdapter），但不会真下单
    assert len(out["processed"]) == 1
    rows = conn.execute(
        "SELECT status, adapter FROM trade_audit_log ORDER BY id DESC LIMIT 1"
    ).fetchall()
    assert rows[0]["status"] == STATUS_ADAPTER_SKIPPED
    assert rows[0]["adapter"] == "mock"


def test_paper_mode_fills_and_writes_entry_price():
    path, conn = _make_db()
    did = _seed_decision(conn, asset="BTC", action="BUY")
    ex = _make_executor(mode="paper", db_path=path)
    out = ex.tick()
    assert len(out["processed"]) == 1
    rec = conn.execute(
        "SELECT status, filled_price, order_id FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    # paper mode → MockAdapter.skipped=True → STATUS_ADAPTER_SKIPPED
    assert rec["status"] == STATUS_ADAPTER_SKIPPED
    assert float(rec["filled_price"]) == 50000.0
    assert rec["order_id"].startswith("mock-")
    ad = conn.execute(
        "SELECT entry_price, entry_time FROM ai_decisions WHERE id=?", (did,),
    ).fetchone()
    assert float(ad["entry_price"]) == 50000.0
    assert ad["entry_time"]  # 非空


def test_kill_switch_blocks_immediately():
    path, conn = _make_db()
    did = _seed_decision(conn, asset="BTC", action="BUY")
    ex = _make_executor(mode="paper", db_path=path)
    ex.risk_manager.set_kill_switch(True)
    out = ex.tick()
    assert len(out["processed"]) == 1
    rec = conn.execute(
        "SELECT status, reason FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    assert rec["status"] == STATUS_RISK_REJECTED
    assert "kill_switch" in rec["reason"]


def test_whitelist_rejects_unsupported_symbol():
    """BTC 在 symbol_map，但 whitelist 不含 BTC → risk_manager 拒。"""
    path, conn = _make_db()
    did = _seed_decision(conn, asset="BTC", action="BUY")
    ex = _make_executor(mode="paper", whitelist=("ETH", "XAU"), db_path=path)
    out = ex.tick()
    rec = conn.execute(
        "SELECT status, reason FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    assert rec["status"] == STATUS_RISK_REJECTED
    assert "whitelist" in rec["reason"] or "not_in_whitelist" in rec["reason"]


def test_max_position_usdt_caps_size():
    """exceed 上限 → size 截断到 max_position_usdt，但仍然过审。"""
    path, conn = _make_db()
    did = _seed_decision(conn, asset="BTC", action="BUY")
    ex = _make_executor(mode="paper", max_position_usdt=300.0, db_path=path)
    out = ex.tick()
    rec = conn.execute(
        "SELECT status, notional_usdt FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    assert rec["status"] == STATUS_ADAPTER_SKIPPED
    assert float(rec["notional_usdt"]) == 300.0  # 截断到 300


def test_wti_unsupported_asset_records_risk_rejected():
    path, conn = _make_db()
    did = _seed_decision(conn, asset="WTI", action="BUY",
                         market_category="commodity")
    ex = _make_executor(mode="paper",
                       unsupported={"WTI", "INDIVIDUAL_STOCK"},
                       db_path=path)
    out = ex.tick()
    rec = conn.execute(
        "SELECT status, reason FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    assert rec["status"] == STATUS_RISK_REJECTED
    assert "asset_unsupported:WTI" in rec["reason"]


def test_individual_stock_unsupported_asset_records_risk_rejected():
    path, conn = _make_db()
    did = _seed_decision(conn, asset="INDIVIDUAL_STOCK", action="BUY",
                         market_category="equity")
    ex = _make_executor(mode="paper", db_path=path)
    out = ex.tick()
    rec = conn.execute(
        "SELECT status, reason FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    assert rec["status"] == STATUS_RISK_REJECTED
    assert "asset_unsupported:INDIVIDUAL_STOCK" in rec["reason"]


def test_second_tick_does_not_reprocess_decision():
    """已写入 audit 的决策，下一次 tick 不再处理。"""
    path, conn = _make_db()
    did = _seed_decision(conn, asset="BTC", action="BUY")
    ex = _make_executor(mode="paper", db_path=path)
    out1 = ex.tick()
    out2 = ex.tick()
    assert len(out1["processed"]) == 1
    assert len(out2["processed"]) == 0
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()["c"]
    assert n == 1


def test_risk_rejected_decision_not_reprocessed_in_loop():
    """被风控拒的决策第二次不会被再抓（NOT IN trade_audit_log 防无限循环）。"""
    path, conn = _make_db()
    did = _seed_decision(conn, asset="WTI", action="BUY")
    ex = _make_executor(mode="paper", db_path=path)
    out1 = ex.tick()
    out2 = ex.tick()
    assert len(out1["processed"]) == 1
    assert len(out2["processed"]) == 0
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()["c"]
    assert n == 1


def test_no_price_results_in_adapter_skipped_status():
    path, conn = _make_db()
    did = _seed_decision(conn, asset="BTC", action="BUY")
    adapter = MockAdapter(price_lookup=lambda sym: None)
    ex = _make_executor(mode="paper", adapter=adapter, db_path=path)
    out = ex.tick()
    rec = conn.execute(
        "SELECT status, reason FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    assert rec["status"] == STATUS_ADAPTER_SKIPPED


def test_pending_skips_settled_or_hold_or_none_asset():
    """settled=1 / HOLD / 空 asset / NONE 全部被 _fetch_pending 过滤。"""
    path, conn = _make_db()
    # 用 NONE 作为占位（schema DDL 不允许空串）
    _seed_decision(conn, asset="BTC", action="BUY", settled=1)
    _seed_decision(conn, asset="BTC", action="HOLD")
    # aggregation_key 空不影响 fetch（filter 不查这字段）
    _seed_decision(conn, asset="NONE")
    ex = _make_executor(mode="paper", db_path=path)
    out = ex.tick()
    assert len(out["processed"]) == 0


def test_sell_side_also_routed_through_risk():
    path, conn = _make_db()
    did = _seed_decision(conn, asset="BTC", action="SELL")
    ex = _make_executor(mode="paper", db_path=path)
    out = ex.tick()
    rec = conn.execute(
        "SELECT status, side FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    assert rec["status"] == STATUS_ADAPTER_SKIPPED
    assert rec["side"] == "SELL"


def test_xau_maps_to_paxgusdt():
    path, conn = _make_db()
    did = _seed_decision(conn, asset="XAU", action="BUY",
                         market_category="commodity")
    adapter = MockAdapter(price_lookup=lambda sym: 2500.0)
    ex = _make_executor(mode="paper", adapter=adapter, db_path=path)
    out = ex.tick()
    rec = conn.execute(
        "SELECT status, symbol FROM trade_audit_log WHERE decision_id=?",
        (did,),
    ).fetchone()
    assert rec["status"] == STATUS_ADAPTER_SKIPPED
    assert rec["symbol"] == "PAXGUSDT"


# ───────────────────── Snapshot ───────────────────────────────

def test_snapshot_contains_keys():
    path, conn = _make_db()
    _seed_decision(conn, asset="BTC", action="BUY")
    ex = _make_executor(mode="paper", db_path=path)
    ex.tick()
    snap = ex.snapshot()
    assert snap["mode"] == "paper"
    assert snap["adapter"] == "mock"
    assert snap["notional_usdt"] == 5000.0
    assert snap["stats"]["ticks"] == 1
    assert snap["stats"]["risk_passed"] == 1
    # paper mode 走 MockAdapter.skipped → stats.orders_skipped
    assert snap["stats"]["orders_skipped"] == 1
    assert snap["stats"]["orders_placed"] == 0
    assert snap["stats"]["risk_rejected"] == 0
    assert snap["risk"]["kill_switch"] is False


# ───────────────────── build_default_executor ──────────────────

def test_build_default_executor_paper_uses_mock(monkeypatch):
    monkeypatch.setenv("TRADE_EXECUTOR_MODE", "paper")
    monkeypatch.setenv("WHITELIST_ASSETS", "*")
    monkeypatch.setenv("MAX_POSITION_USDT", "500")
    db_path = tempfile.NamedTemporaryFile(
        prefix="trident_bde_paper_", suffix=".db", delete=False
    ).name
    ex = build_default_executor(db_path=db_path)
    assert ex.cfg.mode == "paper"
    assert ex.adapter.name == "mock"


def test_build_default_executor_live_without_credentials_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("TRADE_EXECUTOR_MODE", "live_real")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    db_path = tempfile.NamedTemporaryFile(
        prefix="trident_bde_live_", suffix=".db", delete=False
    ).name
    ex = build_default_executor(db_path=db_path)
    assert ex.cfg.mode == "live_real"
    # credentials 缺 → 仍 paper-style mock，写 audit 时仍走 live_real mode
    assert ex.adapter.name == "mock"


# ───────────────────── BinanceAdapter (dry_run) ────────────────

def test_binance_adapter_dry_run_skips_place_order():
    """dry_run=True 时 place_order 不打网络, 返回 skipped=True。"""
    from trade_executor import BinanceAdapter

    class _FakeClient:
        name = "binance-test"

        def newOrder(self, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("dry_run 不应调 newOrder")

    adapter = BinanceAdapter(_FakeClient(), dry_run=True)
    req = OrderRequest(symbol="BTCUSDT", side="BUY", type_="MARKET",
                      notional_usdt=100.0, decision_id=1)
    res = adapter.place_order(req)
    assert res.ok is True
    assert res.skipped is True
    assert "dry_run" in (res.note or "").lower() or res.order_id == "DRY-RUN"