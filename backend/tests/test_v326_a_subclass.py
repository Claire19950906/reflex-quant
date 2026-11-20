#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v326_a_subclass.py — A 类细分 (v326) 的口径测试
====================================================
守三件事:
  1. deny_reason 映射: 三种机制各自落到不同小类, 未知原因返回 None (不许默认桶);
  2. 优先级: E 码 > deny_reason > reason_code > ontology (机器事实优于 LLM 推测);
  3. 同组联动判定: 有同组同向异动 → 市场状态误读; 单资产独走 → 形态没识别。
  · 全部离线 (sqlite3 :memory:), 不碰生产库。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

from review import reflection_dimension_map as rdm
import backfill_reflection_a_subclass as bf


# ── 1. deny_reason → (dimension, subcategory) ────────────────────────────
def test_asset_quota_deny_is_risk_limit():
    got = rdm.classify_deny_reason("asset_BTC")
    assert got == ("A", "risk_limit_blocked", got[2])


def test_asset_quota_deny_any_asset():
    for reason in ("asset_WTI", "asset_SOL", "asset_ETH"):
        assert rdm.classify_deny_reason(reason)[:2] == ("A", "risk_limit_blocked")


def test_chinese_whitelist_reason_is_risk_limit():
    assert rdm.classify_deny_reason("资产 XAU 不在模拟盘允许交易列表")[:2] == \
        ("A", "risk_limit_blocked")


def test_sibling_batch_deny_is_batch_conflict():
    assert rdm.classify_deny_reason("R8:同批次兄弟决策=9>3")[:2] == \
        ("A", "batch_conflict_blocked")
    assert rdm.classify_deny_reason("同批次兄弟决策=4>3")[:2] == \
        ("A", "batch_conflict_blocked")


def test_strength_gate_deny_is_strength():
    assert rdm.classify_deny_reason("high_strength")[:2] == ("A", "strength_gate_blocked")


def test_low_confidence_deny_goes_to_calibration():
    got = rdm.classify_deny_reason("统一置信度 11.2 低于闸门 20.0，仅输出观望结论")
    assert got[:2] == ("C", "calibration_drift")


def test_stale_news_deny_goes_to_signal_delay():
    got = rdm.classify_deny_reason("新闻已发布 50 分钟，超过时效门槛 45 分钟")
    assert got[:2] == ("C", "signal_delay")


def test_direction_conflict_deny_goes_to_b():
    assert rdm.classify_deny_reason("LLM 方向与分数符号矛盾")[:2] == \
        ("B", "news_direction_inverted")


def test_unknown_deny_reason_returns_none():
    """未识别的原因必须是 None, 绝不能默认落进某个桶。"""
    assert rdm.classify_deny_reason("某个未来才有的新规则") is None
    assert rdm.classify_deny_reason("") is None
    assert rdm.classify_deny_reason(None) is None


def test_all_deny_reasons_map_to_valid_subcategory():
    subs = {s for v in rdm.SUBCATEGORIES_BY_DIMENSION.values() for s in v}
    for _pat, (dim, sub), _note in rdm.DENY_REASON_PATTERNS:
        assert dim in ("A", "B", "C")
        assert sub in subs, f"{sub} 不在受控词表"
        assert sub in rdm.SUBCATEGORIES_BY_DIMENSION[dim], f"{sub} 不属于 {dim}"


# ── 2. 优先级 ────────────────────────────────────────────────────────────
def test_error_code_beats_deny_reason():
    assert rdm.resolve_dimension(error_codes=["E009:3"], deny_reason="asset_BTC") == \
        ("A", "psychological_anchor")


def test_deny_reason_beats_reason_code():
    assert rdm.resolve_dimension(reason_code=11, deny_reason="asset_BTC") == \
        ("A", "risk_limit_blocked")


def test_deny_reason_beats_ontology():
    assert rdm.resolve_dimension(layer="filter", deny_reason="high_strength") == \
        ("A", "strength_gate_blocked")


def test_falls_back_to_ontology_without_deny_reason():
    assert rdm.resolve_dimension(layer="filter") == ("A", "market_regime_misread")


def test_classify_row_reads_no_trade_reason():
    row = {"no_trade_reason": "R8:同批次兄弟决策=4>3"}
    assert rdm.classify_reflection_row(row) == ("A", "batch_conflict_blocked")


