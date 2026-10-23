"""
Q3 fix verification: news_events.action 三态 (BUY/HOLD/SELL) 100% 覆盖.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
sys.path.insert(0, str(BACKEND / "src_python"))


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture(scope="module")
def three_state_audit():
    """调 db.check_three_state_consistency() (Q3 主函数)."""
    from db import check_three_state_consistency
    return check_three_state_consistency()


def test_no_empty_actions(conn):
    """Q3 P0: news_events.action 不应该有空字符串 (后污染)."""
    n = conn.execute(
        "SELECT COUNT(*) FROM news_events WHERE action=''"
    ).fetchone()[0]
    assert n == 0, f"found {n} empty action rows — Q3 backfill not done"


def test_no_null_actions(conn):
    """Q3: news_events.action 不应该有 NULL."""
    n = conn.execute(
        "SELECT COUNT(*) FROM news_events WHERE action IS NULL"
    ).fetchone()[0]
    assert n == 0, f"found {n} NULL action rows"


def test_no_polluted_actions(conn):
    """Q3: news_events.action 不应有非法值 (IGNORE/媒体名/分类名)."""
    n = conn.execute("""
        SELECT COUNT(*) FROM news_events
        WHERE action NOT IN ('BUY','SELL','HOLD') AND action IS NOT NULL
    """).fetchone()[0]
    assert n == 0, f"found {n} polluted action rows"


def test_three_state_coverage_100pct(three_state_audit):
    """Q3: (BUY + SELL + HOLD) 必须 = 100% 总行数."""
    assert three_state_audit["three_state_coverage_pct"] == 100.0, \
        f"coverage {three_state_audit['three_state_coverage_pct']}% below 100%"
    assert three_state_audit["ok"] is True, "Q3 audit failed"


def test_score_action_consistency(conn):
    """Q3 派生规则: |score|>=0.3 → BUY/SELL, |score|<0.3 → HOLD.

    已知规则来源: news_score_worker.py:_decide_action()
    """
    # BUY: score >= 0.3
    buy_with_low = conn.execute(
        "SELECT COUNT(*) FROM news_events WHERE action='BUY' AND score < 0.3"
    ).fetchone()[0]
    assert buy_with_low == 0, "BUY rows should have score >= 0.3"
    # SELL: score <= -0.3
    sell_with_high = conn.execute(
        "SELECT COUNT(*) FROM news_events WHERE action='SELL' AND score > -0.3"
    ).fetchone()[0]
    assert sell_with_high == 0, "SELL rows should have score <= -0.3"


def test_view_news_events_three_state(conn):
    """Q3: 视图 news_events_three_state 必须存在且派生正确."""
    rows = conn.execute("""
        SELECT action_three_state, COUNT(*)
        FROM news_events_three_state
        GROUP BY action_three_state ORDER BY 2 DESC
    """).fetchall()
    states = {r[0]: r[1] for r in rows}
    # 视图必须三态全覆盖, 无 UNSCORED/IGNORE 等中间态
    assert set(states.keys()) == {"BUY", "SELL", "HOLD"}, \
        f"view has non-three-state values: {set(states.keys())}"
    total = sum(states.values())
    assert total > 0, "view is empty"


def test_view_sentiment_state_mapping(conn):
    """Q3 视图派生: sentiment_state ∈ {-1, 0, 1} 对应 SELL/HOLD/BUY."""
    invalid = conn.execute("""
        SELECT COUNT(*) FROM news_events_three_state
        WHERE sentiment_state NOT IN (-1, 0, 1)
    """).fetchone()[0]
    assert invalid == 0, f"view sentiment_state has {invalid} invalid rows"


def test_view_has_signal_flag(conn):
    """Q3 视图派生: has_signal ∈ {0, 1}."""
    invalid = conn.execute("""
        SELECT COUNT(*) FROM news_events_three_state
        WHERE has_signal NOT IN (0, 1)
    """).fetchone()[0]
    assert invalid == 0, f"view has_signal has {invalid} invalid rows"


def test_score_worker_catches_empty_action():
    """Q3 P0: news_score_worker 的 WHERE 必须能抓到 action='' 的行."""
    src = (BACKEND / "src_python" / "news_score_worker.py").read_text()
    assert "OR action = ''" in src, \
        "news_score_worker.py missing 'action = \\'\\'' clause — Q3 regression"


def test_bridge_filter_stream_returns_three_state():
    """Q3: bridge_server 端点 _row_to_filter_item 内部不再返 UNSCORED."""
    src = (BACKEND / "src_python" / "bridge_server.py").read_text()
    # 必须包含三态强制逻辑 (非空 action 不在 BUY/SELL/HOLD 中就归 HOLD)
    # 检查 raw src (双引号和单引号两种情况)
    has_three_state_check = (
        'action not in ("BUY", "SELL", "HOLD")' in src
        or "action not in ('BUY', 'SELL', 'HOLD')" in src
        or "if action not in" in src
    )
    assert has_three_state_check, \
        "bridge_server.py missing three-state enforcement"