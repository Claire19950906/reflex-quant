"""v335: 影子账本 —— 不管闸门放不放行, 有方向的决策都要能结算。

背景: 统一置信度闸门 (20) 把 94% 的方向决策降级成 HOLD, 一天只剩 0~1 单,
统计层没有样本, 准确率/权重/校准全部无从计算。影子账本把"当时怎么想 + 当时什么价
+ 4 小时后什么价"拼起来, 每条决策都变成可结算样本。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import shadow_ledger as sl


def _db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("""CREATE TABLE market_ticks (symbol TEXT, ts INTEGER, price REAL)""")
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER, ts INTEGER, target_asset TEXT, ungated_action TEXT,
        suggested_action TEXT, evidence_confidence REAL,
        analysis_degraded INTEGER DEFAULT 0)""")
    return conn


def test_hit_counts_direction_with_price_move(tmp_path):
    conn = _db(tmp_path)
    base = 1_700_000_000
    conn.execute("INSERT INTO market_ticks VALUES ('BTC', ?, 100.0)", (base,))
    conn.execute("INSERT INTO market_ticks VALUES ('BTC', ?, 102.0)", (base + 240 * 60,))
    conn.execute("""INSERT INTO ai_decisions VALUES
        (1, ?, 'BTC', 'BUY', 'HOLD', 0.0, 0)""", (base,))
    items = sl.collect(conn, days=3650, horizon=240)
    assert len(items) == 1
    assert items[0]["hit"] == 1                     # BUY + 涨 → 命中
    assert items[0]["action_after"] == "HOLD"        # 闸门后实际没开
    assert round(items[0]["move_pct"], 2) == 2.0


def test_sell_on_rise_is_miss(tmp_path):
    conn = _db(tmp_path)
    base = 1_700_000_000
    conn.execute("INSERT INTO market_ticks VALUES ('XAU', ?, 2000.0)", (base,))
    conn.execute("INSERT INTO market_ticks VALUES ('XAU', ?, 2020.0)", (base + 240 * 60,))
    conn.execute("INSERT INTO ai_decisions VALUES (2, ?, 'XAU', 'SELL', 'HOLD', 0.0, 0)", (base,))
    items = sl.collect(conn, days=3650, horizon=240)
    assert items[0]["hit"] == 0


def test_asset_without_ticks_is_dropped(tmp_path):
    conn = _db(tmp_path)
    base = 1_700_000_000
    conn.execute("INSERT INTO ai_decisions VALUES (3, ?, 'SKHYNIX', 'BUY', 'HOLD', 0.0, 0)", (base,))
    assert sl.collect(conn, days=3650, horizon=240) == []


def test_unsettled_row_still_collected(tmp_path):
    """到期价还没进来时也要记账, 只是 hit 为空 —— 明天自动补结算。"""
    conn = _db(tmp_path)
    base = 1_700_000_000
    conn.execute("INSERT INTO market_ticks VALUES ('WTI', ?, 70.0)", (base,))
    conn.execute("INSERT INTO ai_decisions VALUES (4, ?, 'WTI', 'SELL', 'HOLD', 0.0, 0)", (base,))
    items = sl.collect(conn, days=3650, horizon=240)
    assert len(items) == 1 and items[0].get("hit") is None


def test_write_then_settle_updates_row(tmp_path):
    conn = _db(tmp_path)
    base = 1_700_000_000
    conn.execute("INSERT INTO market_ticks VALUES ('ETH', ?, 1000.0)", (base,))
    conn.execute("INSERT INTO ai_decisions VALUES (5, ?, 'ETH', 'BUY', 'HOLD', 0.0, 0)", (base,))
    sl.write(conn, sl.collect(conn, days=3650, horizon=240))      # 未结算
    conn.execute("INSERT INTO market_ticks VALUES ('ETH', ?, 1010.0)", (base + 240 * 60,))
    sl.write(conn, sl.collect(conn, days=3650, horizon=240))      # 补结算
    row = conn.execute("SELECT hit, move_pct FROM shadow_ledger WHERE decision_id=5").fetchone()
    assert row[0] == 1 and round(row[1], 2) == 1.0


def test_degraded_direction_excluded(tmp_path):
    """v400: 降级路径拼出来的方向不许进账本——否则等于用模板给判断力打分。"""
    conn = _db(tmp_path)
    base = 1_700_000_000
    conn.execute("INSERT INTO market_ticks VALUES ('BTC', ?, 100.0)", (base,))
    conn.execute("INSERT INTO market_ticks VALUES ('BTC', ?, 105.0)", (base + 240 * 60,))
    conn.execute("""INSERT INTO ai_decisions VALUES
        (7, ?, 'BTC', 'BUY', 'HOLD', 0.0, 1)""", (base,))
    conn.execute("""INSERT INTO ai_decisions VALUES
        (8, ?, 'BTC', 'BUY', 'HOLD', 0.0, 0)""", (base,))
    items = sl.collect(conn, days=3650, horizon=240)
    assert [i["decision_id"] for i in items] == [8]


def test_gate_sweep_reports_monotone_sample_count(tmp_path, capsys):
    conn = _db(tmp_path)
    base = 1_700_000_000
    conn.execute("INSERT INTO market_ticks VALUES ('BTC', ?, 100.0)", (base,))
    conn.execute("INSERT INTO market_ticks VALUES ('BTC', ?, 101.0)", (base + 240 * 60,))
    conn.execute("INSERT INTO ai_decisions VALUES (6, ?, 'BTC', 'BUY', 'HOLD', 25.0, 0)", (base,))
    items = sl.collect(conn, days=3650, horizon=240)
    sl.sweep(items)
    out = capsys.readouterr().out
    assert "闸门扫描" in out
