# -*- coding: utf-8 -*-
"""v462：模拟盘账本的口径测试。

锁四件事：
  - 空头浮盈方向不能反；标记价只认 tick / 1m / 5m，没有就不估值；
  - 无止损、集中度、手续费未入账必须报出来；
  - 重置后没有已平仓样本时，不许算胜率；
  - 账本只从当前快照往后算，不回溯。
"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import ledger as lm


def _db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Path(f.name)


def _init(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE t2_account(id INTEGER PRIMARY KEY, mode TEXT, cash REAL, equity REAL,
                            realized_pnl REAL, fee_bps REAL, updated_at INTEGER);
    CREATE TABLE t2_positions(id INTEGER PRIMARY KEY, asset TEXT, side TEXT, qty REAL,
                              entry_price REAL, entry_ts INTEGER, stop_price REAL,
                              target_price REAL, time_stop_min INTEGER, trailing_bp REAL,
                              thesis_json TEXT, strategy_id TEXT, subaccount TEXT,
                              status TEXT, closed_ts INTEGER, exit_price REAL,
                              realized_pnl REAL, note TEXT);
    CREATE TABLE t2_orders(id INTEGER PRIMARY KEY, ts INTEGER, asset TEXT, side TEXT,
                           order_type TEXT, qty REAL, notional_usdt REAL, price REAL,
                           stop_price REAL, target_price REAL, oco_group TEXT,
                           time_in_force TEXT, status TEXT, mode TEXT, decision_id INTEGER,
                           parent_position_id INTEGER, filled_price REAL, filled_qty REAL,
                           note TEXT, updated_at INTEGER);
    CREATE TABLE t2_cost_ledger(id INTEGER PRIMARY KEY, ts INTEGER, day TEXT,
                                strategy_id TEXT, category TEXT, amount REAL,
                                currency TEXT, note TEXT);
    CREATE TABLE market_ticks(symbol TEXT, ts INTEGER, price REAL);
    CREATE TABLE kline_bars(symbol TEXT, timeframe TEXT, ts INTEGER, close REAL);
    """)
    c.commit()
    c.close()


def _seed(path, cash=10000.0, side="short", qty=1.0, entry=100.0,
          mark=99.0, stop=None, closed=False, fee_rows=0, filled_notional=5000.0,
          mark_age_s=0):
    c = sqlite3.connect(path)
    now = int(time.time())
    c.execute("INSERT INTO t2_account VALUES(1,'paper',?,?,0,8,?)",
              (cash, cash, now))
    status = "closed" if closed else "open"
    c.execute(
        "INSERT INTO t2_positions(id,asset,side,qty,entry_price,entry_ts,stop_price,"
        "target_price,status,thesis_json,note,realized_pnl,closed_ts,exit_price) "
        "VALUES(1,'XAU',?,?,?,?,?,?,?,?,?,?,?,?)",
        (side, qty, entry, now - 1800, stop, None, status,
         '{"decision_id":12700}', 'executor decision#12700',
         12.34 if closed else None, now - 60 if closed else None,
         101.0 if closed else None))
    if mark is not None:
        c.execute("INSERT INTO market_ticks(symbol,ts,price) VALUES('XAU',?,?)",
                  (now - mark_age_s, mark))
    c.execute("INSERT INTO t2_orders(id,ts,asset,side,qty,notional_usdt,price,status,"
              "mode,decision_id,filled_price,filled_qty) "
              "VALUES(53,?,'XAU','sell',?,? ,?,'filled','paper',12700,?,?)",
              (now - 1800, qty, filled_notional, entry, entry, qty))
    for i in range(fee_rows):
        c.execute("INSERT INTO t2_cost_ledger(id,ts,day,strategy_id,category,amount,currency,note) "
                  "VALUES(?,?, '2026-11-19','ai_news','fee',4.0,'USDT','x')", (i + 1, now))
    c.commit()
    c.close()


