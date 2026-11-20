#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v512：决策引擎结算入口的行为测试（decision_engine 从 0% 起步）。

这一层是"串起来用对"的地方，前面成本、T+1、多周期各自都测了，但组合口径没测。
四条主线：
  ① 去重必须只认真正相等的 signal_id —— 子串匹配会把 7 和 70 混成一单（静默少做一单）；
  ② 结算收益必须扣成本、命中按净收益判，且裸价收益仍按历史语义存；
  ③ 离谱收益（|x|>90%）拒写、状态保持 open，不能污染统计；
  ④ 只结算 BUY/SELL 且已到期的，REJECTED/HOLD/未到期一律不进队列。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import decision_engine as de

DDL = """
CREATE TABLE trade_decisions(
    id INTEGER PRIMARY KEY,
    asset TEXT, final_action TEXT, entry_price REAL, decision_ts INTEGER,
    event_strength TEXT, context_json TEXT, ai_decision_id INTEGER,
    status TEXT, settled_at INTEGER, actual_outcome_pct REAL
);
CREATE TABLE trade_reflections_v2(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER UNIQUE, paper_run_id INTEGER, asset TEXT, action TEXT,
    entry_price REAL, exit_price REAL, pnl_pct REAL,
    dimension TEXT DEFAULT '', subcategory TEXT DEFAULT '',
    hit INTEGER DEFAULT 0, actual_outcome_pct REAL, confidence_at_time REAL,
    settled INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE ohlcv_daily(asset TEXT, ts INTEGER, close REAL);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    return c


@pytest.fixture(autouse=True)
def stub_glue(monkeypatch):
    """把外部依赖固定住：远期价、维度解析、降级判定。"""
    monkeypatch.setattr(de._shared_db, "forward_price",
                        lambda conn, asset, ts, settle_days=5, min_ts=None: 110.0,
                        raising=False)
    monkeypatch.setattr(de, "_resolve_dimension_for_decision",
                        lambda conn, did: ("A", "momentum"), raising=False)
    import insight_pool as ip
    monkeypatch.setattr(ip, "is_degraded_decision", lambda conn, ai_id: False, raising=False)


def _dec(conn, did=1, asset="BTC", action="BUY", entry=100.0, ts=1_600_000_000,
         strength="high", ctx=None, status="open", ai_id=None):
    conn.execute(
        "INSERT INTO trade_decisions(id, asset, final_action, entry_price, decision_ts,"
        " event_strength, context_json, ai_decision_id, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (did, asset, action, entry, ts, strength, json.dumps(ctx or {}), ai_id, status))
    conn.commit()
    # settle_one 只按 key 取值（和 sqlite3.Row 同接口），用 dict 造一条即可
    return {"id": did, "asset": asset, "action": action, "price_at_decision": entry,
            "created_at": ts, "event_strength": strength,
            "context_json": json.dumps(ctx or {}), "ai_decision_id": ai_id}


def _row(conn, did=1):
    return conn.execute("SELECT * FROM trade_decisions WHERE id=?", (did,)).fetchone()


# ── 纪律①：去重只认整数相等 ────────────────────────────────
def test_dedup_ignores_longer_signal_id(conn):
    """`"signal_id": 70` 不能被当成 signal 7 已决策 —— 否则白丢一单。"""
    conn.execute("INSERT INTO trade_decisions(id, context_json) VALUES(9, ?)",
                 (json.dumps({"source": "decision_engine", "signal_id": 70}),))
    conn.commit()
    assert de._is_already_decided(conn, 7) is False
    assert de._is_already_decided(conn, 70) is True


def test_dedup_matches_exact_signal(conn):
    conn.execute("INSERT INTO trade_decisions(id, context_json) VALUES(9, ?)",
                 (json.dumps({"signal_id": 7, "move_ts": 123}),))
    conn.commit()
    assert de._is_already_decided(conn, 7) is True
    assert de._is_already_decided(conn, 8) is False


def test_dedup_survives_broken_json(conn):
    """context_json 坏掉时退回严格字面匹配，不能因此重复下单。"""
    conn.execute("INSERT INTO trade_decisions(id, context_json) VALUES(9, ?)",
                 ('{"signal_id": 7, broken',))
    conn.commit()
    assert de._is_already_decided(conn, 7) is True
    assert de._is_already_decided(conn, 70) is False


def test_dedup_empty_table(conn):
    assert de._is_already_decided(conn, 1) is False


# ── 动作映射：0.10% 是分界 ─────────────────────────────────
def test_action_from_move_band_is_exclusive():
    assert de._action_from_move(0.11) == "BUY"
    assert de._action_from_move(0.10) == "HOLD", "恰好 0.10 不算超过阈值"
    assert de._action_from_move(0.0) == "HOLD"
    assert de._action_from_move(-0.10) == "HOLD"
    assert de._action_from_move(-0.11) == "SELL"


# ── 入场价回退链 ─────────────────────────────────────────
def test_price_at_decision_fallback_order(conn):
    assert de._price_at_decision(conn, "BTC", 100, 100.0, 99.0) == 100.0
    assert de._price_at_decision(conn, "BTC", 100, None, 99.0) == 99.0
    assert de._price_at_decision(conn, "BTC", 100, 0, 0) is None

    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC', 90, 95.0)")
    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC', 100, 96.0)")
    conn.commit()
    assert de._price_at_decision(conn, "BTC", 100, None, None) == 96.0
    assert de._price_at_decision(conn, "ETH", 100, None, None) is None


# ── 结算：成本 / 命中 / 语义 ──────────────────────────────
def test_settle_buy_applies_cost_and_keeps_raw(conn):
    dec = _dec(conn, did=1, action="BUY", entry=100.0)
    res = de.settle_one(conn, dec)
    assert res["action"] == "settled"
    assert res["outcome_pct"] == pytest.approx(10.0), "远期价 110 / 入场 100 = +10%"
    bps = __import__("cost_model").round_trip_cost_bps("BTC")
    assert res["outcome_pct_net"] == pytest.approx(10.0 - bps / 100.0)
    assert res["hit"] == 1

    row = _row(conn, 1)
    assert row["status"] == "settled" and row["settled_at"]
    assert row["actual_outcome_pct"] == pytest.approx(10.0), "裸价收益保持历史语义"

    refl = conn.execute("SELECT pnl_pct, hit, action, entry_price, dimension, subcategory "
                        "FROM trade_reflections_v2 WHERE decision_id=1").fetchone()
    assert refl["pnl_pct"] == pytest.approx(res["outcome_pct_net"]), "反思行存净收益"
    assert refl["dimension"] == "A" and refl["subcategory"] == "momentum"


def test_settle_sell_judges_hit_by_net_sign(conn, monkeypatch):
    monkeypatch.setattr(de._shared_db, "forward_price",
                        lambda conn, asset, ts, settle_days=5, min_ts=None: 105.0, raising=False)
    dec = _dec(conn, did=2, action="SELL", entry=100.0)
    res = de.settle_one(conn, dec)
    assert res["outcome_pct"] == pytest.approx(5.0)
    assert res["outcome_pct_net"] < 5.0
    assert res["hit"] == 0 and 0 == conn.execute(
        "SELECT hit FROM trade_reflections_v2 WHERE decision_id=2").fetchone()["hit"]


def test_settle_hit_requires_net_positive(conn, monkeypatch):
    """裸价涨 0.05%，扣 30bps 后为负 —— 不能算赢。"""
    monkeypatch.setattr(de._shared_db, "forward_price",
                        lambda conn, asset, ts, settle_days=5, min_ts=None: 100.05, raising=False)
    dec = _dec(conn, did=3, action="BUY", entry=100.0)
    res = de.settle_one(conn, dec)
    assert res["outcome_pct"] > 0 and res["outcome_pct_net"] < 0
    assert res["hit"] == 0


def test_settle_skips_without_entry_or_forward(conn, monkeypatch):
    dec = _dec(conn, did=4, action="BUY", entry=0.0)
    assert de.settle_one(conn, dec)["reason"] == "no price_at_decision"

    monkeypatch.setattr(de._shared_db, "forward_price",
                        lambda *a, **k: None, raising=False)
    dec2 = _dec(conn, did=5, action="BUY", entry=100.0)
    res = de.settle_one(conn, dec2)
    assert res["action"] == "skipped" and "forward price" in res["reason"]
    assert _row(conn, 5)["status"] == "open", "取不到远期价不许结算"


# ── 纪律③：离谱收益拒写 ───────────────────────────────────
def test_implausible_outcome_leaves_row_open(conn, monkeypatch):
    monkeypatch.setattr(de._shared_db, "forward_price",
                        lambda conn, asset, ts, settle_days=5, min_ts=None: 300.0, raising=False)
    dec = _dec(conn, did=6, action="BUY", entry=100.0)
    res = de.settle_one(conn, dec)
    assert res["action"] == "skipped" and "implausible" in res["reason"]
    assert _row(conn, 6)["status"] == "open", "异常价必须保持 open 等重新取价"
    assert conn.execute("SELECT COUNT(*) FROM trade_reflections_v2").fetchone()[0] == 0


def test_second_settle_does_not_rewrite_amount(conn):
    dec = _dec(conn, did=7, action="BUY", entry=100.0)
    de.settle_one(conn, dec)
    first = _row(conn, 7)["actual_outcome_pct"]
    de.settle_one(conn, dec)
    assert _row(conn, 7)["actual_outcome_pct"] == pytest.approx(first), \
        "UPDATE 带 status='open' 条件，重复结算不能改金额"


# ── 降级决策：账务照写、反思不写 ─────────────────────────
def test_degraded_decision_skips_reflection(conn, monkeypatch):
    import insight_pool as ip
    monkeypatch.setattr(ip, "is_degraded_decision", lambda conn, ai_id: True, raising=False)
    dec = _dec(conn, did=8, action="BUY", entry=100.0)
    res = de.settle_one(conn, dec)
    assert res["reflection"] == "skipped_degraded"
    assert _row(conn, 8)["status"] == "settled", "降级也得记账"
    assert conn.execute("SELECT COUNT(*) FROM trade_reflections_v2").fetchone()[0] == 0


# ── 待结算队列 ───────────────────────────────────────────
def test_due_settlements_filters_and_orders(conn):
    now = 1_600_000_000
    _dec(conn, did=1, action="BUY", ts=now - de.HORIZON_S)      # 正好到期
    _dec(conn, did=2, action="SELL", ts=now - de.HORIZON_S - 100)
    _dec(conn, did=3, action="HOLD", ts=now - de.HORIZON_S)     # HOLD 不结算
    _dec(conn, did=4, action="REJECTED", ts=now - de.HORIZON_S) # 未成交不结算
    _dec(conn, did=5, action="BUY", ts=now - 60)                # 未到期
    _dec(conn, did=6, action="BUY", ts=now - de.HORIZON_S, status="settled")

    rows = de.list_due_settlements(conn, now_ts=now)
    assert [r["id"] for r in rows] == [2, 1], "只取到期且未结算的 BUY/SELL，按时间升序"
    assert rows[0]["action"] == "SELL" and rows[0]["price_at_decision"] == 100.0
