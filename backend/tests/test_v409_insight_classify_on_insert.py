# -*- coding: utf-8 -*-
"""v409 — 洞察"生成即判定"。

v399 把洞察分类放在反思 worker 每轮开头（5 分钟后才打标），属于事后筛。
本版把同一把尺子接到写入口：INSERT 当刻就写 param_key / direction / window_h /
observe_only，晋升链路读到的永远是已判定的行。

验收：
1. 带 "[参数键] + 方向" 的洞察 → observe_only=0，参数键与窗口落库；
2. 没有参数键 → observe_only=1 且原因可读（不编"看起来可测"）；
3. 已判定过的行再调用不改写（幂等）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import insight_hypothesis as ih

_DDL = """
CREATE TABLE review_insights (
  id INTEGER PRIMARY KEY,
  rule TEXT NOT NULL,
  asset TEXT DEFAULT '',
  scope TEXT DEFAULT 'all',
  active INTEGER DEFAULT 1,
  source_samples INTEGER DEFAULT 0,
  origin TEXT DEFAULT 'real',
  kind INTEGER DEFAULT 1,
  counter_factual TEXT DEFAULT '',
  created_at TEXT DEFAULT '2026-11-17 00:00:00',
  manual_deactivated INTEGER DEFAULT 0,
  verification_score REAL DEFAULT 0,
  verification_verdict TEXT DEFAULT '',
  verification_method TEXT DEFAULT '',
  verification_sample_n INTEGER DEFAULT 0,
  verification_kline_n INTEGER DEFAULT 0,
  verified_at TEXT DEFAULT '',
  insight_hypothesis TEXT DEFAULT '',
  insight_param_key TEXT DEFAULT '',
  insight_param_bucket TEXT DEFAULT '',
  insight_direction INTEGER DEFAULT 0,
  insight_window_h INTEGER DEFAULT 0,
  observe_only INTEGER DEFAULT 0,
  observe_reason TEXT DEFAULT '',
  insight_classified_at TEXT DEFAULT ''
)
"""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(_DDL)
    return conn


def test_executable_insight_classified_on_insert():
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO review_insights (rule, asset, source_samples, created_at) "
        "VALUES (?, 'BTC', 40, '2026-11-17 00:00:00')",
        ("[score.buy_min_score] 消息冲击后 72 小时内应提高开仓门槛",),
    )
    assert ih.classify_one(conn, cur.lastrowid) == 1
    row = conn.execute(
        "SELECT insight_param_key, insight_direction, insight_window_h, observe_only "
        "FROM review_insights WHERE id=?", (cur.lastrowid,)).fetchone()
    assert row[0] == "score.buy_min_score"
    assert row[1] == 1                     # 提高 → +1
    assert row[2] == 72
    assert row[3] == 0, "可映射 + 有方向 → 不该是 observe_only"


def test_unmappable_insight_is_observe_only():
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO review_insights (rule, asset, created_at) "
        "VALUES ('注意样本少，消息冲击时别追高', 'XAU', '2026-11-17 00:00:00')")
    assert ih.classify_one(conn, cur.lastrowid) == 1
    row = conn.execute(
        "SELECT observe_only, observe_reason FROM review_insights WHERE id=?",
        (cur.lastrowid,)).fetchone()
    assert row[0] == 1
    assert "参数键" in row[1]


def test_classify_one_is_idempotent():
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO review_insights (rule, asset, created_at) "
        "VALUES ('[score.signal_threshold] 应降低信号门槛', 'BTC', '2026-11-17 00:00:00')")
    assert ih.classify_one(conn, cur.lastrowid) == 1
    first = conn.execute("SELECT observe_only, insight_classified_at "
                         "FROM review_insights WHERE id=?", (cur.lastrowid,)).fetchone()
    assert ih.classify_one(conn, cur.lastrowid) == 0
    second = conn.execute("SELECT observe_only, insight_classified_at "
                          "FROM review_insights WHERE id=?", (cur.lastrowid,)).fetchone()
    assert first == second
