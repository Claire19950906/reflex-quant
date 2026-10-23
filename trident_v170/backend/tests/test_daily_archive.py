"""v191 daily_archive helper + signal_chain_archive 集成测试。"""
import json
import os
import sqlite3
import tempfile

import pytest


# ── 最小 fixture:在临时 DB 文件里建 v191 三张归档表 ──────────────────
# 注意：测试 fixture 必须**严格匹配生产 schema**(无 title 列,ai_decisions 无 updated_at 列,
# trade_reflections_v2 有 applied_experience_ids)。否则测试通过但生产 SQL 报
# "no such column" —— v191 已踩过这个坑。
_DDL = """
CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER,
    target_asset TEXT NOT NULL DEFAULT 'NONE',
    suggested_action TEXT NOT NULL DEFAULT 'HOLD',
    market_category TEXT NOT NULL DEFAULT 'OTHER',
    sentiment_score REAL,
    reasoning TEXT DEFAULT '',
    agent_model_id TEXT DEFAULT '',
    bias_after_reflection TEXT DEFAULT '',
    entry_price REAL DEFAULT NULL,
    exit_price REAL DEFAULT NULL,
    is_correct TEXT DEFAULT '',
    settled INTEGER DEFAULT 0,
    forward_pnl REAL DEFAULT NULL,
    mfe_pct REAL DEFAULT NULL,
    mae_pct REAL DEFAULT NULL,
    decision_context TEXT DEFAULT '{}',
    paper_trading_run_id INTEGER DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT DEFAULT '',
    content TEXT DEFAULT '',
    relevance_score REAL DEFAULT 0.0,
    published_at TEXT DEFAULT '',
    timestamp TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS trade_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    asset TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    notional_usdt REAL DEFAULT 0.0,
    quantity REAL DEFAULT 0.0,
    filled_price REAL DEFAULT 0.0,
    order_id TEXT DEFAULT '',
    adapter TEXT DEFAULT '',
    mode TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    raw_json TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS trade_reflections_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    paper_run_id INTEGER,
    asset TEXT DEFAULT '',
    action TEXT DEFAULT '',
    layer TEXT DEFAULT '',
    sub_layer TEXT DEFAULT '',
    ontology_signal TEXT DEFAULT '',
    root_cause TEXT DEFAULT '',
    lesson TEXT DEFAULT '',
    applied_experience_ids TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- v191 三张归档表(直接复制 db.py DDL,避免 import 触发整个 migrate)
CREATE TABLE IF NOT EXISTS daily_reset_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reset_date TEXT NOT NULL,
    reset_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    trigger TEXT NOT NULL DEFAULT 'auto_daily'
        CHECK (trigger IN ('auto_daily', 'manual', 'startup', 'mode_change', 'kill_switch')),
    run_id TEXT NOT NULL DEFAULT '',
    trade_mode TEXT NOT NULL DEFAULT '',
    cleared_decisions INTEGER DEFAULT 0,
    cleared_audit_rows INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS signal_chain_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL UNIQUE,
    run_id TEXT NOT NULL DEFAULT '',
    decision_ts TEXT NOT NULL DEFAULT '',
    decision_date TEXT NOT NULL DEFAULT '',
    news_id INTEGER,
    news_published_at TEXT DEFAULT '',
    news_source TEXT DEFAULT '',
    news_title TEXT DEFAULT '',
    sentiment_score REAL,
    target_asset TEXT NOT NULL DEFAULT '',
    suggested_action TEXT NOT NULL DEFAULT '',
    market_category TEXT DEFAULT '',
    reasoning TEXT DEFAULT '',
    agent_model_id TEXT DEFAULT '',
    bias_after_reflection TEXT DEFAULT '',
    audit_status TEXT DEFAULT '',
    audit_reason TEXT DEFAULT '',
    order_id TEXT DEFAULT '',
    symbol TEXT DEFAULT '',
    side TEXT DEFAULT '',
    notional_usdt REAL DEFAULT 0.0,
    quantity REAL DEFAULT 0.0,
    filled_price REAL DEFAULT 0.0,
    entry_price REAL,
    adapter TEXT DEFAULT '',
    mode TEXT DEFAULT '',
    is_correct TEXT DEFAULT '',
    exit_price REAL,
    forward_pnl REAL,
    mfe_pct REAL,
    mae_pct REAL,
    settled_at TEXT DEFAULT '',
    reflection_layer TEXT DEFAULT '',
    reflection_signal TEXT DEFAULT '',
    experience_ids TEXT DEFAULT '',
    raw_payload TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS daily_reflection_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    trade_mode TEXT DEFAULT '',
    n_decisions INTEGER DEFAULT 0,
    n_trades INTEGER DEFAULT 0,
    n_skipped INTEGER DEFAULT 0,
    n_risk_rejected INTEGER DEFAULT 0,
    n_adapter_errors INTEGER DEFAULT 0,
    n_pending INTEGER DEFAULT 0,
    n_wins INTEGER DEFAULT 0,
    n_losses INTEGER DEFAULT 0,
    n_hold INTEGER DEFAULT 0,
    n_unsettled INTEGER DEFAULT 0,
    win_rate REAL,
    total_pnl_usdt REAL DEFAULT 0.0,
    avg_pnl_usdt REAL DEFAULT 0.0,
    avg_mfe_pct REAL,
    avg_mae_pct REAL,
    max_drawdown_pct REAL,
    asset_breakdown TEXT DEFAULT '{}',
    action_breakdown TEXT DEFAULT '{}',
    layer_breakdown TEXT DEFAULT '{}',
    key_takeaways TEXT DEFAULT '',
    lessons_learned TEXT DEFAULT '',
    next_day_focus TEXT DEFAULT '',
    reset_log_id INTEGER,
    generated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(summary_date, run_id)
);
"""


