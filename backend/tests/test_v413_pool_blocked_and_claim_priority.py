#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v413：pool-blocked 不再被看门狗当"卡死"强杀 + 认领按门卫结论优先。

背景（2026-11-17 实测）
----------------------
1) 主循环进入 pool-blocked = 上游 LLM **全池熔断**（quota/auth/net 全 closed）。
   此时进程健康、只是没活干，重启对上游故障毫无作用，只会丢掉在办批次。
   实测：23:35 停在 pool-blocked 11,400s → 02:18 被强杀；04:00-06:00 又零产出；
   engine_watchdog.log 累计强杀 859 次。
2) 认领只按 ts DESC，门卫已判「可交易」的新闻和「未判定」的一起排队，
   积压时永远排不到（当日 62 条可交易只分析了 8 条）。
"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src_python"))

import engine_watchdog as wd


# ── pool-blocked 的判死阈值 ─────────────────────────────────────────────

def test_pool_blocked_one_hour_is_not_hang():
    """熔断 1 小时：绝不能判死（重启对上游故障无效）。"""
    hung, hard, reason, restart = wd.decide(
        True, age=5.0, uptime=3600.0, threshold=600.0,
        step_age=3600.0, step="pool-blocked")
    assert not hung and not restart
    assert "熔断" in reason


def test_pool_blocked_beyond_pool_threshold_is_hang():
    """熔断超过 6 小时仍不恢复：才允许当"真挂"处理。"""
    hung, _, _, restart = wd.decide(
        True, age=5.0, uptime=3600.0, threshold=600.0,
        step_age=wd.POOL_BLOCKED_HANG_AFTER_S + 600, step="pool-blocked")
    assert hung and restart


def test_analyze_still_judged_hang_at_30min():
    """回归：pool-blocked 放宽**不能**连累正常步骤的判死线。"""
    hung, _, _, restart = wd.decide(
        True, age=5.0, uptime=3600.0, threshold=600.0,
        step_age=2400.0, step="analyze")
    assert hung and restart


def test_analyze_within_threshold_is_normal():
    # STEP_HANG_AFTER_S 现为固定 700s（不再等于 3×threshold），
    # 所以"阈值内不判死"这条要靠显式 hang_after 表达，不能吃默认值。
    hung, _, _, restart = wd.decide(
        True, age=5.0, uptime=3600.0, threshold=600.0,
        step_age=1200.0, step="analyze", hang_after=1800.0)
    assert not hung and not restart


def test_step_name_is_optional_for_backward_compat():
    """老调用方不传 step 时，行为与 v398c 一致。"""
    hung, _, _, restart = wd.decide(
        True, age=5.0, uptime=3600.0, threshold=600.0, step_age=2400.0)
    assert hung and restart


