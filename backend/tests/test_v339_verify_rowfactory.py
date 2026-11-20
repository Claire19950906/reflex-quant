"""v339 回归：verify_all 不能假设调用方的行工厂。

calc_event_move_attribution 用 tuple 行连接调用 verify_all，
原实现 `dict(r)` → `TypeError: object is not iterable`，
v338 接进重建后每轮都被 try/except 吞掉，表现为「窗口内新行永远 NULL」，
验收 #12 长期卡在 34.65%。
"""

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "src_python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from attribution_rule_verify import verify_all


def _make_conn(row_factory):
    conn = sqlite3.connect(":memory:")
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.executescript("""
        CREATE TABLE event_move_attribution (
            id INTEGER PRIMARY KEY, asset TEXT, move_pct REAL, move_kind TEXT,
            move_ts INTEGER, ev_ts INTEGER, time_lag_sec INTEGER, ev_action TEXT,
            algo_score REAL, ev_news_id INTEGER
        );
        CREATE TABLE news_events (id INTEGER PRIMARY KEY, headline TEXT, assets TEXT, news_id INTEGER);
        CREATE TABLE raw_news (id INTEGER PRIMARY KEY, content TEXT);
        INSERT INTO news_events (id, headline, assets, news_id) VALUES (1, '美联储宣布降息', 'XAU', 1);
        INSERT INTO raw_news (id, content) VALUES (1, '美联储宣布降息');
        INSERT INTO event_move_attribution
            (id, asset, move_pct, move_kind, move_ts, ev_ts, time_lag_sec, ev_action, algo_score, ev_news_id)
            VALUES (1, 'XAU', 1.2, 'fast_rise', 1000, 1000, 5, 'BUY', 0.8, 1);
    """)
    return conn


def test_verify_all_works_with_tuple_rows():
    """calc 的连接是 tuple 行 —— 必须不抛 TypeError。"""
    conn = _make_conn(None)
    r = verify_all(conn)
    assert r["total"] == 1
    assert "confirm" in r["dist"] or "partial" in r["dist"]
    assert conn.execute("SELECT rule_verdict FROM event_move_attribution").fetchone()[0] is not None


def test_verify_all_works_with_row_factory():
    conn = _make_conn(sqlite3.Row)
    r = verify_all(conn)
    assert r["total"] == 1
