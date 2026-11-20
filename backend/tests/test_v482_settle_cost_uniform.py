"""v482 反思三修：结算口径统一（止损/止盈/砍仓也必须扣成本并留毛值）。

背景（2026-11-19 生产实测）：
    8 条已结算纸上单里 4 条 cost_bps=0、gross_pnl_pct=NULL —— 全是止损/止盈路径。
    同一张 quick_sim_trades 里一半净收益、一半毛收益，反思层又直接拿它当"判断对错"
    的真值 → 标签不可比。

验收：
  1) 三条路径（SL / TP2 / cut）写入的 cost_bps = round_trip_cost_bps(asset)；
  2) gross_pnl_pct − pnl_pct == cost_bps/100（净值口径）；
  3) verdict 跟净收益符号走；
  4) 到期结算（settle_trades）口径不变，仍是净。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import config
import db
from cost_model import apply_costs, round_trip_cost_bps
from quick_sim import check_tp_sl, cut_bleeding_trades


def _setup_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.migrate(c)
    return c


def _insert_news(c):
    return c.execute(
        """INSERT INTO raw_news (source, content, timestamp, published_at)
           VALUES ('test', 'v482 cost', datetime('now'), '')"""
    ).lastrowid


def _insert_decision(c, did, action="BUY", asset="ETH"):
    c.execute(
        """INSERT INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, reasoning,
            target_asset, evidence_confidence, event_strength,
            event_phase, evidence_action, created_at)
           VALUES (?, ?, 0.0, ?, '', ?, 0.5, 'medium', 'mid', 'HOLD',
                   datetime('now', 'localtime'))""",
        (did, _insert_news(c), action, asset),
    )


def _open_trade(c, tid, decision_id, action="BUY", asset="ETH",
                entry_price=100.0, notional=100.0, horizon=20):
    c.execute(
        """INSERT INTO quick_sim_trades
           (id, decision_id, asset, action, entry_price, notional_usdt,
            entry_ts, horizon_minutes, settled)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now','-1 minutes'), ?, 0)""",
        (tid, decision_id, asset, action, entry_price, notional, horizon),
    )


def _row(c, tid):
    return dict(c.execute(
        "SELECT * FROM quick_sim_trades WHERE id=?", (tid,)).fetchone())


def _set_tp_sl(c, tp_enabled=1):
    c.execute(
        """UPDATE paper_trading_settings
           SET tp_enabled=?, tp1_mult=0.6, tp2_mult=1.0,
               tp1_ratio=0.5, tp2_ratio=0.3, sl_mult=0.5
           WHERE id=1""",
        (tp_enabled,),
    )
    c.commit()


@pytest.fixture(autouse=True)
def _breakeven_off(monkeypatch):
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", False)


def test_sl_path_writes_cost_and_gross():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, 9001)
    _open_trade(c, 1, 9001)
    hits = check_tp_sl(c, lambda a: 99.0)
    assert len(hits) == 1 and hits[0]["trigger"] == "SL"

    r = _row(c, 1)
    bps = float(round_trip_cost_bps("ETH"))
    assert r["cost_bps"] == pytest.approx(bps, abs=1e-6)
    assert r["gross_pnl_pct"] == pytest.approx(-0.5, abs=1e-3)
    assert r["pnl_pct"] == pytest.approx(float(apply_costs(-0.5, "ETH", "BUY")), abs=1e-3)
    assert r["gross_pnl_pct"] - r["pnl_pct"] == pytest.approx(bps / 100.0, abs=1e-3)
    assert r["verdict"] == "LOSS"
    # 决策侧同口径
    d = c.execute("SELECT forward_pnl FROM ai_decisions WHERE id=9001").fetchone()
    assert d["forward_pnl"] == pytest.approx(r["pnl_pct"], abs=1e-4)


def test_tp2_path_writes_cost_and_gross():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, 9002)
    _open_trade(c, 1, 9002)
    hits = check_tp_sl(c, lambda a: 101.5)
    assert len(hits) == 1 and hits[0]["trigger"] == "TP2"

    r = _row(c, 1)
    bps = float(round_trip_cost_bps("ETH"))
    assert r["cost_bps"] == pytest.approx(bps, abs=1e-6)
    assert r["gross_pnl_pct"] == pytest.approx(0.6, abs=1e-3)
    assert r["pnl_pct"] == pytest.approx(float(apply_costs(0.6, "ETH", "BUY")), abs=1e-3)
    assert r["gross_pnl_pct"] - r["pnl_pct"] == pytest.approx(bps / 100.0, abs=1e-3)


def test_cut_path_writes_cost_and_gross():
    """强制砍仓同样要扣成本 —— 这是生产库里另一半 cost_bps=0 的来源。"""
    c = _setup_db()
    _insert_decision(c, 9003, action="BUY", asset="BTC")
    _open_trade(c, 1, 9003, action="BUY", asset="BTC", entry_price=100.0,
                notional=200.0)
    cut = cut_bleeding_trades(c, lambda a: 99.0, max_loss_pct=0.3)
    assert len(cut) == 1

    r = _row(c, 1)
    bps = float(round_trip_cost_bps("BTC"))
    assert r["cost_bps"] == pytest.approx(bps, abs=1e-6)
    assert r["gross_pnl_pct"] == pytest.approx(-1.0, abs=1e-3)
    assert r["pnl_pct"] == pytest.approx(float(apply_costs(-1.0, "BTC", "BUY")), abs=1e-3)
    assert r["gross_pnl_pct"] - r["pnl_pct"] == pytest.approx(bps / 100.0, abs=1e-3)
    assert r["pnl_usdt"] == pytest.approx(200.0 * r["pnl_pct"] / 100.0, abs=1e-3)
    # 返回体也要带毛值，否则调用方仍只看到净
    assert cut[0]["gross_pnl_pct"] == pytest.approx(-1.0, abs=1e-3)
    assert cut[0]["cost_bps"] == pytest.approx(bps, abs=1e-6)


def test_xau_cost_is_16bps_not_30():
    """黄金成本与加密不同，不能用同一档 30bp 近似（生产实测 16bp）。"""
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, 9004, asset="XAU")
    _open_trade(c, 1, 9004, asset="XAU")
    check_tp_sl(c, lambda a: 99.0)
    r = _row(c, 1)
    assert r["cost_bps"] == pytest.approx(float(round_trip_cost_bps("XAU")), abs=1e-6)
    assert r["cost_bps"] < 30.0
