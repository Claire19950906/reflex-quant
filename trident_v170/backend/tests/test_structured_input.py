"""Tests for structured_input + structured_check bypass modules.

These tests are read-only: never touch ai_worker / reflection / review_agent.
Goal: verify (1) payload shape, (2) token budget, (3) check logic, (4) zero regression.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_BACKEND, "src_python"))

from structured_input import (
    build_structured_input,
    serialize,
    estimate_tokens,
    _events_block,
    _snapshot_block,
    _baseline_block,
    _meta_block,
    _text_summary,
    _relations_block,
)
from structured_check import (
    check_reasoning_vs_structured_input,
    serialize_check,
)


class FakeRow:
    def __init__(self, d):
        self._d = d
    def keys(self):
        return self._d.keys()
    def __getitem__(self, k):
        return self._d[k]


def test_build_structured_input_has_7_blocks():
    # v200+: 加了 microstructure block, 现在是 7 块
    news = FakeRow({"id": 1, "source": "jin10", "text": "美联储加息25bp", "published_at": 1758328800})
    p = build_structured_input(news)
    assert isinstance(p, dict)
    assert set(p.keys()) == {"meta", "events", "snapshot", "relations", "baseline", "microstructure", "text_summary"}
    assert p["relations"] == []
    assert isinstance(p["events"], list)
    assert isinstance(p["snapshot"], list)
    assert isinstance(p["baseline"], dict)
    assert isinstance(p["microstructure"], dict)
    assert isinstance(p["text_summary"], str)
    # microstructure 块应有 4 个键：lsr/funding/stale_symbols/source
    assert "lsr" in p["microstructure"]
    assert "funding" in p["microstructure"]
    assert "stale_symbols" in p["microstructure"]
    assert "source" in p["microstructure"]


def test_meta_block_extracts_news_metadata():
    news = FakeRow({"id": 42, "source": "fxstreet", "text": "x" * 1000, "published_at": 1758000000})
    meta = _meta_block(news)
    assert meta["news_id"] == 42
    assert meta["source"] == "fxstreet"
    assert meta["text_len"] == 1000
    assert len(meta["text_hash"]) == 10
    assert isinstance(meta["freshness_min"], int)
    assert meta["freshness_min"] > 0


def test_meta_block_handles_bad_row():
    news = FakeRow({})
    meta = _meta_block(news)
    assert meta["news_id"] == 0
    assert meta["source"] == ""


def test_serialize_truncates_long_payload():
    p = build_structured_input(FakeRow({"id": 1, "text": "x" * 10000}))
    p["text_summary"] = "x" * 10000
    s = serialize(p)
    assert len(s) <= 6000


def test_estimate_tokens_within_budget():
    p = build_structured_input(FakeRow({"id": 1, "text": "美联储宣布加息25个基点,黄金承压"}))
    t = estimate_tokens(p)
    assert 50 <= t <= 1500, f"unexpected token count: {t}"


def test_events_block_uses_event_atom_first():
    fake_atom = [FakeRow({
        "id": 7, "event_type_code": "rate_hike", "type_name_zh": "加息",
        "actor": "美联储", "event_time": 1758328800,
        "magnitude_value": 25, "magnitude_unit": "bp",
        "asset_code": "XAU,BTC", "direction": "bearish",
        "direction_confidence": 0.85, "direction_source": "rule_bias",
        "evidence_count": 1,
    })]
    out = _events_block(None, fake_atom)
    assert len(out) == 1
    assert out[0]["type_code"] == "rate_hike"
    assert out[0]["actor"] == "美联储"
    assert out[0]["assets"] == ["XAU", "BTC"]
    assert out[0]["source"] == "event_atom"


def test_events_block_falls_back_to_event_struct():
    struct_events = [{
        "type_code": "geopolitical",
        "event_type": "geopolitical",
        "actor": "Russia",
        "event_time": 0,
        "magnitude_value": None,
        "magnitude_unit": "",
        "target_assets": ["XAU"],
        "direction": "bullish",
        "direction_confidence": 0.6,
        "direction_source": "rule_bias",
    }]
    out = _events_block(struct_events, None)
    assert len(out) == 1
    assert out[0]["source"] == "event_struct"
    assert out[0]["assets"] == ["XAU"]


def test_events_block_empty_when_no_input():
    assert _events_block(None, None) == []
    assert _events_block([], []) == []


def test_snapshot_block_empty_when_no_market_data():
    assert _snapshot_block(None) == []
    assert _snapshot_block({}) == []
    assert _snapshot_block({"assets": {}}) == []


def test_snapshot_block_handles_unavailable_asset():
    m = {"assets": {"BTC": {"status": "unavailable", "price": None}}}
    out = _snapshot_block(m)
    assert len(out) == 1
    assert out[0]["asset"] == "BTC"
    assert out[0]["status"] == "unavailable"


def test_baseline_block_returns_dict_when_no_conn():
    b = _baseline_block(None)
    assert b["n_total"] == 0
    assert b["by_type"] == {}
    assert b["min_sample"] == 5


def test_check_marks_missing_events():
    payload = {
        "events": [
            {"type_code": "rate_hike"},
            {"type_code": "central_bank"},
        ]
    }
    out = check_reasoning_vs_structured_input("美联储宣布加息", payload)
    assert "rate_hike" in out["events_mentioned"]
    assert "central_bank" in out["events_missing"]


def test_check_handles_empty_reasoning():
    payload = {"events": [{"type_code": "rate_hike"}]}
    out = check_reasoning_vs_structured_input("", payload)
    assert out["events_in_payload"] == 1
    assert out["events_missing"] == ["rate_hike"]
    assert out["verdict"] == "partial"


def test_check_handles_empty_payload():
    out = check_reasoning_vs_structured_input("美联储加息", {"events": []})
    assert out["events_in_payload"] == 0
    assert out["verdict"] == "ok"


def test_check_detects_fabricated_asset():
    payload = {
        "events": [{"type_code": "rate_hike"}],
        "snapshot": [{"asset": "BTC"}],
    }
    out = check_reasoning_vs_structured_input(
        "美联储加息,导致XAU金价承压",
        payload,
    )
    assert "XAU" in out["entities_fabricated"]


def test_check_verdict_thresholds():
    payload = {"events": [{"type_code": "rate_hike"}]}
    out = check_reasoning_vs_structured_input("美联储加息", payload)
    assert out["verdict"] == "ok"

    out = check_reasoning_vs_structured_input("完全无关内容", payload)
    assert out["verdict"] == "partial"


def test_build_then_check_full_flow():
    news = FakeRow({"id": 99, "text": "美联储宣布加息25bp,黄金承压", "published_at": 1758328800})
    payload = build_structured_input(news)
    payload["events"] = [
        {"type_code": "rate_hike", "actor": "美联储", "assets": ["XAU"],
         "magnitude": {"value": 25, "unit": "bp"},
         "direction": "bearish", "direction_confidence": 0.85,
         "direction_source": "rule_bias", "evidence_count": 1, "source": "event_atom"},
    ]
    payload["snapshot"] = [{"asset": "XAU", "price": 2400, "trend_1d": "down"}]

    check = check_reasoning_vs_structured_input(
        "美联储加息25bp利空黄金,XAU应做空",
        payload,
    )
    assert check["events_in_payload"] == 1
    assert "rate_hike" in check["events_mentioned"]
    assert check["verdict"] in ("ok", "partial")


def test_serialize_check_roundtrip():
    check = check_reasoning_vs_structured_input(
        "美联储加息",
        {"events": [{"type_code": "rate_hike"}]},
    )
    s = serialize_check(check)
    parsed = json.loads(s)
    assert parsed["events_mentioned"] == ["rate_hike"]


def test_modules_do_not_modify_db():
    """Read-only invariant: modules never INSERT/UPDATE/DELETE."""
    import importlib, inspect
    si = importlib.import_module("structured_input")
    sc = importlib.import_module("structured_check")
    si_src = inspect.getsource(si)
    sc_src = inspect.getsource(sc)
    for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert keyword not in si_src, f"structured_input should not contain {keyword}"
        assert keyword not in sc_src, f"structured_check should not contain {keyword}"


# ============================================================
# PR-4 relations / PR-5 baseline 真实数据 / PR-6 text_summary
# ============================================================
import sqlite3
import tempfile


def _make_minimal_db() -> sqlite3.Connection:
    """建一张最小可跑通 baseline/relations 查询的内存 DB。

    包含: dim_event_type, dim_asset, event_atom, raw_news, ai_decisions,
          signal_performance, backtest_event_outcomes.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE dim_event_type (
            id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE,
            name_zh TEXT NOT NULL DEFAULT '');
        CREATE TABLE dim_asset (
            id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'crypto',
            active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE event_atom (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_news_id INTEGER NOT NULL, event_type_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL DEFAULT 0, event_time INTEGER NOT NULL,
            magnitude_value REAL, magnitude_unit TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            asset_code TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT 'unknown',
            direction_confidence REAL NOT NULL DEFAULT 0.0,
            direction_source TEXT NOT NULL DEFAULT 'rule_bias',
            evidence_count INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE raw_news (id INTEGER PRIMARY KEY);
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL);
        CREATE TABLE signal_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL, asset TEXT NOT NULL DEFAULT 'NONE',
            ts INTEGER NOT NULL, action TEXT NOT NULL DEFAULT 'HOLD',
            is_correct TEXT NOT NULL DEFAULT '', forward_pnl REAL,
            UNIQUE (decision_id, ts));
        CREATE TABLE backtest_event_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type_code TEXT NOT NULL, asset TEXT NOT NULL DEFAULT '',
            sample_n INTEGER NOT NULL DEFAULT 0, win_n INTEGER NOT NULL DEFAULT 0,
            loss_n INTEGER NOT NULL DEFAULT 0, avg_pnl_4h REAL NOT NULL DEFAULT 0.0,
            last_updated INTEGER NOT NULL,
            UNIQUE(type_code, asset));
    """)
    # 维度种子
    c.execute("INSERT INTO dim_event_type(id, code, name_zh) VALUES (1,'rate_hike','加息')")
    c.execute("INSERT INTO dim_event_type(id, code, name_zh) VALUES (2,'cpi','CPI')")
    c.execute("INSERT INTO dim_asset(id, code, name, kind) VALUES (10,'BTC','Bitcoin','crypto')")
    c.execute("INSERT INTO dim_asset(id, code, name, kind) VALUES (20,'XAU','Gold','commodity')")
    return conn


# ---------- PR-4 relations ----------

def test_relations_block_returns_empty_when_no_news_id():
    conn = _make_minimal_db()
    rels = _relations_block(0, conn)
    assert rels == []


def test_relations_block_returns_empty_when_no_event_atoms():
    conn = _make_minimal_db()
    rels = _relations_block(999, conn)
    assert rels == []


def test_relations_block_returns_empty_when_only_one_atom():
    conn = _make_minimal_db()
    c = conn.cursor()
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 10, 1700000000)")
    rels = _relations_block(1, conn)
    assert rels == []


def test_relations_block_builds_pairs_for_shared_asset():
    conn = _make_minimal_db()
    c = conn.cursor()
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 10, 1700000000)")
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 2, 10, 1700000100)")
    rels = _relations_block(1, conn)
    assert len(rels) == 1
    assert rels[0]['shared_asset'] == 'BTC'
    assert set(rels[0]['type_codes']) == {'rate_hike', 'cpi'}
    assert rels[0]['atom_a_id'] != rels[0]['atom_b_id']


def test_relations_block_skips_yoon_asset():
    """无 asset 关联的原子不参与 pair"""
    conn = _make_minimal_db()
    c = conn.cursor()
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 0, 1700000000)")
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 2, 0, 1700000100)")
    rels = _relations_block(1, conn)
    assert rels == []


def test_relations_block_handles_multiple_assets():
    conn = _make_minimal_db()
    c = conn.cursor()
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 10, 1)")
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 2, 10, 2)")
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 20, 3)")
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 2, 20, 4)")
    rels = _relations_block(1, conn)
    assert len(rels) == 2
    assets = sorted([r['shared_asset'] for r in rels])
    assert assets == ['BTC', 'XAU']


# ---------- PR-5 baseline 真实数据 ----------

def test_baseline_block_reads_backtest_outcomes():
    """baseline 应当从 backtest_event_outcomes 读真实 win_rate/avg_pnl"""
    conn = _make_minimal_db()
    c = conn.cursor()
    import time as _t
    now = int(_t.time())
    c.execute("INSERT INTO backtest_event_outcomes(type_code, asset, sample_n, win_n, loss_n, avg_pnl_4h, last_updated) VALUES ('rate_hike', 'BTC', 10, 7, 3, 0.012, ?)", (now,))
    c.execute("INSERT INTO backtest_event_outcomes(type_code, asset, sample_n, win_n, loss_n, avg_pnl_4h, last_updated) VALUES ('cpi', 'XAU', 2, 1, 1, 0.005, ?)", (now,))
    base = _baseline_block(conn, min_sample=5)
    assert 'rate_hike' in base['by_type']
    assert base['by_type']['rate_hike']['sample_n'] == 10
    assert base['by_type']['rate_hike']['win_n'] == 7
    assert base['by_type']['rate_hike']['loss_n'] == 3
    assert base['by_type']['rate_hike']['win_rate_4h'] == 0.7
    # sample < min_sample -> win_rate_4h 为 None
    assert base['by_type']['cpi']['win_rate_4h'] is None


def test_baseline_block_handles_empty_cache():
    conn = _make_minimal_db()
    base = _baseline_block(conn)
    assert base['by_type'] == {}
    assert base['source'] == 'empty'


def test_baseline_block_n_total_correct():
    conn = _make_minimal_db()
    c = conn.cursor()
    import time as _t
    now = int(_t.time())
    c.execute("INSERT INTO backtest_event_outcomes(type_code, asset, sample_n, win_n, loss_n, avg_pnl_4h, last_updated) VALUES ('rate_hike', 'BTC', 10, 7, 3, 0.012, ?)", (now,))
    c.execute("INSERT INTO backtest_event_outcomes(type_code, asset, sample_n, win_n, loss_n, avg_pnl_4h, last_updated) VALUES ('cpi', 'XAU', 20, 12, 8, 0.005, ?)", (now,))
    base = _baseline_block(conn, min_sample=3)
    assert base['n_total'] == 30


# ---------- PR-6 text_summary ----------

def test_text_summary_picks_first_sentence():
    text = "美联储宣布加息25个基点。鲍威尔表示通胀仍偏高。BTC应声下跌3%。"
    s = _text_summary(text, budget=200)
    assert "美联储宣布加息" in s


def test_text_summary_picks_key_sentences():
    text = "今晨市场情绪稳定。BTC价格突破65000美元大关。XAU上涨1.5%。"
    s = _text_summary(text, budget=200)
    assert "BTC" in s
    assert "65000" in s or "6.5" in s


def test_text_summary_handles_english():
    text = "Fed raises rates by 25bp. BTC fell 3%. XAU stable."
    s = _text_summary(text, budget=200)
    assert "Fed" in s


def test_text_summary_handles_empty():
    assert _text_summary("") == ""


def test_text_summary_respects_budget():
    long_text = "美联储加息。".join(["宏观环境分析"] * 100)
    s = _text_summary(long_text, budget=100)
    assert len(s) <= 100 * 3 + 5


# ---------- PR-5 db.refresh_backtest_event_outcomes ----------

def test_refresh_backtest_event_outcomes_writes_cache():
    """refresh 函数应把 JOIN 结果写入缓存表"""
    import db as dbmod
    conn = _make_minimal_db()
    c = conn.cursor()
    # 构造数据链路: signal_performance → ai_decisions → raw_news → event_atom
    c.execute("INSERT INTO raw_news(id) VALUES (1)")
    c.execute("INSERT INTO ai_decisions(news_id) VALUES (1)")
    # 用动态 ts (now = 测试运行时间 2026+),确保落在 30 天窗口内
    import time as _t
    _recent = int(_t.time()) - 86400  # 1 天前
    c.execute("INSERT INTO signal_performance(decision_id, ts, action, is_correct, forward_pnl) VALUES (1, ?, 'BUY', 'WIN', 0.01)", (_recent,))
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 10, ?)", (_recent,))

    n = dbmod.refresh_backtest_event_outcomes(conn, days=30, min_sample=1)
    assert n >= 1
    row = c.execute("SELECT sample_n, win_n, avg_pnl_4h FROM backtest_event_outcomes WHERE type_code='rate_hike' AND asset='BTC'").fetchone()
    assert row is not None
    assert int(row['sample_n']) == 1
    assert int(row['win_n']) == 1


def test_refresh_backtest_event_outcomes_idempotent():
    """连续 2 次 refresh 不应破坏"""
    import db as dbmod
    conn = _make_minimal_db()
    c = conn.cursor()
    c.execute("INSERT INTO raw_news(id) VALUES (1)")
    c.execute("INSERT INTO ai_decisions(news_id) VALUES (1)")
    import time as _t
    _recent = int(_t.time()) - 86400
    c.execute("INSERT INTO signal_performance(decision_id, ts, is_correct, forward_pnl) VALUES (1, ?, 'WIN', 0.02)", (_recent,))
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 10, ?)", (_recent,))
    n1 = dbmod.refresh_backtest_event_outcomes(conn, days=30, min_sample=1)
    n2 = dbmod.refresh_backtest_event_outcomes(conn, days=30, min_sample=1)
    assert n1 >= 1
    assert n2 >= 1
    total_rows = c.execute("SELECT COUNT(*) FROM backtest_event_outcomes").fetchone()[0]
    assert total_rows == 1


def test_refresh_handles_empty_db():
    """空表应回 0,不抛"""
    import db as dbmod
    conn = _make_minimal_db()
    n = dbmod.refresh_backtest_event_outcomes(conn, days=30, min_sample=1)
    assert n == 0


# ---------- 集成测试 ----------

def test_full_flow_with_relations_and_baseline():
    """build_structured_input 在带 conn + 真实 event_atom 时返回完整 6 字段 payload,
    含 relations 真实数据 + baseline 真实 win_rate。"""
    conn = _make_minimal_db()
    c = conn.cursor()
    import time as _t
    now = int(_t.time())
    c.execute("INSERT INTO backtest_event_outcomes(type_code, asset, sample_n, win_n, loss_n, avg_pnl_4h, last_updated) VALUES ('rate_hike', 'BTC', 20, 14, 6, 0.012, ?)", (now,))
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 10, 1700000000)")
    c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 2, 10, 1700000100)")
    news = FakeRow({"id": 1, "source": "jin10", "text": "美联储加息25bp。BTC反应温和。", "published_at": 1758328800})

    rows = conn.execute("""
        SELECT ea.id, ea.event_time, ea.actor,
               ea.magnitude_value, ea.magnitude_unit, ea.asset_code,
               ea.direction, ea.direction_confidence, ea.direction_source,
               ea.evidence_count, det.code AS event_type_code,
               det.name_zh AS type_name_zh
        FROM event_atom ea
        LEFT JOIN dim_event_type det ON det.id = ea.event_type_id
        WHERE ea.raw_news_id = 1
        ORDER BY ea.id LIMIT 8
    """).fetchall()
    payload = build_structured_input(
        news,
        event_atom_rows=rows,
        connection=conn,
    )
    # relations 应有 1 条(BTC 上 2 事件)
    assert len(payload['relations']) == 1
    assert payload['relations'][0]['shared_asset'] == 'BTC'
    # baseline 应有 rate_hike 真实胜率
    assert 'rate_hike' in payload['baseline']['by_type']
    assert payload['baseline']['by_type']['rate_hike']['win_rate_4h'] == 0.7
    # text_summary 应取首句
    assert '美联储' in payload['text_summary']


def test_serialize_with_new_blocks_under_6k():
    """payload 含 relations + 真实 baseline 时仍应 < 6000 chars"""
    conn = _make_minimal_db()
    c = conn.cursor()
    import time as _t
    now = int(_t.time())
    for i, tc in enumerate(['rate_hike', 'cpi', 'nfp', 'opec_cut', 'geopolitics']):
        c.execute("INSERT INTO backtest_event_outcomes(type_code, asset, sample_n, win_n, loss_n, avg_pnl_4h, last_updated) VALUES (?, 'BTC', ?, ?, ?, 0.01, ?)",
                  (tc, 10 + i, 7 + i, 3, now))
    for j in range(5):
        c.execute("INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time) VALUES (1, 1, 10, ?)", (1700000000 + j,))
    news = FakeRow({"id": 1, "source": "test", "text": "新闻。", "published_at": 1758000000})
    rows = conn.execute("SELECT * FROM event_atom WHERE raw_news_id = 1 ORDER BY id LIMIT 8").fetchall()
    payload = build_structured_input(news, event_atom_rows=rows, connection=conn)
    s = serialize(payload)
    assert len(s) <= 6000


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
