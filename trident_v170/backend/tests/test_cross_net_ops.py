"""v170 cross_net 全网溯源日常运维工具 — 单测。

覆盖 scripts/watch_cross_net.py / sync_prompts.py / daily_audit.py 三个工具的核心函数：
  - _audit_label 各种状态 → 正确标签
  - _report 缺失字段识别
  - _candidate_news 数据候选筛选

跑法：python3 -m pytest tests/test_cross_net_ops.py -v
"""
from __future__ import annotations

import os
import sqlite3
import sys

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import pytest

from daily_audit import _audit_label, _candidate_news
from sync_prompts import _keyword_presence, KEY_FIELDS
from watch_cross_net import _ts_delta_label, _pct


# ────────────── _audit_label ──────────────


class _Row:
    """假 Row（dataclass 模拟 sqlite3.Row）"""
    def __init__(self, d):
        self._d = d
    def __getitem__(self, k):
        return self._d[k]
    def __contains__(self, k):
        return k in self._d
    def keys(self):
        return self._d.keys()


def test_audit_label_exact_ok():
    r = _Row({"cross_net_first_seen_ts": "2026-09-19 17:34:16",
              "cross_net_source": "美联储官网新闻稿",
              "net_origin_info": "美联储9月19日22:00官网新闻稿"})
    assert _audit_label(r) == "✅ 精确"


def test_audit_label_ambiguous_with_or():
    r = _Row({"cross_net_first_seen_ts": "AMBIGUOUS",
              "cross_net_source": "候选1: 美联储官网 / 候选2: 路透社",
              "net_origin_info": "可能 FOMC 也可能 OP"})
    assert _audit_label(r) == "🔶 AMBIGUOUS"


def test_audit_label_ambiguous_no_or():
    r = _Row({"cross_net_first_seen_ts": "AMBIGUOUS",
              "cross_net_source": "美联储官网",
              "net_origin_info": ""})
    assert "AMBIGUOUS" in _audit_label(r)
    assert "❓" in _audit_label(r)


def test_audit_label_empty_everything():
    r = _Row({"cross_net_first_seen_ts": "",
              "cross_net_source": "",
              "net_origin_info": ""})
    assert "❌" in _audit_label(r)


def test_audit_label_only_noi():
    r = _Row({"cross_net_first_seen_ts": "",
              "cross_net_source": "",
              "net_origin_info": "金十数据9月20日08:30快讯"})
    assert "🔶" in _audit_label(r)


def test_audit_label_time_too_old():
    r = _Row({"cross_net_first_seen_ts": "1989-01-01 00:00:00",
              "cross_net_source": "未知",
              "net_origin_info": ""})
    assert "过早" in _audit_label(r)


def test_audit_label_time_format_bad():
    r = _Row({"cross_net_first_seen_ts": "yesterday",
              "cross_net_source": "未知",
              "net_origin_info": ""})
    assert "⚠️" in _audit_label(r)


# ────────────── _candidate_news ──────────────


@pytest.fixture()
def audit_db(tmp_path):
    db_path = str(tmp_path / "audit.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE raw_news (
            id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT, status TEXT,
            relevance_score REAL, cross_net_first_seen_ts TEXT,
            cross_net_source TEXT, net_origin_info TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def test_candidate_news_filters_done_and_relevance(audit_db):
    c = sqlite3.connect(audit_db)
    c.row_factory = sqlite3.Row
    c.execute("""INSERT INTO raw_news VALUES
        (1, 'A', '2026-09-20T01:00:00+08:00', 'DONE', 0.9, '', '', ''),
        (2, 'B', '2026-09-20T01:00:00+08:00', 'NOISE', 0.95, '', '', ''),
        (3, 'C', '2026-09-20T01:00:00+08:00', 'DONE', NULL, '', '', ''),
        (4, 'D', '2026-09-20T01:00:00+08:00', 'DONE', 0.7, '', '', ''),
        (5, 'E', '2019-01-01T00:00:00+08:00', 'DONE', 0.99, '', '', '')
    """)
    c.commit()
    c.close()

    conn = sqlite3.connect(audit_db)
    conn.row_factory = sqlite3.Row
    rows = _candidate_news(conn, hours=48, top=10, since_id=0)
    assert {r["id"] for r in rows} == {1, 4}  # NOISE 和 NULL relevance 和 5 年前都排除
    conn.close()


def test_candidate_news_top_orders_by_relevance(audit_db):
    c = sqlite3.connect(audit_db)
    c.execute("""INSERT INTO raw_news VALUES
        (1, 'A', '2026-09-20T01:00:00+08:00', 'DONE', 0.5, '', '', ''),
        (2, 'B', '2026-09-20T01:00:00+08:00', 'DONE', 0.9, '', '', ''),
        (3, 'C', '2026-09-20T01:00:00+08:00', 'DONE', 0.7, '', '', '')
    """)
    c.commit()
    c.close()

    conn = sqlite3.connect(audit_db)
    conn.row_factory = sqlite3.Row
    rows = _candidate_news(conn, hours=48, top=2, since_id=0)
    assert [r["id"] for r in rows] == [2, 3]
    conn.close()


# ────────────── sync_prompts._keyword_presence ──────────────


def test_keyword_presence_identifies_missing_fields():
    code_text = """
        - cross_net_first_seen_ts: ...
        - cross_net_source: ...
        - net_origin_info: ...
        - data_anchor_ts: ...
    """
    db_text = """
        - cross_net_first_seen_ts: ...
        - cross_net_source: ...
    """
    code_kw = _keyword_presence(code_text)
    db_kw = _keyword_presence(db_text)
    missing = [k for k in KEY_FIELDS if code_kw[k] and not db_kw[k]]
    assert "net_origin_info" in missing
    assert "data_anchor_ts" in missing
    assert "cross_net_first_seen_ts" not in missing


# ────────────── watch_cross_net helpers ──────────────


def test_ts_delta_minutes():
    # db_ts=100, now=160 → delta=60 → "   1m"（4 字符宽右对齐）
    assert _ts_delta_label(100, 160) == "   1m"


def test_ts_delta_hours():
    # db_ts=1000, now=4700 → delta=3700 → "  1h01m"
    assert _ts_delta_label(1000, 4700).endswith("m")
    assert "h" in _ts_delta_label(1000, 4700)


def test_ts_delta_days():
    # db_ts=1000, now=10_000_000 → delta=~10000000s ≈ 115d
    out = _ts_delta_label(1000, 10_000_000)
    assert "d" in out


def test_ts_delta_none():
    assert _ts_delta_label(None, 0) == "   ?"


def test_pct_zero_denominator():
    assert _pct(0, 0) == "n/a"


def test_pct_normal():
    assert _pct(7, 10) == "70%"