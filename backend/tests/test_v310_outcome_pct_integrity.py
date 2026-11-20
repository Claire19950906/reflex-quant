#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v310 P1 回归测试 —— 结算收益(实际结果)口径与历史污染防护。

背景 (2026-10-29 事故)
----------------------
2026-10-26T20:56:37 有一批结算行把 trade_reflections_v2.actual_outcome_pct 写成
256.8 / 269.8 / 399.5 这种**物理上不可能**的 5 日收益, 把「真实持仓业绩」均值
从 +1.45% 拉到 +117.42%。

三道防线, 本文件逐条钉住:
  1. 取价口径唯一: db.forward_price / db.forward_outcome_pct —— 分钟线优先、
     日线回退(与 resolve_entry_price 同源), bridge / decision_engine / 回填脚本
     三处共用同一实现。
  2. 写入前合理性校验: db.is_implausible_outcome —— |收益| > 90% 一律拒写。
  3. 历史数据修复范围: 只同步结算行(paper_run_id IS NULL), 绝不碰 worker 行
     (worker 的 pnl_pct 是比例、actual_outcome_pct 是百分数, 单位不同)。

用例不触网、不碰生产库(全部用 :memory: 库)。
"""
import os
import sqlite3
import sys

import pytest

_SRC_PYTHON = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python")
)
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

import db as _db
import fix_polluted_outcome_pct as _fix


T0 = 1_700_000_000
DAY = 86400


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE ohlcv_intraday (asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE market_ticks (symbol TEXT, ts INTEGER, price REAL);
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER UNIQUE,
            paper_run_id INTEGER,
            asset TEXT, action TEXT,
            entry_price REAL, pnl_pct REAL, actual_outcome_pct REAL,
            hit INTEGER
        );
        """
    )
    yield c
    c.close()


# ── 1. 取价口径: 分钟线优先, 日线回退 ────────────────────────────────────────
def test_forward_price_prefers_intraday(conn):
    """分钟线与日线价差巨大时, forward 价必须取分钟线(与 entry 同源)。"""
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC', ?, 100.0)", (T0 + 4 * DAY,))
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 900.0)", (T0 + 4 * DAY,))
    assert _db.forward_price(conn, "BTC", T0, settle_days=5) == 100.0


def test_forward_price_falls_back_to_daily(conn):
    """分钟线缺失时回退日线。"""
    conn.execute("INSERT INTO ohlcv_daily VALUES ('ETH', ?, 200.0)", (T0 + 4 * DAY,))
    assert _db.forward_price(conn, "ETH", T0, settle_days=5) == 200.0


def test_forward_outcome_pct_same_source(conn):
    """entry 与 forward 同源 → 收益不被口径噪声放大。"""
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC', ?, 101.0)", (T0 + 4 * DAY,))
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC', ?, 900.0)", (T0 + 4 * DAY,))
    pct = _db.forward_outcome_pct(conn, "BTC", T0, 100.0, settle_days=5)
    assert pct == pytest.approx(1.0)


def test_forward_outcome_pct_none_without_data(conn):
    assert _db.forward_outcome_pct(conn, "BTC", T0, 100.0) is None
    assert _db.forward_outcome_pct(conn, "BTC", T0, None) is None
    assert _db.forward_outcome_pct(conn, "BTC", T0, 0) is None


def test_forward_price_ignores_market_ticks(conn):
    """market_ticks 是最粗的口径, 不能参与 forward 取价(与 entry 不同源)。"""
    conn.execute("INSERT INTO market_ticks VALUES ('BTC', ?, 999.0)", (T0 + 4 * DAY,))
    assert _db.forward_price(conn, "BTC", T0, settle_days=5) is None


# ── 2. 合理性上限: |收益| > 90% 拒写 ─────────────────────────────────────────
def test_is_implausible_outcome():
    assert _db.is_implausible_outcome(399.5) is True
    assert _db.is_implausible_outcome(-256.8) is True
    assert _db.is_implausible_outcome(90.0) is False
    assert _db.is_implausible_outcome(4.013) is False
    assert _db.is_implausible_outcome(None) is False


def test_max_plausible_constant_is_90():
    assert _db.MAX_PLAUSIBLE_OUTCOME_PCT == 90.0


