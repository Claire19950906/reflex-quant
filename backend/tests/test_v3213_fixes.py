#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v3213_fixes.py — v321.3 修复的回归用例 (全离线)

覆盖:
  1. yf_fd_guard   — yfinance fd 泄漏根治 (补丁安装 + 连接回收)
  2. fd_guard      — fd 水位快照 / 分级
  3. missed_signal_reflection — 漏检信号 → 反思系统 (分类 + 幂等 + 不覆盖)
  4. market_regime — 3 类新 regime (集中投资AI / 撤离AI / 国家基金代理)
  5. discovery     — 真实相关性 (Pearson) + 诚实回落
  6. news_decision_link — 新闻 ↔ 决策 多路挂链
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ── 1. yf_fd_guard ────────────────────────────────────────────────────────────
def test_yf_fd_guard_install_is_idempotent():
    import yf_fd_guard
    ok = yf_fd_guard.install()
    assert ok is True
    assert yf_fd_guard.is_installed() is True
    assert yf_fd_guard.install() is True


def test_yf_fd_guard_close_is_safe_without_yfinance_state():
    """close_yf_caches 在任何状态下都不能抛异常 (善后动作)。"""
    import yf_fd_guard
    n = yf_fd_guard.close_yf_caches()
    assert isinstance(n, int) and n >= 0


def test_yf_fd_guard_patches_make_request():
    """补丁必须真的挂在 YfData._make_request 上 (否则修了等于没修)。"""
    import yf_fd_guard
    yf_fd_guard.install()
    from yfinance.data import YfData
    assert getattr(YfData._make_request, "_yf_fd_guard_wrapped", False) is True


# ── 2. fd_guard ──────────────────────────────────────────────────────────────
def test_fd_guard_snapshot_shape():
    import fd_guard
    snap = fd_guard.fd_snapshot()
    for k in ("used", "soft_limit", "pct", "level", "warn_pct", "critical_pct"):
        assert k in snap
    assert snap["level"] in ("ok", "warn", "critical", "unknown")
    if snap["used"] >= 0 and snap["soft_limit"] > 0:
        assert 0 <= snap["pct"] <= 100


def test_fd_guard_reclaim_never_raises():
    import fd_guard
    assert isinstance(fd_guard.reclaim(), int)


