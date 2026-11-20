# -*- coding: utf-8 -*-
"""v444 —— 断档恢复后不补历史新闻（用户 2026-11-18 定策）的回归用例。

规则原话："以后所有的 LLM 通道恢复之后，都不去做历史的新闻……它只分析现在
进来的，包括现在到以后进来的新闻。"

覆盖四件事：
  1. 水位只前进不后退（多进程/多次调用都安全）
  2. 断档判定：主分析连续 N 分钟没有成功调用 → 水位推进到"恢复时刻"
  3. 认领 SQL 真的把水位之下的行挡在门外
  4. 归档 SQL 把它们标成 FILTERED + pre_recovery_skip（留痕，不删数据）

全部离线：临时库 + 临时状态文件，不碰生产库、不发任何 LLM 请求。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import news_cutoff as nc


@pytest.fixture()
def state(tmp_path, monkeypatch):
    """把水位状态文件指到 tmp_path，避免读到生产 runtime/news_cutoff.json。"""
    f = tmp_path / "news_cutoff.json"
    monkeypatch.setattr(nc, "_STATE_FILE", f)
    monkeypatch.setattr(nc, "_LOCK_FILE", tmp_path / "news_cutoff.json.lock")
    return f


@pytest.fixture()
def db(tmp_path):
    """最小 raw_news + ai_call_stats 表（只建被测 SQL 用到的列）。"""
    path = tmp_path / "evt.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE raw_news ("
        " id INTEGER PRIMARY KEY, ts INTEGER, status TEXT, is_noise INTEGER,"
        " processing_at TEXT, gate_unavailable INTEGER, gate_verdict TEXT,"
        " filter_reason TEXT NOT NULL DEFAULT '', retry_count INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE ai_call_stats ("
        " id INTEGER PRIMARY KEY, ts INTEGER, purpose TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE ai_decisions ("
        " id INTEGER PRIMARY KEY, news_id INTEGER, created_at TEXT,"
        " decision_kind TEXT DEFAULT 'analysis')"
    )
    conn.commit()
    yield conn
    conn.close()


# ── 1. 水位只前进不后退 ──────────────────────────────────────────────────
def test_floor_only_moves_forward(state):
    t0 = time.time()
    assert nc.advance_floor(t0, "first") == int(t0)
    # 想往回退 → 不允许（保住"已丢弃的历史不再复活"）
    assert nc.advance_floor(t0 - 3600, "rewind") == int(t0)
    assert nc.get_floor() == int(t0)
    # 往前推 → 生效
    assert nc.advance_floor(t0 + 60, "later") == int(t0 + 60)


def test_is_stale_uses_floor(state):
    floor = nc.advance_to_now("test")
    assert nc.is_stale(floor - 1) is True
    assert nc.is_stale(floor + 1) is False
    # 没有时间戳 → 不在这里判死
    assert nc.is_stale(None) is False


def test_floor_zero_means_unlimited(state, monkeypatch):
    monkeypatch.setenv("NEWS_SKIP_BACKLOG_ON_RECOVERY", "0")
    # 未推进过 → 0 → 谁都不算 stale
    assert nc.get_floor() == 0
    assert nc.is_stale(time.time() - 99999) is False


def test_state_file_survives_restart(state):
    """进程重启（重新读文件）后水位仍在 —— 引擎重启不能把规则重置掉。"""
    ts = int(time.time())
    nc.advance_floor(ts, "persist")
    raw = state.read_text(encoding="utf-8")
    assert str(ts) in raw


# ── 2. 断档判定 ──────────────────────────────────────────────────────────
def test_advance_when_main_analysis_stalled(state, db, monkeypatch):
    monkeypatch.setenv("NEWS_RECOVERY_GAP_MIN", "15")
    now = time.time()
    # 最近一次成功的主分析调用在 5 小时前 → 判定断档
    db.execute("INSERT INTO ai_call_stats (ts, purpose, status) VALUES (?, 'main', 'ok')",
               (int(now - 5 * 3600),))
    db.commit()
    floor, delta = nc.maybe_advance_on_recovery(db, now=now)
    assert floor == int(now)
    assert delta > 0


def test_no_advance_when_pipeline_healthy(state, db, monkeypatch):
    monkeypatch.setenv("NEWS_RECOVERY_GAP_MIN", "15")
    now = time.time()
    db.execute("INSERT INTO ai_call_stats (ts, purpose, status) VALUES (?, 'main', 'ok')",
               (int(now - 30),))
    db.commit()
    floor, delta = nc.maybe_advance_on_recovery(db, now=now)
    assert floor == 0 and delta == 0


def test_gate_ok_alone_does_not_count_as_healthy(state, db, monkeypatch):
    """门卫偶发成功 ≠ 决策链活着（2026-11-18 事故就长这样：gate 在响、决策为 0）。"""
    monkeypatch.setenv("NEWS_RECOVERY_GAP_MIN", "15")
    now = time.time()
    db.execute("INSERT INTO ai_call_stats (ts, purpose, status) VALUES (?, 'gate', 'ok')",
               (int(now - 60),))
    db.execute("INSERT INTO ai_call_stats (ts, purpose, status) VALUES (?, 'main', 'ok')",
               (int(now - 5 * 3600),))
    db.commit()
    floor, _ = nc.maybe_advance_on_recovery(db, now=now)
    assert floor == int(now)


def test_first_run_never_advances(state, db, monkeypatch):
    """从没有过成功调用（新库）→ 不推进，避免把首批数据当垃圾丢掉。"""
    monkeypatch.setenv("NEWS_RECOVERY_GAP_MIN", "15")
    floor, delta = nc.maybe_advance_on_recovery(db, now=time.time())
    assert floor == 0 and delta == 0


# ── 3. 认领 SQL 挡住水位之下的行 ──────────────────────────────────────────
def _seed_news(db, now):
    rows = [
        (1, int(now - 5 * 3600), "PENDING", 0, None, 0, "", ""),   # 断档期间（旧）
        (2, int(now - 60), "PENDING", 0, None, 0, "", ""),        # 恢复之后（新）
    ]
    db.executemany(
        "INSERT INTO raw_news (id, ts, status, is_noise, processing_at,"
        " gate_unavailable, gate_verdict, filter_reason) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()


def _claim_ids(db, monkeypatch, floor):
    from engine import ai_worker as w
    monkeypatch.setattr(nc, "get_floor", lambda: floor)
    sql, params = w._build_claim_sql()
    return [r["id"] for r in db.execute(sql, params).fetchall()]


def test_claim_skips_news_below_floor(state, db, monkeypatch):
    now = time.time()
    _seed_news(db, now)
    monkeypatch.setenv("NEWS_ANALYZE_MAX_AGE_HOURS", "6")
    # 水位 = 恢复时刻（now-120，介于两条之间）
    ids = _claim_ids(db, monkeypatch, int(now - 120))
    assert 1 not in ids
    assert 2 in ids


def test_claim_processes_all_when_floor_zero(state, db, monkeypatch):
    now = time.time()
    _seed_news(db, now)
    monkeypatch.setenv("NEWS_ANALYZE_MAX_AGE_HOURS", "6")
    ids = _claim_ids(db, monkeypatch, 0)
    assert {1, 2} <= set(ids)


# ── 4. 归档留痕 ─────────────────────────────────────────────────────────
def test_archive_below_floor_marks_reason(state, db, monkeypatch):
    now = time.time()
    _seed_news(db, now)
    floor = int(now - 120)
    monkeypatch.setattr(nc, "get_floor", lambda: floor)
    from engine import ai_worker as w
    n = w._archive_below_floor(db)
    assert n == 1
    row = db.execute("SELECT status, filter_reason FROM raw_news WHERE id=1").fetchone()
    assert row["status"] == "FILTERED"
    assert row["filter_reason"] == "pre_recovery_skip"
    # 新新闻不受影响
    assert db.execute("SELECT status FROM raw_news WHERE id=2").fetchone()["status"] == "PENDING"


# ── 5. 面板计数同口径 ───────────────────────────────────────────────────
def test_funnel_pending_excludes_below_floor(state, db, monkeypatch, tmp_path):
    now = time.time()
    _seed_news(db, now)
    monkeypatch.setattr(nc, "get_floor", lambda: int(now - 120))
    import news_funnel
    out = news_funnel.funnel_stats(str(tmp_path / "evt.db"), max_age_hours=6, now=now)
    assert out["pending_news"] == 1   # 只剩恢复之后那一条
