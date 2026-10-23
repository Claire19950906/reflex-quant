"""v290 P0+ (review-system time-window unification) regression tests.

Bug: review page showed inconsistent time windows
  - top cards (ReviewOverview): 159 trades
  - block A (ReviewPanel, real positions): 49 trades
  - block B (ReviewHistoryPanel, quick eval): 25 trades
Three independent time-window controls with mismatched defaults (today / week / 7d).

Fix: plumb the global PeriodSelector's PeriodRange through ReviewPanel and
ReviewHistoryPanel as a `period` prop, and extend the /api/review/run endpoint
plus review_agent to accept an explicit (lo, hi) UTC-second window.
"""

import os
import sys
import sqlite3
import tempfile
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ──────────── helpers ────────────

def _build_synth_db():
    """Build a minimal ai_decisions + quick_sim_trades + raw_news schema.

    5 rows: id 1/5 outside window, id 2/3/4 inside window.
    Window is [1730400000, 1730572799] = 2024-11-02..2024-11-04 (UTC seconds).
    """
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ai_decisions (
          id INTEGER PRIMARY KEY,
          target_asset TEXT, suggested_action TEXT, created_at TEXT, news_id INTEGER,
          expected_horizon TEXT, event_strength REAL, reasoning TEXT, decision_context TEXT,
          sentiment_score REAL, factor_ranking TEXT, hard_data_evidence TEXT, data_anchor_ts TEXT,
          agent_model_id TEXT, market_category TEXT,
          completeness_score REAL, field_sources TEXT, missing_fields TEXT,
          llm_field_count INTEGER, rule_field_count INTEGER,
          business_kb_field_count INTEGER, historical_field_count INTEGER
        );
        CREATE TABLE quick_sim_trades (
          id INTEGER PRIMARY KEY, decision_id INTEGER, news_id INTEGER,
          asset TEXT, action TEXT, pnl_pct REAL, entry_price REAL, exit_price REAL,
          max_price REAL, min_price REAL, mfe_pct REAL, mae_pct REAL,
          entry_ts INTEGER, created_at TEXT, settled INTEGER, verdict TEXT,
          gate_passed INTEGER, gate_reason TEXT, score REAL, horizon_minutes INTEGER
        );
        CREATE TABLE raw_news (
          id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT,
          gate_core_impact TEXT, gate_main_asset TEXT, gate_kind TEXT,
          gate_strength TEXT, gate_verdict TEXT
        );
        """
    )
    lo = 1730400000
    hi = 1730572799
    timestamps = [1730313600, 1730420000, 1730500000, 1730550000, 1730659200]
    for i, ts in enumerate(timestamps, 1):
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
        conn.execute(
            "INSERT INTO ai_decisions (id, target_asset, suggested_action, created_at, news_id, completeness_score) VALUES (?,?,?,?,?,?)",
            (i, "BTC", "LONG", iso, i, 0.9),
        )
        conn.execute(
            "INSERT INTO quick_sim_trades (id, decision_id, asset, action, pnl_pct, entry_price, exit_price, settled, verdict, created_at) VALUES (?,?,?,?,?,?,?,1,?,?)",
            (i, i, "BTC", "LONG", 0.05, 50000, 52500, "WIN", iso),
        )
    conn.commit()
    return conn, db_path, lo, hi


@pytest.fixture
def synth_db():
    conn, db_path, lo, hi = _build_synth_db()
    yield conn, lo, hi
    conn.close()
    os.unlink(db_path)


@pytest.fixture
def synth_db_no_llm(monkeypatch, synth_db):
    """Stub out LLM-dependent helpers so run_review can run offline."""
    import review_agent
    monkeypatch.setattr(review_agent, "_llm", lambda *a, **kw: None)
    monkeypatch.setattr(review_agent, "_generate_insights_llm", lambda *a, **kw: [])
    monkeypatch.setattr(review_agent, "_extract_causal_chains_llm", lambda *a, **kw: [])
    monkeypatch.setattr(
        review_agent,
        "_extract_from_quick_sim_llm",
        lambda *a, **kw: {"insights": [], "causal": [], "prompts": []},
    )
    monkeypatch.setattr(review_agent, "_feedback_comparison", lambda conn: {})
    monkeypatch.setattr(review_agent, "_model_stats", lambda conn: [])
    monkeypatch.setattr(review_agent, "_evaluate_insights", lambda conn: {})
    monkeypatch.setattr(review_agent, "build_attribution_rows", lambda *a, **kw: [])
    return synth_db


# ──────────── 1) helper output ────────────

def test_window_where_for_decisions_full_range():
    from review_agent import _window_where_for_decisions
    out = _window_where_for_decisions(1730400000, 1730572799)
    assert "datetime(?, 'unixepoch', 'localtime')" in out
    assert out.count("datetime(?, 'unixepoch', 'localtime')") == 2


def test_window_where_for_decisions_lo_only():
    from review_agent import _window_where_for_decisions
    out = _window_where_for_decisions(1730400000, None)
    assert ">= datetime(?, 'unixepoch', 'localtime')" in out
    assert "<= datetime(?, 'unixepoch', 'localtime')" not in out


def test_window_where_for_decisions_hi_only():
    from review_agent import _window_where_for_decisions
    out = _window_where_for_decisions(None, 1730572799)
    assert ">= datetime(?, 'unixepoch', 'localtime')" not in out
    assert "<= datetime(?, 'unixepoch', 'localtime')" in out


def test_window_where_empty_when_no_bounds():
    from review_agent import _window_where_for_decisions
    assert _window_where_for_decisions(None, None) == ""


# ──────────── 2) _resolve_window_clause behavior ────────────

def test_resolve_window_uses_explicit_window_over_scope():
    """When lo/hi are set, they take precedence over scope today/week/all."""
    from review_agent import _resolve_window_clause
    where, params = _resolve_window_clause("today", 1000, 2000, "decisions")
    assert "datetime(?, 'unixepoch', 'localtime')" in where
    assert params == (1000, 2000)
    assert "date('now'" not in where


def test_resolve_window_fallback_to_scope_when_no_bounds():
    from review_agent import _resolve_window_clause
    where, params = _resolve_window_clause("today", None, None, "decisions")
    assert "date(ad.created_at) = date('now'" in where
    assert params == ()
    where_qs, params_qs = _resolve_window_clause("week", None, None, "quick_sim")
    assert "datetime(qt.created_at" in where_qs
    assert "-7 days" in where_qs


# ──────────── 3) _fetch_trades / _fetch_quick_sim_trades end-to-end ────────────

def test_fetch_trades_with_window_filters_correctly(synth_db):
    conn, lo, hi = synth_db
    from review_agent import _fetch_trades
    rows = _fetch_trades(conn, scope="all", limit=10, lo=lo, hi=hi)
    ids = sorted(r["id"] for r in rows)
    assert ids == [2, 3, 4], f"unexpected ids: {ids}"


def test_fetch_quick_sim_with_window_filters_correctly(synth_db):
    conn, lo, hi = synth_db
    from review_agent import _fetch_quick_sim_trades
    rows = _fetch_quick_sim_trades(conn, scope="all", limit=10, lo=lo, hi=hi)
    ids = sorted(r["id"] for r in rows)
    assert ids == [2, 3, 4]


def test_fetch_trades_scope_fallback_returns_all(synth_db):
    conn, _, _ = synth_db
    from review_agent import _fetch_trades
    rows = _fetch_trades(conn, scope="all", limit=10)
    assert len(rows) == 5


# ──────────── 4) run_review pipeline with window field ────────────

def test_run_review_records_window_source(synth_db_no_llm):
    conn, lo, hi = synth_db_no_llm
    from review_agent import run_review
    r = run_review(conn, scope="all", limit=500, lo=lo, hi=hi)
    assert r["window"]["from_ts"] == lo
    assert r["window"]["to_ts"] == hi
    assert r["window"]["source"] == "period_selector"
    assert r["samples"] == 3
    assert r["quick_sim_samples"] == 3


def test_run_review_window_changes_sample_count(synth_db_no_llm):
    conn, _, _ = synth_db_no_llm
    from review_agent import run_review
    r_all = run_review(conn, scope="all", limit=500)
    assert r_all["samples"] == 5
    assert r_all["window"]["source"] == "scope_fallback"
    r_narrow = run_review(conn, scope="all", limit=500, lo=1730450000, hi=1730510000)
    assert r_narrow["samples"] < r_all["samples"]
    assert r_narrow["window"]["source"] == "period_selector"


# ──────────── 5) /api/review/run endpoint accepts new params ────────────

def test_api_review_run_internal_sync_signature():
    """_run_review_sync must expose scope/hours/from_ts/to_ts.

    Note: import api_server triggers relative imports (`from . import ...`) that
    fail when run as a script; we inspect the source instead of importing.
    """
    import re
    api_server_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "src_python", "api_server.py")
    )
    src = open(api_server_path).read()
    # find the def line
    m = re.search(r"^def _run_review_sync\(([^)]*)\)", src, re.MULTILINE)
    assert m is not None, "_run_review_sync not found in api_server.py"
    sig_text = m.group(1)
    assert "scope" in sig_text
    assert "hours" in sig_text
    assert "from_ts" in sig_text
    assert "to_ts" in sig_text


def test_api_review_run_endpoint_signature():
    """FastAPI handler /api/review/run must accept hours/from_ts/to_ts query params."""
    import re
    api_server_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "src_python", "api_server.py")
    )
    src = open(api_server_path).read()
    # find the @app.post("/api/review/run") ... async def run_review(...) block
    m = re.search(
        r'@app\.post\("/api/review/run"\)\s*\nasync def run_review\(([^)]*)\)',
        src,
        re.MULTILINE,
    )
    assert m is not None, "/api/review/run handler not found in api_server.py"
    sig_text = m.group(1)
    assert "scope" in sig_text
    assert "hours" in sig_text
    assert "from_ts" in sig_text
    assert "to_ts" in sig_text

