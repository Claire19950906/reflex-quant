"""v173 — Decision Evidence 证据链（全文溯源基石）。

新增 db.record_evidence / record_evidence_batch / fetch_evidence + decision_evidence 表。
覆盖：schema 迁移 / 单条记录 / 批量 / used_for 多样性 / 快照冻结（防篡改）/ join raw_news /
降级（表不存在不阻断）/ ai_worker wire-in 模拟。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _seed_news(conn, *, source="RSS:test", content="美联储宣布加息25bp",
               published_at="2026-09-18 10:00:00",
               fingerprint="fingerprint_abc", ts=1700000000):
    cur = conn.execute(
        """INSERT INTO raw_news
           (source, content, timestamp, status, is_noise, relevance_score,
            ts, published_at, content_fingerprint, first_seen_ts)
           VALUES (?, ?, datetime('now'), 'PENDING', 0, 0.5,
                   ?, ?, ?, '')""",
        (source, content, ts, published_at, fingerprint),
    )
    return int(cur.lastrowid)


def _seed_decision(conn, news_id):
    cur = conn.execute(
        """INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, reasoning)
           VALUES (?, 0.5, 'BUY', 'test')""",
        (news_id,),
    )
    return int(cur.lastrowid)


# ────────────────────────────────────────────────────────────────────
# 1. Schema / migration
# ────────────────────────────────────────────────────────────────────

def test_migrate_creates_decision_evidence_table():
    """migrate() 后 decision_evidence 表 + 7 列必须齐。"""
    conn = _fresh_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(decision_evidence)")}
    expected = {
        "decision_id", "news_id", "used_for", "captured_at",
        "published_at_snapshot", "fetched_at_snapshot", "content_hash_snapshot",
    }
    missing = expected - cols
    assert not missing, f"decision_evidence 缺列: {missing}"
    conn.close()


def test_migrate_idempotent_decision_evidence():
    """migrate() 跑两次不报错（CREATE TABLE IF NOT EXISTS + 空迁移列）。"""
    conn = _fresh_db()
    db.migrate(conn)
    db.migrate(conn)
    cur = conn.execute("SELECT COUNT(*) FROM decision_evidence")
    assert int(cur.fetchone()[0]) == 0
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 2. record_evidence 单条
# ────────────────────────────────────────────────────────────────────

def test_record_evidence_writes_snapshot_from_raw_news():
    """record_evidence 自动从 raw_news 抓 published_at / ts / fingerprint 作快照。"""
    conn = _fresh_db()
    nid = _seed_news(conn, published_at="2026-09-18 12:34:56",
                     fingerprint="hash_a", ts=1700000123)
    did = _seed_decision(conn, nid)
    ok = db.record_evidence(conn, decision_id=did, news_id=nid, used_for="trigger")
    assert ok is True
    row = conn.execute(
        """SELECT * FROM decision_evidence
           WHERE decision_id=? AND news_id=? AND used_for='trigger'""",
        (did, nid),
    ).fetchone()
    assert row is not None
    assert row["published_at_snapshot"] == "2026-09-18 12:34:56"
    assert int(row["fetched_at_snapshot"]) == 1700000123
    assert row["content_hash_snapshot"] == "hash_a"
    assert int(row["captured_at"]) > 0
    conn.close()


def test_record_evidence_default_used_for():
    """不传 used_for 时默认 'context'。"""
    conn = _fresh_db()
    nid = _seed_news(conn)
    did = _seed_decision(conn, nid)
    db.record_evidence(conn, decision_id=did, news_id=nid)
    row = conn.execute(
        "SELECT used_for FROM decision_evidence WHERE decision_id=?", (did,)
    ).fetchone()
    assert row["used_for"] == "context"
    conn.close()


def test_record_evidence_returns_false_on_missing_table():
    """未 migrate 的旧库：record_evidence 应降级返回 False（不抛错）。"""
    conn = sqlite3.connect(":memory:")
    try:
        ok = db.record_evidence(conn, decision_id=1, news_id=1)
        assert ok is False
    except Exception as exc:
        assert False, f"record_evidence 应该降级，实际抛错: {exc}"
    conn.close()


def test_record_evidence_multiple_used_for_for_same_news():
    """同一 (decision, news) 多次写不同 used_for 应分别成行（PK 三键）。"""
    conn = _fresh_db()
    nid = _seed_news(conn)
    did = _seed_decision(conn, nid)
    db.record_evidence(conn, decision_id=did, news_id=nid, used_for="trigger")
    db.record_evidence(conn, decision_id=did, news_id=nid, used_for="context")
    db.record_evidence(conn, decision_id=did, news_id=nid, used_for="confirm")
    n = conn.execute(
        "SELECT COUNT(*) FROM decision_evidence WHERE decision_id=? AND news_id=?",
        (did, nid),
    ).fetchone()[0]
    assert int(n) == 3
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 3. record_evidence_batch
# ────────────────────────────────────────────────────────────────────

def test_record_evidence_batch_counts_success():
    """批量：3 条 news 应 3 条 evidence 全成功。"""
    conn = _fresh_db()
    n1 = _seed_news(conn, content="加息25bp", fingerprint="fp1")
    n2 = _seed_news(conn, content="鲍威尔讲话", fingerprint="fp2")
    n3 = _seed_news(conn, content="美股盘前", fingerprint="fp3")
    did = _seed_decision(conn, n1)
    n_ok = db.record_evidence_batch(
        conn, decision_id=did, news_ids=[n1, n2, n3], used_for="context"
    )
    assert n_ok == 3
    rows = conn.execute(
        "SELECT news_id FROM decision_evidence WHERE decision_id=? ORDER BY news_id",
        (did,),
    ).fetchall()
    assert [int(r["news_id"]) for r in rows] == sorted([n1, n2, n3])
    conn.close()


def test_record_evidence_batch_skips_none():
    """批量里塞 None 应安全跳过（不抛错、不入库）。"""
    conn = _fresh_db()
    nid = _seed_news(conn)
    did = _seed_decision(conn, nid)
    n_ok = db.record_evidence_batch(
        conn, decision_id=did, news_ids=[None, nid, None], used_for="context"
    )
    assert n_ok == 1
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 4. fetch_evidence 全文溯源 join
# ────────────────────────────────────────────────────────────────────

def test_fetch_evidence_returns_joined_raw_news():
    """fetch_evidence 应 join raw_news 拿到原文 + 快照 + 现行（对比）。"""
    conn = _fresh_db()
    nid = _seed_news(conn, source="RSS:reuters", content="Fed 加息25bp",
                     published_at="2026-09-18 11:00:00", fingerprint="fp21",
                     ts=1700001000)
    did = _seed_decision(conn, nid)
    db.record_evidence(conn, decision_id=did, news_id=nid, used_for="trigger")
    rows = db.fetch_evidence(conn, did)
    assert len(rows) == 1
    r = rows[0]
    assert int(r["news_id"]) == nid
    assert r["used_for"] == "trigger"
    assert r["source"] == "RSS:reuters"
    assert "Fed" in r["content"]
    assert r["published_at_snapshot"] == "2026-09-18 11:00:00"
    assert r["content_hash_snapshot"] == "fp21"
    assert r["current_published_at"] == "2026-09-18 11:00:00"
    conn.close()


def test_fetch_evidence_detects_tampering():
    """snapshot vs current 对比：raw_news 被改后仍能从 snapshot 复原原始溯源。"""
    conn = _fresh_db()
    nid = _seed_news(conn, published_at="2026-09-18 11:00:00", fingerprint="original")
    did = _seed_decision(conn, nid)
    db.record_evidence(conn, decision_id=did, news_id=nid)
    conn.execute(
        "UPDATE raw_news SET published_at=?, content_fingerprint=? WHERE id=?",
        ("9999-12-31 00:00:00", "tampered", nid),
    )
    conn.commit()
    rows = db.fetch_evidence(conn, did)
    r = rows[0]
    assert r["published_at_snapshot"] == "2026-09-18 11:00:00"
    assert r["content_hash_snapshot"] == "original"
    assert r["current_published_at"] == "9999-12-31 00:00:00"
    assert r["current_hash"] == "tampered"
    conn.close()


def test_fetch_evidence_empty_when_no_decision():
    """未记录过 evidence 的 decision 返回空列表（不报错）。"""
    conn = _fresh_db()
    rows = db.fetch_evidence(conn, 99999)
    assert rows == []
    conn.close()


def test_fetch_evidence_multiple_evidence_for_same_decision():
    """同 decision 多 evidence（trigger + context + confirm）按 captured_at 升序。"""
    conn = _fresh_db()
    n1 = _seed_news(conn, fingerprint="fp_a")
    n2 = _seed_news(conn, fingerprint="fp_b")
    n3 = _seed_news(conn, fingerprint="fp_c")
    did = _seed_decision(conn, n1)
    db.record_evidence(conn, decision_id=did, news_id=n1, used_for="trigger")
    time.sleep(0.01)
    db.record_evidence(conn, decision_id=did, news_id=n2, used_for="context")
    time.sleep(0.01)
    db.record_evidence(conn, decision_id=did, news_id=n3, used_for="confirm")
    rows = db.fetch_evidence(conn, did)
    assert len(rows) == 3
    assert [r["used_for"] for r in rows] == ["trigger", "context", "confirm"]
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 5. ai_worker 接入 smoke
# ────────────────────────────────────────────────────────────────────

def test_ai_worker_wire_in_pattern():
    """复刻 ai_worker 在 INSERT ai_decisions 后调 record_evidence 的接入模式。"""
    conn = _fresh_db()
    nid = _seed_news(conn, content="鲍威尔讲话", published_at="2026-09-18 11:00:00")
    cur = conn.execute(
        """INSERT INTO ai_decisions
           (news_id, sentiment_score, suggested_action, reasoning)
           VALUES (?, 0.6, 'BUY', 'test')""",
        (nid,),
    )
    decision_id = int(cur.lastrowid)
    db.record_evidence(
        conn, decision_id=decision_id, news_id=nid, used_for="trigger",
    )
    conn.commit()
    ev = db.fetch_evidence(conn, decision_id)
    assert len(ev) == 1
    assert "鲍威尔" in ev[0]["content"]
    assert ev[0]["used_for"] == "trigger"
    conn.close()


def test_ai_worker_wire_in_does_not_break_on_missing_news():
    """ai_worker 接入必须 try/except 包裹（缺失 news_id 时不阻断 ai_worker 主流程）。"""
    conn = _fresh_db()
    did = _seed_decision(conn, 99999)
    ok = db.record_evidence(conn, decision_id=did, news_id=99999, used_for="trigger")
    assert ok is True
    ev = db.fetch_evidence(conn, did)
    assert len(ev) == 1
    assert ev[0]["published_at_snapshot"] == ""
    conn.close()