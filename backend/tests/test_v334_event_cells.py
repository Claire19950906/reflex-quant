"""v334: 统计层的两个"样本虚高/分母混窗"问题。

S1: 1343 个原子样本只落在 **30 个 (日期×资产) 格** 里 —— 同一天几十条同题材新闻
    共享同一根 K 线。按原子数加权会把假的精度当真 (区间 [47.4,53.0] 窄得离谱),
    而且一天的逆向行情能一次性翻转几百条。改成按独立日格统计。
S7: 异动表跨 244 天, 新闻存档只有 40 天 —— 40 天前的异动永远不可能被归因。
    把它算进分母, 覆盖率必然虚低 (35.8% vs 真实 46.3%)。分子分母必须同窗口。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_scorecard as sc


# ── S1: 独立日格 ─────────────────────────────────────────────────────────────
def _mk_event_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dim_asset (id INTEGER PRIMARY KEY, code TEXT)")
    conn.execute("CREATE TABLE event_atom (id INTEGER PRIMARY KEY, asset_id INTEGER, "
                 "event_time INTEGER, direction TEXT)")
    conn.execute("CREATE TABLE event_impact_curve (id INTEGER PRIMARY KEY, event_atom_id INTEGER, "
                 "asset_id INTEGER, window_label TEXT, sample_ts INTEGER, price REAL, delta_pct REAL)")
    conn.execute("INSERT INTO dim_asset VALUES (1,'XAU')")
    return conn


def _add_atom(conn, atom_id, day_epoch, direction, delta):
    conn.execute("INSERT INTO event_atom VALUES (?,1,?,?)", (atom_id, day_epoch, direction))
    conn.execute("INSERT INTO event_impact_curve (event_atom_id, asset_id, window_label, "
                 "sample_ts, price, delta_pct) VALUES (?,1,'1d',?, 100, ?)",
                 (atom_id, day_epoch + 86400, delta))


def test_many_atoms_same_day_collapse_to_one_cell():
    """同一天 50 条 bullish 新闻 + 当天实际下跌 → 只算 1 个格、1 次未命中。"""
    conn = _mk_event_db()
    day = 1791705600
    for i in range(50):
        _add_atom(conn, i + 1, day + i, "bullish", -0.35)
    k, n = sc._q_dir_event_1d(conn)
    assert (k, n) == (0.0, 1)


def test_cell_hit_uses_majority_direction_vs_average_move():
    import math

    conn = _mk_event_db()
    day = 1791705600
    # 3 bullish + 1 bearish, 当天平均上涨 → 多数方向命中
    for i in range(3):
        _add_atom(conn, i + 1, day + i, "bullish", 0.4)
    _add_atom(conn, 4, day + 4, "bearish", 0.4)
    k, n = sc._q_dir_event_1d(conn)
    assert (k, n) == (1.0, 1)


def test_different_days_are_separate_cells():
    conn = _mk_event_db()
    day1, day2 = 1791705600, 1791705600 + 86400
    _add_atom(conn, 1, day1, "bullish", 0.5)     # 命中
    _add_atom(conn, 2, day2, "bullish", -0.5)    # 未命中
    k, n = sc._q_dir_event_1d(conn)
    assert (k, n) == (1.0, 2)


def test_neutral_or_unknown_direction_not_counted():
    conn = _mk_event_db()
    _add_atom(conn, 1, 1791705600, "unknown", 0.5)
    _add_atom(conn, 2, 1791705600, "neutral", 0.5)
    k, n = sc._q_dir_event_1d(conn)
    assert (k, n) == (0.0, 0)


def test_missing_delta_excluded():
    conn = _mk_event_db()
    conn.execute("INSERT INTO event_atom VALUES (1,1,1791705600,'bullish')")
    conn.execute("INSERT INTO event_impact_curve (event_atom_id, asset_id, window_label, "
                 "sample_ts, price, delta_pct) VALUES (1,1,'1d',1791792000,100,NULL)")
    k, n = sc._q_dir_event_1d(conn)
    assert (k, n) == (0.0, 0)


# ── S7: 同窗口 ─────────────────────────────────────────────────────────────
def _mk_attr_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE news_events (id INTEGER PRIMARY KEY, ts INTEGER)")
    conn.execute("CREATE TABLE critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER)")
    conn.execute("CREATE TABLE event_move_attribution (id INTEGER PRIMARY KEY, move_id INTEGER, "
                 "asset TEXT, move_ts INTEGER)")
    return conn


def test_moves_before_news_archive_are_excluded_from_denominator():
    conn = _mk_attr_db()
    conn.execute("INSERT INTO news_events VALUES (1, 1791000000)")   # 新闻存档起点
    conn.execute("INSERT INTO critical_price_moves VALUES (1,'BTC',1790000000)")  # 早于存档
    conn.execute("INSERT INTO critical_price_moves VALUES (2,'BTC',1791100000)")  # 存档之后
    conn.execute("INSERT INTO event_move_attribution VALUES (1,2,'BTC',1791100000)")
    k, n = sc._q_attr_coverage(conn)
    assert (k, n) == (1.0, 1)


def test_orphan_move_id_still_matches_by_stable_key():
    conn = _mk_attr_db()
    conn.execute("INSERT INTO news_events VALUES (1, 1790000000)")
    conn.execute("INSERT INTO critical_price_moves VALUES (9,'ETH',1791100000)")
    conn.execute("INSERT INTO event_move_attribution VALUES (1, 55,'ETH',1791100000)")  # id 是孤儿
    k, n = sc._q_attr_coverage(conn)
    assert (k, n) == (1.0, 1)


def test_no_news_archive_means_empty_denominator():
    conn = _mk_attr_db()
    conn.execute("INSERT INTO critical_price_moves VALUES (1,'BTC',1791100000)")
    k, n = sc._q_attr_coverage(conn)
    assert (k, n) == (0.0, 0)
