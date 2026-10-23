"""Tests for v230+ P0.5 quick_sim.cut_bleeding_trades."""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
from quick_sim import cut_bleeding_trades  # noqa: E402

_DID = [10000]


def _next_did():
    _DID[0] += 1
    return _DID[0]


def _price_fn(p):
    return lambda a: p.get(a)


def _setup_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = OFF")
    db.migrate(c)
    return c


def _insert_trade(c, *, action="BUY", asset="BTC", entry_price=100.0,
                  notional_usdt=100.0, decision_id=None):
    if decision_id is None:
        decision_id = _next_did()
    cur = c.execute(
        """INSERT INTO quick_sim_trades
           (decision_id, asset, action, score, gate_passed, gate_reason,
            notional_usdt, entry_price, entry_ts, horizon_minutes, settled)
           VALUES (?, ?, ?, 0.1, 1, 'test', ?, ?, ?, 20, 0)""",
        (decision_id, asset, action, notional_usdt, entry_price, int(time.time())),
    )
    c.commit()
    return int(cur.lastrowid)


def _insert_decision(c, did):
    # Insert parent raw_news first (ai_decisions.news_id is FK)
    cur = c.execute(
        """INSERT INTO raw_news
           (source, content, timestamp, published_at)
           VALUES ('test', 'test content', datetime('now'), '')""",
    )
    news_id = cur.lastrowid
    c.execute(
        """INSERT INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, reasoning,
            target_asset, evidence_confidence, event_strength,
            event_phase, evidence_action)
           VALUES (?, ?, 0.0, 'BUY', '', 'BTC', 0.5, 'medium',
                   'now', 'HOLD')""",
        (did, news_id),
    )
    c.commit()


def _settled(c, tid):
    return c.execute("SELECT settled FROM quick_sim_trades WHERE id=?",
                     (tid,)).fetchone()["settled"]


def test_cut_disabled_when_threshold_none():
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="SELL")
    assert cut_bleeding_trades(c, _price_fn({"BTC": 105.0}), max_loss_pct=None) == []
    assert _settled(c, tid) == 0


def test_cut_disabled_when_threshold_zero():
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="BUY")
    assert cut_bleeding_trades(c, _price_fn({"BTC": 80.0}), max_loss_pct=0.0) == []
    assert _settled(c, tid) == 0


def test_cut_no_action_below_threshold():
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="BUY")
    assert cut_bleeding_trades(c, _price_fn({"BTC": 99.9}), max_loss_pct=0.3) == []
    assert _settled(c, tid) == 0


def test_cut_no_action_at_exact_threshold():
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="SELL")
    assert cut_bleeding_trades(c, _price_fn({"BTC": 100.3}), max_loss_pct=0.3) == []
    assert _settled(c, tid) == 0


def test_cut_buy_above_threshold():
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="BUY", entry_price=100.0)
    cut = cut_bleeding_trades(c, _price_fn({"BTC": 99.0}), max_loss_pct=0.3)
    assert len(cut) == 1
    assert cut[0]["id"] == tid
    assert cut[0]["action"] == "BUY"
    assert cut[0]["pnl_pct"] == pytest.approx(-1.0, abs=1e-3)
    assert cut[0]["loss_pct"] == pytest.approx(1.0, abs=1e-3)
    row = c.execute("SELECT * FROM quick_sim_trades WHERE id=?", (tid,)).fetchone()
    assert row["settled"] == 1
    assert row["verdict"] == "LOSS"
    assert row["exit_price"] == pytest.approx(99.0)
    dec = c.execute("SELECT * FROM ai_decisions WHERE id=?", (did,)).fetchone()
    assert dec["settled"] == 1 and dec["is_correct"] == "LOSS"


def test_cut_sell_above_threshold():
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="SELL", entry_price=100.0,
                        notional_usdt=200.0)
    cut = cut_bleeding_trades(c, _price_fn({"BTC": 100.5}), max_loss_pct=0.3)
    assert len(cut) == 1
    assert cut[0]["action"] == "SELL"
    assert cut[0]["pnl_pct"] == pytest.approx(-0.5, abs=1e-3)
    assert cut[0]["pnl_usdt"] == pytest.approx(-1.0, abs=1e-3)
    assert cut[0]["loss_pct"] == pytest.approx(0.5, abs=1e-3)
    assert _settled(c, tid) == 1


def test_cut_skips_when_price_unavailable():
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="BUY")
    assert cut_bleeding_trades(c, _price_fn({}), max_loss_pct=0.3) == []
    assert _settled(c, tid) == 0


def test_cut_handles_no_open_trades():
    c = _setup_db()
    assert cut_bleeding_trades(c, _price_fn({"BTC": 100.0}), max_loss_pct=0.3) == []


