# -*- coding: utf-8 -*-
"""v461：样本工厂的口径测试。

锁五件事：
  - 出场只认入场之后的第一根 5m 收盘，不能用入场前/入场那根；
  - 缺口超过 horizon+10 分钟就跳过，不补数据；
  - 同向闸的基准是窗口内最早一根已完成 bar，不是最近一根；
  - 未收盘的 bar 不能当基准（它自己就是未来）；
  - 分段晋升必须同时满足 n、t、多数分段为正、最差段不崩。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import sample_factory as sf


def _bar(ts, close, open_=None):
    return {"ts": ts, "open": open_ or close, "high": close, "low": close, "close": close}


def test_stats_basic():
    s = sf.stats([1.0, -1.0, 3.0])
    assert s["n"] == 3
    assert abs(s["avg"] - 1.0) < 1e-9
    assert abs(s["win_rate"] - 2 / 3) < 1e-3


def test_exit_uses_first_bar_after_horizon():
    bars = [_bar(0, 100), _bar(600, 101), _bar(1200, 102), _bar(1500, 200)]
    e = {"action": "BUY", "entry_ts": 600, "entry_price": 100.0}
    # 入场时刻是 600，20 分钟目标 1800；第一根 >=1800 是 1500? 不，1500<1800 -> 无
    assert sf.exit_net(e, bars, 1200, 2.0) is None
    # 目标 1200：第一根 >=1200 是 1200 close 102，而不是 600 或 1500
    e2 = {"action": "BUY", "entry_ts": 0, "entry_price": 100.0}
    assert abs(sf.exit_net(e2, bars, 1200, 2.0) - 1.98) < 1e-9


def test_price_at_interpolates_midpoint():
    bars = [_bar(0, 100), _bar(300, 110), _bar(600, 120)]
    # 目标 200：夹在 0(100) 与 300(110) 之间，线性插值 = 106.666...
    px = sf.price_at(bars, 200)
    assert abs(px - 106.6667) < 1e-3


def test_exit_sell_inverts():
    bars = [_bar(0, 100), _bar(1200, 98)]
    e = {"action": "SELL", "entry_ts": 0, "entry_price": 100.0}
    assert abs(sf.exit_net(e, bars, 1200, 0) - 2.0) < 1e-9


def test_gap_is_skipped_not_filled():
    bars = [_bar(0, 100), _bar(2000, 105)]
    e = {"action": "BUY", "entry_ts": 0, "entry_price": 100.0}
    # 目标 1200，最近 bar 2000，缺口 800s > 600s，必须跳过
    assert sf.exit_net(e, bars, 1200, 0) is None


def test_gate_uses_earliest_bar_in_window_not_latest():
    # 窗口起点价 99（+1.01% 同向），最近一根 101（反而逆向）；
    # 用最近一根会误杀，用窗口起点才是"这 20 分钟"的方向。
    bars = [_bar(0, 99), _bar(300, 101), _bar(600, 101)]
    e = {"action": "BUY", "entry_ts": 610, "entry_price": 100.0}
    got = sf.apply_gate([e], bars, window_min=20, min_move_pct=0.10)
    assert len(got) == 1


def test_gate_rejects_unfinished_base_bar():
    bars = [_bar(100, 99)]
    e = {"action": "BUY", "entry_ts": 100, "entry_price": 100.0}
    # bar 100 还没收盘（100+300>100），不能拿它的收盘价判断
    assert sf.apply_gate([e], bars, window_min=20, min_move_pct=0.10) == []


def test_fold_bounds_equal_time():
    b = sf.fold_bounds(0, 400, 4)
    assert b == [(0, 100), (100, 200), (200, 300), (300, 400)]


def test_promote_requires_majority_and_floor():
    pooled = {"n": 40, "avg": 0.2, "t_stat": 2.5}
    folds = [{"n": 10, "avg": 0.3}, {"n": 10, "avg": 0.2},
             {"n": 10, "avg": 0.1}, {"n": 10, "avg": -0.2}]
    # 传 promote 需要 stats 嵌套
    folds = [{"stats": f} for f in folds]
    ok = sf.promote(pooled, folds)
    assert ok["pass"] is True

    bad = [{"stats": {"n": 10, "avg": 0.3}}, {"stats": {"n": 10, "avg": -0.4}},
           {"stats": {"n": 10, "avg": -0.3}}, {"stats": {"n": 10, "avg": -0.2}}]
    res = sf.promote(pooled, bad)
    assert res["pass"] is False


def test_promote_rejects_t_below_two():
    folds = [{"stats": {"n": 10, "avg": 0.1}} for _ in range(4)]
    res = sf.promote({"n": 40, "avg": 0.1, "t_stat": 1.5}, folds)
    assert res["pass"] is False


def test_build_empty_ledger_is_sample_short(tmp_path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE kline_bars (symbol TEXT, timeframe TEXT, ts INTEGER, "
                 "open REAL, high REAL, low REAL, close REAL, volume REAL)")
    conn.execute("CREATE TABLE shadow_ledger (decision_id INTEGER, asset TEXT, "
                 "direction TEXT, entry_ts INTEGER, entry_price REAL, confidence REAL, "
                 "settled_at INTEGER)")
    import time
    now = int(time.time())
    conn.execute("INSERT INTO kline_bars VALUES ('BTC','5m',?,100,100,100,100,0)",
                 ((now - 3600) * 1000,))
    conn.commit()
    conn.close()
    rep = sf.build(days=60, cost_bps=2, db_path=db)
    assert rep["verdict"] == "样本不足"
    assert "没有可结算样本" in rep["headline"]