@pytest.fixture
def db_path():
    """每个测试一个临时 DB 文件,自动清理。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    yield tmp.name
    try:
        os.unlink(tmp.name)
    except FileNotFoundError:
        pass


def _open(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


# ─────────────────────────────────────────────────────────────────────
# 真实函数 import(必须在 fixture 后才 import,避免 import 触发 db.migrate 的 :memory: bug)
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
import daily_archive as da  # noqa: E402


def _seed_decision(conn, *, target_asset="BTC", action="BUY",
                   sentiment=0.5, market_cat="crypto",
                   reasoning="测试用决策", news_id=None,
                   created_at="2026-09-29 10:00:00",
                   paper_trading_run_id=None) -> int:
    cur = conn.execute(
        """INSERT INTO ai_decisions
           (news_id, target_asset, suggested_action, market_category,
            sentiment_score, reasoning, paper_trading_run_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (news_id, target_asset, action, market_cat,
         sentiment, reasoning, paper_trading_run_id, created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_news(conn, *, source="Reuters", content="Test news",
               sentiment=0.5, published_at="2026-09-29 09:00:00") -> int:
    cur = conn.execute(
        """INSERT INTO raw_news (source, content, relevance_score, published_at)
           VALUES (?, ?, ?, ?)""",
        (source, content, sentiment, published_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_audit(conn, decision_id, *, status="ORDER_PLACED",
                asset="BTC", symbol="BTCUSDT", side="BUY",
                notional=100.0, qty=0.002, price=50000.0,
                order_id="B-NEW", adapter="mock", mode="paper") -> int:
    cur = conn.execute(
        """INSERT INTO trade_audit_log
           (decision_id, asset, symbol, side, status, notional_usdt,
            quantity, filled_price, order_id, adapter, mode, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (decision_id, asset, symbol, side, status,
         float(notional), float(qty), float(price),
         order_id, adapter, mode, "test"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_reflection(conn, decision_id, *, layer="narrative_bias",
                     signal="narrative_bias/over_trust/false_breakout"):
    cur = conn.execute(
        """INSERT INTO trade_reflections_v2
           (decision_id, layer, ontology_signal)
           VALUES (?, ?, ?)""",
        (decision_id, layer, signal),
    )
    conn.commit()
    return int(cur.lastrowid)


# ═══════════════════════════════════════════════════════════════════
# Tests — 10 个
# ═══════════════════════════════════════════════════════════════════

def test_01_archive_daily_reset_basic(db_path):
    """最基本的写入 + 默认 trigger"""
    c = _open(db_path)
    rid = da.archive_daily_reset(c, run_id="2026-09-29",
                                  trade_mode="paper", trigger="auto_daily",
                                  cleared_decisions=12, cleared_audit_rows=8,
                                  notes="eod reset")
    assert rid > 0
    rows = da.query_daily_resets(c, limit=5)
    assert len(rows) == 1
    r = rows[0]
    assert r["trigger"] == "auto_daily"
    assert r["trade_mode"] == "paper"
    assert r["cleared_decisions"] == 12
    assert r["cleared_audit_rows"] == 8
    assert r["notes"] == "eod reset"
    assert r["reset_date"]
    c.close()


def test_02_archive_daily_reset_check_constraint(db_path):
    """trigger 必须落在 CHECK 枚举里"""
    import sqlite3 as _sq
    c = _open(db_path)
    with pytest.raises(_sq.IntegrityError):
        da.archive_daily_reset(c, trigger="invalid_trigger")
    c.close()


def test_03_archive_signal_chain_full_link(db_path):
    """单笔决策的全链路拼装:news → decision → audit → reflection 全部带上"""
    c = _open(db_path)
    news_id = _seed_news(c)
    did = _seed_decision(c, target_asset="BTC", action="BUY",
                         sentiment=0.8, market_cat="crypto",
                         reasoning="BTC 突破 50k", news_id=news_id,
                         created_at="2026-09-29 10:00:00",
                         paper_trading_run_id=42)
    _seed_audit(c, did, status="ORDER_PLACED", side="BUY",
                notional=200.0, price=50000.0)
    _seed_reflection(c, did, layer="narrative_bias",
                     signal="narrative_bias/over_trust/false_breakout")

    ok = da.archive_signal_chain(c, did, run_id="42")
    assert ok is True
    rows = da.query_signal_chain(c, decision_id=did)
    assert len(rows) == 1
    r = rows[0]
    assert r["decision_id"] == did
    assert r["target_asset"] == "BTC"
    assert r["suggested_action"] == "BUY"
    assert r["audit_status"] == "ORDER_PLACED"
    assert r["news_title"] == "Test news"
    assert r["news_source"] == "Reuters"
    assert r["sentiment_score"] == 0.8
    assert r["reflection_layer"] == "narrative_bias"
    assert r["reflection_signal"] == "narrative_bias/over_trust/false_breakout"
    assert r["entry_price"] is None
    assert r["decision_date"] == "2026-09-29"
    c.close()


def test_04_archive_signal_chain_pending_when_no_audit(db_path):
    """没 audit 记录 → audit_status = PENDING(待执行)"""
    c = _open(db_path)
    did = _seed_decision(c, target_asset="BTC", action="BUY")
    ok = da.archive_signal_chain(c, did)
    assert ok is True
    rows = da.query_signal_chain(c, decision_id=did)
    assert rows[0]["audit_status"] == "PENDING"
    c.close()


def test_05_archive_signal_chain_idempotent(db_path):
    """重复调用同一 decision_id 不增加行,只刷新字段"""
    c = _open(db_path)
    did = _seed_decision(c)
    da.archive_signal_chain(c, did)
    da.archive_signal_chain(c, did)
    da.archive_signal_chain(c, did)
    cnt = c.execute(
        "SELECT COUNT(*) FROM signal_chain_archive WHERE decision_id = ?",
        (did,),
    ).fetchone()[0]
    assert cnt == 1
    c.close()


def test_06_archive_signal_chain_with_experience_ids(db_path):
    """experience_ids 参数正确写入"""
    c = _open(db_path)
    did = _seed_decision(c)
    da.archive_signal_chain(c, did, experience_ids=[101, 202, 303])
    rows = da.query_signal_chain(c, decision_id=did)
    assert rows[0]["experience_ids"] == "101,202,303"
    c.close()


def test_07_build_daily_summary_multi_decision(db_path):
    """多笔决策聚合:n_decisions / n_wins / n_losses / total_pnl / win_rate"""
    c = _open(db_path)
    # 3 笔决策:WIN/LOSS/未结算
    d1 = _seed_decision(c, target_asset="BTC", action="BUY",
                        created_at="2026-09-29 09:00:00")
    _seed_audit(c, d1, status="ORDER_PLACED")
    c.execute("UPDATE ai_decisions SET is_correct='WIN', settled=1, forward_pnl=50.0 WHERE id=?", (d1,))

    d2 = _seed_decision(c, target_asset="ETH", action="SELL",
                        created_at="2026-09-29 11:00:00")
    _seed_audit(c, d2, status="ADAPTER_SKIPPED")
    c.execute("UPDATE ai_decisions SET is_correct='LOSS', settled=1, forward_pnl=-20.0 WHERE id=?", (d2,))

    d3 = _seed_decision(c, target_asset="BTC", action="BUY",
                        created_at="2026-09-29 14:00:00")
    _seed_audit(c, d3, status="RISK_REJECTED")
    # 不 settled

    for did in (d1, d2, d3):
        da.archive_signal_chain(c, did)

    sid = da.build_daily_summary(c, "2026-09-29", run_id="r1",
                                  trade_mode="paper",
                                  lessons_learned="L", next_day_focus="F")
    assert sid > 0
    rows = da.query_daily_summary(c, summary_date="2026-09-29")
    assert len(rows) == 1
    s = rows[0]
    assert s["n_decisions"] == 3
    assert s["n_trades"] == 1
    assert s["n_skipped"] == 1
    assert s["n_risk_rejected"] == 1
    assert s["n_wins"] == 1
    assert s["n_losses"] == 1
    assert s["n_unsettled"] == 1
    assert abs(s["win_rate"] - 0.5) < 1e-9
    assert abs(s["total_pnl_usdt"] - 30.0) < 1e-9
    asset_breakdown = json.loads(s["asset_breakdown"])
    assert asset_breakdown == {"BTC": 2, "ETH": 1}
    action_breakdown = json.loads(s["action_breakdown"])
    assert action_breakdown == {"BUY": 2, "SELL": 1}
    assert "决策 3 笔" in s["key_takeaways"]
    c.close()


def test_08_build_daily_summary_idempotent(db_path):
    """INSERT OR REPLACE:同一 (date, run_id) 重复调用只 1 行"""
    c = _open(db_path)
    d1 = _seed_decision(c)
    _seed_audit(c, d1)
    da.archive_signal_chain(c, d1)
    da.build_daily_summary(c, "2026-09-29", run_id="r1")
    da.build_daily_summary(c, "2026-09-29", run_id="r1")
    da.build_daily_summary(c, "2026-09-29", run_id="r1")
    cnt = c.execute(
        "SELECT COUNT(*) FROM daily_reflection_summary "
        "WHERE summary_date='2026-09-29' AND run_id='r1'"
    ).fetchone()[0]
    assert cnt == 1
    c.close()


def test_09_query_signal_chain_filters(db_path):
    """按 asset + audit_status 过滤"""
    c = _open(db_path)
    d1 = _seed_decision(c, target_asset="BTC", created_at="2026-09-29 09:00:00")
    _seed_audit(c, d1, status="ORDER_PLACED")
    da.archive_signal_chain(c, d1)

    d2 = _seed_decision(c, target_asset="ETH", created_at="2026-09-29 10:00:00")
    _seed_audit(c, d2, status="RISK_REJECTED", asset="ETH", symbol="ETHUSDT")
    da.archive_signal_chain(c, d2)

    d3 = _seed_decision(c, target_asset="BTC", created_at="2026-09-29 11:00:00")
    _seed_audit(c, d3, status="ADAPTER_SKIPPED")
    da.archive_signal_chain(c, d3)

    assert len(da.query_signal_chain(c, asset="BTC")) == 2
    assert len(da.query_signal_chain(c, asset="ETH")) == 1
    assert len(da.query_signal_chain(c, audit_status="ORDER_PLACED")) == 1
    assert len(da.query_signal_chain(c, decision_date="2026-09-29")) == 3
    assert len(da.query_signal_chain(c, asset="BTC", audit_status="ORDER_PLACED")) == 1
    assert len(da.query_signal_chain(c, asset="XYZ")) == 0
    c.close()


def test_10_query_daily_resets_limit_and_filter(db_path):
    """limit + trigger 过滤"""
    c = _open(db_path)
    for _ in range(5):
        da.archive_daily_reset(c, trigger="auto_daily", run_id="r")
    da.archive_daily_reset(c, trigger="startup", run_id="r")
    all_rows = da.query_daily_resets(c)
    assert len(all_rows) == 6
    limited = da.query_daily_resets(c, limit=3)
    assert len(limited) == 3
    only_startup = da.query_daily_resets(c, trigger="startup")
    assert len(only_startup) == 1
    assert only_startup[0]["trigger"] == "startup"
    c.close()

