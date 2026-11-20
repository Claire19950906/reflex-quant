#!/usr/bin/env python3
"""v433 回归：时间戳统一 epoch 秒 + 门卫故障归档改名 gate_down。

守三件事（全部来自 2026-11-17 的实测）：
1. `coerce_epoch_ts` 把 int / 10 位串 / 13 位毫秒 / ISO 带时区 / 本地时间串
   都归一成 epoch 秒；非法值返回 default，绝不猜；
2. `insert_raw_news` 落库后 timestamp 必须等于 ts（同一真源），
   否则 `now - timestamp` 对 ISO 行按 0 算，超龄/窗口/去重全部静默出错；
3. 门卫未判定（gate_unavailable=2）超时的行归档理由必须是 `gate_down`
   —— 不能再用 `degraded_stale` 把「门卫故障」伪装成「新闻旧了」。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

import db
import engine.ai_worker as aiw
import pipeline_guard as pg

_HOUR = 3600


# ─────────────────── ① coerce_epoch_ts ───────────────────

@pytest.mark.parametrize("value,expected", [
    (1794912899, 1794912899),            # int
    ("1794912899", 1794912899),          # 10 位 epoch 串
    ("1794912899000", 1794912899),       # 13 位毫秒 → 秒
    (1794912899000, 1794912899),
    ("2026-11-17T18:55:02+08:00", 1794912902),  # ISO 带时区，按自带时区
    ("2026-11-17T10:55:02Z", 1794912902),       # ISO UTC
    ("2026-11-17 18:55:02", 1794912902),        # 无时区 → 按本地时区
])
def test_coerce_epoch_ts_accepts_all_shapes(value, expected):
    assert db.coerce_epoch_ts(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "不是时间", True, "abc-12"])
def test_coerce_epoch_ts_never_guesses(value):
    assert db.coerce_epoch_ts(value, default=None) is None
    assert db.coerce_epoch_ts(value, default=7) == 7


# ─────────────────── ② insert_raw_news 同源落库 ───────────────────

_SCHEMA = """
CREATE TABLE raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL DEFAULT '',
    ts INTEGER,
    published_at TEXT DEFAULT '',
    url TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    is_noise INTEGER NOT NULL DEFAULT 0,
    relevance_score REAL NOT NULL DEFAULT 0
);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA)
    yield c
    c.close()


def test_insert_raw_news_writes_epoch_equal_to_ts(conn):
    ts = 1794912902
    db.insert_raw_news(conn, source="jin10", content="测试",
                       timestamp="2026-11-17T18:55:02+08:00", ts=ts)
    row = conn.execute("SELECT timestamp, ts FROM raw_news").fetchone()
    assert row[0] == str(ts), "timestamp 必须是 epoch 秒字符串"
    assert row[1] == ts
    assert len(row[0]) == 10


def test_insert_raw_news_derives_ts_when_missing(conn):
    """调用方只给 ISO、不给 ts → 用同一个归一值同时写两列。"""
    db.insert_raw_news(conn, source="jin10", content="测试",
                       timestamp="2026-11-17T18:55:02+08:00")
    row = conn.execute("SELECT timestamp, ts FROM raw_news").fetchone()
    assert row[0] == "1794912902"
    assert row[1] == 1794912902


def test_insert_raw_news_falls_back_to_now_on_garbage(conn):
    """解析不出来也不能写 ISO 原文（那会被 SQL 当 0）。"""
    before = int(time.time())
    db.insert_raw_news(conn, source="未知源", content="测试", timestamp="坏时间")
    row = conn.execute("SELECT timestamp, ts FROM raw_news").fetchone()
    assert row[0].isdigit() and len(row[0]) == 10
    assert int(row[1]) >= before
    assert row[0] == str(row[1])


# ─────────────────── ③ 门卫故障归档理由 = gate_down ───────────────────

_ARCHIVE_SCHEMA = """
CREATE TABLE raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    is_noise INTEGER NOT NULL DEFAULT 0,
    ts INTEGER,
    processing_at TEXT DEFAULT NULL,
    gate_unavailable INTEGER DEFAULT 0,
    gate_verdict TEXT DEFAULT '',
    filter_reason TEXT NOT NULL DEFAULT ''
);
"""


def _archive_conn():
    c = sqlite3.connect(":memory:")
    c.executescript(_ARCHIVE_SCHEMA)
    now = int(time.time())
    c.execute("INSERT INTO raw_news(status, ts, gate_unavailable, gate_verdict) "
              "VALUES ('PENDING', ?, 2, '')", (now - 10 * _HOUR,))
    c.execute("INSERT INTO raw_news(status, ts, gate_unavailable, gate_verdict) "
              "VALUES ('PENDING', ?, 2, '')", (now - 20 * 60,))
    c.commit()
    return c


def test_gate_down_archive_uses_own_reason(monkeypatch):
    monkeypatch.delenv("NEWS_EXPIRE_GATE_DOWN_H", raising=False)
    monkeypatch.delenv("NEWS_EXPIRE_DEGRADED_H", raising=False)
    c = _archive_conn()
    aiw._expire_stale_news(c)
    row = c.execute("SELECT status, filter_reason FROM raw_news WHERE id=1").fetchone()
    assert row[0] == "FILTERED"
    assert row[1] == "gate_down", "门卫故障必须自成一类，不能叫 degraded_stale"
    # 20 分钟那条还在归档宽限（默认 50min）内
    assert c.execute("SELECT status FROM raw_news WHERE id=2").fetchone()[0] == "PENDING"
    c.close()


def test_gate_down_archive_env_alias_respected(monkeypatch):
    """老变量名 NEWS_EXPIRE_DEGRADED_H 仍要生效（旧 .env 不至于突然改成 0）。"""
    monkeypatch.delenv("NEWS_EXPIRE_GATE_DOWN_H", raising=False)
    monkeypatch.setenv("NEWS_EXPIRE_DEGRADED_H", "0")
    c = _archive_conn()
    assert aiw._expire_stale_news(c) == 0
    assert c.execute("SELECT status FROM raw_news WHERE id=1").fetchone()[0] == "PENDING"
    c.close()


# ─────────────────── ④ 守卫断言：异形时间戳当场变红 ───────────────────

_GUARD_SCHEMA = """
CREATE TABLE raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT '',
    ts INTEGER
);
"""


def _guard_conn():
    c = sqlite3.connect(":memory:")
    c.executescript(_GUARD_SCHEMA)
    now = int(time.time())
    c.execute("INSERT INTO raw_news(timestamp, ts) VALUES (?, ?)", (str(now), now))
    c.commit()
    return c


def test_guard_timestamp_format_passes_on_epoch():
    c = _guard_conn()
    res = pg.check_timestamp_format(c, int(time.time()), 24)
    assert res["severity"] == "ok" and res["value"] == 0
    c.close()


def test_guard_timestamp_format_flags_iso_and_mismatch():
    c = _guard_conn()
    now = int(time.time())
    c.execute("INSERT INTO raw_news(timestamp, ts) VALUES (?, ?)",
              ("2026-11-17T18:55:02+08:00", now))          # ISO
    c.execute("INSERT INTO raw_news(timestamp, ts) VALUES (?, ?)",
              (str(now - 100), now))                        # 10 位但与 ts 不一致
    c.commit()
    res = pg.check_timestamp_format(c, now, 24)
    assert res["severity"] == "broken" and res["value"] == 2
    c.close()
