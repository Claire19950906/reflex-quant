# -*- coding: utf-8 -*-
"""v453 P1：运维语义修复的单测（僵尸回收 / FF 批量事务 / 门卫路径 / 评审收窄）。
"""

from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import flash_gate
import macro_calendar_ff as ff
from engine import ai_worker


def _news_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE raw_news(id INTEGER PRIMARY KEY, status TEXT,"
              " processing_at TEXT, retry_count INTEGER)")
    return c


def test_reap_stale_processing():
    c = _news_conn()
    c.execute("INSERT INTO raw_news VALUES (1,'PROCESSING',"
              " datetime('now','localtime','-30 minutes'),0)")
    c.execute("INSERT INTO raw_news VALUES (2,'PROCESSING',"
              " datetime('now','localtime','-1 minutes'),0)")
    c.execute("INSERT INTO raw_news VALUES (3,'PROCESSING',"
              " datetime('now','localtime','-30 minutes'),2)")
    c.commit()
    n = ai_worker.reap_stale_processing(conn=c, stale_min=15)
    # id1 / id3 被回收（id3 retry 2 → +1 >= 3 → FAILED），id2 还新鲜不动
    assert n == 2
    rows = dict(c.execute("SELECT id, status FROM raw_news").fetchall())
    assert rows[1] == "PENDING" and rows[2] == "PROCESSING" and rows[3] == "FAILED"


def test_ff_sync_is_batch_and_idempotent(monkeypatch, tmp_path):
    _db_path = str(tmp_path / "ff.db")

    def _open():
        c = sqlite3.connect(_db_path)
        c.execute("""CREATE TABLE IF NOT EXISTS macro_calendar(
            source TEXT, event_id TEXT, country TEXT, title TEXT, impact TEXT, star INTEGER,
            event_time INTEGER, forecast TEXT, previous TEXT, actual TEXT,
            fetched_at TEXT, UNIQUE(source, event_id))""")
        return c

    _open().close()
    monkeypatch.setattr(ff, "fetch_ff_events", lambda: [])
    import db as _db
    monkeypatch.setattr(_db, "get_connection", _open)

    events = [
        {"source": "FF", "event_id": "e1", "title": "CPI", "star": 5, "event_time": 1},
        {"source": "FF", "event_id": "e2", "title": "NFP", "star": 4, "event_time": 2},
        {"source": "FF", "title": "坏数据"},  # 缺 event_id → 跳过，不整体回滚
    ]
    r1 = ff.sync_to_db(events)
    assert r1 == {"fetched": 3, "inserted": 2}
    c = sqlite3.connect(_db_path)
    c.execute("UPDATE macro_calendar SET title='CPI2' WHERE event_id='e1'")
    c.commit()
    c.close()
    r2 = ff.sync_to_db(events)  # 幂等：更新不新增
    assert r2["inserted"] == 2
    c = sqlite3.connect(_db_path)
    assert c.execute("SELECT COUNT(*) FROM macro_calendar").fetchone()[0] == 2
    rows = dict(c.execute("SELECT event_id, title FROM macro_calendar").fetchall())
    c.close()
    assert rows["e1"] == "CPI"  # upsert 覆盖回原值


def test_flash_gate_does_not_write_filter_reason():
    def fake_eval(*a, **kw):
        return {"is_noise": 0, "relevance_score": 0.5, "gate_verdict": "可交易",
                "gate_unavailable": 0,
                "gate": {"strength": 8, "main_asset": "XAU", "verdict": "可交易"}}
    cols = flash_gate.gate_columns("美联储讲话", _evaluate=fake_eval)
    assert cols["filter_reason"] == ""
    assert cols["gate_verdict"] == "可交易"


def test_peer_review_only_for_high_gate_strength(monkeypatch):
    monkeypatch.setattr(ai_worker, "_VERIFY_SAMPLE_RATE", 0.0)
    monkeypatch.setenv("PEER_REVIEW_SAMPLE_LOW_STRENGTH", "0")
    high = {"id": 1, "gate_strength": 8, "source": "x", "relevance_score": 0}
    low = {"id": 2, "gate_strength": 2, "source": "x", "relevance_score": 0}
    assert ai_worker._is_high_value_news(high, "") is True
    assert ai_worker._is_high_value_news(low, "") is False
