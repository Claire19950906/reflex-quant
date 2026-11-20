#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v516：时刻→事件反向检索（event_reverse_lookup）的行为测试。

这条链回答的是"这一刻为什么这么走"。口径一旦松，就会变成事后拟合：
  ① 窗口是硬约束：比异动晚 30 分钟以上的新闻不算起因，早于 3 小时也不算；
  ② 噪声与已删新闻不进候选，空标题（只剩 hash 前缀）也不进；
  ③ 品种匹配三条路：资产列表命中 / 单资产字段命中 / 标题里出现代码；
  ④ 打分要能解释：方向一致 +0.1，越远越低（下限 0.5），已配对归因排前面；
  ⑤ 资金面佐证按 kind 去重取最新，未知 kind 不进。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import event_reverse_lookup as erl  # noqa: E402

T = 1_760_000_000
_OPEN = []


@pytest.fixture(autouse=True)
def _close():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _conn():
    conn = sqlite3.connect(":memory:")
    _OPEN.append(conn)
    conn.executescript(
        """
        CREATE TABLE critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER,
                                           move_pct REAL, move_kind TEXT,
                                           duration_min INTEGER, algo_score REAL);
        CREATE TABLE news_events (id INTEGER PRIMARY KEY, source TEXT, headline TEXT,
                                  assets TEXT, asset TEXT, score REAL, ts INTEGER,
                                  action TEXT, status TEXT, is_noise INTEGER);
        CREATE TABLE event_move_attribution (ev_news_id INTEGER, asset TEXT, move_ts INTEGER);
        CREATE TABLE flow_snapshots (asset TEXT, ts INTEGER, kind TEXT, value REAL);
        """
    )
    return conn


def _news(conn, nid, headline, ts, assets="BTC", asset="BTC", score=0.5,
          is_noise=0, status="ok"):
    conn.execute(
        "INSERT INTO news_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        (nid, "src", headline, assets, asset, score, ts, "BUY", status, is_noise),
    )


def _move(conn, mid, asset, ts, pct, kind="fast_spike"):
    conn.execute("INSERT INTO critical_price_moves VALUES (?,?,?,?,?,?,?)",
                 (mid, asset, ts, pct, kind, 15, 0.8))


# ── ① 窗口 ────────────────────────────────────────────────────
def test_event_later_than_lookahead_is_not_a_cause():
    conn = _conn()
    _news(conn, 1, "BTC breaks new high", T + erl.LOOKAHEAD_SEC + 60)
    res = erl.lookup(conn, "BTC", T)
    assert res["event_count"] == 0


def test_event_earlier_than_lookback_is_not_a_cause():
    conn = _conn()
    _news(conn, 1, "BTC rallies", T - erl.LOOKBACK_SEC - 60)
    res = erl.lookup(conn, "BTC", T)
    assert res["event_count"] == 0


def test_boundary_events_are_inside_the_window():
    conn = _conn()
    _news(conn, 1, "BTC rallies", T - erl.LOOKBACK_SEC)   # 正好 -3h
    _news(conn, 2, "BTC climbs", T + erl.LOOKAHEAD_SEC)   # 正好 +30min
    res = erl.lookup(conn, "BTC", T)
    assert res["event_count"] == 2


# ── ② 噪声 / 已删 / 空标题 ──────────────────────────────────
def test_noise_and_removed_news_are_filtered_out():
    conn = _conn()
    _news(conn, 1, "BTC rallies", T, is_noise=1)
    _news(conn, 2, "BTC rallies", T, status="removed")
    _news(conn, 3, "BTC rallies", T)
    res = erl.lookup(conn, "BTC", T)
    assert res["event_count"] == 1


def test_hash_prefix_only_headline_is_dropped():
    conn = _conn()
    _news(conn, 1, "[hash:abc123]   ", T)
    _news(conn, 2, "[hash:def456] BTC surges", T)
    res = erl.lookup(conn, "BTC", T)
    assert res["event_count"] == 1
    assert res["events"][0]["headline"] == "BTC surges"


# ── ③ 品种匹配 ───────────────────────────────────────────────
def test_asset_matches_by_list_field_or_headline_mention():
    conn = _conn()
    _news(conn, 1, "no name here", T, assets="ETH,BTC", asset="")
    _news(conn, 2, "no name here", T, assets="", asset="BTC")
    _news(conn, 3, "btc volume explodes", T, assets="", asset="")
    _news(conn, 4, "ETH only", T, assets="ETH", asset="ETH")
    res = erl.lookup(conn, "btc", T)
    assert res["event_count"] == 3
    types = {e["match_type"] for e in res["events"]}
    assert "headline" in types and "asset" in types


def test_empty_asset_means_every_market():
    conn = _conn()
    _news(conn, 1, "ETH only", T, assets="ETH", asset="ETH")
    _news(conn, 2, "XAU only", T, assets="XAU", asset="XAU")
    res = erl.lookup(conn, "", T)
    assert res["asset"] == "ALL"
    assert res["event_count"] == 2


