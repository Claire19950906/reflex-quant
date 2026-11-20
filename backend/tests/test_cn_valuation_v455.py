"""v455 A股/港股估值历史推导 —— 单元测试（全部离线：不联网、不碰生产库）。

覆盖五件事：
  1. 法定披露截止日：季末 ≠ 已知日，年报的截止日在次年；
  2. 历史期的 TTM 还原（切片复用同一套「累计 + 上年年报 − 上年同期」）；
  3. 防前视：财报公布之前的价格，只能用上一期财报解释；
  4. 亏损（EPS≤0）不给 PE、缺 BPS 不给 PB —— 留空而不是猜；
  5. 推导点写进 `cn_valuation_snapshots` 后，分位读取能直接用（≥60 点）。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import cn_fundamentals as cn
import cn_prices as cp


def _ts(day: str) -> int:
    return int(time.mktime(time.strptime(day, "%Y-%m-%d")))


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE cn_valuation_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, market TEXT NOT NULL,
        ts INTEGER NOT NULL, pe REAL, pb REAL, source TEXT NOT NULL DEFAULT 'eastmoney',
        UNIQUE(ticker, market, ts))""")
    return conn


# ── 1) 已知日 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("report,label,expect", [
    ("2026-03-31", "一季报", "2026-04-30"),
    ("2026-06-30", "半年报", "2026-08-31"),
    ("2026-09-30", "三季报", "2026-10-31"),
    ("2025-12-31", "年报", "2026-04-30"),          # 年报披露截止在次年
    ("2026-09-30", "2026年三季报", "2026-10-31"),   # 港股 REPORT_TYPE 口径
])
def test_known_date_uses_legal_deadline(report, label, expect):
    assert cn.known_date(report, label) == expect


def test_known_date_without_label_falls_back_to_period_end():
    assert cn.known_date("2026-09-30", "") == "2026-09-30"


# ── 2) 历史期 TTM ─────────────────────────────────────────────────────────
def _rows():
    """倒序（新→旧）的四期：2026Q3 / 2026H1 / 2026Q1 / 2025FY。

    累计口径：Q1=0.30、H1=0.60、Q3=0.90、FY=1.20；去年 FY=1.00、去年 Q3=0.70。
    于是 Q3 的 TTM = 0.90 + 1.00 − 0.70 = 1.20。
    """
    return [
        {"REPORTDATE": "2026-09-30", "DATEMMDD": "三季报", "BASIC_EPS": 0.90, "BPS": 5.0},
        {"REPORTDATE": "2026-06-30", "DATEMMDD": "半年报", "BASIC_EPS": 0.60, "BPS": 4.6},
        {"REPORTDATE": "2026-03-31", "DATEMMDD": "一季报", "BASIC_EPS": 0.30, "BPS": 4.4},
        {"REPORTDATE": "2025-12-31", "DATEMMDD": "年报", "BASIC_EPS": 1.20, "BPS": 4.2},
        {"REPORTDATE": "2025-09-30", "DATEMMDD": "三季报", "BASIC_EPS": 0.70, "BPS": 3.9},
        {"REPORTDATE": "2025-06-30", "DATEMMDD": "半年报", "BASIC_EPS": 0.50, "BPS": 3.6},
        {"REPORTDATE": "2025-03-31", "DATEMMDD": "一季报", "BASIC_EPS": 0.20, "BPS": 3.4},
        {"REPORTDATE": "2024-12-31", "DATEMMDD": "年报", "BASIC_EPS": 1.00, "BPS": 3.2},
    ]


def test_ttm_at_reconstructs_each_period():
    rows = _rows()
    # Q3'26 TTM = 本期累计 0.90 + 上一年年报 1.20 − 去年同期累计 0.70
    assert cn.ttm_at(rows, "BASIC_EPS", 0) == pytest.approx(0.90 + 1.20 - 0.70)
    assert cn.ttm_at(rows, "BASIC_EPS", 3) == pytest.approx(1.20)   # 年报期 TTM 就是年报
    assert cn.ttm_at(rows, "BASIC_EPS", 99) is None