def test_cut_only_above_threshold():
    c = _setup_db()
    d_ok = _next_did(); d_bb = _next_did(); d_bs = _next_did()
    for d in (d_ok, d_bb, d_bs):
        _insert_decision(c, d)
    # BTC BUY entry 100, current 100 -> 0% loss (below)
    _insert_trade(c, decision_id=d_ok, action="BUY", asset="BTC",
                  entry_price=100.0)
    # XAU BUY entry 100, current 98.5 -> 1.5% loss (above)
    _insert_trade(c, decision_id=d_bb, action="BUY", asset="XAU",
                  entry_price=100.0)
    # ETH SELL entry 100, current 100.5 -> 0.5% loss (above)
    _insert_trade(c, decision_id=d_bs, action="SELL", asset="ETH",
                  entry_price=100.0)
    # all assets not priced -> nothing
    assert cut_bleeding_trades(c, _price_fn({}), max_loss_pct=0.3) == []
    # partial pricing: only BTC priced = 100 (no loss)
    cut = cut_bleeding_trades(c, _price_fn({"BTC": 100.0}), max_loss_pct=0.3)
    assert cut == []
    # full pricing: BTC=100 (loss 0), XAU=98.5 (loss 1.5), ETH=100.5 (loss 0.5)
    cut = cut_bleeding_trades(c, _price_fn({"BTC": 100.0, "XAU": 98.5,
                                            "ETH": 100.5}), max_loss_pct=0.3)
    assert sorted(c0["action"] for c0 in cut) == ["BUY", "SELL"]
    rows = c.execute("SELECT verdict FROM quick_sim_trades WHERE settled=1").fetchall()
    assert all(r["verdict"] == "LOSS" for r in rows)


def test_cut_handles_no_decision_id():
    c = _setup_db()
    tid = _insert_trade(c, decision_id=None, action="BUY")
    cut = cut_bleeding_trades(c, _price_fn({"BTC": 98.0}), max_loss_pct=0.3)
    assert len(cut) == 1
    assert _settled(c, tid) == 1


def test_cut_mae_uses_stored_min_price_not_current():
    """v230.2 P0.2 平行修：cut_bleeding_trades 的 mae_pct 应取 update_open_extremes
    已跟踪的 min_price 极值，而非只看 cur_price。

    触发条件仍看 cur_price（与"早砍"语义一致：价反弹就不再砍），
    但写库时 mae_pct 必须取本轮内最深的 min_price。

    场景：BTC BUY entry=100，cur_price=99.5（cur loss=-0.5%，过 0.3 阈值触发 cut），
    update_open_extremes 已跟踪到 min_price=99（最深 -1%）。
    mae_pct 应为 (entry - min_price) / entry = 1.0%，而非 (entry - cur_price) / entry = 0.5%。
    """
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="BUY", entry_price=100.0)
    # 模拟 update_open_extremes 已写入 max_price=100.2, min_price=99.0（最深 -1%）
    c.execute(
        "UPDATE quick_sim_trades SET max_price=?, min_price=? WHERE id=?",
        (100.2, 99.0, tid),
    )
    c.commit()
    # 当前价 99.5：cur loss=-0.5%（过 0.3 阈值触发 cut）
    cut = cut_bleeding_trades(c, _price_fn({"BTC": 99.5}), max_loss_pct=0.3)
    assert len(cut) == 1
    row = c.execute(
        "SELECT mae_pct, pnl_pct, min_price, max_price FROM quick_sim_trades WHERE id=?",
        (tid,),
    ).fetchone()
    # pnl_pct 仍基于 cur_price（实际离场价）
    assert row["pnl_pct"] == pytest.approx(-0.5, abs=1e-3)
    # mae_pct 必须基于 min_price=99（最坏 -1%），不是 cur_price=99.5
    assert row["mae_pct"] == pytest.approx(1.0, abs=1e-3), (
        f"MAE 应来自 min_price=99 而非 cur_price=99.5；got {row['mae_pct']}"
    )
    assert row["min_price"] == pytest.approx(99.0, abs=1e-3), "min_price 应保留极值"
    # ai_decisions 也应同步写入 final_min=99
    dec = c.execute(
        "SELECT min_price, mae_pct FROM ai_decisions WHERE id=?", (did,),
    ).fetchone()
    assert dec["min_price"] == pytest.approx(99.0, abs=1e-3)
    assert dec["mae_pct"] == pytest.approx(1.0, abs=1e-3)


def test_cut_mae_uses_stored_max_price_sell():
    """v230.2 P0.2 SELL 方向：cut 的 MAE 应来自 max_price 而非 cur_price。"""
    c = _setup_db()
    did = _next_did(); _insert_decision(c, did)
    tid = _insert_trade(c, decision_id=did, action="SELL", entry_price=100.0)
    # update_open_extremes 跟踪到 max_price=101（最深 -1%）
    c.execute(
        "UPDATE quick_sim_trades SET max_price=?, min_price=? WHERE id=?",
        (101.0, 100.0, tid),
    )
    c.commit()
    # 当前价 100.5：cur loss=(100.5-100)/100=0.5%（过 0.3 阈值触发 cut）
    cut = cut_bleeding_trades(c, _price_fn({"BTC": 100.5}), max_loss_pct=0.3)
    assert len(cut) == 1
    row = c.execute(
        "SELECT mae_pct, pnl_pct, max_price FROM quick_sim_trades WHERE id=?",
        (tid,),
    ).fetchone()
    # pnl_pct 仍基于 cur_price（实际离场价）
    assert row["pnl_pct"] == pytest.approx(-0.5, abs=1e-3)
    # mae_pct 必须基于 max_price=101（最坏 -1%），不是 cur_price=100.5（-0.5%）
    assert row["mae_pct"] == pytest.approx(1.0, abs=1e-3), (
        f"SELL MAE 应来自 max_price=101 而非 cur_price=100.5；got {row['mae_pct']}"
    )
    assert row["max_price"] == pytest.approx(101.0, abs=1e-3)