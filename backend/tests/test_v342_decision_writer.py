"""v342 — trade_decisions 口径统一（decision_writer 回归测试）

钉死的五件事：
  1. 三条写入口径共用同一列集合（status / decision_source / score / confidence …）
  2. 口径错了不落：algo_score 不再冒充 confidence（NULL，而不是错值）
  3. 老写法 log_decision 行为不变（签名兼容、返回 id、status=open）
  4. entry_price 缺失时走回溯，取不到留 NULL，不猜价
  5. 越界值被夹住而不是写进库（score ±1 / confidence 0-100）
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
from signals import decision_writer
import trade_decisions


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    trade_decisions.init_schema(conn)
    db.migrate(conn)
    return conn


def _row(conn, did):
    return conn.execute("SELECT * FROM trade_decisions WHERE id=?", (did,)).fetchone()


def test_unified_columns_exist_after_migrate():
    conn = _fresh_db()
    flags = decision_writer.has_decision_columns(conn)
    assert all(flags.values()), flags
    # status 这一列此前只在 INSERT 里出现、迁移从未建 → 全新库直接报错
    assert "status" in {r[1] for r in conn.execute("PRAGMA table_info(trade_decisions)")}


def test_three_sources_write_same_columns():
    conn = _fresh_db()
    did_log = trade_decisions.log_decision(
        conn, "btc", "buy", "buy", "ENTRY", guard="NONE", context={"k": 1})
    did_algo = decision_writer.write_decision(
        conn, asset="XAU", intended_action="sell", decision_type="ENTRY",
        source=decision_writer.SOURCE_ALGO_MOVE, score=0.4,
        context={"algo_score": 0.4})
    did_bridge = decision_writer.write_decision(
        conn, asset="WTI", intended_action="BUY", decision_type="ENTRY",
        source=decision_writer.SOURCE_BRIDGE_API, score=0.2, confidence=73.0,
        context={"rationale": "x"})

    rows = [_row(conn, d) for d in (did_log, did_algo, did_bridge)]
    assert [r["decision_source"] for r in rows] == ["log_decision", "algo_move", "bridge_api"]
    assert [r["status"] for r in rows] == ["open", "open", "open"]
    # 列集合一致：同一批字段在三行里都有（非 NULL 由各路径自带，不强制）
    for name in ("decision_ts", "asset", "intended_action", "final_action",
                 "decision_type", "context_json", "status", "decision_source"):
        assert all(r[name] is not None for r in rows), name


def test_algo_score_does_not_pretend_to_be_confidence():
    """口径错误必须留空，不得把 -1..1 的方向分塞进 0-100 的 confidence。"""
    conn = _fresh_db()
    did = decision_writer.write_decision(
        conn, asset="BTC", intended_action="BUY", source=decision_writer.SOURCE_ALGO_MOVE,
        score=0.42, confidence=None)
    row = _row(conn, did)
    assert row["score"] == 0.42
    assert row["confidence"] is None


def test_bridge_score_and_confidence_land_in_columns():
    conn = _fresh_db()
    did = decision_writer.write_decision(
        conn, asset="ETH", intended_action="SELL", source=decision_writer.SOURCE_BRIDGE_API,
        score=-0.3, confidence=61.5, context={"rationale": "r"})
    row = _row(conn, did)
    assert row["score"] == -0.3
    assert row["confidence"] == 61.5
    assert "rationale" in (row["context_json"] or "")   # 老读者仍可读 context


def test_log_decision_keeps_signature_and_returns_id():
    conn = _fresh_db()
    did = trade_decisions.log_decision(conn, "SOL", "BUY", "BUY", "ENTRY")
    assert isinstance(did, int) and did > 0
    assert _row(conn, did)["asset"] == "SOL"


def test_entry_price_falls_back_to_null_without_market_data():
    conn = _fresh_db()          # 没有任何行情表 → 取不到价
    did = decision_writer.write_decision(conn, asset="BTC", intended_action="BUY")
    assert _row(conn, did)["entry_price"] is None


def test_values_are_clamped_not_written_raw():
    conn = _fresh_db()
    did = decision_writer.write_decision(
        conn, asset="BTC", intended_action="BUY", score=5.0, confidence=130.0,
        structure_score=-9.0)
    row = _row(conn, did)
    assert row["score"] == 1.0
    assert row["confidence"] == 100.0
    assert row["structure_score"] == -2.0


def test_actions_normalized_to_upper():
    conn = _fresh_db()
    did = decision_writer.write_decision(
        conn, asset="btc", intended_action="buy", final_action="buy")
    row = _row(conn, did)
    assert row["asset"] == "BTC" and row["intended_action"] == "BUY" and row["final_action"] == "BUY"


def test_empty_asset_rejected():
    conn = _fresh_db()
    try:
        decision_writer.write_decision(conn, asset="  ", intended_action="BUY")
    except ValueError as exc:
        assert "asset" in str(exc)
    else:
        raise AssertionError("空 asset 必须被拒绝")