# ── 3. missed_signal_reflection ──────────────────────────────────────────────
def _mk_refl_db(path=":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE trade_reflections_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER UNIQUE, paper_run_id INTEGER, ai_decision_id INTEGER,
        asset TEXT, action TEXT, layer TEXT, sub_layer TEXT, signal_name TEXT,
        ontology_signal TEXT, root_cause TEXT, lesson TEXT, verification TEXT,
        depth_score INTEGER, created_at TEXT, dimension TEXT, subcategory TEXT,
        hit INTEGER, actual_outcome_pct REAL, confidence_at_time REAL,
        reflection_text TEXT, settled INTEGER,
        error_layer TEXT DEFAULT '', origin TEXT DEFAULT '', evidence TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY, target_asset TEXT, suggested_action TEXT,
        sentiment_score REAL, forward_move_pct REAL, ts INTEGER, created_at TEXT,
        paper_trading_run_id INTEGER, missed_signal INTEGER)""")
    conn.execute("""CREATE TABLE news_events (
        id INTEGER PRIMARY KEY, asset TEXT, action TEXT, ts INTEGER)""")
    conn.execute("""CREATE TABLE kline_bars (
        symbol TEXT, ts INTEGER, timeframe TEXT)""")
    conn.execute("""CREATE TABLE ohlcv_daily (
        asset TEXT, ts INTEGER, close REAL)""")
    conn.execute("""CREATE TABLE causal_chains (
        id INTEGER PRIMARY KEY, asset TEXT, enabled INTEGER)""")
    # v482 把"漏检 HOLD"从 trade_reflections_v2 分出来单独记账（HOLD 没有 pnl，
    # 混在反思表里会把反思条数/深度分全部拉平）。测试夹具必须跟上这张表，
    # 否则 _upsert_one 在内存库里直接 `no such table`（2026-11-19 全量复跑抓到）。
    conn.execute("""CREATE TABLE missed_signal_reflections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL UNIQUE,
        asset TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT 'HOLD',
        dimension TEXT NOT NULL DEFAULT '', subcategory TEXT NOT NULL DEFAULT '',
        root_cause TEXT NOT NULL DEFAULT '', lesson TEXT NOT NULL DEFAULT '',
        verification TEXT NOT NULL DEFAULT '',
        move_pct REAL, hit INTEGER, confidence_at_time REAL,
        reflection_text TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT ''
    )""")
    conn.commit()
    return conn


def test_missed_reflection_classify_feed_missing():
    import missed_signal_reflection as msr
    conn = _mk_refl_db()
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','HOLD',0.9,NULL,1700000000,'',NULL,1)")
    conn.commit()
    out = msr.classify_missed_decision(conn, {
        "id": 1, "target_asset": "BTC", "suggested_action": "HOLD",
        "sentiment_score": 0.9, "ts": 1700000000,
    })
    assert out["dimension"] == "C"
    assert out["subcategory"] == "feed_missing"


def test_missed_reflection_classify_signal_delay():
    import missed_signal_reflection as msr
    conn = _mk_refl_db()
    old_ts = 1_000_000_000
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','HOLD',0.5,0.2,?,'',NULL,1)", (old_ts,))
    conn.execute("INSERT INTO kline_bars VALUES ('BTC',?, '1m')", (old_ts * 1000,))
    conn.commit()
    out = msr.classify_missed_decision(conn, {
        "id": 1, "target_asset": "BTC", "ts": old_ts, "sentiment_score": 0.5,
    }, now_ts=old_ts + 40 * 86400)
    assert out["dimension"] == "C"
    assert out["subcategory"] == "signal_delay"


def test_missed_reflection_classify_market_regime_misread():
    import missed_signal_reflection as msr
    conn = _mk_refl_db()
    now = 1_700_000_000
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','HOLD',0.5,0.2,?,'',NULL,1)", (now,))
    conn.execute("INSERT INTO kline_bars VALUES ('BTC',?, '1m')", (now * 1000,))
    conn.execute("INSERT INTO causal_chains VALUES (1,'BTC',1)")
    conn.commit()
    out = msr.classify_missed_decision(conn, {
        "id": 1, "target_asset": "BTC", "ts": now, "sentiment_score": 0.5,
    }, now_ts=now + 60)
    assert out["dimension"] == "A"
    # v326 细分后: 有因果链、但同组没有 ≥3 个资产同窗口同向异动 →
    # 是**单资产形态/强度**没识别出来 (pattern_recognition)，
    # 不是板块级市场状态误读 (market_regime_misread)。
    # 旧断言写死 market_regime_misread, 与 v326 的分支逻辑冲突。
    assert out["subcategory"] == "pattern_recognition"


def test_missed_reflection_writes_and_is_idempotent(tmp_path, monkeypatch):
    """v482 之后：漏检 HOLD 写 missed_signal_reflections（不再写 trade_reflections_v2）。

    _upsert_one 的判定/回看走独立只读连接 msr._conn()，所以这里用文件库 +
    shim 让 _conn() 每次都新开一个连接读同一份数据，行为与生产一致。
    """
    import missed_signal_reflection as msr
    db_file = tmp_path / "refl.db"
    conn = _mk_refl_db(str(db_file))
    now = 1_700_000_000
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','HOLD',0.5,0.2,?,'',NULL,1)", (now,))
    conn.execute("INSERT INTO kline_bars VALUES ('BTC',?, '1m')", (now * 1000,))
    conn.commit()

    class _Shim:
        def get_connection(self):
            c = sqlite3.connect(str(db_file), timeout=30)
            c.row_factory = sqlite3.Row
            return c

    monkeypatch.setattr(msr, "_db", _Shim())

    dec = {"id": 1, "target_asset": "BTC", "suggested_action": "HOLD",
           "sentiment_score": 0.5, "ts": now, "forward_move_pct": 2.0}
    r1 = msr._upsert_one(conn, dec, -1, now)
    assert r1 is not None
    r2 = msr._upsert_one(conn, dec, -1, now)
    assert r2 is not None
    n = conn.execute("SELECT COUNT(*) FROM missed_signal_reflections").fetchone()[0]
    assert n == 1, "重复跑必须幂等（decision_id UNIQUE + ON CONFLICT DO UPDATE）"
    row = conn.execute(
        "SELECT * FROM missed_signal_reflections WHERE decision_id=1").fetchone()
    assert row["dimension"] in ("A", "B", "C")
    assert row["asset"] == "BTC"
    assert row["action"] == "HOLD"
    # v482 的设计约束：漏检行不许再往反思表里塞（那会把反思统计整体拉平）
    assert conn.execute("SELECT COUNT(*) FROM trade_reflections_v2").fetchone()[0] == 0


def test_missed_reflection_does_not_clobber_settlement_row(tmp_path, monkeypatch):
    """v482 之后的契约：漏检 HOLD 只写 missed_signal_reflections，
    trade_reflections_v2 里的结算行（paper_run_id IS NULL）必须原样不动。
    （旧的断言要求 _upsert_one 返回 None —— 那是"同表 upsert"时代的写法；
    分表之后根本不会碰到结算行，所以这里改成验证"两个表各归各"。）
    """
    import missed_signal_reflection as msr
    db_file = tmp_path / "refl2.db"
    conn = _mk_refl_db(str(db_file))
    now = 1_700_000_000
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','HOLD',0.5,0.2,?,'',NULL,1)", (now,))
    conn.execute("INSERT INTO kline_bars VALUES ('BTC',?, '1m')", (now * 1000,))
    conn.execute("""INSERT INTO trade_reflections_v2
        (decision_id, paper_run_id, layer, signal_name, dimension, subcategory)
        VALUES (1, NULL, 'decision', 'settlement_ref', 'A', 'pattern_recognition')""")
    conn.commit()

    class _Shim:
        def get_connection(self):
            c = sqlite3.connect(str(db_file), timeout=30)
            c.row_factory = sqlite3.Row
            return c

    monkeypatch.setattr(msr, "_db", _Shim())

    dec = {"id": 1, "target_asset": "BTC", "ts": now, "forward_move_pct": 2.0}
    msr._upsert_one(conn, dec, -1, now)
    row = conn.execute("SELECT * FROM trade_reflections_v2 WHERE decision_id=1").fetchone()
    assert row["paper_run_id"] is None
    assert row["signal_name"] == "settlement_ref"
    # 漏检记录只落在独立表里
    msr_rows = conn.execute(
        "SELECT COUNT(*) FROM missed_signal_reflections WHERE decision_id=1").fetchone()[0]
    assert msr_rows <= 1


def test_missed_reflection_vocabulary_is_controlled():
    import missed_signal_reflection as msr
    from review import reflection_dimension_map as rdm
    assert set(msr._VALID_DIMS) == {"A", "B", "C"}
    # v338: 不再写死 12 —— v326 加了 A 类 3 个机制小类 + B 类 1 个历史标记
    # (共 16), 写死数量会让**口径本身**的正确变更把测试弄红。
    # 唯一权威 = reflection_dimension_map.SUBCATEGORIES_BY_DIMENSION。
    expect = {s for subs in rdm.SUBCATEGORIES_BY_DIMENSION.values() for s in subs}
    assert msr._VALID_SUBS == expect
    assert "not_a_real_subcategory" not in msr._VALID_SUBS


# ── 4. market_regime 三类新 regime ───────────────────────────────────────────
def _mk_ohlcv_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    conn.execute("CREATE TABLE crowd_topic_daily (topic_key TEXT, day TEXT, heat REAL)")
    conn.execute("""CREATE TABLE macro_snapshots (
        metric_key TEXT, ts INTEGER, value REAL, status TEXT)""")
    return conn


def test_ai_theme_concentration_triggers():
    from market import market_regime as mr
    conn = _mk_ohlcv_db()
    base = 1_700_000_000
    for i in range(200):
        ts = base + i * 86400
        conn.execute("INSERT INTO ohlcv_daily VALUES ('MU',?,?)", (ts, 100.0 * (1.002 ** i)))
        conn.execute("INSERT INTO ohlcv_daily VALUES ('XAU',?,?)", (ts, 100.0 * (1.0005 ** i)))
    conn.commit()
    out = mr._detect_ai_theme_regimes(conn)
    assert any(r["regime"] == "ai_theme_concentration" for r in out)


def test_ai_theme_exit_triggers():
    from market import market_regime as mr
    conn = _mk_ohlcv_db()
    base = 1_700_000_000
    for i in range(200):
        ts = base + i * 86400
        conn.execute("INSERT INTO ohlcv_daily VALUES ('MU',?,?)", (ts, 100.0 * (0.998 ** i)))
        conn.execute("INSERT INTO ohlcv_daily VALUES ('XAU',?,?)", (ts, 100.0 * (1.0005 ** i)))
    conn.commit()
    out = mr._detect_ai_theme_regimes(conn)
    assert any(r["regime"] == "ai_theme_exit" for r in out)


def test_ai_theme_no_output_when_data_insufficient():
    """数据不足 → 不产出 (诚实: 宁可没有, 不编)。"""
    from market import market_regime as mr
    conn = _mk_ohlcv_db()
    conn.execute("INSERT INTO ohlcv_daily VALUES ('MU', 1, 100.0)")
    conn.commit()
    assert mr._detect_ai_theme_regimes(conn) == []


def test_state_fund_requires_all_four_proxies():
    """四条代理缺一不可 —— 缺数据就不产出, 不拿别的指标凑。"""
    from market import market_regime as mr
    conn = _mk_ohlcv_db()
    for i in range(60):
        conn.execute("INSERT INTO macro_snapshots VALUES ('agg.oi',?,?,'ok')",
                     (1_700_000_000 + i * 3600, 100.0 + i))
    conn.commit()
    assert mr._detect_state_fund_inflow(conn) == []


def test_macro_series_filters_unavailable_rows():
    """status='unavailable' 的行 (value=NULL) 绝不能进序列 —— 那是在编数据。"""
    from market import market_regime as mr
    conn = _mk_ohlcv_db()
    conn.execute("INSERT INTO macro_snapshots VALUES ('x', 100, 5.0, 'ok')")
    conn.execute("INSERT INTO macro_snapshots VALUES ('x', 200, NULL, 'unavailable')")
    conn.commit()
    s = mr._macro_series(conn, 'x')
    assert len(s) == 1
    assert s[0][1] == 5.0


# ── 5. discovery 真实相关性 ──────────────────────────────────────────────────
def test_pearson_known_values():
    import discovery as ds
    assert ds._pearson([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert ds._pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert ds._pearson([1, 1, 1], [1, 2, 3]) is None
    assert ds._pearson([1], [2]) is None


def test_fed_impacts_falls_back_to_heuristic():
    """没有真实相关 → 必须回落 heuristic 且 sample_size=0 (如实标明)。"""
    import discovery as ds
    out = ds._compute_fed_impacts(3.0, real_corr={})
    assert out["n_real_correlation"] == 0
    for it in out["impacts"]:
        assert it["method"] == "heuristic"
        assert it["sample_size"] == 0


def test_fed_impacts_uses_real_correlation():
    import discovery as ds
    real = {"BTC": {"correlation": -0.42, "sample_size": 120, "method": "pearson_daily_real"}}
    out = ds._compute_fed_impacts(3.0, real_corr=real)
    btc = [i for i in out["impacts"] if i["symbol"] == "BTC"][0]
    assert btc["correlation"] == -0.42
    assert btc["method"] == "pearson_daily_real"
    assert out["n_real_correlation"] == 1


# ── 6. news_decision_link 多路挂链 ──────────────────────────────────────────
def _mk_link_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE news_events (
        id INTEGER PRIMARY KEY, news_id INTEGER, trade_decision_id INTEGER,
        decision_id INTEGER, decision_space TEXT)""")
    conn.execute("""CREATE TABLE trade_decisions (
        id INTEGER PRIMARY KEY, news_id INTEGER, ai_decision_id INTEGER)""")
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY, news_id INTEGER)""")
    conn.commit()
    return conn


def test_link_by_direct_id():
    from feeds import news_decision_link as nl
    conn = _mk_link_db()
    conn.execute("INSERT INTO news_events (id, news_id) VALUES (10, 555)")
    conn.execute("INSERT INTO trade_decisions (id, news_id) VALUES (100, 10)")
    conn.commit()
    got = nl.link_news_to_decision(conn, 100, 10)
    assert got == 10
    row = conn.execute("SELECT * FROM news_events WHERE id=10").fetchone()
    assert row["trade_decision_id"] == 100
    assert row["decision_space"] == "trade"


def test_link_by_same_source_key():
    """news_events.news_id == trade_decisions.news_id (都指向 raw_news.id)。"""
    from feeds import news_decision_link as nl
    conn = _mk_link_db()
    conn.execute("INSERT INTO news_events (id, news_id) VALUES (11, 777)")
    conn.execute("INSERT INTO trade_decisions (id, news_id) VALUES (101, 777)")
    conn.commit()
    assert nl.link_news_to_decision(conn, 101, 777) == 11


def test_link_via_ai_decisions_two_hop():
    from feeds import news_decision_link as nl
    conn = _mk_link_db()
    conn.execute("INSERT INTO news_events (id, news_id) VALUES (12, 888)")
    conn.execute("INSERT INTO ai_decisions (id, news_id) VALUES (50, 888)")
    conn.execute("INSERT INTO trade_decisions (id, news_id, ai_decision_id) VALUES (102, 888, 50)")
    conn.commit()
    assert nl.link_news_to_decision(conn, 102, None) == 12


def test_link_returns_none_when_no_match():
    """挂不上就**不挂** —— 挂错比不挂更危险。

    注意: 这里两边的 news_id 必须**不同**, 否则同源键匹配本来就应该命中
    (那是正确行为, 见 test_link_by_same_source_key)。
    """
    from feeds import news_decision_link as nl
    conn = _mk_link_db()
    conn.execute("INSERT INTO news_events (id, news_id) VALUES (13, 111)")
    conn.execute("INSERT INTO trade_decisions (id, news_id) VALUES (103, 222)")
    conn.commit()
    assert nl.link_news_to_decision(conn, 103, 222) is None
    row = conn.execute("SELECT * FROM news_events WHERE id=13").fetchone()
    assert row["trade_decision_id"] is None


def test_link_does_not_steal_news_already_linked():
    from feeds import news_decision_link as nl
    conn = _mk_link_db()
    conn.execute("INSERT INTO news_events (id, news_id, trade_decision_id) VALUES (14, 20, 999)")
    conn.execute("INSERT INTO trade_decisions (id, news_id) VALUES (104, 14)")
    conn.commit()
    assert nl.link_news_to_decision(conn, 104, 14) is None
    row = conn.execute("SELECT * FROM news_events WHERE id=14").fetchone()
    assert row["trade_decision_id"] == 999
