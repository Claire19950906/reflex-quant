"""v302+ P2 PR-16: reasoning_review.segment_history 单元测试。

聚焦: 按天聚合 8 段命中率 — 与 compute_review / _present_steps 行为一致。
不依赖外部服务 / 网络。
"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import sqlite3
from datetime import datetime, timedelta

import reasoning_review as rr


def _make_conn():
    """造一个内存 DB,只建 ai_decisions 表的最小子集(segment_history 需要的列)。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            reasoning_path TEXT DEFAULT '',
            asset_reasoning TEXT DEFAULT '',
            factor_ranking TEXT DEFAULT '',
            decision_context TEXT DEFAULT '{}'
        );
    """)
    return conn


def _insert(conn, days_ago: int, hour: int, reasoning: str):
    """插入一条决策: created_at = (now - days_ago) 那天的 hour 点。"""
    dt = datetime.now() - timedelta(days=days_ago)
    dt = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    created = dt.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO ai_decisions (created_at, reasoning) VALUES (?, ?)",
        (created, reasoning),
    )
    conn.commit()


# ──────────────────── 基础聚合 ────────────────────

def test_segment_history_groups_by_date():
    conn = _make_conn()
    # 昨天 2 条 reasoning 都不全(都缺"跨资产资金")
    _insert(conn, 1, 9, "总框架:XYZ。因素定位:ABC。驱动力:QWE。水位博弈:DSA。反共识结论:RTY。逻辑自检:CCC。总结:JJJ。")
    _insert(conn, 1, 14, "总框架:YYY。因素定位:BBB。驱动力:NNN。水位博弈:MMM。反共识结论:QQQ。逻辑自检:LLL。总结:HHH。")
    # 前天 1 条 全 8 段
    _insert(conn, 2, 11, "总框架:AAA。因素定位:BBB。驱动力:CCC。水位博弈:DDD。跨资产资金:EEE。反共识结论:FFF。逻辑自检:GGG。总结:HHH。")

    out = rr.segment_history(days=7, conn=conn)
    assert len(out) == 2

    # 排序按 date asc,前天在前
    by_date = {it["date"]: it for it in out}
    assert "跨资产资金" not in by_date[list(by_date)[0]]["stages"] or by_date[list(by_date)[0]]["stages"]["跨资产资金"] == 1.0  # 前天全段
    # 昨天 2 条都没"跨资产资金"
    yest = [it for it in out if it["count"] == 2][0]
    assert yest["stages"]["跨资产资金"] == 0.0
    assert yest["stages"]["总框架"] == 1.0  # 2/2 都有


def test_segment_history_hit_rate_averages_correctly():
    conn = _make_conn()
    # 3 天前: 1 条全段
    _insert(conn, 3, 10, "总框架:1。因素定位:1。驱动力:1。水位博弈:1。跨资产资金:1。反共识结论:1。逻辑自检:1。总结:1。")
    # 1 天前: 2 条 — 1 全段,1 缺"总结"
    _insert(conn, 1, 10, "总框架:2。因素定位:2。驱动力:2。水位博弈:2。跨资产资金:2。反共识结论:2。逻辑自检:2。总结:2。")
    _insert(conn, 1, 11, "总框架:3。因素定位:3。驱动力:3。水位博弈:3。跨资产资金:3。反共识结论:3。逻辑自检:3。")

    out = rr.segment_history(days=7, conn=conn)
    yest = [it for it in out if it["count"] == 2][0]
    assert yest["stages"]["总结"] == 0.5  # 1/2
    assert yest["stages"]["总框架"] == 1.0  # 2/2


def test_segment_history_empty():
    conn = _make_conn()
    out = rr.segment_history(days=7, conn=conn)
    assert out == []


def test_segment_history_no_conn():
    out = rr.segment_history(days=7, conn=None)
    assert out == []


# ──────────────────── 边界 ────────────────────

def test_segment_history_days_clamps():
    """days=0 强制到 1,days=100 强制到 30。"""
    conn = _make_conn()
    _insert(conn, 0, 10, "总框架:X。")  # 今天
    # days=0 → 1,今天仍在窗内
    out = rr.segment_history(days=0, conn=conn)
    assert len(out) == 1
    # days=100 → 30,今天仍在窗内
    out = rr.segment_history(days=100, conn=conn)
    assert len(out) == 1
    # 钳到上限 30 — 跑 30 次确认不报错
    out = rr.segment_history(days=99999, conn=conn)
    assert len(out) == 1


def test_segment_history_skips_malformed_created_at():
    """created_at 异常时整行跳过,不影响其他行。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO ai_decisions (created_at, reasoning) VALUES (?, ?)",
        ("not-a-date", "总框架:X。"),
    )
    conn.execute(
        "INSERT INTO ai_decisions (created_at, reasoning) VALUES (?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "总框架:Y。"),
    )
    conn.commit()
    out = rr.segment_history(days=7, conn=conn)
    # 只 1 条有效
    assert len(out) == 1
    assert out[0]["count"] == 1


def test_segment_history_consistent_with_compute_review():
    """segment_history 的段位判定与 compute_review 完全一致(共享 _present_steps)。"""
    conn = _make_conn()
    full = "总框架:1。因素定位:1。驱动力:1。水位博弈:1。跨资产资金:1。反共识结论:1。逻辑自检:1。总结:1。"
    _insert(conn, 0, 10, full)
    out = rr.segment_history(days=7, conn=conn)
    assert out[0]["stages"] == {k: 1.0 for k in rr._ANALYSIS_STEPS}

    # 同日再插 1 条缺"跨资产资金" — 当天 2 条,跨资产资金 1/2 = 0.5
    _insert(conn, 0, 11, "总框架:1。因素定位:1。驱动力:1。水位博弈:1。反共识结论:1。逻辑自检:1。总结:1。")
    out = rr.segment_history(days=7, conn=conn)
    assert out[0]["stages"]["跨资产资金"] == 0.5
    assert out[0]["stages"]["总框架"] == 1.0
    assert out[0]["count"] == 2