# ── 3. 历史修复范围: 只动结算行 ─────────────────────────────────────────────
def _seed(conn):
    # 结算行: actual 被污染, pnl_pct 是权威值
    conn.execute(
        "INSERT INTO trade_reflections_v2 "
        "(decision_id, paper_run_id, asset, action, entry_price, pnl_pct, "
        " actual_outcome_pct, hit) VALUES "
        "(1, NULL, 'WTI', 'REJECTED', 90.5, 2.066, 399.5, 0), "
        "(2, NULL, 'ETH', 'REJECTED', 2434.1, 4.013, 256.8, 0), "
        "(3, NULL, 'BTC', 'BUY', 100.0, 1.5, 1.5, 1), "
        # worker 行: 两列单位不同(比例 vs 百分数), 绝不能被覆盖
        "(4, 77, 'BTC', 'BUY', 100.0, 2.568, 256.8, 1)"
    )
    conn.commit()


def test_sync_only_touches_settlement_rows(conn):
    _seed(conn)
    n = _fix.sync_settlement_outcomes(conn)
    assert n == 2
    rows = {r["decision_id"]: r for r in conn.execute(
        "SELECT * FROM trade_reflections_v2")}
    # 结算行: actual_outcome_pct 已同步为 pnl_pct
    assert rows[1]["actual_outcome_pct"] == pytest.approx(2.066)
    assert rows[2]["actual_outcome_pct"] == pytest.approx(4.013)
    # 本来就一致的行不动
    assert rows[3]["actual_outcome_pct"] == pytest.approx(1.5)
    # worker 行: 原样保留
    assert rows[4]["paper_run_id"] == 77
    assert rows[4]["actual_outcome_pct"] == pytest.approx(256.8)
    assert rows[4]["pnl_pct"] == pytest.approx(2.568)


def test_sync_is_idempotent(conn):
    _seed(conn)
    _fix.sync_settlement_outcomes(conn)
    assert _fix.sync_settlement_outcomes(conn) == 0


def test_sync_ignores_rows_without_pnl(conn):
    conn.execute(
        "INSERT INTO trade_reflections_v2 "
        "(decision_id, paper_run_id, asset, pnl_pct, actual_outcome_pct) "
        "VALUES (9, NULL, 'XAU', NULL, 399.5)"
    )
    conn.commit()
    assert _fix.sync_settlement_outcomes(conn) == 0
    row = conn.execute("SELECT * FROM trade_reflections_v2 WHERE decision_id=9").fetchone()
    assert row["actual_outcome_pct"] == pytest.approx(399.5)


# ── 4. 写入点源码守卫: 结算写入不得再碰 actual_outcome_pct ──────────────────
_ONEOFF = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "oneoff")
)


def _src(name):
    path = os.path.join(_SRC_PYTHON, name)
    if not os.path.exists(path):
        path = os.path.join(_ONEOFF, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_bridge_settlement_writer_does_not_write_actual_outcome_pct():
    """bridge 的结算反思写入器不得写 actual_outcome_pct(那是污染源)。"""
    src = _src("bridge_server.py")
    start = src.index("def _insert_settlement_reflection")
    end = src.index("def ensure_trade_decisions_table")
    body = src[start:end]
    assert "actual_outcome_pct" not in body


def test_bridge_settlement_writers_have_implausible_guard():
    src = _src("bridge_server.py")
    assert src.count("is_implausible_outcome") >= 3, "三个结算写入点都应加合理性守卫"


def test_decision_engine_uses_shared_price_source_and_guard():
    src = _src("decision_engine.py")
    assert "forward_price" in src, "decision_engine 必须走 db.forward_price(同源取价)"
    assert "is_implausible_outcome" in src, "decision_engine.settle_one 必须有合理性守卫"


def test_backfill_script_uses_shared_price_source():
    src = _src("backfill_settlement_outcomes.py")
    assert "forward_outcome_pct" in src, "回填脚本必须走 db.forward_outcome_pct(同一口径)"


def test_backfill_syncs_reflection_actual_outcome_pct():
    """回填必须同时同步 pnl_pct 与 actual_outcome_pct。

    历史教训: v310 P0 回填只写了 pnl_pct, actual_outcome_pct 仍是 2026-10-26
    的污染值 → 真实持仓业绩被带偏到 +117%。这条钉住"三处同源同值"。
    """
    src = _src("backfill_settlement_outcomes.py")
    assert "actual_outcome_pct=?" in src, "回填脚本必须同步 actual_outcome_pct"
    assert "paper_run_id IS NULL" in src, "同步必须限定结算行, 不碰 paper worker 行"
