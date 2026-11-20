"""v330: 归因覆盖率必须按"稳定键"算, 不能只认自增 id。

`backfill_critical_moves.py` 回填异动时是"删旧行 + 插入新行", 同一根 K 线会拿到
新 id。归因行留在库里, move_id 就变成孤儿 —— 实测 176 条归因里 62 条指向已删 id,
S7 覆盖率被算成 18.5%, 真实是 29.8%。归因表本身存了 asset + move_ts, 用它兜底。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_scorecard as sc


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER)")
    conn.execute("CREATE TABLE event_move_attribution (id INTEGER PRIMARY KEY, move_id INTEGER, "
                 "asset TEXT, move_ts INTEGER)")
    # v334/v339: _q_attr_coverage 会先取 news_events 的 MIN(ts) 收窗口
    # （只统计新闻存档开始之后的异动，否则 244 天的异动全进分母、覆盖率必然虚低）。
    # 夹具原先没这张表 → "no such table: news_events"。这里放一条 ts=0 的锚点，
    # 等价于"窗口从最早开始"，各个用例的分母语义不变。
    conn.execute("CREATE TABLE news_events (id INTEGER PRIMARY KEY, ts INTEGER)")
    conn.execute("INSERT INTO news_events (id, ts) VALUES (1, 0)")
    return conn


def test_counts_move_by_stable_key_when_id_is_orphaned():
    conn = _mk_db()
    # 异动表: 回填后 id 变成 4, 但归因行还指着旧 id=1
    conn.execute("INSERT INTO critical_price_moves VALUES (4, 'BTC', 1000)")
    conn.execute("INSERT INTO event_move_attribution VALUES (1, 1, 'BTC', 1000)")
    n, d = sc._q_attr_coverage(conn)
    assert (n, d) == (1.0, 1)


def test_counts_by_id_fast_path():
    conn = _mk_db()
    conn.execute("INSERT INTO critical_price_moves VALUES (7, 'ETH', 2000)")
    conn.execute("INSERT INTO event_move_attribution VALUES (1, 7, 'ETH', 1999)")
    n, d = sc._q_attr_coverage(conn)
    assert (n, d) == (1.0, 1)


def test_unattributed_move_still_counts_as_miss():
    conn = _mk_db()
    conn.execute("INSERT INTO critical_price_moves VALUES (1, 'SOL', 3000)")
    conn.execute("INSERT INTO critical_price_moves VALUES (2, 'SOL', 4000)")
    conn.execute("INSERT INTO event_move_attribution VALUES (1, 1, 'SOL', 3000)")
    n, d = sc._q_attr_coverage(conn)
    assert (n, d) == (1.0, 2)


def test_same_ts_different_asset_is_not_a_match():
    """时间对齐但资产不同绝不能算覆盖 —— 否则一个资产的归因会替别的资产刷分。"""
    conn = _mk_db()
    conn.execute("INSERT INTO critical_price_moves VALUES (1, 'WTI', 5000)")
    conn.execute("INSERT INTO event_move_attribution VALUES (1, 99, 'XAU', 5000)")
    n, d = sc._q_attr_coverage(conn)
    assert (n, d) == (0.0, 1)