# ── 3. 同组联动 → 市场状态 vs 单资产形态 ────────────────────────────────
def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE critical_price_moves "
                 "(asset TEXT, ts INTEGER, move_pct REAL)")
    conn.execute("CREATE TABLE causal_chains "
                 "(asset TEXT, enabled INTEGER)")
    return conn


def test_peer_co_move_counts_same_direction_only():
    conn = _mem_db()
    ts = 1_700_000_000
    conn.executemany("INSERT INTO critical_price_moves VALUES (?,?,?)", [
        ("BTC", ts, 2.4), ("ETH", ts + 600, 1.8),   # 同向, 计入
        ("SOL", ts - 900, -3.1),                    # 反向, 不计
        ("XRP", ts, 0.4),                           # 幅度不够, 不计
    ])
    assert bf._peer_co_move_count(conn, "DOGE", ts, 1) == 2
    assert bf._peer_co_move_count(conn, "DOGE", ts, -1) == 1


def test_peer_co_move_ignores_unrelated_asset():
    conn = _mem_db()
    # 不在任何分组里的资产 → 无从比较, 返回 0
    assert bf._peer_co_move_count(conn, "UNKNOWNX", 1_700_000_000, 1) == 0


def test_peer_co_move_zero_sign_returns_zero():
    conn = _mem_db()
    assert bf._peer_co_move_count(conn, "BTC", 1_700_000_000, 0) == 0


def test_classify_one_batch_deny_is_deterministic():
    conn = _mem_db()
    row = {"asset": "SOL", "decision_ts": 0, "pnl_pct": 2.0,
           "deny_reason": "R8:同批次兄弟决策=9>3", "dimension": "A",
           "subcategory": "market_regime_misread"}
    first = bf.classify_one(conn, row)
    second = bf.classify_one(conn, row)
    assert first == second
    assert first[:2] == ("A", "batch_conflict_blocked")
    assert "deny_reason" in first[2]


def test_classify_one_chain_plus_peers_is_regime():
    conn = _mem_db()
    ts = 1_700_000_000
    conn.execute("INSERT INTO causal_chains VALUES ('BTC', 1)")
    conn.executemany("INSERT INTO critical_price_moves VALUES (?,?,?)", [
        ("ETH", ts, 2.5), ("SOL", ts + 300, 3.0),
    ])
    dim, sub, ev = bf.classify_one(conn, {"asset": "BTC", "decision_ts": ts,
                                          "pnl_pct": 2.2, "deny_reason": ""})
    assert (dim, sub) == ("A", "market_regime_misread")
    assert "同组" in ev


def test_classify_one_chain_without_peers_is_pattern():
    conn = _mem_db()
    ts = 1_700_000_000
    conn.execute("INSERT INTO causal_chains VALUES ('BTC', 1)")
    dim, sub, ev = bf.classify_one(conn, {"asset": "BTC", "decision_ts": ts,
                                          "pnl_pct": 1.5, "deny_reason": ""})
    assert (dim, sub) == ("A", "pattern_recognition")
    assert "同组无联动" in ev


def test_classify_one_keeps_original_when_no_evidence():
    """没有 deny_reason 也没有链 → 不乱改 (不为了分布好看去编造)。"""
    conn = _mem_db()
    dim, sub, ev = bf.classify_one(conn, {"asset": "BTC", "decision_ts": 0,
                                          "pnl_pct": 1.0, "deny_reason": "",
                                          "dimension": "A",
                                          "subcategory": "psychological_anchor"})
    assert (dim, sub) == ("A", "psychological_anchor")
    assert "维持原判定" in ev


def test_a_subcategories_include_mechanism_buckets():
    """A 类必须同时有认知类与机制类小类, 否则又会塌回一个桶。"""
    a_subs = set(rdm.SUBCATEGORIES_BY_DIMENSION["A"])
    assert {"market_regime_misread", "pattern_recognition",
            "psychological_anchor", "volume_price_divergence"} <= a_subs
    assert {"risk_limit_blocked", "batch_conflict_blocked",
            "strength_gate_blocked"} <= a_subs
    assert len(a_subs) <= 7
