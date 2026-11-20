# -*- coding: utf-8 -*-
"""v476 策略赛马场单测：只用合成 K 线，不碰生产库、不联网。

覆盖：
  - 时间解析（带 +08:00 / naive UTC）
  - 信号去重（news_id × asset 只保留最早）
  - 固定止损 vs 动态保本/移动止损的行为差异
  - 成本闸“只用决策之前样本”的语义
  - 指标/报告结构
  - 独立 arena 库写入
"""
from __future__ import annotations

import json
import sqlite3
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src_python"))

import variant_arena as va
from variant_arena import Bar, Signal, VariantConfig


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts, o, h, l, c)


def _buy_path() -> list[Bar]:
    # 入场 100；先冲到 100.35（MFE 0.35%），再跌到 99.5（触发固定 SL 0.5%）
    t0 = 1_700_000_000_000
    return [
        _bar(t0, 100.0, 100.1, 99.9, 100.0),
        _bar(t0 + 60_000, 100.0, 100.35, 99.9, 100.1),
        _bar(t0 + 120_000, 100.1, 100.2, 99.5, 99.6),
        _bar(t0 + 180_000, 99.6, 99.9, 99.2, 99.4),
    ]


def _signal(ts: int = 1_700_000_000, asset: str = "BTC") -> Signal:
    return Signal(
        decision_id=1, news_id=100, asset=asset, action="BUY",
        ts=ts, created_at="", horizon_minutes=40,
    )


def test_parse_ts_handles_iso_and_naive_utc():
    assert va.parse_ts("2026-11-19T13:14:42.763+08:00") == 1795065282
    assert va.parse_ts("2026-11-19 08:56:37") == 1795078597
    assert va.parse_ts(1795059397) == 1795059397
    assert va.parse_ts("") == 0


