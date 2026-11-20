# -*- coding: utf-8 -*-
"""v445 —— 队列容量兜底：待分析超过上限时只保留最新 N 条。

背景（2026-11-18 实测）：把 LLM 限速关掉、批大小从 4 抬到 16 之后，
门卫吞吐翻倍，但新闻到达 200 条/小时、分析 24 条/小时，队列只涨不降。
断档水位（v444）只管"彻底停摆后恢复的那一刻"，管不住这种慢性堆积，
所以补一条容量闸：`maybe_advance_on_backlog()`。

覆盖：
  1. 未超上限 → 不动水位
  2. 超上限 → 水位推进到"最新 keep 条之下"，被推下去的都是 PENDING
  3. 水位之上剩下的条数 == keep
  4. CAP=0 → 关闭
  5. 水位只前进不后退（重复调用幂等）
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
    f = tmp_path / "news_cutoff.json"
    monkeypatch.setattr(nc, "_STATE_FILE", f)
    monkeypatch.setattr(nc, "_LOCK_FILE", tmp_path / "news_cutoff.json.lock")
    if f.exists():
        f.unlink()
    return f


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "evt.db"))
    conn.execute("CREATE TABLE raw_news (id INTEGER PRIMARY KEY, ts INTEGER,"
                 " status TEXT, is_noise INTEGER, title TEXT)")
    conn.execute("CREATE TABLE ai_call_stats (id INTEGER PRIMARY KEY, ts INTEGER,"
                 " status TEXT, purpose TEXT)")
    conn.execute("INSERT INTO ai_call_stats (ts, status, purpose) VALUES (?, 'ok', 'main')",
                 (int(time.time()),))
    conn.commit()
    yield conn
    conn.close()


def _set_cfg(monkeypatch, **kw):
    """同时改 env 和 config 模块属性。

    news_cutoff 的 _cfg() 优先读 `config.XXX`（已经从 .env 加载过），
    只 monkeypatch.setenv 不生效 —— 这是本套用例最容易踩的坑。
    """
    for k, v in kw.items():
        monkeypatch.setenv(k, str(v))
        try:
            import config
            monkeypatch.setattr(config, k, v, raising=False)
        except Exception:
            pass


def _seed(conn, n, start=1000):
    for i in range(n):
        conn.execute("INSERT INTO raw_news (ts, status, is_noise, title) VALUES (?, 'PENDING', 0, ?)",
                     (start + i, "n%d" % i))
    conn.commit()


def _pending_above(conn, floor):
    return conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE status='PENDING' AND is_noise=0"
        " AND (? <= 0 OR ts >= ?)", (floor, floor)).fetchone()[0]


def test_no_advance_below_cap(state, db, monkeypatch):
    _set_cfg(monkeypatch, NEWS_BACKLOG_CAP=50, NEWS_BACKLOG_KEEP=20)
    _seed(db, 30)
    floor, moved = nc.maybe_advance_on_backlog(db)
    assert floor == 0 and moved == 0
    assert nc.get_floor() == 0


def test_advance_when_over_cap(state, db, monkeypatch):
    _set_cfg(monkeypatch, NEWS_BACKLOG_CAP=50, NEWS_BACKLOG_KEEP=20)
    _seed(db, 80)
    floor, moved = nc.maybe_advance_on_backlog(db)
    assert moved > 0
    # 最新 20 条留在水位之上
    kept = _pending_above(db, floor)
    assert kept == 20, kept
    # 水位落在第 21 新那条的时间上（seed 是 ts=1000+i，最老 i=0）
    assert floor == 1000 + 80 - 20


def test_cap_zero_disables(state, db, monkeypatch):
    _set_cfg(monkeypatch, NEWS_BACKLOG_CAP=0)
    _seed(db, 200)
    floor, moved = nc.maybe_advance_on_backlog(db)
    assert moved == 0 and nc.get_floor() == 0


def test_idempotent_and_monotonic(state, db, monkeypatch):
    _set_cfg(monkeypatch, NEWS_BACKLOG_CAP=10, NEWS_BACKLOG_KEEP=5)
    _seed(db, 40)
    f1, _ = nc.maybe_advance_on_backlog(db)
    f2, moved2 = nc.maybe_advance_on_backlog(db)
    assert f2 == f1 and moved2 == 0       # 已经只剩 5 条，不再推进
    _seed(db, 30, start=5000)             # 又来一批 → 再推
    f3, moved3 = nc.maybe_advance_on_backlog(db)
    assert f3 > f2 and moved3 > 0
    assert _pending_above(db, f3) == 5


def test_disabled_by_switch(state, db, monkeypatch):
    _set_cfg(monkeypatch, NEWS_BACKLOG_CAP=10, NEWS_SKIP_BACKLOG_ON_RECOVERY=0)
    _seed(db, 60)
    _, moved = nc.maybe_advance_on_backlog(db)
    assert moved == 0
