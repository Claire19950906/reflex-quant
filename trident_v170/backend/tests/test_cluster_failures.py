"""v302+ P2 PR-17: cluster_failures 失败模式聚类测试。"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import sqlite3
from datetime import datetime, timedelta


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE decision_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            error_code TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'checker',
            severity INTEGER NOT NULL DEFAULT 2,
            detail TEXT NOT NULL DEFAULT '',
            evidence_refs TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT '',
            weight INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY,
            target_asset TEXT
        );
    """)
    return conn


def _insert_error(conn, decision_id, code, severity, days_ago=0, detail="x"):
    ts = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO decision_errors(decision_id, error_code, severity, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (decision_id, code, severity, detail, ts),
    )


def _insert_asset(conn, decision_id, asset):
    conn.execute("INSERT OR REPLACE INTO ai_decisions(id, target_asset) VALUES (?, ?)", (decision_id, asset))


def test_no_conn():
    from src_python.reflection_checkers import cluster_failures
    r = cluster_failures(days=30, conn=None)
    assert r["total_errors"] == 0 and r["patterns"] == [] and r["days"] == 30


def test_no_rows():
    from src_python.reflection_checkers import cluster_failures
    r = cluster_failures(days=30, conn=_make_conn())
    assert r["total_errors"] == 0 and r["patterns"] == []


def test_aggregates():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(5):
        _insert_error(conn, i, "E014", 3, days_ago=1)
    _insert_error(conn, 99, "E014", 1, days_ago=0)
    r = cluster_failures(days=30, conn=conn)
    assert r["total_errors"] == 6
    p = r["patterns"][0]
    assert p["code"] == "E014" and p["count"] == 6
    assert p["severity_avg"] == 2.67


def test_sorted_by_count_desc():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(2):
        _insert_error(conn, i, "E001", 2, days_ago=1)
    for i in range(5):
        _insert_error(conn, i + 100, "E014", 3, days_ago=1)
    for i in range(3):
        _insert_error(conn, i + 200, "E015", 2, days_ago=1)
    r = cluster_failures(days=30, conn=conn)
    assert [p["code"] for p in r["patterns"]] == ["E014", "E015", "E001"]
    assert [p["count"] for p in r["patterns"]] == [5, 3, 2]


def test_min_count_filter():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    _insert_error(conn, 1, "E001", 2, days_ago=1)  # 单次,过滤
    for i in range(2, 5):
        _insert_error(conn, i, "E014", 3, days_ago=1)
    r = cluster_failures(days=30, min_count=2, conn=conn)
    assert len(r["patterns"]) == 1 and r["patterns"][0]["code"] == "E014"


def test_days_clamps():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    assert cluster_failures(days=0, conn=conn)["days"] == 1
    assert cluster_failures(days=500, conn=conn)["days"] == 90


def test_examples_limit():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(10):
        _insert_error(conn, i, "E014", 3, days_ago=i)
    r = cluster_failures(days=30, min_count=1, limit_per_group=3, conn=conn)
    p = r["patterns"][0]
    assert p["count"] == 10 and len(p["examples"]) == 3


def test_trend_up():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(20):
        _insert_error(conn, i, "E014", 3, days_ago=1)
    for i in range(100, 110):
        _insert_error(conn, i, "E014", 3, days_ago=10)
    p = cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]
    assert p["last_7d"] == 20 and p["prior_7d"] == 10
    assert p["trend"] == "up" and abs(p["trend_delta"] - 1.0) < 0.01


def test_trend_down():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(5):
        _insert_error(conn, i, "E014", 3, days_ago=1)
    for i in range(100, 120):
        _insert_error(conn, i, "E014", 3, days_ago=10)
    p = cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]
    assert p["trend"] == "down" and p["trend_delta"] < 0


def test_trend_flat():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(10):
        _insert_error(conn, i, "E014", 3, days_ago=1)
    for i in range(100, 110):
        _insert_error(conn, i, "E014", 3, days_ago=10)
    p = cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]
    assert p["trend"] == "flat" and p["trend_delta"] == 0.0


def test_new_pattern_trend_up():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(3):
        _insert_error(conn, i, "E099", 3, days_ago=1)
    p = cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]
    assert p["last_7d"] == 3 and p["prior_7d"] == 0 and p["trend"] == "up"


def test_top_assets_sorted():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(3):
        _insert_error(conn, i, "E014", 3, days_ago=1)
        _insert_asset(conn, i, "BTC")
    _insert_error(conn, 10, "E014", 3, days_ago=1)
    _insert_asset(conn, 10, "ETH")
    _insert_error(conn, 20, "E014", 3, days_ago=1)
    _insert_asset(conn, 20, "SOL")
    p = cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]
    assert [a["asset"] for a in p["top_assets"]] == ["BTC", "ETH", "SOL"]
    assert [a["count"] for a in p["top_assets"]] == [3, 1, 1]


def test_pct_of_total():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(3):
        _insert_error(conn, i, "E014", 3, days_ago=1)
    for i in range(7):
        _insert_error(conn, i + 100, "E015", 3, days_ago=1)
    rs = {p["code"]: p for p in cluster_failures(days=30, min_count=1, conn=conn)["patterns"]}
    assert rs["E014"]["pct_of_total"] == 0.3
    assert rs["E015"]["pct_of_total"] == 0.7


def test_join_asset():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    for i in range(2):
        _insert_error(conn, i, "E014", 3, days_ago=1)
        _insert_asset(conn, i, "BTC")
    assert cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]["top_assets"][0]["asset"] == "BTC"


def test_missing_asset():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    _insert_error(conn, 999, "E014", 3, days_ago=1)
    assert cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]["top_assets"][0]["asset"] == "?"


def test_outside_window_excluded():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    _insert_error(conn, 1, "E014", 3, days_ago=1)
    _insert_error(conn, 2, "E014", 3, days_ago=40)
    p = cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]
    assert p["count"] == 1


def test_trend_boundary():
    from src_python.reflection_checkers import _compute_trend
    assert _compute_trend(12, 10) == ("flat", 0.2)
    assert _compute_trend(12.1, 10)[0] == "up"
    assert _compute_trend(8, 10) == ("flat", -0.2)
    assert _compute_trend(7.9, 10)[0] == "down"


def test_trend_both_zero_flat():
    from src_python.reflection_checkers import _compute_trend
    assert _compute_trend(0, 0) == ("flat", 0.0)


def test_detail_truncated():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    _insert_error(conn, 1, "E014", 2, days_ago=1, detail="x" * 500)
    assert len(cluster_failures(days=30, min_count=1, conn=conn)["patterns"][0]["examples"][0]["detail"]) == 160


def test_examples_order():
    from src_python.reflection_checkers import cluster_failures
    conn = _make_conn()
    _insert_error(conn, 1, "E014", 2, days_ago=5)
    _insert_error(conn, 2, "E014", 2, days_ago=1)
    _insert_error(conn, 3, "E014", 2, days_ago=3)
    p = cluster_failures(days=30, min_count=1, limit_per_group=3, conn=conn)["patterns"][0]
    assert [e["decision_id"] for e in p["examples"]] == [2, 3, 1]
