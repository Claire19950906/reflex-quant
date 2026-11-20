#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v508：多周期反思判定的行为测试（horizon_reflection 从 0% 起步）。

这个模块存在的唯一理由：把「方向错」和「节奏错」拆开。
一条 SELL 亏了，如果只是入场太早/追高，改推理就是改错方向 ——
所以测试重点在分类边界，而不是覆盖率：
  ① 短期亏 + 长期赚 = 节奏错（timing_wrong），绝不判方向错；
  ② 长期也亏 = 方向错；阈值是闭区间（-0.5 算错，-0.49 不算）；
  ③ 长周期不可执行（T+1）= 周期选错，不判判断错；
  ④ 没价就不下结论（no_data / partial），不许拿"没有数据"当"判断错"。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import horizon_reflection as hr


def H(horizon, status="settled", pnl=None, raw=None, hit=None):
    return {"horizon": horizon, "status": status, "pnl_pct": pnl,
            "outcome_pct": raw, "hit": hit}


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE trade_decisions(id INTEGER PRIMARY KEY, asset TEXT, final_action TEXT);
    """)
    c.execute("CREATE TABLE decision_outcomes_horizon(decision_id INTEGER, horizon TEXT,"
              " horizon_sec INTEGER, status TEXT, outcome_pct REAL, pnl_pct REAL, hit INTEGER)")
    hr.init_schema(c)
    return c


# ── 核心：节奏错 vs 方向错 ─────────────────────────────────
def test_short_loss_long_gain_is_timing_not_direction():
    """本模块存在的理由：短期亏、长期赚 → 改执行，不改推理。"""
    res = hr.classify([
        H("15m", pnl=-1.2, raw=-1.0, hit=0),
        H("1h", pnl=-0.8, raw=-0.6, hit=0),
        H("5d", pnl=2.5, raw=2.7, hit=1),
    ])
    assert res["verdict"] == hr.VERDICT_TIMING
    assert res["code"] == "H-TIME"
    assert res["long_horizon"] == "5d"
    assert "方向对" in res["why"] and "入场时点" in res["why"]


def test_long_loss_is_direction_wrong():
    res = hr.classify([
        H("15m", pnl=0.9, raw=1.1, hit=1),
        H("5d", pnl=-2.0, raw=-1.8, hit=0),
    ])
    assert res["verdict"] == hr.VERDICT_DIRECTION
    assert res["code"] == "H-DIR"


def test_direction_threshold_is_closed_interval():
    # 恰好 -0.5 → 方向错（<= 阈值）
    assert hr.classify([H("5d", pnl=-0.5)])["verdict"] == hr.VERDICT_DIRECTION
    # -0.49 → 还没到阈值，落在"无结论区间"，不许硬判
    assert hr.classify([H("5d", pnl=-0.49)])["verdict"] == hr.VERDICT_PARTIAL


def test_timing_thresholds_are_closed_interval():
    res = hr.classify([H("15m", pnl=-0.5), H("5d", pnl=0.5)])
    assert res["verdict"] == hr.VERDICT_TIMING, "恰好 -0.5 / +0.5 都算命中阈值"
    # 短期 -0.4 不够亏，不该判节奏错
    assert hr.classify([H("15m", pnl=-0.4), H("5d", pnl=0.5)])["verdict"] == hr.VERDICT_OK


def test_worst_short_used_not_average():
    """坏样本不该被好样本平均掉：只要有一个短期周期亏得够深，就是节奏问题。"""
    res = hr.classify([
        H("15m", pnl=3.0), H("1h", pnl=0.1), H("4h", pnl=-2.0), H("5d", pnl=4.0),
    ])
    assert res["verdict"] == hr.VERDICT_TIMING
    assert "-2.00%" in res["why"]


# ── 周期不可执行 ────────────────────────────────────────────
def test_not_executable_long_horizon_is_mismatch_not_judgement():
    res = hr.classify([
        H("15m", pnl=-3.0, hit=0),
        H("1d", status="not_executable"),
        H("5d", status="not_executable"),
    ])
    assert res["verdict"] == hr.VERDICT_HORIZON
    assert res["code"] == "H-EXEC"
    assert "不能据此改判断" in res["why"]


def test_not_executable_short_only_does_not_trigger_mismatch():
    # 只有 15m 不可执行、长周期正常 → 按长周期正常判
    res = hr.classify([H("15m", status="not_executable"), H("5d", pnl=2.0, hit=1)])
    assert res["verdict"] == hr.VERDICT_OK


# ── 没数据不下结论 ─────────────────────────────────────────
def test_all_no_price_is_no_data():
    res = hr.classify([H(h, status="no_price") for h in hr.HORIZONS])
    assert res["verdict"] == hr.VERDICT_NODATA
    assert res["code"] == "H-NODATA"


def test_short_only_is_partial():
    res = hr.classify([H("15m", pnl=-5.0), H("1d", status="no_price")])
    assert res["verdict"] == hr.VERDICT_PARTIAL
    assert "长周期未结算" in res["why"]


def test_empty_input_is_no_data():
    assert hr.classify([])["verdict"] == hr.VERDICT_NODATA


def test_no_price_never_counts_as_loss():
    """no_price 是"没数据"，不是"亏钱" —— 否则会凭空造出方向错。"""
    res = hr.classify([H("15m", status="no_price"), H("5d", pnl=1.5)])
    assert res["verdict"] == hr.VERDICT_OK


# ── 长期回退到 1d ──────────────────────────────────────────
def test_falls_back_to_1d_when_5d_unsettled():
    res = hr.classify([H("1d", pnl=-3.0), H("5d", status="no_price")])
    assert res["verdict"] == hr.VERDICT_DIRECTION
    assert res["long_horizon"] == "1d"


def test_prefers_5d_over_1d():
    res = hr.classify([H("1d", pnl=-3.0), H("5d", pnl=2.0)])
    assert res["long_horizon"] == "5d"
    assert res["verdict"] in (hr.VERDICT_OK, hr.VERDICT_PARTIAL)


# ── 成本侵蚀 ───────────────────────────────────────────────
def test_cost_drag_when_raw_all_positive_but_net_thin():
    res = hr.classify([
        H("15m", pnl=0.05, raw=0.3), H("1h", pnl=0.08, raw=0.3),
        H("5d", pnl=0.1, raw=0.3),
    ])
    assert res["verdict"] == hr.VERDICT_COST
    assert res["code"] == "H-COST"
    assert "成本吃掉利润" in res["why"]


def test_negative_raw_is_not_cost_drag():
    res = hr.classify([H("5d", pnl=0.05, raw=-0.2)])
    assert res["verdict"] != hr.VERDICT_COST


def test_by_horizon_accepts_sqlite_rows(conn):
    conn.execute("INSERT INTO decision_outcomes_horizon VALUES(1,'15m',900,'settled',1.0,0.9,1)")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM decision_outcomes_horizon").fetchone()
    got = hr._by_horizon([row])
    assert got["15m"]["pnl_pct"] == 0.9


# ── run_once ───────────────────────────────────────────────
def _seed(conn, did, rows, asset="BTC", action="BUY"):
    conn.execute("INSERT INTO trade_decisions VALUES(?,?,?)", (did, asset, action))
    for h, status, pnl in rows:
        sec = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "5d": 432000}[h]
        conn.execute("INSERT INTO decision_outcomes_horizon VALUES(?,?,?,?,?,?,?)",
                     (did, h, sec, status, pnl, pnl, 1 if (pnl or 0) > 0 else 0))


def test_run_once_writes_verdict_and_detail(conn):
    _seed(conn, 1, [("15m", "settled", -1.0), ("5d", "settled", 2.0)])
    _seed(conn, 2, [("5d", "settled", -3.0)], action="SELL")
    summary = hr.run_once(conn)
    assert summary["decisions"] == 2
    assert summary["by_verdict"][hr.VERDICT_TIMING] == 1
    assert summary["by_verdict"][hr.VERDICT_DIRECTION] == 1

    row = conn.execute("SELECT asset, action, verdict, codes_json, detail_json, horizons_used "
                       "FROM decision_horizon_reflection WHERE decision_id=1").fetchone()
    assert row[0] == "BTC" and row[1] == "BUY" and row[2] == hr.VERDICT_TIMING
    assert json.loads(row[3]) == ["H-TIME"]
    detail = json.loads(row[4])
    assert detail["long_horizon"] == "5d" and "why" in detail
    assert row[5] == "15m,5d", "参与判定的周期按固定顺序记录"


def test_run_once_is_idempotent(conn):
    _seed(conn, 3, [("5d", "settled", -2.0)])
    hr.run_once(conn)
    hr.run_once(conn)
    assert conn.execute("SELECT COUNT(*) FROM decision_horizon_reflection").fetchone()[0] == 1


def test_run_once_without_trade_decision_row(conn):
    conn.execute("INSERT INTO decision_outcomes_horizon VALUES(9,'5d',432000,'settled',1.0,1.0,1)")
    summary = hr.run_once(conn)
    row = conn.execute("SELECT asset, action FROM decision_horizon_reflection "
                       "WHERE decision_id=9").fetchone()
    assert row == (None, None), "找不到主表行时留空，不要编造标的"
    assert summary["decisions"] == 1


# ── read_stats ─────────────────────────────────────────────
def test_read_stats_splits_direction_vs_timing(conn):
    now = int(time.time())
    for did, verdict in ((1, hr.VERDICT_TIMING), (2, hr.VERDICT_TIMING),
                         (3, hr.VERDICT_DIRECTION), (4, hr.VERDICT_OK)):
        conn.execute("INSERT OR REPLACE INTO decision_horizon_reflection "
                     "(decision_id, asset, action, verdict, codes_json, detail_json,"
                     " horizons_used, computed_at) VALUES (?,?,?,?,?,?,?,?)",
                     (did, "BTC", "BUY", verdict, "[]", "{}", "5d", now))
    # 窗口外的一条，不该被统计
    conn.execute("INSERT OR REPLACE INTO decision_horizon_reflection "
                 "(decision_id, asset, action, verdict, codes_json, detail_json,"
                 " horizons_used, computed_at) VALUES (?,?,?,?,?,?,?,?)",
                 (5, "ETH", "BUY", hr.VERDICT_DIRECTION, "[]", "{}", "5d",
                  now - 40 * 86400))
    conn.commit()

    st = hr.read_stats(conn, days=30)
    assert st["total"] == 4
    assert st["direction_vs_timing"] == {"direction_wrong": 1, "timing_wrong": 2}

    st_eth = hr.read_stats(conn, days=30, asset="eth")
    assert st_eth["total"] == 0, "窗口外的 ETH 样本不算"


def test_read_stats_without_table_is_graceful():
    bare = sqlite3.connect(":memory:")
    st = hr.read_stats(bare)
    assert st["ok"] is True and st["by_verdict"] == {}
    assert "尚未跑过" in st["note"]
