"""FIX-8 第 4 步: 决策链停摆告警（离线单测）。"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import pipeline_stall_guard as psg

NOW = datetime(2026, 11, 9, 19, 30, tzinfo=psg.SHANGHAI)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, created_at TEXT)")
    conn.execute("CREATE TABLE raw_news (id INTEGER PRIMARY KEY, timestamp TEXT)")
    return conn


def _iso(dt):
    return dt.isoformat(timespec="milliseconds")


def test_detects_14h_stall_with_mixed_time_formats():
    """真实场景复现：ISO+offset 与空格格式混存，MAX(created_at) 会骗人。"""
    conn = _db()
    conn.execute("INSERT INTO ai_decisions VALUES (1, '2026-10-10 19:14:58')")
    conn.execute("INSERT INTO ai_decisions VALUES (10608, ?)", (_iso(NOW - timedelta(hours=14, minutes=27)),))
    for i in range(5):
        conn.execute("INSERT INTO raw_news VALUES (?, ?)", (i + 1, _iso(NOW - timedelta(hours=i))))
    conn.commit()

    d = psg.check_stall(conn, now=NOW)
    assert d["stalled"] is True
    assert d["last_decision_id"] == 10608      # 按 id 取最新，不被字符串比较带偏
    assert d["idle_hours"] == 14.45
    assert d["news_since"] == 5                # 停摆期间还在进新闻
    conn.close()


def test_not_stalled_when_recent():
    conn = _db()
    conn.execute("INSERT INTO ai_decisions VALUES (1, ?)", (_iso(NOW - timedelta(minutes=20)),))
    conn.commit()
    d = psg.check_stall(conn, now=NOW)
    assert d["stalled"] is False
    assert d["idle_hours"] == 0.33
    conn.close()


def test_empty_table_is_reported_not_crashed():
    conn = _db()
    d = psg.check_stall(conn, now=NOW)
    assert d["stalled"] is False           # 无记录不当作停摆（避免首装即误报）
    assert d["last_decision"] is None
    assert "没有任何决策" in psg.format_report(d)
    conn.close()


def test_space_format_parsed_as_shanghai_time():
    """裸串不能被当成 UTC，否则会凭空多出/少掉 8 小时。"""
    conn = _db()
    conn.execute("INSERT INTO ai_decisions VALUES (1, '2026-11-09 05:03:36')")
    conn.commit()
    d = psg.check_stall(conn, now=NOW)
    assert abs(d["idle_hours"] - 14.44) < 0.02
    conn.close()


def test_threshold_is_configurable():
    conn = _db()
    conn.execute("INSERT INTO ai_decisions VALUES (1, ?)", (_iso(NOW - timedelta(hours=3)),))
    conn.commit()
    assert psg.check_stall(conn, now=NOW)["stalled"] is True
    assert psg.check_stall(conn, now=NOW, stall_hours=6)["stalled"] is False
    conn.close()


def test_report_text_states_the_key_point():
    conn = _db()
    conn.execute("INSERT INTO ai_decisions VALUES (1, ?)", (_iso(NOW - timedelta(hours=14)),))
    conn.execute("INSERT INTO raw_news VALUES (1, ?)", (_iso(NOW - timedelta(hours=1)),))
    conn.commit()
    text = psg.format_report(psg.check_stall(conn, now=NOW))
    assert "[STALL]" in text
    assert "不是没数据，是没处理" in text
    conn.close()
