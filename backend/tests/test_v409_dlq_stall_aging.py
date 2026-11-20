# -*- coding: utf-8 -*-
"""v409.4: DLQ 里「已经恢复」的 decision_stall 告警要自动结单。

事故背景（2026-11-18 00:08 实测）
--------------------------------
`dlq_events` 有 3 条 `kind='decision_stall', resolved=0`：
  11-16 08:38 / 11-16 22:47 / 11-17 13:14
而决策链早就恢复了（当时最新决策 11-18 00:04）。后果是 quota-sentinel 每 5 分钟
照本宣科报一次 `DLQ未解决=3` —— 告警系统自己变成噪音源，真出事时没人看。

结单判据用**事实**，不是「时间到了」：
  ① 告警存在 ≥ min_age_hours（避免刚报警就自己把自己关掉）；
  ② 当前 ai_decisions 最新一条**晚于**该告警创建时间（= 停摆期确实结束）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python", "engine"))


def _build_db(latest_decision_ts: str | None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE dlq_events (id INTEGER PRIMARY KEY, kind TEXT, source TEXT,"
        " payload TEXT, last_error TEXT, retry_count INTEGER DEFAULT 0,"
        " created_at TEXT, resolved_at TEXT, resolved INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, news_id INTEGER,"
        " created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO dlq_events (id,kind,source,created_at,resolved,last_error)"
        " VALUES (1,'decision_stall','ai_worker',? ,0,'old stall')",
        ((datetime.now() - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    # 刚发生的告警：不该被结单
    conn.execute(
        "INSERT INTO dlq_events (id,kind,source,created_at,resolved,last_error)"
        " VALUES (2,'decision_stall','ai_worker',? ,0,'fresh stall')",
        ((datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    # 非 decision_stall 的告警：不归它管
    conn.execute(
        "INSERT INTO dlq_events (id,kind,source,created_at,resolved,last_error)"
        " VALUES (3,'write_fail','bridge',? ,0,'other kind')",
        ((datetime.now() - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    if latest_decision_ts:
        conn.execute(
            "INSERT INTO ai_decisions (id,news_id,created_at) VALUES (1,1,?)",
            (latest_decision_ts,),
        )
    conn.commit()
    return conn


def _load_fn():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "src_python",
                        "engine", "ai_worker.py")
    spec = importlib.util.spec_from_file_location("_aiw_for_test", path)
    mod = importlib.util.module_from_spec(spec)
    # 只取函数定义，不执行模块主体（模块顶部会加载大量依赖）
    src = open(path, encoding="utf-8").read()
    start = src.index("def _auto_resolve_recovered_stalls")
    end = src.index("def _check_and_alert_decision_stall")
    ns: dict = {}
    exec(compile(src[start:end], "<aging>", "exec"), ns)
    return ns["_auto_resolve_recovered_stalls"]


def test_aged_stall_is_resolved_when_output_recovered():
    fn = _load_fn()
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _build_db(now_local)          # 最新决策就在此刻 → 已恢复
    assert fn(conn, min_age_hours=2.0) == 1
    rows = dict((r[0], r[1]) for r in conn.execute(
        "SELECT id,resolved FROM dlq_events").fetchall())
    assert rows[1] == 1                  # 20h 前那条结单
    assert rows[2] == 0                  # 刚报的不动
    assert rows[3] == 0                  # 非 decision_stall 不动
    note = conn.execute("SELECT last_error FROM dlq_events WHERE id=1").fetchone()[0]
    assert "resolved: auto" in note      # 原文保留 + 追结算说明


def test_no_resolve_when_output_still_stalled():
    """最新决策早于告警时间 → 说明当时就在停摆，不该结单。"""
    fn = _load_fn()
    old = (datetime.now() - timedelta(hours=40)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _build_db(old + ".000+08:00")
    assert fn(conn, min_age_hours=2.0) == 0
    assert conn.execute(
        "SELECT resolved FROM dlq_events WHERE id=1").fetchone()[0] == 0


def test_no_resolve_without_decisions():
    fn = _load_fn()
    conn = _build_db(None)
    assert fn(conn, min_age_hours=2.0) == 0
