"""v174 — Carry-over 信号残留表 + 指数衰减。

新增 db.signal_state 表 + record_signal / get_active_signals / purge_expired_signals /
compute_decay helpers。覆盖：schema 迁移 / 衰减数学 / upsert max-impact / TTL 过滤 /
asset 过滤 / purge / 端到端生命周期。
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn
# ────────────────────────────────────────────────────────────────────
# 1. Schema / migration
# ────────────────────────────────────────────────────────────────────

def test_migrate_creates_signal_state_table():
    """migrate() 后 signal_state 表 + 9 列必须齐。"""
    conn = _fresh_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(signal_state)")}
    expected = {
        "signal_id", "asset", "impact", "decay_lambda",
        "created_at", "last_seen_at", "ttl_seconds", "direction", "reason",
    }
    missing = expected - cols
    assert not missing, f"signal_state 缺列: {missing}"
    conn.close()


def test_migrate_idempotent_signal_state():
    """migrate() 跑两次不报错。"""
    conn = _fresh_db()
    db.migrate(conn)
    db.migrate(conn)
    cur = conn.execute("SELECT COUNT(*) FROM signal_state")
    assert int(cur.fetchone()[0]) == 0
    conn.close()
    return conn

# ────────────────────────────────────────────────────────────────────
# 2. compute_decay 数学
# ────────────────────────────────────────────────────────────────────

def test_compute_decay_math():
    """impact=1.0, λ=1e-4, age=1000s → impact * exp(-0.1) ≈ 0.9048。"""
    v = db.compute_decay(1.0, 1.0e-4, 1000.0)
    assert math.isclose(v, 0.904837418, rel_tol=1e-6)


def test_compute_decay_zero_impact():
    """impact=0 → 0（避免数学噪声）。"""
    assert db.compute_decay(0.0, 1e-4, 100.0) == 0.0
    assert db.compute_decay(-1.0, 1e-4, 100.0) == 0.0


def test_compute_decay_negative_age_clamp():
    """age=-100（时钟漂移）→ clamp 到 0 → 不衰减。"""
    v = db.compute_decay(0.5, 1e-4, -100.0)
    assert math.isclose(v, 0.5, rel_tol=1e-6)


def test_compute_decay_zero_lambda_no_decay():
    """λ=0 → 无衰减（max(λ, 0) 钳位保护）。"""
    v = db.compute_decay(0.7, 0.0, 10000.0)
    assert math.isclose(v, 0.7, rel_tol=1e-6)
    v2 = db.compute_decay(0.7, -0.5, 10000.0)
    assert math.isclose(v2, 0.7, rel_tol=1e-6)
# ────────────────────────────────────────────────────────────────────
# 3. record_signal 新增 + upsert
# ────────────────────────────────────────────────────────────────────

def test_record_signal_inserts_new():
    """新 signal_id → INSERT。"""
    conn = _fresh_db()
    ok = db.record_signal(
        conn, signal_id="sig_fed_001", asset="XAU",
        impact=0.8, decay_lambda=1e-4, direction="BULL",
        reason="Fed 25bp 加息", now=1700000000,
    )
    assert ok is True
    row = conn.execute(
        "SELECT * FROM signal_state WHERE signal_id=?", ("sig_fed_001",)
    ).fetchone()
    assert row is not None
    assert row["asset"] == "XAU"
    assert float(row["impact"]) == 0.8
    assert row["direction"] == "BULL"
    assert row["reason"] == "Fed 25bp 加息"
    conn.close()


def test_record_signal_upsert_keeps_max_impact():
    """同 signal_id 第二次 record：impact 取 max，且 last_seen_at 刷新。"""
    conn = _fresh_db()
    db.record_signal(
        conn, signal_id="sig_fed_001", asset="XAU", impact=0.5,
        now=1700000000,
    )
    db.record_signal(
        conn, signal_id="sig_fed_001", asset="XAU", impact=0.8,
        now=1700001000,
    )
    db.record_signal(
        conn, signal_id="sig_fed_001", asset="XAU", impact=0.3,
        now=1700002000,
    )
    row = conn.execute(
        "SELECT impact, last_seen_at FROM signal_state WHERE signal_id=?",
        ("sig_fed_001",),
    ).fetchone()
    assert float(row["impact"]) == 0.8
    assert int(row["last_seen_at"]) == 1700002000
    conn.close()


def test_record_signal_returns_false_on_missing_table():
    """未 migrate 的旧库：record_signal 应降级返回 False。"""
    conn = sqlite3.connect(":memory:")
    try:
        ok = db.record_signal(
            conn, signal_id="x", asset="XAU", impact=0.5,
        )
        assert ok is False
    except Exception as exc:
        assert False, f"应降级，实际抛错: {exc}"
    conn.close()

def test_get_active_filters_ttl():
    """TTL 到期的信号应不返回。"""
    conn = _fresh_db()
    db.record_signal(conn, signal_id="old", asset="XAU", impact=0.5,
                     ttl_seconds=200, now=100)
    db.record_signal(conn, signal_id="new", asset="XAU", impact=0.5,
                     ttl_seconds=200, now=500)
    rows = db.get_active_signals(conn, now=400)
    ids = [r["signal_id"] for r in rows]
    assert "old" not in ids
    assert "new" in ids
    conn.close()


def test_get_active_filters_asset():
    """asset 过滤应只返回该品种。"""
    conn = _fresh_db()
    db.record_signal(conn, signal_id="s1", asset="XAU", impact=0.5, now=1000)
    db.record_signal(conn, signal_id="s2", asset="BTC", impact=0.5, now=1000)
    rows = db.get_active_signals(conn, asset="XAU", now=1000)
    assert len(rows) == 1
    assert rows[0]["asset"] == "XAU"
    assert rows[0]["signal_id"] == "s1"
    conn.close()


def test_get_active_returns_current_impact_decayed():
    """返回 current_impact 必须经过指数衰减计算（≠ 原始 impact）。"""
    conn = _fresh_db()
    db.record_signal(
        conn, signal_id="sig", asset="XAU", impact=1.0,
        decay_lambda=1e-4, now=1000,
    )
    rows = db.get_active_signals(conn, now=2000)
    assert len(rows) == 1
    r = rows[0]
    expected = db.compute_decay(1.0, 1e-4, 1000.0)
    assert math.isclose(float(r["current_impact"]), expected, rel_tol=1e-4)
    assert r["age_seconds"] == 1000
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 5. purge_expired_signals
# ────────────────────────────────────────────────────────────────────

def test_purge_deletes_expired():
    """purge 应只删 TTL 到期的，没到期的留下。"""
    conn = _fresh_db()
    db.record_signal(conn, signal_id="old", asset="XAU", impact=0.5,
                     ttl_seconds=200, now=100)
    db.record_signal(conn, signal_id="new", asset="XAU", impact=0.5,
                     ttl_seconds=200, now=500)
    deleted = db.purge_expired_signals(conn, now=500)
    assert int(deleted) == 1
    rows = conn.execute("SELECT signal_id FROM signal_state").fetchall()
    assert [r["signal_id"] for r in rows] == ["new"]
    conn.close()


def test_purge_returns_zero_on_empty():
    """空表 purge 应返回 0（不报错）。"""
    conn = _fresh_db()
    deleted = db.purge_expired_signals(conn)
    assert int(deleted) == 0
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 6. 端到端生命周期
# ────────────────────────────────────────────────────────────────────

def test_end_to_end_lifecycle():
    """完整生命周期：record → 衰减 → 第二次 record 续报 → purge。"""
    conn = _fresh_db()
    base = 1700000000
    # 第一波：非农初值，TTL=3600s
    db.record_signal(
        conn, signal_id="nfp_release_v1", asset="XAU",
        impact=0.7, decay_lambda=2e-4, direction="BEAR",
        reason="非农初值 +25万", ttl_seconds=3600, now=base,
    )
    # 30min 后看残留（TTL 内）
    rows = db.get_active_signals(conn, asset="XAU", now=base + 1800)
    assert len(rows) == 1
    v_at_30m = float(rows[0]["current_impact"])
    assert 0.0 < v_at_30m < 0.7
    # 60min 后：同源新闻续报（修正值上修到 +35万）
    db.record_signal(
        conn, signal_id="nfp_release_v1", asset="XAU",
        impact=0.9, decay_lambda=2e-4, direction="BEAR",
        reason="非农修正值上修", ttl_seconds=3600, now=base + 3600,
    )
    # 续报后 last_seen_at 应刷新到 base+3600
    rows = db.get_active_signals(conn, asset="XAU", now=base + 3600)
    assert len(rows) == 1
    assert math.isclose(float(rows[0]["impact"]), 0.9, rel_tol=1e-6)
    assert int(rows[0]["last_seen_at"]) == base + 3600
    # TTL 到期 purge（base + 3600 + 3600 + 1 = base+7201 > expires=base+3600+3600=base+7200）
    deleted = db.purge_expired_signals(conn, now=base + 7201)
    assert int(deleted) == 1
    rows = conn.execute("SELECT COUNT(*) FROM signal_state").fetchone()[0]
    assert int(rows) == 0
    conn.close()