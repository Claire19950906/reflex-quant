# -*- coding: utf-8 -*-
"""v404 回归：反思表结算口径回填 + 元反思空转 + 错误码分层

对应 docs/v404-反思机制落地-2026-11-16.md。
三个断言分别锁住三件"以前是假的"事：
1. `hit` 必须以结算表为准（污染价行必须被识别成差异并回填）；
2. `no_baseline_new` 不得写 reflection_cycles（否则"元反思 105 次"全是记账）；
3. 33 个错误码必须都进台账分层，tier 只能是 hard/soft/research。
"""
from __future__ import annotations

import sqlite3

import meta_reflection_loop as mr
import error_code_ledger_build as ecl
from review import reflection_truth_backfill as rtb


def _mem_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY, decision_id INTEGER, paper_run_id INTEGER,
            asset TEXT, action TEXT, exit_price REAL, pnl_pct REAL,
            mfe_pct REAL, mae_pct REAL, hit INTEGER, actual_outcome_pct REAL,
            settle_source TEXT NOT NULL DEFAULT '');
        CREATE TABLE quick_sim_trades (
            id INTEGER PRIMARY KEY, decision_id INTEGER, exit_price REAL,
            pnl_pct REAL, mfe_pct REAL, mae_pct REAL, verdict TEXT,
            settled INTEGER);
        """
    )
    return c


def test_backfill_fixes_polluted_hit_and_is_idempotent():
    conn = _mem_db()
    # 污染行：反思表记 WIN（+2.03%），结算表记 LOSS（-0.13%）
    conn.execute("INSERT INTO trade_reflections_v2 "
                 "(id, decision_id, paper_run_id, asset, action, exit_price, pnl_pct, "
                 " mfe_pct, mae_pct, hit, actual_outcome_pct) "
                 "VALUES (95789, 11816, 2, 'WTI', 'SELL', 86.42, 2.0315, 0.4, -0.2, 1, 2.0315)")
    conn.execute("INSERT INTO quick_sim_trades "
                 "(decision_id, exit_price, pnl_pct, mfe_pct, mae_pct, verdict, settled) "
                 "VALUES (11816, 88.335, -0.1327, 0.2, -0.5, 'LOSS', 1)")
    conn.commit()

    items = rtb.find_mismatches(conn)
    assert len(items) == 1
    assert items[0]["old_hit"] == 1 and items[0]["new_hit"] == 0

    rtb.apply_mismatches(conn, items)
    conn.commit()

    row = conn.execute("SELECT exit_price, pnl_pct, hit, settle_source "
                       "FROM trade_reflections_v2 WHERE id = 95789").fetchone()
    assert abs(row["exit_price"] - 88.335) < 1e-9
    assert abs(row["pnl_pct"] + 0.1327) < 1e-9
    assert row["hit"] == 0
    assert row["settle_source"] == "quick_sim_trades"
    # 幂等：再跑一次不应再发现差异
    assert rtb.find_mismatches(conn) == []


def test_meta_loop_does_not_write_no_baseline_cycles():
    # 守卫在触碰 conn 之前就返回，所以传 None 也必须安全
    assert mr._write_cycle(None, 1, "analysis", "missed_signal", 1,
                           "no_baseline_new", "首次观察", {}, 7) is None
    assert mr._write_cycle(None, 1, "analysis", "missed_signal", 1,
                           "low_sample_skip", "样本不足", {}, 7) is None


def test_error_code_registry_is_tiered_and_bounded():
    assert len(ecl.REGISTRY) >= 33
    tiers = {t for t, _, _ in ecl.REGISTRY.values()}
    assert tiers <= {"hard", "soft", "research"}
    # 这两条是"文本模板合规"检查，必须留在 soft，不能进闸门统计
    assert ecl.REGISTRY["E021"][0] == "soft"
    assert ecl.REGISTRY["E022"][0] == "soft"
