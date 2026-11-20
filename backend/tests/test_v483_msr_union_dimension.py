"""v483: 三维度反思聚合收口 missed_signal_reflections（V482 迁移读侧漏接）。

背景：
    V482 起 HOLD 漏检行改写独立记账表 missed_signal_reflections，
    trade_reflections_v2 不再新增漏检模板行。但 /api/v237/missed-signals/by-dimension
    的反思聚合仍只读 trade_reflections_v2 → 新漏检反思在三维度面板里不可见。

验收：
  1) v2 反思行与 MSR 漏检反思行并入同一套 dimension/subcategory 聚合；
  2) 命中率/均值口径仍只统计已结算（outcome 非空且 |outcome|≤60）的行；
  3) MSR 表缺失时回退只读 v2，不报错。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import db  # noqa: E402
import bridge_builders  # noqa: E402


def _stub_upstream(conn: sqlite3.Connection) -> None:
    """上游强信号/利率/喂料表在离线测试里给空桩，只验反思聚合口径。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT, "
        "ts INTEGER, move_pct REAL, move_kind TEXT, algo_score REAL, duration_min INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fed_rate_history (ts INTEGER, dgs2_pct REAL, "
        "effr_pct REAL, spread_bp REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feed_health (ts TEXT, asset TEXT, source TEXT, "
        "latency_ms INTEGER, status TEXT, error TEXT, feed_kind TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, ts INTEGER, price REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_decisions (id INTEGER PRIMARY KEY, target_asset TEXT, "
        "suggested_action TEXT, sentiment_score REAL, created_at TEXT, "
        "market_category TEXT, event_struct TEXT, reasoning TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trade_decisions (id INTEGER PRIMARY KEY, asset TEXT, "
        "decision_ts REAL, created_at TEXT)"
    )


@pytest.fixture()
def dim_db(tmp_path, monkeypatch):
    path = tmp_path / "dim_union.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    _stub_upstream(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(bridge_builders, "DB_PATH", path)
    return path


def test_v2_and_msr_rows_are_unioned_into_dimensions(dim_db):
    conn = sqlite3.connect(dim_db)
    conn.row_factory = sqlite3.Row
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    # v2 已结算 A 类反思（有真实涨跌）
    conn.execute(
        "INSERT INTO trade_reflections_v2 "
        "(decision_id, asset, action, dimension, subcategory, reflection_text, "
        " hit, actual_outcome_pct, confidence_at_time, created_at) "
        "VALUES (1, 'BTC', 'SELL', 'A', 'pattern_recognition', 'v2 反思内容', "
        " 1, 1.5, 0.4, ?)",
        (now,),
    )
    # MSR 未结算 B 类漏检反思（move_pct 为空 → 不计入命中分母）
    conn.execute(
        "INSERT INTO missed_signal_reflections "
        "(decision_id, asset, action, dimension, subcategory, reflection_text, "
        " hit, move_pct, confidence_at_time, created_at) "
        "VALUES (2, 'XAU', 'HOLD', 'B', 'news_direction_inverted', 'msr 漏检反思', "
        " 0, NULL, 0.3, ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    res = bridge_builders._v237_by_dimension_build(90)
    dims = res["dimensions"]

    assert dims["A"]["total"] == 1
    assert dims["A"]["by_subcategory"]["pattern_recognition"]["n"] == 1
    # A 行已结算且命中 → 命中计数 1
    assert dims["A"]["hit"] == 1

    assert dims["B"]["total"] == 1
    assert dims["B"]["by_subcategory"]["news_direction_inverted"]["n"] == 1
    # B 行无真实结果 → 命中率 0，覆盖率 0，不能伪装成已验证
    b_sub = dims["B"]["by_subcategory"]["news_direction_inverted"]
    assert b_sub["hit"] == 0
    assert b_sub["outcome_coverage"] == 0.0

    # 反思样本总数 = 两表之和
    assert res["totals"]["reflections_total"] == 2

    # 明细面板能取到两表的行
    assets_a = [i["asset"] for i in dims["A"]["items"]]
    assets_b = [i["asset"] for i in dims["B"]["items"]]
    assert "BTC" in assets_a
    assert "XAU" in assets_b


def test_empty_text_rows_excluded_from_aggregation(dim_db):
    conn = sqlite3.connect(dim_db)
    conn.row_factory = sqlite3.Row
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    # 空壳行：两表都不该进聚合分母
    conn.execute(
        "INSERT INTO trade_reflections_v2 "
        "(decision_id, asset, dimension, subcategory, reflection_text, created_at) "
        "VALUES (3, 'ETH', 'A', 'pattern_recognition', '', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO missed_signal_reflections "
        "(decision_id, asset, dimension, subcategory, reflection_text, created_at) "
        "VALUES (4, 'SOL', 'C', 'feed_missing', '', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    res = bridge_builders._v237_by_dimension_build(90)
    assert res["totals"]["reflections_total"] == 0
