"""Tests for v230+ P0.5b quick_sim.gate_sell_signal (建仓前拒签)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import config  # noqa: E402
from quick_sim import gate_sell_signal, open_trades  # noqa: E402


def _setup_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = OFF")
    db.migrate(c)
    return c


def _insert_news(c, source="test"):
    cur = c.execute(
        """INSERT INTO raw_news (source, content, timestamp, published_at)
           VALUES (?, 'gate test', datetime('now'), '')""",
        (source,),
    )
    return cur.lastrowid


def _insert_decision(c, *, did, action="SELL", asset="WTI",
                     event_strength="medium"):
    """v231+: 插入 QUALITY_FILTER-valid 的 signal (默认 WTI/XAU 都过 R4 allowlist).
    evidence_action 跟随 action, confidence 30, trade_gate_reason 通过.
    """
    news_id = _insert_news(c)
    c.execute(
        """INSERT INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, reasoning,
            target_asset, evidence_confidence, trade_gate_reason,
            event_strength, event_phase, evidence_action, status, created_at)
           VALUES (?, ?, 0.0, ?, '', ?, 30.0, ?, ?, 'mid', ?, 'AUTO_APPROVED',
                   datetime('now', 'localtime'))""",
        (did, news_id, action, asset,
         config.TRADE_GATE_PASSED_REASON, event_strength, action),
    )
    c.commit()


@pytest.fixture
def gate_cfg(monkeypatch):
    """Temporarily override gate config (auto-restores on teardown)."""
    saved = {
        "SELL_GATE_ENABLED": config.SELL_GATE_ENABLED,
        "SELL_GATE_BLOCK_HIGH_STRENGTH": config.SELL_GATE_BLOCK_HIGH_STRENGTH,
        "SELL_GATE_BLOCKED_ASSETS": config.SELL_GATE_BLOCKED_ASSETS,
    }

    def _set(enabled=True, block_high=True, blocked="BTC,WTI"):
        monkeypatch.setattr(config, "SELL_GATE_ENABLED", enabled)
        monkeypatch.setattr(config, "SELL_GATE_BLOCK_HIGH_STRENGTH", block_high)
        monkeypatch.setattr(
            config, "SELL_GATE_BLOCKED_ASSETS",
            frozenset(a.strip().upper() for a in blocked.split(",") if a.strip()),
        )

    yield _set
    for k, v in saved.items():
        monkeypatch.setattr(config, k, v)


# ─────────────────────── gate_sell_signal unit tests ────────────────────────


def test_gate_disabled_lets_everything_through(gate_cfg):
    gate_cfg(enabled=False)
    ok, reason = gate_sell_signal({"action": "SELL", "asset": "BTC",
                                    "event_strength": "high"})
    assert ok is True
    assert reason == "disabled"


def test_gate_buy_always_passes(gate_cfg):
    ok, reason = gate_sell_signal({"action": "BUY", "asset": "BTC",
                                    "event_strength": "high"})
    assert ok is True
    assert reason == "buy_passthrough"


def test_gate_blocks_high_strength_sell(gate_cfg):
    ok, reason = gate_sell_signal({"action": "SELL", "asset": "ETH",
                                    "event_strength": "high"})
    assert ok is False
    assert reason == "high_strength"


def test_gate_blocks_btc_sell(gate_cfg):
    ok, reason = gate_sell_signal({"action": "SELL", "asset": "BTC",
                                    "event_strength": "medium"})
    assert ok is False
    assert reason == "asset_BTC"


def test_gate_blocks_wti_sell(gate_cfg):
    ok, reason = gate_sell_signal({"action": "SELL", "asset": "WTI",
                                    "event_strength": "medium"})
    assert ok is False
    assert reason == "asset_WTI"


def test_gate_lets_medium_xau_sell_through(gate_cfg):
    ok, reason = gate_sell_signal({"action": "SELL", "asset": "XAU",
                                    "event_strength": "medium"})
    assert ok is True
    assert reason == "passed"


def test_gate_case_insensitive(gate_cfg):
    ok, reason = gate_sell_signal({"action": "sell", "asset": "btc",
                                    "event_strength": "high"})
    assert ok is False
    assert reason in ("high_strength", "asset_BTC")


def test_gate_high_strength_check_can_be_disabled(gate_cfg):
    gate_cfg(block_high=False)
    ok, reason = gate_sell_signal({"action": "SELL", "asset": "ETH",
                                    "event_strength": "high"})
    assert ok is True
    assert reason == "passed"


def test_gate_custom_blocked_assets(gate_cfg):
    gate_cfg(blocked="ETH,SOL")
    ok, reason = gate_sell_signal({"action": "SELL", "asset": "ETH",
                                    "event_strength": "medium"})
    assert ok is False
    assert reason == "asset_ETH"
    ok, reason = gate_sell_signal({"action": "SELL", "asset": "BTC",
                                    "event_strength": "medium"})
    assert ok is True
    assert reason == "passed"


# ─────────────────────── open_trades integration ────────────────────────────


def test_open_trades_blocks_bad_sell_via_gate(gate_cfg, capsys):
    c = _setup_db()
    # v231+: 改用 WTI/XAU (都在 SIM_ALLOWED_ASSETS), 行为对应: WTI SELL 被 SELL_GATE 拦
    _insert_decision(c, did=20001, action="SELL", asset="WTI",
                     event_strength="medium")
    _insert_decision(c, did=20002, action="SELL", asset="XAU",
                     event_strength="medium")
    created = open_trades(c, lambda a: 100.0)
    # only XAU SELL should be opened (WTI blocked by SELL_GATE)
    assert len(created) == 1
    assert created[0]["asset"] == "XAU"
    rows = c.execute("SELECT asset FROM quick_sim_trades").fetchall()
    assert [r["asset"] for r in rows] == ["XAU"]
    captured = capsys.readouterr()
    assert "[SELL_GATE]" in captured.out


def test_open_trades_high_strength_sell_blocked(gate_cfg):
    c = _setup_db()
    # v231+: 用 XAU (在 allowlist) 测 high strength SELL block
    _insert_decision(c, did=20010, action="SELL", asset="XAU",
                     event_strength="high")
    _insert_decision(c, did=20011, action="BUY",  asset="XAU",
                     event_strength="high")
    created = open_trades(c, lambda a: 100.0)
    # only BUY should pass (high strength SELL gated)
    assert len(created) == 1
    assert created[0]["action"] == "BUY"


def test_open_trades_gate_disabled_allows_all(gate_cfg):
    gate_cfg(enabled=False)
    c = _setup_db()
    # v231+: WTI/XAU 都在 allowlist
    _insert_decision(c, did=20020, action="SELL", asset="WTI",
                     event_strength="high")
    _insert_decision(c, did=20021, action="SELL", asset="XAU",
                     event_strength="low")
    created = open_trades(c, lambda a: 100.0)
    # gate off → both opened
    assert len(created) == 2
    rows = c.execute(
        "SELECT asset FROM quick_sim_trades ORDER BY id"
    ).fetchall()
    # pending_signals returns ORDER BY ad.id DESC → 20021 (XAU) before 20020 (WTI)
    assert sorted(r["asset"] for r in rows) == ["WTI", "XAU"]


