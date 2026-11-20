#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v501：归因 T+1 打标的行为测试（attribution_t1_labeler 从 0% 起步）。

这块是"归因到底准不准"的唯一尺子：不打标，归因能力只能算覆盖率，算不了对错。
规则里有两条硬约束必须钉住：
  ① 铁律 2 —— 进场一律推到异动次日收盘，不许拿异动当天的收盘价当进场价；
  ② 可分离性只看 ±15 分钟窗口，窗口外的事件不算"另一条能解释它的新闻"。
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import attribution_t1_labeler as t1


import pytest


def _ts(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE ohlcv_daily(asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE news_events(id INTEGER PRIMARY KEY, asset TEXT, assets TEXT,
                                 ts INTEGER, is_noise INTEGER DEFAULT 0, action TEXT);
        CREATE TABLE event_move_attribution(id INTEGER PRIMARY KEY, asset TEXT, move_ts INTEGER,
                                            move_pct REAL, move_kind TEXT,
                                            ev_action TEXT, news_event_id INTEGER);
    """)
    yield c
    c.close()


def _add_series(c, asset, closes, start="2026-11-10"):
    days = ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"]
    for d, px in zip(days, closes):
        c.execute("INSERT INTO ohlcv_daily VALUES(?,?,?)", (asset, _ts(d), px))
    c.commit()


def _add_move(c, mid, asset, move_date, move_pct, action="BUY"):
    c.execute("INSERT INTO event_move_attribution VALUES(?,?,?,?,?,?,?)",
              (mid, asset, _ts(move_date), move_pct, "NEWS", action, None))
    c.commit()


# ── 日线读取 ───────────────────────────────────────────────────
def test_daily_closes_uppercases_and_sorts(conn):
    _add_series(conn, " btc ", [100, 101, 102, 103, 104, 105])
    book = t1.daily_closes(conn)
    assert list(book) == ["BTC"]
    assert list(book["BTC"]) == sorted(book["BTC"])


def test_daily_closes_ignores_zero_and_null(conn):
    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC',?,0)", (_ts("2026-11-10"),))
    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC',?,NULL)", (_ts("2026-11-11"),))
    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC',?,100)", (_ts("2026-11-12"),))
    conn.commit()
    book = t1.daily_closes(conn)
    assert list(book["BTC"]) == ["2026-11-12"]


# ── 铁律 2：进场是异动次日收盘 ────────────────────────────────
def test_entry_is_next_day_close_not_move_day(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133.1, 146.41, 161])
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    assert t1.label(conn) == 1
    row = conn.execute("""SELECT entry_date, fwd1, fwd3 FROM event_attribution_t1""").fetchone()
    assert row[0] == "2026-11-11"                 # 异动次日，不是 11-10
    assert row[1] == pytest.approx(0.1)           # 110 → 121
    assert row[2] == pytest.approx(0.331)         # 110 → 146.41（进场后第 3 根）


# ── 延续与新闻方向 ─────────────────────────────────────────────
def test_continued_true_when_move_keeps_direction(conn):
    _add_series(conn, "BTC", [100, 101, 102, 103, 104, 105])
    _add_move(conn, 1, "BTC", "2026-11-10", 5.0)      # UP
    t1.label(conn)
    row = conn.execute("SELECT move_dir, continued FROM event_attribution_t1").fetchone()
    assert row[0] == "UP" and row[1] == 1


def test_continued_false_when_move_reverses(conn):
    _add_series(conn, "BTC", [100, 105, 104, 103, 102, 101])
    _add_move(conn, 1, "BTC", "2026-11-10", 5.0)
    t1.label(conn)
    assert conn.execute("SELECT continued FROM event_attribution_t1").fetchone()[0] == 0


def test_news_dir_correct_buy_and_sell(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133, 146, 161])
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0, action="BUY")
    _add_move(conn, 2, "BTC", "2026-11-10", 10.0, action="SELL")
    t1.label(conn)
    rows = dict(conn.execute("SELECT attribution_id, news_dir_correct FROM event_attribution_t1"))
    assert rows[1] == 1      # BUY 且确实涨了
    assert rows[2] == 0      # SELL 但涨了 → 方向错


def test_news_dir_correct_null_when_action_is_hold(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133, 146, 161])
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0, action="HOLD")
    t1.label(conn)
    assert conn.execute("SELECT news_dir_correct FROM event_attribution_t1").fetchone()[0] is None


# ── 可分离性：只看 ±15 分钟 ────────────────────────────────────
def test_other_news_in_window_makes_it_not_separable(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133, 146, 161])
    move_ts = _ts("2026-11-10")
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    conn.execute("INSERT INTO news_events VALUES(9,'BTC','BTC',?,0,'BUY')", (move_ts + 60,))
    conn.execute("INSERT INTO news_events VALUES(10,'BTC','BTC',?,0,'BUY')", (move_ts + 300,))
    conn.commit()
    t1.label(conn)
    assert conn.execute("SELECT separable FROM event_attribution_t1").fetchone()[0] == 0


def test_news_outside_window_keeps_it_separable(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133, 146, 161])
    move_ts = _ts("2026-11-10")
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    conn.execute("INSERT INTO news_events VALUES(10,'BTC','BTC',?,0,'BUY')", (move_ts + 3600,))
    conn.commit()
    t1.label(conn)
    assert conn.execute("SELECT separable FROM event_attribution_t1").fetchone()[0] == 1


def test_noise_news_does_not_count_toward_separability(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133, 146, 161])
    move_ts = _ts("2026-11-10")
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    conn.execute("INSERT INTO news_events VALUES(11,'BTC','BTC',?,1,'BUY')", (move_ts + 120,))
    conn.commit()
    t1.label(conn)
    assert conn.execute("SELECT separable FROM event_attribution_t1").fetchone()[0] == 1


# ── 边界：数据不足就跳过，不编 ─────────────────────────────────
def test_skips_when_no_daily_series(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133, 146, 161])
    _add_move(conn, 1, "ETH", "2026-11-10", 10.0)
    assert t1.label(conn) == 0


def test_skips_when_move_is_last_day(conn):
    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC',?,100)", (_ts("2026-11-10"),))
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    assert t1.label(conn) == 0          # 没有次日可进场


def test_skips_when_entry_is_last_bar(conn):
    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC',?,100)", (_ts("2026-11-10"),))
    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC',?,110)", (_ts("2026-11-11"),))
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    assert t1.label(conn) == 0          # 进场日之后没有收盘，算不出 fwd1


def test_fwd3_null_when_not_enough_bars(conn):
    for d, px in (("2026-11-10", 100), ("2026-11-11", 110), ("2026-11-12", 121)):
        conn.execute("INSERT INTO ohlcv_daily VALUES('BTC',?,?)", (_ts(d), px))
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    t1.label(conn)
    row = conn.execute("SELECT fwd1, fwd3, continued FROM event_attribution_t1").fetchone()
    assert row[0] == pytest.approx(0.1)
    assert row[1] is None and row[2] is None


def test_rerun_replaces_old_labels(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133, 146, 161])
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    t1.label(conn)
    t1.label(conn)
    assert conn.execute("SELECT COUNT(*) FROM event_attribution_t1").fetchone()[0] == 1


def test_labelled_at_is_utc_iso(conn):
    _add_series(conn, "BTC", [100, 110, 121, 133, 146, 161])
    _add_move(conn, 1, "BTC", "2026-11-10", 10.0)
    t1.label(conn)
    stamp = conn.execute("SELECT labelled_at FROM event_attribution_t1").fetchone()[0]
    assert stamp.startswith("20") and "T" in stamp
