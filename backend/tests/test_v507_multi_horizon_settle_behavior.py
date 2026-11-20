#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v507：多周期结算的行为测试（multi_horizon_settle 从 0% 起步）。

这块的核心风险是"把日内判断拿去用 5 天结算"——方向对、节奏错会被判成判断错，
再进反思、污染错误码归因。所以测试钉五条：
  ① 未到期的周期直接跳过，不写空行（写了空行 = 后面永远填不上，历史被冻住）；
  ② 取不到价就记 no_price，绝不补造价格；
  ③ |收益| > 90% 视为取价异常，记 implausible 且不写净收益；
  ④ T+1 品种的日内周期记 not_executable，不写假收益；
  ⑤ 输赢按**净收益**判（BUY 看 >0，SELL 看 <0），毛利为正但扣成本后亏钱 = 不计命中。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import multi_horizon_settle as mhs

D0 = 1_700_000_000       # 决策时刻


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE ohlcv_intraday(asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE ohlcv_daily(asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE trade_decisions(id INTEGER PRIMARY KEY, asset TEXT, final_action TEXT,
                                     entry_price REAL, decision_ts REAL);
    """)
    mhs.init_schema(c)
    return c


def _intra(c, asset, ts, px):
    c.execute("INSERT INTO ohlcv_intraday VALUES(?,?,?)", (asset, ts, px))


def _daily(c, asset, ts, px):
    c.execute("INSERT INTO ohlcv_daily VALUES(?,?,?)", (asset, ts, px))


def _rows(c, did=None):
    q = ("SELECT horizon, status, outcome_pct, pnl_pct, hit, fwd_price, note "
         "FROM decision_outcomes_horizon")
    if did is not None:
        q += f" WHERE decision_id={did}"
    q += " ORDER BY horizon_sec"
    return c.execute(q).fetchall()


# ── 建表幂等 ────────────────────────────────────────────────
def test_init_schema_is_idempotent(conn):
    mhs.init_schema(conn)
    mhs.init_schema(conn)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='decision_outcomes_horizon'").fetchall()
    assert len(rows) == 1


# ── 取价：分钟线优先，日线回退，拒零价 ────────────────────────
def test_resolve_price_prefers_intraday_and_respects_window(conn):
    # 目标时点 D0+1200，容差 60 秒 → 窗口 [1200, 1260]
    _intra(conn, "BTC", D0 + 1200, 101.0)                # 分钟线命中
    _daily(conn, "BTC", D0 + 1500, 50.0)
    px, ts = mhs._resolve_price(conn, "BTC", D0 + 1200, 60)
    assert px == pytest.approx(101.0), "分钟线优先"
    assert ts == D0 + 1200

    # 该窗口内没有分钟线、只有日线 → 回退到日线
    px2, ts2 = mhs._resolve_price(conn, "BTC", D0 + 1500, 60)
    assert px2 == pytest.approx(50.0), "分钟线缺位要回退到日线"
    assert ts2 == D0 + 1500


def test_resolve_price_rejects_zero_and_negative(conn):
    _intra(conn, "BTC", D0 + 10, 0.0)
    _intra(conn, "BTC", D0 + 20, -5.0)
    _intra(conn, "BTC", D0 + 30, 100.0)
    px, ts = mhs._resolve_price(conn, "BTC", D0, 900)
    assert px == pytest.approx(100.0) and ts == D0 + 30


def test_resolve_price_missing_tables_returns_none():
    bare = sqlite3.connect(":memory:")
    assert mhs._resolve_price(bare, "BTC", D0, 900) == (None, None)


# ── 输赢判定：按净收益 ──────────────────────────────────────
def test_hit_uses_net_sign():
    assert mhs._hit("BUY", 0.5) == 1
    assert mhs._hit("BUY", -0.5) == 0
    assert mhs._hit("SELL", -0.5) == 1
    assert mhs._hit("SELL", 0.5) == 0
    assert mhs._hit("BUY", 0.0) == 0, "零收益不算赢"
    assert mhs._hit("SELL", 0.0) == 0, "零收益不算赢"
    assert mhs._hit("HOLD", -5.0) == 0, "HOLD 不参与输赢"
    assert mhs._hit("BUY", None) == 0


# ── 纪律①：未到期不写空行 ──────────────────────────────────
def test_unmatured_horizons_are_not_written(conn):
    res = mhs.settle_horizons(conn, decision_id=1, asset="BTC", action="BUY",
                              entry_price=100.0, decision_ts=D0, now_ts=D0 + 60)
    assert res["written"] == 0, "刚决策 1 分钟，5 个周期一个都不该成熟"
    assert _rows(conn) == []


# ── 纪律②：取不到价记 no_price ─────────────────────────────
def test_missing_price_records_no_price_without_inventing(conn):
    res = mhs.settle_horizons(conn, decision_id=2, asset="BTC", action="BUY",
                              entry_price=100.0, decision_ts=D0, now_ts=D0 + 2 * 3600)
    got = {r[0]: r for r in _rows(conn)}
    assert set(got) == {"15m", "1h"}, "只成熟两个周期"
    for h in ("15m", "1h"):
        assert got[h][1] == "no_price"
        assert got[h][2] is None and got[h][3] is None and got[h][5] is None
        assert got[h][4] == 0


# ── 正常结算：毛利、净利、命中 ─────────────────────────────
def test_settled_horizon_computes_net_and_hit(conn):
    _intra(conn, "BTC", D0 + 900, 101.0)
    res = mhs.settle_horizons(conn, decision_id=3, asset="BTC", action="BUY",
                              entry_price=100.0, decision_ts=D0, now_ts=D0 + 3600)
    r = {x[0]: x for x in _rows(conn)}["15m"]
    assert r[1] == "settled"
    assert r[2] == pytest.approx(1.0), "毛利 +1.0%"
    assert r[3] < r[2], "净利必须扣成本后小于毛利"
    assert r[4] == 1 and r[5] == pytest.approx(101.0)
    assert res["horizons"]["15m"]["status"] == "settled"


def test_small_gain_turns_into_loss_after_cost(conn):
    """毛利为正但扣成本后为负 → BUY 不算命中。这是"看起来对、实际亏"的样本。"""
    _intra(conn, "BTC", D0 + 900, 100.02)      # +0.02%，远小于往返成本
    mhs.settle_horizons(conn, decision_id=4, asset="BTC", action="BUY",
                        entry_price=100.0, decision_ts=D0, now_ts=D0 + 3600)
    r = {x[0]: x for x in _rows(conn)}["15m"]
    assert r[2] > 0 and r[3] < 0, "毛利正、净利负"
    assert r[4] == 0


def test_sell_profits_when_price_drops(conn):
    _intra(conn, "BTC", D0 + 900, 99.0)
    mhs.settle_horizons(conn, decision_id=5, asset="BTC", action="SELL",
                        entry_price=100.0, decision_ts=D0, now_ts=D0 + 3600)
    r = {x[0]: x for x in _rows(conn)}["15m"]
    assert r[2] == pytest.approx(-1.0), "跌 1% 的原始收益是 -1%"
    assert r[4] == 1, "SELL 下跌 = 命中"


# ── 纪律③：离谱收益记 implausible，不给净收益 ───────────────
def test_implausible_jump_is_flagged_not_settled(conn):
    _intra(conn, "BTC", D0 + 900, 500.0)       # +400%，必是取价异常
    mhs.settle_horizons(conn, decision_id=6, asset="BTC", action="BUY",
                        entry_price=100.0, decision_ts=D0, now_ts=D0 + 3600)
    r = {x[0]: x for x in _rows(conn)}["15m"]
    assert r[1] == "implausible"
    assert r[3] is None and r[4] == 0, "异常样本不许写净收益、不许算命中"
    assert "90%" in (r[6] or "")


# ── 纪律④：T+1 的日内周期不可执行 ──────────────────────────
def test_t_plus_1_intraday_is_not_executable(conn, monkeypatch):
    # AAPL 这类 T+1：最早平仓在次日开盘，15m/1h/4h 全在它之前
    monkeypatch.setattr(mhs, "_resolve_price",
                        lambda *a, **k: (None, None), raising=False)
    info = mhs.settle_horizons(conn, decision_id=7, asset="AAPL", action="BUY",
                               entry_price=100.0, decision_ts=D0,
                               now_ts=D0 + 2 * 86400)
    got = {r[0]: r for r in _rows(conn)}
    for h in ("15m", "1h", "4h"):
        assert got[h][1] == "not_executable"
        assert got[h][2] is None and got[h][3] is None
    assert "T+1" in (got["15m"][6] or "")


def test_bad_input_is_refused(conn):
    res = mhs.settle_horizons(conn, decision_id=8, asset="  ", action="BUY",
                              entry_price=100.0, decision_ts=D0, now_ts=D0 + 86400)
    assert res["written"] == 0 and res["reason"] == "bad input"
    res2 = mhs.settle_horizons(conn, decision_id=9, asset="BTC", action="BUY",
                               entry_price=0.0, decision_ts=D0, now_ts=D0 + 86400)
    assert res2["written"] == 0 and res2["reason"] == "bad input"


# ── 幂等：同一决策同一周期只有一行 ──────────────────────────
def test_rerun_upserts_instead_of_duplicating(conn):
    _intra(conn, "BTC", D0 + 900, 101.0)
    for _ in range(2):
        mhs.settle_horizons(conn, decision_id=10, asset="BTC", action="BUY",
                            entry_price=100.0, decision_ts=D0, now_ts=D0 + 3600)
    rows = _rows(conn, 10)
    assert len(rows) == len({r[0] for r in rows})


# ── run_once：动手扫库 ──────────────────────────────────────
def test_run_once_settles_and_backfills(conn, monkeypatch):
    now = D0 + 20 * 86400 + 600
    recent_ts = now - 2 * 86400          # 最近这批里最新的
    old_ts = now - 25 * 86400            # 掉出"最近 limit 条"，但还在 30 天回溯窗口
    _intra(conn, "BTC", recent_ts + 900, 101.0)
    _intra(conn, "BTC", old_ts + 900, 102.0)
    conn.execute("INSERT INTO trade_decisions VALUES(1,'BTC','BUY',100.0,?)", (float(recent_ts),))
    conn.execute("INSERT INTO trade_decisions VALUES(2,'BTC','BUY',100.0,?)", (float(old_ts),))
    conn.commit()
    res = mhs.run_once(conn, now_ts=now, limit=1)
    assert res["decisions"] == 2, "回溯批次必须把老决策带回来"
    assert res["backfill"] >= 1
    assert res["written"] > 0
    assert any(r[1] == "settled" for r in _rows(conn, 2)), "老决策也该结算出读数"


def test_read_horizons_returns_dicts(conn):
    _intra(conn, "BTC", D0 + 900, 101.0)
    mhs.settle_horizons(conn, decision_id=11, asset="BTC", action="BUY",
                        entry_price=100.0, decision_ts=D0, now_ts=D0 + 3600)
    got = mhs.read_horizons(conn, 11)
    assert [r["horizon"] for r in got] == ["15m", "1h"]
    assert got[0]["status"] == "settled"
