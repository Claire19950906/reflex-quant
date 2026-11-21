# -*- coding: utf-8 -*-
"""财报数据的解析契约测试（全离线，不发网络请求）。

这层的风险是"占位值冒充数据"：`N/A` / `-` / `nan` 一旦被当成 0 或某个数，
下游的"预期差"就会凭空出现一个数字，而且看起来很可信。
"""
from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import equity_fundamentals as ef  # noqa: E402


def test_num_treats_placeholders_and_bool_as_missing():
    for placeholder in (None, "", "  ", "N/A", "na", "None", "null", "-", True, False,
                        "abc", float("nan")):
        assert ef._num(placeholder) is None, placeholder
    assert ef._num(0) == 0.0          # 0 是数据，不是缺失
    assert ef._num("1.5") == 1.5
    assert ef._num("  -2.25 ") == -2.25


def test_merge_statements_fills_gaps_without_overwriting_primary():
    primary = [{"period_end": "2026-09-30", "revenue": 100, "net_income": None}]
    extra = [
        {"period_end": "2026-09-30", "revenue": 999, "net_income": 20},   # 只补空字段
        {"period_end": "2026-06-30", "revenue": 80},                       # 缺期追加
    ]
    merged = ef._merge_statements(primary, extra)
    assert [r["period_end"] for r in merged] == ["2026-09-30", "2026-06-30"]  # 降序
    assert merged[0]["revenue"] == 100        # 主源不被覆盖
    assert merged[0]["net_income"] == 20      # 空字段被补上


def test_parse_earnings_keeps_date_only_rows_and_drops_invalid():
    assert ef.parse_earnings({"not": "a list"}) == []
    rows = [
        {"symbol": "aapl", "date": "2026-11-20T00:00:00", "epsEstimated": "2.1",
         "epsActual": "N/A"},
        {"symbol": "", "date": "2026-11-20"},          # 无 ticker → 丢
        {"symbol": "MSFT", "date": ""},                # 无日期 → 丢
        {"symbol": "TSLA", "date": "2026-12-01"},      # 只有日期也收
        "garbage",
    ]
    out = ef.parse_earnings(rows)
    assert [r["ticker"] for r in out] == ["AAPL", "TSLA"]
    assert out[0]["eps_estimated"] == 2.1 and out[0]["eps_actual"] is None
    assert out[0]["event_date"] == "2026-11-20"


def test_parse_av_earnings_uses_reported_date_and_surprise():
    body = {"symbol": "nvda", "quarterlyEarnings": [
        {"reportedDate": "2026-10-28 00:00:00", "fiscalDateEnding": "2026-09-30",
         "estimatedEPS": "1.10", "reportedEPS": "1.25", "surprisePercentage": "13.6364"},
        {"reportedDate": "", "fiscalDateEnding": ""},     # 无日期 → 丢
    ]}
    out = ef.parse_av_earnings(body)
    assert len(out) == 1
    row = out[0]
    assert row["ticker"] == "NVDA" and row["event_date"] == "2026-10-28"
    assert row["eps_actual"] == 1.25 and row["eps_estimated"] == 1.1
    assert row["surprise_pct"] == 13.6364
    assert ef.parse_av_earnings([1, 2]) == []


def test_earnings_metrics_next_event_and_surprise():
    now = time.mktime(time.strptime("2026-11-21", "%Y-%m-%d"))
    events = [
        {"event_date": "2026-11-30", "eps_estimated": 2.5, "eps_actual": None},
        {"event_date": "2026-11-10", "eps_estimated": 2.0, "eps_actual": 2.2},
        {"event_date": "2026-11-15", "eps_estimated": None, "eps_actual": 1.0},  # 缺预期 → 不算预期差
    ]
    m = ef.earnings_metrics(events, now_ts=now)
    assert m["next_date"] == "2026-11-30"
    assert m["next_days"] == 9
    assert m["last_report_date"] == "2026-11-10"
    assert m["last_surprise_pct"] == 10.0          # (2.2-2.0)/2.0
    empty = ef.earnings_metrics([], now_ts=now)
    assert empty["next_date"] is None and empty["last_surprise_pct"] is None


def test_earnings_metrics_zero_estimate_does_not_divide():
    now = time.mktime(time.strptime("2026-11-21", "%Y-%m-%d"))
    events = [{"event_date": "2026-11-10", "eps_estimated": 0, "eps_actual": 0.5}]
    m = ef.earnings_metrics(events, now_ts=now)
    assert m["last_surprise_pct"] is None


def test_days_between_and_calendar_window():
    assert ef._days_between("2026-11-21", "2026-11-22") == 1
    assert ef._days_between("2026-11-21", "2026-11-21") == 0
    assert ef._days_between("bad", "2026-11-21") is None

    today_plus1 = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    long_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 400 * 86400))
    csv_text = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\n"
                f"AAPL,Apple,{today_plus1},2026-09-30,1.50,USD\n"
                f"OLD,OldCo,{long_ago},2020-09-30,9.99,USD\n"
                "BAD,Bad,not-a-date,2026-09-30,1.0,USD\n")
    out = ef.parse_av_calendar(csv_text)
    assert [r["ticker"] for r in out] == ["AAPL"]
    assert out[0]["eps_estimated"] == 1.5 and out[0]["eps_actual"] is None
    assert math.isclose(out[0]["eps_estimated"], 1.5)