# ── 3) 防前视 ─────────────────────────────────────────────────────────────
def test_series_never_uses_unpublished_report():
    """10/31 才披露的三季报，不能用来解释 9 月的价格。"""
    rows = _rows()
    bars = [[_ts("2026-09-15") * 1000, 0, 0, 0, 12.0, 0],
            [_ts("2026-11-10") * 1000, 0, 0, 0, 12.0, 0]]
    series = cn.valuation_series(rows, bars, market="A")
    by_day = {time.strftime("%Y-%m-%d", time.localtime(p[0])): p for p in series}
    # 9/15：最新已公布的是半年报（8/31 截止），TTM=0.60+1.20−0.50=1.30 → PE 12/1.30
    assert by_day["2026-09-15"][1] == pytest.approx(round(12.0 / 1.30, 2))
    # 11/10：三季报已公布 → TTM 1.40 → PE 12/1.40
    assert by_day["2026-11-10"][1] == pytest.approx(round(12.0 / 1.40, 2))
    assert by_day["2026-09-15"][1] != by_day["2026-11-10"][1]   # 提前用三季报就会两边相等


def test_series_leaves_pe_empty_for_loss_makers():
    rows = [{"REPORTDATE": "2026-09-30", "DATEMMDD": "三季报", "BASIC_EPS": -0.5, "BPS": 2.0},
            {"REPORTDATE": "2025-12-31", "DATEMMDD": "年报", "BASIC_EPS": -1.0, "BPS": 2.1}]
    bars = [[_ts("2026-11-05") * 1000, 0, 0, 0, 8.0, 0]]
    series = cn.valuation_series(rows, bars, market="A")
    assert series[0][1] is None                      # 亏损不给 PE 分位
    assert series[0][2] == pytest.approx(4.0)        # PB 仍然有意义


def test_series_skips_bars_before_any_report():
    rows = _rows()
    bars = [[_ts("2025-01-10") * 1000, 0, 0, 0, 9.0, 0]]
    series = cn.valuation_series(rows, bars, market="A", days=4000)
    assert series and series[0][1] is None and series[0][2] is None


# ── 4) 写库 + 分位可读 ────────────────────────────────────────────────────
def test_percentile_becomes_available_after_derived_rows():
    conn = _memdb()
    now = int(time.time())
    for i in range(70):
        conn.execute("""INSERT INTO cn_valuation_snapshots(ticker,market,ts,pe,pb,source)
                        VALUES('600000.SH','A',?,?,?,'derived')""",
                     (now - (70 - i) * 86400, 10.0 + i * 0.1, 1.0))
    pct = cn.pe_percentile(conn, "600000.SH", "A")
    conn.close()
    assert pct["pe_samples"] == 70
    assert pct["pe_position"] == pytest.approx(100.0)   # 当前是历史最贵
    assert pct["pb_samples"] == 70


def test_percentile_refuses_below_threshold():
    conn = _memdb()
    now = int(time.time())
    for i in range(5):
        conn.execute("""INSERT INTO cn_valuation_snapshots(ticker,market,ts,pe,pb)
                        VALUES('600001.SH','A',?,?,?)""", (now - (5 - i) * 86400, 10.0, 1.0))
    pct = cn.pe_percentile(conn, "600001.SH", "A")
    conn.close()
    assert pct["pe_samples"] == 5 and "pe_position" not in pct


# ── 5) 个股 symbol 映射 ───────────────────────────────────────────────────
@pytest.mark.parametrize("code,market,expect", [
    ("600519.SH", "A", "sh600519"),
    ("000001.SZ", "A", "sz000001"),
    ("00700.HK", "HK", "hk00700"),
    ("700.HK", "HK", "hk00700"),
    ("300502", "", "sz300502"),
    ("600519", "", "sh600519"),
])
def test_stock_symbol_mapping(code, market, expect):
    assert cp.stock_symbol(code, market) == expect
