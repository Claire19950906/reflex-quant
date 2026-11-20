#!/usr/bin/env python3
"""v409 回归：/api/health/models 的漏斗数字必须是真值，且失败要显形。

守三件事（都来自 2026-11-17 的实测事故）：
1. 「待分析」只算真会被认领的（PENDING + is_noise=0 + 未超龄），超龄的不算；
2. 「24h 分析量」用 ISO 口径比较，不能因为 created_at 是 `T` 格式而把跨日旧数据算进来
   （原实现实测虚高 68%：521 vs 310）；
3. 查不成时返回 None + db_error，绝不能用 0 冒充"查询成功且为空"。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

import news_funnel


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000+08:00", time.localtime(ts))


def _make_db(path: Path, now: float):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE raw_news (id INTEGER PRIMARY KEY, status TEXT, is_noise INTEGER, ts INTEGER);
        CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, created_at TEXT, decision_kind TEXT DEFAULT 'analysis');
    """)
    h = 3600
    rows = [
        ("PENDING", 0, int(now - 1 * h)),    # 新鲜、会被认领 → 算 1
        ("PENDING", 0, int(now - 30 * h)),   # 超龄 → 不算（迟早要被归档）
        ("PENDING", 1, int(now - 1 * h)),    # 已判噪音 → 不算
        ("DONE", 0, int(now - 1 * h)),       # 已分析 → 不算
    ]
    conn.executemany("INSERT INTO raw_news(status, is_noise, ts) VALUES (?,?,?)", rows)
    decisions = [
        _iso(now - 2 * h),    # 24h 内 → 算
        _iso(now - 30 * h),   # 24h 外 → 不算
    ]
    conn.executemany("INSERT INTO ai_decisions(created_at) VALUES (?)",
                     [(d,) for d in decisions])
    conn.commit()
    conn.close()


def test_pending_excludes_stale_and_noise(tmp_path):
    now = 1_760_000_000.0
    db = tmp_path / "t.db"
    _make_db(db, now)
    st = news_funnel.funnel_stats(str(db), 6, now=now)
    assert st["db_error"] == ""
    assert st["pending_news"] == 1, st
    assert st["analyzed_24h"] == 1, st


def test_analyzed_24h_uses_iso_cutoff(tmp_path):
    """跨日边界：把 24h 前的旧行放进去，旧的空格口径会误算，ISO 口径不会。"""
    now = 1_760_000_000.0
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE raw_news (id INTEGER PRIMARY KEY, status TEXT, is_noise INTEGER, ts INTEGER);"
                       "CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, created_at TEXT, decision_kind TEXT DEFAULT 'analysis');")
    boundary = _iso(now - 25 * 3600)   # 25h 前：任何口径都不该算
    inside = _iso(now - 23 * 3600)
    conn.executemany("INSERT INTO ai_decisions(created_at) VALUES (?)",
                     [(boundary,), (inside,)])
    conn.commit()
    conn.close()
    st = news_funnel.funnel_stats(str(db), 6, now=now)
    assert st["analyzed_24h"] == 1, st
    # 对照：旧口径（空格 cutoff + datetime('now')）在这张表上会算成 2 —— 说明修复必要
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    old = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions WHERE created_at >= ?",
        (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 24 * 3600)),)).fetchone()[0]
    conn.close()
    assert old == 2, f"旧口径应虚高，实际 {old}"


def test_zero_max_age_means_no_age_filter(tmp_path):
    now = 1_760_000_000.0
    db = tmp_path / "t.db"
    _make_db(db, now)
    st = news_funnel.funnel_stats(str(db), 0, now=now)
    assert st["pending_news"] == 2, st   # 两条 is_noise=0 的 PENDING 都算


def test_missing_db_reports_error_not_zero(tmp_path):
    st = news_funnel.funnel_stats(str(tmp_path / "nope.db"), 6)
    assert st["pending_news"] is None
    assert st["analyzed_24h"] is None
    assert "unable to open" in st["db_error"].lower() or st["db_error"], st


def test_missing_table_reports_error_not_zero(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    st = news_funnel.funnel_stats(str(db), 6)
    assert st["pending_news"] is None, st
    assert st["db_error"], st


# ─────────────── 门卫真实判出率（v410，同属面板口径） ───────────────


def _make_gate_db(path: Path, now: float):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE raw_news (id INTEGER PRIMARY KEY, ts INTEGER,
            gate_unavailable INTEGER, gate_verdict TEXT);
    """)
    rows = [
        (int(now - 60), 1, "未判定"),        # 冻结
        (int(now - 60), 2, "未判定"),        # 降级放行
        (int(now - 60), 0, "可交易"),        # 判了
        (int(now - 60), 0, ""),              # 无标签
        (int(now - 60), 0, "未判定"),        # 无标签（未判定但没标记）
        (int(now - 40 * 3600), 0, "可交易"),  # 窗口外 → 不计
    ]
    conn.executemany(
        "INSERT INTO raw_news(ts, gate_unavailable, gate_verdict) VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def test_gate_health_counts(tmp_path):
    now = 1_760_000_000.0
    db = tmp_path / "g.db"
    _make_gate_db(db, now)
    h = news_funnel.gate_health(str(db), 30, now=now)
    assert h["db_error"] == ""
    assert h["total"] == 5, h
    assert h["frozen"] == 1
    assert h["degraded"] == 1
    assert h["judged"] == 1
    assert h["unlabeled"] == 2
    assert h["judged_pct"] == 20.0


def test_gate_health_missing_table_reports_error(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    h = news_funnel.gate_health(str(db), 30)
    assert h["total"] is None and h["db_error"], h
