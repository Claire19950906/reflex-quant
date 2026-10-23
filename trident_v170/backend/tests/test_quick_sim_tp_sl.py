"""Tests for v230+ P0.2 quick_sim.check_tp_sl (TP/SL hard exit)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import config  # noqa: E402
import db  # noqa: E402
from quick_sim import check_tp_sl  # noqa: E402


def _setup_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = OFF")
    db.migrate(c)
    return c


def _insert_news(c):
    cur = c.execute(
        """INSERT INTO raw_news (source, content, timestamp, published_at)
           VALUES ('test', 'tp_sl test', datetime('now'), '')"""
    )
    return cur.lastrowid


def _insert_decision(c, *, did, action="BUY", asset="ETH",
                    event_strength="medium"):
    news_id = _insert_news(c)
    c.execute(
        """INSERT INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, reasoning,
            target_asset, evidence_confidence, event_strength,
            event_phase, evidence_action, created_at)
           VALUES (?, ?, 0.0, ?, '', ?, 0.5, ?, 'mid', 'HOLD',
                   datetime('now', 'localtime'))""",
        (did, news_id, action, asset, event_strength),
    )


def _open_trade(c, *, tid, decision_id, action="BUY", asset="ETH",
                entry_price=100.0, notional=100.0,
                max_price=None, min_price=None,
                horizon_minutes=20, sl_price=None):
    c.execute(
        """INSERT INTO quick_sim_trades
           (id, decision_id, asset, action, entry_price, notional_usdt,
            entry_ts, horizon_minutes, settled, max_price, min_price,
            sl_price)
           VALUES (?, ?, ?, ?, ?, ?,
                   datetime('now', '-' || ? || ' minutes'),
                   ?, 0, ?, ?, ?)""",
        (tid, decision_id, asset, action, entry_price, notional,
         1, horizon_minutes, max_price, min_price, sl_price),
    )


def _trade_sl_price(c, tid):
    r = c.execute(
        "SELECT sl_price FROM quick_sim_trades WHERE id=?", (tid,),
    ).fetchone()
    return r["sl_price"] if r else None


def _set_tp_sl(c, *, tp_enabled=1, tp1_mult=0.6, tp2_mult=1.0,
               tp1_ratio=0.5, tp2_ratio=0.3, sl_mult=0.5):
    c.execute(
        """UPDATE paper_trading_settings
           SET tp_enabled=?, tp1_mult=?, tp2_mult=?, tp1_ratio=?, tp2_ratio=?,
               sl_mult=?
           WHERE id=1""",
        (tp_enabled, tp1_mult, tp2_mult, tp1_ratio, tp2_ratio, sl_mult),
    )
    c.commit()


@pytest.fixture
def tp_sl_cfg(monkeypatch):
    saved = {"TP_SL_ENABLED": config.TP_SL_ENABLED}
    yield lambda enabled: monkeypatch.setattr(config, "TP_SL_ENABLED", enabled)
    for k, v in saved.items():
        monkeypatch.setattr(config, k, v)


@pytest.fixture(autouse=True)
def _disable_breakeven_by_default(monkeypatch):
    """v230+ P0.2 part B：breakeven 默认关闭，让原 TP/SL 测试保持原语义。

    只有 `test_breakeven_*` 系列通过显式 monkeypatch 重新打开。
    """
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", False)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0)


def _row(c, tid):
    return dict(c.execute(
        "SELECT * FROM quick_sim_trades WHERE id=?", (tid,)
    ).fetchone())


def _decision(c, did):
    return dict(c.execute(
        "SELECT * FROM ai_decisions WHERE id=?", (did,)
    ).fetchone())


# ──────────────────────── global / per-run kill switches ─────────────────────


def test_global_tp_sl_disabled_returns_empty(tp_sl_cfg):
    tp_sl_cfg(False)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=101, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=101, action="BUY",
                entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 110.0)
    assert hits == []
    assert _row(c, 1)["settled"] == 0


def test_per_run_tp_disabled_returns_empty():
    c = _setup_db()
    _set_tp_sl(c, tp_enabled=0)
    _insert_decision(c, did=102, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=102, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 110.0)
    assert hits == []
    assert _row(c, 1)["settled"] == 0


def test_bad_config_zero_tp1_returns_empty():
    c = _setup_db()
    _set_tp_sl(c, tp1_mult=0.0)
    _insert_decision(c, did=103, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=103, entry_price=100.0)
    assert check_tp_sl(c, lambda a: 110.0) == []


def test_bad_config_sum_ratio_over_1_returns_empty():
    c = _setup_db()
    _set_tp_sl(c, tp1_ratio=0.7, tp2_ratio=0.5)
    _insert_decision(c, did=104, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=104, entry_price=100.0)
    assert check_tp_sl(c, lambda a: 110.0) == []


# ──────────────────────── TP1 partial ────────────────────────


def test_buy_tp1_partial_hit_records_partial():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=200, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=200, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 100.6)
    assert len(hits) == 1
    h = hits[0]
    assert h["trigger"] == "TP1_partial"
    assert h["partial_exit_ratio"] == 0.5
    assert _row(c, 1)["settled"] == 0
    d = _decision(c, 200)
    assert d["partial_exit_ratio"] == 0.5
    assert d["partial_exit_price"] == pytest.approx(100.6, abs=1e-4)
    assert d["partial_pnl_pct"] == pytest.approx(0.3, abs=1e-4)


def test_sell_tp1_partial_hit_records_partial():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=201, action="SELL", asset="ETH")
    _open_trade(c, tid=1, decision_id=201, action="SELL", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 99.4)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "TP1_partial"
    d = _decision(c, 201)
    assert d["partial_pnl_pct"] == pytest.approx(0.3, abs=1e-4)


def test_tp1_partial_idempotent_no_double_record():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=202, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=202, action="BUY", entry_price=100.0)
    hits1 = check_tp_sl(c, lambda a: 100.6)
    assert len(hits1) == 1
    hits2 = check_tp_sl(c, lambda a: 100.6)
    assert hits2 == []
    d = _decision(c, 202)
    assert d["partial_pnl_pct"] == pytest.approx(0.3, abs=1e-4)


def test_tp1_via_max_price_records_partial():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=203, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=203, action="BUY", entry_price=100.0,
                max_price=100.7, min_price=99.8)
    hits = check_tp_sl(c, lambda a: 100.4)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "TP1_partial"


# ──────────────────────── TP2 full close ────────────────────────


def test_buy_tp2_one_step_close_win():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=300, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=300, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 101.5)
    assert len(hits) == 1
    h = hits[0]
    assert h["trigger"] == "TP2"
    # one-step blend = 0.5*0.6 + 0.3*1.0 = 0.6
    assert h["pnl_pct"] == pytest.approx(0.6, abs=1e-3)
    r = _row(c, 1)
    assert r["settled"] == 1
    assert r["verdict"] == "WIN"


def test_buy_tp2_after_partial_blends_pnl():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=301, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=301, action="BUY", entry_price=100.0)
    check_tp_sl(c, lambda a: 100.6)
    d1 = _decision(c, 301)
    assert d1["partial_pnl_pct"] == pytest.approx(0.3, abs=1e-4)
    hits = check_tp_sl(c, lambda a: 101.5)
    assert len(hits) == 1
    # blended = 0.3 (partial) + 0.5 * 1.5 = 1.05
    assert hits[0]["pnl_pct"] == pytest.approx(1.05, abs=1e-3)
    r = _row(c, 1)
    assert r["settled"] == 1
    assert r["verdict"] == "WIN"


def test_sell_tp2_one_step_close_win():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=302, action="SELL", asset="ETH")
    _open_trade(c, tid=1, decision_id=302, action="SELL", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 99.0)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "TP2"
    assert hits[0]["pnl_pct"] == pytest.approx(0.6, abs=1e-3)


# ──────────────────────── SL close ────────────────────────


def test_buy_sl_close():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=400, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=400, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 99.0)
    assert len(hits) == 1
    h = hits[0]
    assert h["trigger"] == "SL"
    assert h["pnl_pct"] == pytest.approx(-0.5, abs=1e-3)
    r = _row(c, 1)
    assert r["settled"] == 1
    assert r["verdict"] == "LOSS"


def test_sell_sl_close():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=401, action="SELL", asset="ETH")
    _open_trade(c, tid=1, decision_id=401, action="SELL", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 101.0)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "SL"
    assert hits[0]["pnl_pct"] == pytest.approx(-0.5, abs=1e-3)


def test_sl_via_min_price_buy():
    """min_price already touched SL even if cur_price bounced back."""
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=402, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=402, action="BUY", entry_price=100.0,
                max_price=100.4, min_price=99.0)
    hits = check_tp_sl(c, lambda a: 100.0)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "SL"


def test_sl_after_partial_blends():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=403, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=403, action="BUY", entry_price=100.0)
    check_tp_sl(c, lambda a: 100.6)
    d = _decision(c, 403)
    partial_pnl = d["partial_pnl_pct"]
    hits = check_tp_sl(c, lambda a: 99.0)
    assert len(hits) == 1
    # blended = partial_pnl + 0.5 * (-0.5) = partial_pnl - 0.25
    assert hits[0]["pnl_pct"] == pytest.approx(partial_pnl - 0.25, abs=1e-3)


# ──────────────────────── no trigger / priority ────────────────────────


def test_no_trigger_returns_open_untouched():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=500, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=500, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 100.3)
    assert hits == []
    assert _row(c, 1)["settled"] == 0


def test_mixed_buy_tp1_and_sell_sl_in_same_cycle():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=600, action="BUY", asset="ETH")
    _insert_decision(c, did=601, action="SELL", asset="ETH")
    _open_trade(c, tid=1, decision_id=600, action="BUY", entry_price=100.0)
    _open_trade(c, tid=2, decision_id=601, action="SELL", entry_price=100.0)
    # Encode SELL SL via stored max_price (price recovered to 100.6).
    c.execute("UPDATE quick_sim_trades SET max_price=101.0 WHERE id=2")
    c.commit()
    hits = check_tp_sl(c, lambda a: 100.6)
    triggers = sorted([h["trigger"] for h in hits])
    assert triggers == ["SL", "TP1_partial"]
    r1 = _row(c, 1)
    r2 = _row(c, 2)
    assert r1["settled"] == 0
    assert r2["settled"] == 1
    assert r2["verdict"] == "LOSS"


# ──────────────────────── pricing / connection failures ──────────────────────


def test_unavailable_price_skips_asset():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=700, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=700, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: None)
    assert hits == []
    assert _row(c, 1)["settled"] == 0


def test_zero_or_negative_price_skipped():
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=701, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=701, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 0.0)
    assert hits == []
    assert _row(c, 1)["settled"] == 0


def test_no_open_trades_returns_empty():
    c = _setup_db()
    _set_tp_sl(c)
    assert check_tp_sl(c, lambda a: 100.0) == []


# ──────────────────────── v230+ P0.2 part B: breakeven-move ────────────────────────


def test_breakeven_disabled_keeps_sl_price_null_after_tp1(monkeypatch):
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", False)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=800, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=800, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 100.6)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "TP1_partial"
    assert hits[0]["breakeven_applied"] is False
    assert _trade_sl_price(c, 1) is None


def test_breakeven_enabled_sets_sl_price_to_entry_after_tp1(monkeypatch):
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=801, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=801, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 100.6)
    assert hits[0]["breakeven_applied"] is True
    assert hits[0]["breakeven_sl_price"] == pytest.approx(100.0, abs=1e-4)
    assert _trade_sl_price(c, 1) == pytest.approx(100.0, abs=1e-4)


def test_breakeven_with_offset_above_entry(monkeypatch):
    """offset=0.01 → 把 SL 推到 entry + 0.01%，锁住微薄保证利润。"""
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.01)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=802, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=802, action="BUY", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 100.6)
    # BUY (sgn=+1) → sl_price = entry × (1 + 0.01/100) = 100.01
    assert hits[0]["breakeven_sl_price"] == pytest.approx(100.01, abs=1e-3)
    assert _trade_sl_price(c, 1) == pytest.approx(100.01, abs=1e-3)


def test_breakeven_uses_override_not_original_sl(monkeypatch):
    """offset=0.05 → SL=100.05。TP1 后 price 跌到 99.8（原 SL=99.5 之上），
    但已被 override 的 breakeven=100.05 触发。

    v230.2 P0.1：sl_price=100.05（breakeven 在 entry 之上），
    sl_pnl_theory = +0.05%；blended = partial_pnl + 0.5*0.05 > 0 → verdict=WIN。
    """
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.05)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=803, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=803, action="BUY", entry_price=100.0)
    check_tp_sl(c, lambda a: 100.6)  # 触发 TP1
    assert _trade_sl_price(c, 1) == pytest.approx(100.05, abs=1e-3)
    # 第二轮：cur_price=99.8，breakeven SL=100.05 → 触发（不用原 SL=99.5）
    hits = check_tp_sl(c, lambda a: 99.8)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "SL"
    assert hits[0]["sl_price"] == pytest.approx(100.05, abs=1e-3)
    r = _row(c, 1)
    assert r["settled"] == 1
    # 修复点：breakeven SL 在 entry 之上时仍赚 → WIN
    assert r["verdict"] == "WIN"


def test_breakeven_sl_fires_at_entry_blends_to_partial_pnl(monkeypatch):
    """TP1 后 price 回到 entry → breakeven SL 触发；blended pnl = partial + 0.5×0。

    v230.2 P0.1 修订：verdict 由 blended pnl 实际符号决定。
    partial > 0 → verdict=WIN（TP1 已锁利，剩余 50% 在 breakeven 平仓仍净赚）。
    """
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=804, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=804, action="BUY", entry_price=100.0)
    check_tp_sl(c, lambda a: 100.6)
    partial = _decision(c, 804)["partial_pnl_pct"]
    assert partial > 0, "TP1 应锁住正利润"
    hits = check_tp_sl(c, lambda a: 100.0)  # 跌回 entry
    assert len(hits) == 1
    assert hits[0]["trigger"] == "SL"
    assert hits[0]["sl_price"] == pytest.approx(100.0, abs=1e-4)
    # 第二段 sl_pnl_theory = 0%（breakeven），blended = partial + 0.5*0 = partial > 0
    assert hits[0]["pnl_pct"] == pytest.approx(partial, abs=1e-3)
    r = _row(c, 1)
    assert r["settled"] == 1
    # 修复点：blended > 0 应判 WIN，而非历史错误写死 LOSS
    assert r["verdict"] == "WIN", (
        f"blended={r['pnl_pct']} > 0 时 verdict 必须是 WIN；"
        f"实际={r['verdict']}（历史 bug：强制 LOSS 导致胜率被压低）"
    )


def test_breakeven_sl_fires_below_entry_still_loss(monkeypatch):
    """TP1 后 price 跌到原 SL 触发（breakeven 关闭）→ blended < 0 → verdict=LOSS。

    v230.2 P0.1：与上面的对称测试 —— 同样 SL 触发，根据 blended 符号分 WIN/LOSS。
    注意：必须关闭 breakeven，否则 SL 会被改写到 entry，blended 不会 < 0。
    直接通过 _open_trade 的 sl_price 参数注入 -2% SL，让触发价位可控。
    """
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", False)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=806, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=806, action="BUY",
                entry_price=100.0, sl_price=98.0)  # -2% SL（不触发 breakeven 改写）
    check_tp_sl(c, lambda a: 100.6)  # TP1 partial
    partial = _decision(c, 806)["partial_pnl_pct"]
    assert partial > 0
    hits = check_tp_sl(c, lambda a: 97.5)  # 跌穿 SL=98 触发
    assert len(hits) == 1
    assert hits[0]["trigger"] == "SL"
    # blended = partial + 0.5 * (98-100)/100*100 = partial - 1.0
    expected = partial - 1.0
    assert hits[0]["pnl_pct"] == pytest.approx(expected, abs=1e-3)
    r = _row(c, 1)
    assert r["settled"] == 1
    assert r["verdict"] == "LOSS"


def test_tp2_path_verdict_uses_blended_sign(monkeypatch):
    """TP2 触发时 verdict 由 blended pnl 符号决定（覆盖旧逻辑的硬编码 WIN）。

    v230.2 P0.1：TP1 partial 后价格冲到 TP2 → blended > 0 → WIN。
    """
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=807, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=807, action="BUY", entry_price=100.0)
    check_tp_sl(c, lambda a: 100.3)  # TP1 partial
    partial = _decision(c, 807)["partial_pnl_pct"]
    # TP2 在 entry×(1+1.0)=102.0；冲到 TP2 → 全平
    hits = check_tp_sl(c, lambda a: 102.0)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "TP2"
    # blended = partial + (1 - partial_ratio) * (102-100)/100*100
    # 用 partial_exit_ratio=0.5（由 TP1 设定）→ blended = partial + 0.5*2.0 = partial + 1.0
    # 实际拿到的 pnl_pct 直接断言 verdict=WIN（不为 0/负数符号之争纠缠）
    r = _row(c, 1)
    assert r["settled"] == 1
    assert r["pnl_pct"] > 0
    assert r["verdict"] == "WIN"


def test_sl_mae_uses_max_min_extremes_not_cur_price(monkeypatch):
    """v230.2 P0.2：SL 路径的 mae_pct 必须用 update_open_extremes 跟踪的 max/min。

    场景：BUY entry=100，盘中最低跌到 97=-3%，然后反弹到 100 触发 breakeven SL。
    旧 bug：mae = abs(sgn*(cur_price-entry)/entry*100) = 0%（把 100 当 reflect）。
    修复后：mae = (entry - min_price) / entry * 100 = (100-97)/100 = 3%。
    """
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=808, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=808, action="BUY", entry_price=100.0)
    check_tp_sl(c, lambda a: 100.6)  # TP1 partial
    # 模拟 update_open_extremes 已记录 min_price=97（极端下跌）
    c.execute(
        "UPDATE quick_sim_trades SET min_price=97.0, max_price=100.6 WHERE id=1"
    )
    c.commit()
    hits = check_tp_sl(c, lambda a: 100.0)  # 反弹回 entry 触发 breakeven SL
    assert len(hits) == 1
    assert hits[0]["trigger"] == "SL"
    r = _row(c, 1)
    # mae 应为 (100-97)/100*100 = 3%（不是 0%）
    assert r["mae_pct"] == pytest.approx(3.0, abs=1e-3), (
        f"mae_pct 应为 3%（反映 min_price=97 的 -3% 回撤），"
        f"实际={r['mae_pct']}（旧 bug：把 cur_price=100 当 reflect 写成 0%）"
    )


def test_tp2_mfe_uses_max_min_extremes_not_cur_price(monkeypatch):
    """v230.2 P0.2：TP2 路径的 mfe_pct 必须用 update_open_extremes 跟踪的 max/min。

    场景：BUY entry=100，默认 tp1_mult=0.6% (TP1=100.6)，tp2_mult=1.0% (TP2=101.0)。
    第一轮 cur=100.6 hit TP1 partial；第二轮手动注入 max_price=103.5（曾探到 +3.5%），
    cur=101.0 触发 TP2 全平。旧 bug：mfe = (101-100)/100 = 1.0%。修复后：3.5%。
    """
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", False)  # 防止 SL 在 entry 先 hit
    c = _setup_db()
    _set_tp_sl(c)  # 默认 tp1_mult=0.6, tp2_mult=1.0
    _insert_decision(c, did=809, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=809, action="BUY", entry_price=100.0)
    check_tp_sl(c, lambda a: 100.6)  # TP1 partial
    # 模拟 update_open_extremes 已记录 max_price=103.5（曾探到更高）
    c.execute(
        "UPDATE quick_sim_trades SET max_price=103.5, min_price=100.0 WHERE id=1"
    )
    c.commit()
    # TP2 在 101.0：ref_max=103.5 触发 TP2 全平（sl 还在 99.5 不会先 hit）
    hits = check_tp_sl(c, lambda a: 101.0)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "TP2"
    r = _row(c, 1)
    # mfe 应为 (103.5-100)/100*100 = 3.5%（不是 (101-100)/100=1.0%）
    assert r["mfe_pct"] == pytest.approx(3.5, abs=1e-3), (
        f"mfe_pct 应为 3.5%（反映 max_price=103.5 的 +3.5% 偏移），"
        f"实际={r['mfe_pct']}（旧 bug：把 cur_price=101 当 reflect 写成 1.0%）"
    )


def test_breakeven_sell_side_symmetric(monkeypatch):
    """SELL：TP1 价跌 → SL 上移到 entry；price 反弹回 entry 触发 SL。"""
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=805, action="SELL", asset="ETH")
    _open_trade(c, tid=1, decision_id=805, action="SELL", entry_price=100.0)
    hits = check_tp_sl(c, lambda a: 99.4)  # 触发 TP1（SELL 的 TP1 在低位）
    assert hits[0]["trigger"] == "TP1_partial"
    assert hits[0]["breakeven_applied"] is True
    # SELL (sgn=-1) → sl_price = entry × (1 + 0/100 × -1) = entry = 100
    assert hits[0]["breakeven_sl_price"] == pytest.approx(100.0, abs=1e-4)
    assert _trade_sl_price(c, 1) == pytest.approx(100.0, abs=1e-4)
    # 第二轮：price 反弹回 100 → SELL 的 SL=100 触发
    hits2 = check_tp_sl(c, lambda a: 100.0)
    assert len(hits2) == 1
    assert hits2[0]["trigger"] == "SL"


def test_breakeven_idempotent_second_tp1_does_not_overwrite_sl_price(monkeypatch):
    """第二轮 TP1 不应覆写 sl_price（已有 override 不会改变）。"""
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0)
    c = _setup_db()
    _set_tp_sl(c)
    _insert_decision(c, did=806, action="BUY", asset="ETH")
    _open_trade(c, tid=1, decision_id=806, action="BUY", entry_price=100.0)
    check_tp_sl(c, lambda a: 100.6)
    assert _trade_sl_price(c, 1) == pytest.approx(100.0, abs=1e-4)
    # 手动改 sl_price 模拟"已被外力调整"——第二轮 TP1 不应改回
    c.execute("UPDATE quick_sim_trades SET sl_price=99.9 WHERE id=1")
    c.commit()
    # 由于 partial_exit_ratio 已 == tp1_ratio，第二轮 TP1 不会触发 partial 写入（幂等）
    hits = check_tp_sl(c, lambda a: 100.65)
    # 没有新 partial，因此 sl_price 保持 99.9
    assert hits == []
    assert _trade_sl_price(c, 1) == pytest.approx(99.9, abs=1e-4)