# ── 认领优先级 ─────────────────────────────────────────────────────────

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE raw_news (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      status TEXT DEFAULT 'PENDING',
      is_noise INTEGER DEFAULT 0,
      gate_unavailable INTEGER DEFAULT 0,
      gate_verdict TEXT DEFAULT '',
      processing_at TEXT,
      retry_count INTEGER DEFAULT 0,
      ts INTEGER
    );
    CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, news_id INTEGER);
    """)
    yield c
    c.close()


def test_tradeable_claimed_before_newer_unjudged(conn):
    """门卫判「可交易」的老新闻，必须先于更新的「未判定」被认领。"""
    from engine import ai_worker
    now = int(time.time())
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,gate_verdict,ts)"
                 " VALUES('PENDING',0,0,'可交易',?)", (now - 5400,))
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,gate_verdict,ts)"
                 " VALUES('PENDING',0,0,'未判定',?)", (now - 120,))

    # 只取 1 条：ORDER BY 决定的是「先拿谁」，而 UPDATE … RETURNING 的行序
    # 由扫描决定、不保证跟随子查询排序 —— 所以断言要落在「取到了哪一条」。
    rows = conn.execute(
        ai_worker._BATCH_CLAIM_FRESH_SQL,
        (6, now - 6 * 3600,
         0, 0,  # v444: 断档水位（0 = 不限制，本用例不测水位）
         0, 0, now - 2700, 1)).fetchall()
    assert [r["gate_verdict"] for r in rows] == ["可交易"]


def test_same_tier_keeps_newest_first(conn):
    """同档内仍按 ts DESC —— v323 的实时性语义不能被 v413 改掉。"""
    from engine import ai_worker
    now = int(time.time())
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,gate_verdict,ts)"
                 " VALUES('PENDING',0,0,'未判定',?)", (now - 3000,))
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,gate_verdict,ts)"
                 " VALUES('PENDING',0,0,'未判定',?)", (now - 60,))

    rows = conn.execute(
        ai_worker._BATCH_CLAIM_FRESH_SQL,
        (6, now - 6 * 3600,
         0, 0,  # v444: 断档水位（0 = 不限制，本用例不测水位）
         0, 0, now - 2700, 1)).fetchall()
    assert [r["id"] for r in rows] == [2], "同档内必须最新优先（v323 语义）"


# ── v415：启动复位「先捞回已落库的」 ──────────────────────────────────

def test_startup_reset_recovers_rows_that_already_have_decisions(conn):
    """已有决策行的 PROCESSING：重启后必须标 DONE，不能被退回 PENDING 重跑。"""
    from engine import ai_worker
    now = int(time.time())
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,processing_at,ts)"
                 " VALUES('PROCESSING',0,0,'2026-11-17 09:00:00',?)", (now,))
    conn.execute("INSERT INTO ai_decisions(id,news_id) VALUES(1,1)")

    recovered = conn.execute(ai_worker._STARTUP_RECOVER_DONE_SQL).rowcount
    assert recovered == 1
    st = conn.execute("SELECT status, processing_at FROM raw_news WHERE id=1").fetchone()
    assert st["status"] == "DONE"
    assert st["processing_at"] is None


def test_startup_reset_only_requeues_rows_without_decisions(conn):
    """没落成决策的 PROCESSING：仍要退回 PENDING（否则永久卡死不被认领）。"""
    from engine import ai_worker
    now = int(time.time())
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,processing_at,ts)"
                 " VALUES('PROCESSING',0,0,'2026-11-17 09:00:00',?)", (now,))

    reset = conn.execute(ai_worker._STARTUP_RESET_PENDING_SQL).rowcount
    assert reset == 1
    st = conn.execute("SELECT status FROM raw_news WHERE id=1").fetchone()
    assert st["status"] == "PENDING"


def test_startup_reset_two_sql_partition_processing_rows(conn):
    """两条 SQL 必须**互斥且穷尽**：每条 PROCESSING 有且只有一个归宿。"""
    from engine import ai_worker
    now = int(time.time())
    for _ in range(3):
        conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,processing_at,ts)"
                     " VALUES('PROCESSING',0,0,'2026-11-17 09:00:00',?)", (now,))
    conn.execute("INSERT INTO ai_decisions(id,news_id) VALUES(1,1)")
    conn.execute("INSERT INTO ai_decisions(id,news_id) VALUES(2,2)")

    recovered = conn.execute(ai_worker._STARTUP_RECOVER_DONE_SQL).rowcount
    reset = conn.execute(ai_worker._STARTUP_RESET_PENDING_SQL).rowcount
    assert (recovered, reset) == (2, 1)
    left = conn.execute(
        "SELECT COUNT(*) c FROM raw_news WHERE status='PROCESSING'").fetchone()["c"]
    assert left == 0, "重启后不能有行滞留在 PROCESSING"


def test_startup_fixes_pending_rows_that_already_have_decisions(conn):
    """PENDING 但决策已落库的行：必须在启动时扶正为 DONE，别一直占"待分析"计数。"""
    from engine import ai_worker
    now = int(time.time())
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,processing_at,ts)"
                 " VALUES('PENDING',0,0,'2026-11-17 09:00:00',?)", (now,))
    conn.execute("INSERT INTO ai_decisions(id,news_id) VALUES(1,1)")

    fixed = conn.execute(ai_worker._STARTUP_FIX_PENDING_WITH_DECISION_SQL).rowcount
    assert fixed == 1
    st = conn.execute("SELECT status, processing_at FROM raw_news WHERE id=1").fetchone()
    assert st["status"] == "DONE"
    assert st["processing_at"] is None


def test_startup_fix_ignores_pending_rows_without_decisions(conn):
    """没决策的正常待分析新闻绝不能被扶正（否则直接丢队列）。"""
    from engine import ai_worker
    now = int(time.time())
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,processing_at,ts)"
                 " VALUES('PENDING',0,0,NULL,?)", (now,))

    fixed = conn.execute(ai_worker._STARTUP_FIX_PENDING_WITH_DECISION_SQL).rowcount
    assert fixed == 0
    st = conn.execute("SELECT status FROM raw_news WHERE id=1").fetchone()
    assert st["status"] == "PENDING"


# ── v418：已过建仓窗口的可交易，不再抢占队头 ─────────────────────────────

def test_expired_tradeable_does_not_block_fresh_tradeable(conn):
    """实测 2026-11-17 10:00：45 条「可交易」平均已等 148 分钟，全部超过
    45 分钟建仓窗口 —— 它们排档 0，把**新进的**可交易信号挤到后面一起过期。
    分档后：窗口内的可交易必须排第一。"""
    from engine import ai_worker
    now = int(time.time())
    # 老的可交易（148 分钟前，已过期）
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,gate_verdict,ts)"
                 " VALUES('PENDING',0,0,'可交易',?)", (now - 148 * 60,))
    # 新的可交易（9 分钟前，窗口内）
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,gate_verdict,ts)"
                 " VALUES('PENDING',0,0,'可交易',?)", (now - 9 * 60,))

    rows = conn.execute(
        ai_worker._BATCH_CLAIM_FRESH_SQL,
        (6, now - 6 * 3600,
         0, 0,  # v444: 断档水位（0 = 不限制，本用例不测水位）
         0, 0, now - 2700, 1)).fetchall()
    assert [r["id"] for r in rows] == [2], "窗口内的可交易必须先被认领"


def test_fresh_tradeable_beats_unjudged(conn):
    """窗口内可交易 vs 更新的未判定：可交易仍优先（这是 v413 的语义，不能丢）。"""
    from engine import ai_worker
    now = int(time.time())
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,gate_verdict,ts)"
                 " VALUES('PENDING',0,0,'可交易',?)", (now - 600,))
    conn.execute("INSERT INTO raw_news(status,is_noise,gate_unavailable,gate_verdict,ts)"
                 " VALUES('PENDING',0,0,'未判定',?)", (now - 60,))

    rows = conn.execute(
        ai_worker._BATCH_CLAIM_FRESH_SQL,
        (6, now - 6 * 3600,
         0, 0,  # v444: 断档水位（0 = 不限制，本用例不测水位）
         0, 0, now - 2700, 1)).fetchall()
    assert [r["gate_verdict"] for r in rows] == ["可交易"]