def test_short_unrealized_direction_and_equity():
    p = _db(); _init(p); _seed(p, side="short", entry=100, mark=99)
    rep = lm.build(p)
    pos = rep["positions"][0]
    assert pos["unrealized_pct"] == 1.0
    assert pos["unrealized_usdt"] == 1.0
    assert rep["account"]["equity_mtm"] == 10001.0
    p.unlink()


def test_long_unrealized_direction():
    p = _db(); _init(p); _seed(p, side="long", entry=100, mark=101)
    rep = lm.build(p)
    assert rep["positions"][0]["unrealized_pct"] == 1.0
    p.unlink()


def test_no_mark_does_not_invent_price():
    p = _db(); _init(p); _seed(p, mark=None)
    rep = lm.build(p)
    pos = rep["positions"][0]
    assert pos["mark"] is None
    assert rep["account"]["equity_mtm"] == 10000.0
    assert any("没有行情价" in w["text"] for w in rep["reconciliation"])
    p.unlink()


def test_no_stop_and_concentration_warnings():
    p = _db(); _init(p); _seed(p, qty=50, entry=100, mark=100, stop=None)
    rep = lm.build(p)
    texts = " ".join(w["text"] for w in rep["reconciliation"])
    assert "无止损" in texts
    assert "占权益" in texts
    p.unlink()


def test_fee_not_booked_warning():
    p = _db(); _init(p); _seed(p, fee_rows=0, filled_notional=5000)
    rep = lm.build(p)
    assert rep["costs"]["expected_fee"] == 4.0
    assert any("手续费未入账" in w["text"] for w in rep["reconciliation"])
    p.unlink()


def test_no_closed_sample_blocks_win_rate():
    p = _db(); _init(p); _seed(p, closed=False)
    rep = lm.build(p)
    assert rep["closed"] == []
    assert any("尚无已平仓样本" in w["text"] for w in rep["reconciliation"])
    assert rep["risk"]["realized_closed"] == 0.0
    p.unlink()


def test_closed_trade_is_counted():
    p = _db(); _init(p); _seed(p, closed=True)
    rep = lm.build(p)
    assert len(rep["closed"]) == 1
    assert rep["risk"]["realized_closed"] == 12.34
    assert not any("尚无已平仓样本" in w["text"] for w in rep["reconciliation"])
    p.unlink()


def test_stale_mark_is_flagged():
    p = _db(); _init(p); _seed(p, mark_age_s=lm.MARK_STALE_S + 60)
    rep = lm.build(p)
    assert rep["positions"][0]["mark_stale"] is True
    assert any("行情已旧" in w["text"] for w in rep["reconciliation"])
    p.unlink()


def test_token_rows_are_not_counted_as_fees():
    p = _db(); _init(p); _seed(p, fee_rows=0)
    c = sqlite3.connect(p)
    c.execute("INSERT INTO t2_cost_ledger(id,ts,day,strategy_id,category,amount,currency,note) "
              "VALUES(99,1,'2026-11-19','','llm_tokens:main',2288868,'TOKEN','calls=290')")
    c.commit(); c.close()
    rep = lm.build(p)
    assert rep["costs"]["fee_rows"] == 0
    assert rep["costs"]["fee_total"] == 0.0
    assert rep["costs"]["token_rows"] == 1
    assert rep["costs"]["token_total"] == 2288868
    assert any("手续费未入账" in w["text"] for w in rep["reconciliation"])
    p.unlink()


def test_same_asset_positions_are_merged():
    p = _db(); _init(p); _seed(p, qty=50, entry=100, mark=100)
    c = sqlite3.connect(p)
    c.execute("INSERT INTO t2_positions(id,asset,side,qty,entry_price,entry_ts,status,thesis_json,note) "
              "VALUES(2,'XAU','short',50,100,1,'open','{\"decision_id\":12701}','executor decision#12701')")
    c.commit(); c.close()
    rep = lm.build(p)
    assert rep["risk"]["concentration_pct"] == 100.0
    assert any("2 笔同向持仓" in w["text"] for w in rep["reconciliation"])
    p.unlink()