def test_load_signals_dedupes_same_news_asset_keep_earliest():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY, news_id INTEGER, ts INTEGER, created_at TEXT,
            target_asset TEXT, suggested_action TEXT, sentiment_score REAL,
            evidence_confidence REAL, holding_horizon_minutes INTEGER,
            event_strength TEXT, event_phase TEXT, decision_kind TEXT,
            analysis_degraded INTEGER, quarantined_at TEXT
        )"""
    )
    rows = [
        (1, 10, 100, "", "BTC", "BUY", 0.5, 0.4, None, "high", "early", "analysis", 0, None),
        (2, 10, 101, "", "BTC", "BUY", 0.5, 0.4, None, "high", "early", "analysis", 0, None),  # dup
        (3, 11, 102, "", "BTC", "SELL", 0.5, 0.4, None, "low", "late", "analysis", 0, None),
        (4, 12, 103, "", "NONE", "BUY", 0.5, 0.4, None, "low", "", "analysis", 0, None),  # 无标的
        (5, 13, 104, "", "XAU", "BUY", 0.5, 0.4, None, "medium", "mid", "analysis", 1, None),  # 降级
    ]
    conn.executemany("INSERT INTO ai_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    sigs = va.load_signals(conn, 0, 200)
    assert [s.decision_id for s in sigs] == [1, 3]
    assert sigs[0].horizon_minutes == 120  # high + early = 60×2
    assert sigs[1].horizon_minutes == 20   # low + late = 20×0.5 → clamp 20


def test_dynamic_exit_beats_fixed_on_reversal():
    bars = _buy_path()
    sig = _signal()
    fixed = va.simulate_trade(sig, bars, VariantConfig(name="baseline", exit_mode="fixed"))
    dyn = va.simulate_trade(sig, bars, VariantConfig(name="dynamic", exit_mode="dynamic"))

    assert fixed["status"] == "taken"
    assert dyn["status"] == "taken"
    # 固定 SL 在 99.5 被打掉；动态应把止损抬到成本后保本位并更早离场
    assert fixed["gross_pnl_pct"] <= -0.49
    assert dyn["gross_pnl_pct"] > fixed["gross_pnl_pct"]
    assert dyn["exit_reason"] in ("BE", "TRAIL")
    assert dyn["net_pnl_pct"] > fixed["net_pnl_pct"]


def test_cost_gate_skips_until_enough_prior_samples():
    bars = _buy_path()
    sig = _signal()
    cfg = VariantConfig(name="gate", cost_gate=True, cost_gate_mode="strict", min_samples=2)
    # 样本不足 → 跳过
    r = va.simulate_trade(sig, bars, cfg, prior_mfe=[])
    assert r["status"] == "skipped" and r["skip_reason"] == "gate_unverified"
    # 样本够但 MFE 中位数低于 1.5×成本（BTC 成本 30bps → 需要 0.45%）
    r = va.simulate_trade(sig, bars, cfg, prior_mfe=[0.1, 0.2])
    assert r["status"] == "skipped" and r["skip_reason"] == "gate_cost"
    # 样本够且 MFE 够 → 放行
    r = va.simulate_trade(sig, bars, cfg, prior_mfe=[0.5, 0.6])
    assert r["status"] == "taken"


def test_compute_metrics_and_report_shape():
    records = [
        {"status": "taken", "entry_ts": 1, "decision_id": 1, "asset": "BTC", "action": "BUY",
         "net_pnl_pct": 0.5, "gross_pnl_pct": 0.8, "cost_bps": 30, "mfe_pct": 1.0,
         "mae_pct": 0.2, "hold_minutes": 20, "entry_price": 100.0, "exit_price": 100.8,
         "exit_reason": "HORIZON"},
        {"status": "taken", "entry_ts": 2, "decision_id": 2, "asset": "XAU", "action": "SELL",
         "net_pnl_pct": -0.3, "gross_pnl_pct": 0.0, "cost_bps": 30, "mfe_pct": 0.4,
         "mae_pct": 0.5, "hold_minutes": 40, "entry_price": 100.0, "exit_price": 100.3,
         "exit_reason": "SL"},
        {"status": "skipped", "skip_reason": "gate_cost", "decision_id": 3, "asset": "WTI", "action": "BUY"},
    ]
    m = va.compute_metrics(records, VariantConfig(name="x"))
    assert m["n_taken"] == 2 and m["n_skipped"] == 1
    assert m["win_rate"] == 0.5
    assert m["net_total_pct"] == 0.2
    assert m["max_drawdown_pct"] >= 0
    assert m["sample_status"] == "insufficient"
    assert m["eligible"] is False

    report = va.build_report(
        run_id="arena_test", db_path=":memory:", since_ts=0, until_ts=0,
        signals=[_signal()], variants=[{"config": {"name": "x"}, "metrics": m, "records": records}],
    )
    assert report["n_signals"] == 1
    assert "样本不足" in report["verdict"]
    md = va.report_markdown(report)
    assert "策略版本赛马场" in md
    assert "| x |" in md


def test_write_arena_db_isolated(tmp_path: Path):
    report = {
        "run_id": "arena_test",
        "generated_at": "2026-11-19T16:00:00+08:00",
        "db_path": ":memory:",
        "window": {"since": "2026-11-19T13:00:00+08:00", "until": "2026-11-19T16:00:00+08:00"},
        "n_signals": 1,
        "signals_by_asset": {"BTC": 1},
        "signals_by_action": {"BUY": 1},
        "variants": [{
            "config": {"name": "baseline"},
            "metrics": {
                "n_signals": 1, "n_taken": 1, "n_skipped": 0, "n_no_price": 0,
                "win_rate": 1.0, "gross_avg_pct": 0.8, "cost_avg_bps": 30,
                "net_avg_pct": 0.5, "net_total_pct": 0.5, "avg_mfe_pct": 1.0,
                "mfe_capture": 0.5, "max_drawdown_pct": 0, "t_stat": None,
                "sample_status": "insufficient", "eligible": False, "reasons": ["样本不足"],
            },
            "records": [{
                "decision_id": 1, "news_id": 100, "asset": "BTC", "action": "BUY",
                "status": "taken", "skip_reason": None, "entry_ts": 1,
                "entry_price": 100.0, "exit_ts": 61, "exit_price": 100.8,
                "hold_minutes": 1, "gross_pnl_pct": 0.8, "cost_bps": 30,
                "net_pnl_pct": 0.5, "mfe_pct": 1.0, "mae_pct": 0.2,
                "exit_reason": "HORIZON",
            }],
        }],
        "ranking": ["baseline"],
        "best_by_net_total": "baseline",
        "eligible_for_promotion": [],
        "verdict": "样本不足，全部变体只作观察，不允许晋升",
    }
    db = tmp_path / "arena.db"
    va.write_arena_db(db, report)
    conn = sqlite3.connect(db)
    assert conn.execute("select count(*) from arena_runs").fetchone()[0] == 1
    assert conn.execute("select count(*) from arena_variants").fetchone()[0] == 1
    assert conn.execute("select count(*) from arena_trades").fetchone()[0] == 1
    conn.close()


def test_prior_mfe_uses_exit_ts_not_entry_ts():
    """一笔还没走完的交易，不能拿它的未来 MFE 决定当下是否开火。"""
    history = {}
    va._mfe_history_add(history, {
        "status": "taken", "asset": "XAU", "horizon_minutes": 40,
        "entry_ts": 100, "exit_ts": 200, "mfe_pct": 0.2,
    })
    sig_before = va.Signal(decision_id=1, news_id=1, asset="XAU", action="BUY", ts=150, created_at="")
    sig_after = va.Signal(decision_id=2, news_id=2, asset="XAU", action="BUY", ts=250, created_at="")
    assert va._prior_mfe(history, sig_before) == []
    assert va._prior_mfe(history, sig_after) == [0.2]