# ── ④ 方向与打分 ─────────────────────────────────────────────
def test_direction_words_follow_the_same_vocabulary_as_attribution():
    assert erl._headline_direction("黄金大涨，突破新高") == 1
    assert erl._headline_direction("BTC plunged after the ban") == -1
    assert erl._headline_direction("涨了又跌") == 0  # 两边都有词 → 不猜
    assert erl._headline_direction("") == 0


def test_direction_alignment_adds_a_small_bonus():
    conn = _conn()
    _move(conn, 1, "BTC", T, 2.0)
    _news(conn, 1, "BTC rally", T)
    _news(conn, 2, "BTC drop", T)
    res = erl.lookup(conn, "BTC", T)
    by_id = {e["event_id"]: e for e in res["events"]}
    assert by_id[1]["score"] > by_id[2]["score"]
    assert by_id[1]["score"] == pytest.approx(0.6)  # 0.5 × 1.0 + 0.1


def test_score_decays_with_distance_but_never_below_the_floor():
    conn = _conn()
    _news(conn, 1, "BTC rumor", T, score=0.5)
    _news(conn, 2, "BTC rumor", T - 5 * 3600 + 60)  # 窗外，会被过滤
    conn.execute("DELETE FROM news_events WHERE id=2")
    _news(conn, 3, "BTC rumor", T - erl.LOOKBACK_SEC + 1, score=0.5)
    res = erl.lookup(conn, "BTC", T)
    near = [e for e in res["events"] if e["event_id"] == 1][0]
    far = [e for e in res["events"] if e["event_id"] == 3][0]
    assert near["score"] == pytest.approx(0.5)
    assert far["score"] >= 0.25  # recency 下限 0.5 → 0.25
    assert far["score"] < near["score"]


def test_linked_events_rank_first_even_with_lower_score():
    conn = _conn()
    _news(conn, 1, "BTC rallies", T, score=0.9)
    _news(conn, 2, "BTC drops", T, score=0.3)
    conn.execute("INSERT INTO event_move_attribution VALUES (2,'btc',?)", (T,))
    res = erl.lookup(conn, "BTC", T)
    assert res["events"][0]["event_id"] == 2
    assert res["events"][0]["linked"] is True
    assert res["events"][1]["linked"] is False


def test_missing_score_is_treated_as_neutral_not_zero():
    conn = _conn()
    _news(conn, 1, "BTC rally", T, score=None)
    res = erl.lookup(conn, "BTC", T)
    # 缺分不是 0 分：按中性 0.5 起算（本用例没有同期行情，故无方向加分）
    assert res["events"][0]["score"] == pytest.approx(0.5)


def test_max_events_truncates_but_event_count_reports_the_truth():
    conn = _conn()
    for i in range(1, 6):
        _news(conn, i, f"BTC item {i}", T)
    res = erl.lookup(conn, "BTC", T, max_events=2)
    assert len(res["events"]) == 2
    assert res["event_count"] == 5


# ── ⑤ 同期行情与资金面 ──────────────────────────────────────
def test_moves_are_window_limited_and_asset_filtered():
    conn = _conn()
    _move(conn, 1, "BTC", T + 1800, 2.0)
    _move(conn, 2, "ETH", T, 3.0)
    _move(conn, 3, "BTC", T + 7200, -1.0)  # 超出 ±90 分钟
    conn.execute("INSERT INTO critical_price_moves VALUES (4,'BTC',?,NULL,'fast',5,0.1)",
                 (T,))
    res = erl.lookup(conn, "BTC", T)
    assert [m["move_id"] for m in res["moves"]] == [4, 1]  # 按距目标时刻远近排序
    assert res["moves"][0]["move_pct"] == 0.0


def test_flow_notes_take_the_latest_value_per_kind():
    conn = _conn()
    for ts, kind, val in [(T - 60, "oi", 100.0), (T - 120, "oi", 90.0),
                          (T - 30, "funding", 0.01), (T - 10, "bogus", 1.0)]:
        conn.execute("INSERT INTO flow_snapshots VALUES ('BTC',?,?,?)", (ts, kind, val))
    notes = erl._flow_notes(conn, "BTC", T)
    assert "持仓=100" in notes and "费率=0.01" in notes
    assert len(notes) == 2
    assert erl._flow_notes(conn, "", T) == []


def test_missing_tables_degrade_to_empty_instead_of_crashing():
    conn = sqlite3.connect(":memory:")
    _OPEN.append(conn)
    res = erl.lookup(conn, "BTC", T)
    assert res["event_count"] == 0 and res["moves"] == []


def test_result_shape_is_stable_for_the_console_entry():
    conn = _conn()
    _news(conn, 1, "BTC rallies", T)
    res = erl.lookup(conn, "BTC", T)
    assert set(res) == {"asset", "target_ts", "target_time", "window",
                       "moves", "events", "event_count"}
    ev = res["events"][0]
    assert set(ev) == {"event_id", "headline", "source", "event_ts", "event_time",
                      "lag_sec", "lag_min", "direction", "score", "linked",
                      "match_type", "flow_notes"}
    assert ev["lag_min"] == 0.0
