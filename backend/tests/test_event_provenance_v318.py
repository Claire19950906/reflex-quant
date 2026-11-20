"""v318 — 事件时间溯源回归测试。

背景（线上实测）：event_time_candidates.source 的 CHECK 约束只允许 5 个值
(platform_published_at / ai_inferred / cross_net / human_annotated / other)，
而 ai_worker 实际写 'ai_worker_review'、回填脚本写 'backfill_v181'
→ INSERT 触发 CHECK 失败 → event_atom 用 `except Exception: return -1`
把异常吞掉 → **事件时间候选表长期只有 2 行**，事件时间溯源形同虚设。

覆盖：
  · 新建库允许全部实际在用的 source
  · 旧库（老 CHECK）迁移后数据不丢、且允许新 source
  · 未知 source 不再让 INSERT 静默失败（映射 other + 原值写进 reasoning）
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import event_atom

# 旧版约束（迁移前线上真实存在的定义）
_OLD_TABLE = """
CREATE TABLE event_time_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_news_id     INTEGER NOT NULL,
    candidate_ts    INTEGER NOT NULL,
    tz              TEXT    NOT NULL DEFAULT 'Asia/Shanghai',
    source          TEXT    NOT NULL DEFAULT ''
        CHECK (source IN ('platform_published_at','ai_inferred','cross_net','human_annotated','other')),
    confidence      REAL    NOT NULL DEFAULT 1.0,
    reasoning       TEXT    NOT NULL DEFAULT '',
    is_primary      INTEGER NOT NULL DEFAULT 0,
    parser_version  TEXT    NOT NULL DEFAULT 'event_struct.v1',
    created_at      INTEGER NOT NULL
);
"""


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _insert(conn, source: str, news_id: int = 4242) -> int:
    return event_atom.insert_event_time_candidate(
        raw_news_id=news_id,
        candidate_ts=1730000000,
        source=source,
        confidence=0.8,
        reasoning="unit-test",
        is_primary=True,
        connection=conn,
    )


# ─────────────────────── 新建库 ───────────────────────

def test_new_db_accepts_all_sources_in_use():
    conn = _fresh_db()
    for source in ("platform_published_at", "ai_inferred", "cross_net",
                   "human_annotated", "other", "ai_worker_review",
                   "llm_fallback", "backfill_v181"):
        cid = _insert(conn, source, news_id=1000 + len(source))
        assert cid > 0, f"source={source} 被拒（id={cid}）"
        row = conn.execute(
            "SELECT source FROM event_time_candidates WHERE id=?", (cid,)
        ).fetchone()
        assert row["source"] == source
    conn.close()


def test_candidate_is_readable_back():
    conn = _fresh_db()
    cid = _insert(conn, "ai_worker_review", news_id=777)
    rows = event_atom.get_event_time_candidates(777, connection=conn)
    assert len(rows) == 1
    assert rows[0]["source"] == "ai_worker_review"
    assert rows[0]["is_primary"] == 1
    conn.close()


# ─────────────────────── 旧库迁移 ───────────────────────

def test_legacy_table_is_rebuilt_and_rows_preserved():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_OLD_TABLE)
    conn.execute(
        """INSERT INTO event_time_candidates
           (raw_news_id, candidate_ts, tz, source, confidence, reasoning,
            is_primary, parser_version, created_at)
           VALUES (1, 1730000000, 'Asia/Shanghai', 'platform_published_at',
                   1.0, 'legacy', 1, 'event_struct.v1', 1730000000)"""
    )
    conn.commit()

    db.migrate(conn)   # 触发 v318 重建

    rows = conn.execute("SELECT * FROM event_time_candidates").fetchall()
    assert len(rows) == 1
    assert rows[0]["reasoning"] == "legacy"

    # 新约束生效
    cid = _insert(conn, "ai_worker_review", news_id=9)
    assert cid > 0
    conn.close()


def test_migration_is_idempotent():
    conn = _fresh_db()
    db.migrate(conn)
    db.migrate(conn)
    cid = _insert(conn, "llm_fallback", news_id=11)
    assert cid > 0
    conn.close()


# ─────────────────────── 未知来源不再静默失败 ───────────────────────

def test_unknown_source_is_mapped_and_preserved_in_reasoning():
    conn = _fresh_db()
    cid = _insert(conn, "some_future_source", news_id=555)
    assert cid > 0
    row = conn.execute(
        "SELECT source, reasoning FROM event_time_candidates WHERE id=?", (cid,)
    ).fetchone()
    assert row["source"] == "other"
    assert "some_future_source" in row["reasoning"]
    conn.close()


def test_insert_failure_does_not_raise():
    """插入失败返回 -1（调用方据此降级），但不得抛异常打断主流程。"""
    conn = _fresh_db()
    assert event_atom.insert_event_time_candidate(
        raw_news_id=0, candidate_ts=1, connection=conn,
    ) == -1
    conn.close()
