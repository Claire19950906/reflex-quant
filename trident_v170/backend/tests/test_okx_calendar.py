"""OKX 交割/到期日历（交易日历）数据源 —— 单元测试（离线，纯函数 + mock 网络层）。

覆盖：_ms_to_gmdate / _uly_from_inst_id / aggregate_expiry_events / _merge_by_date
      / fetch_okx_delivery_calendar（monkeypatch 网络层）。
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import quote_sources as qs


NOW_MS = 1735689600000            # 2025-01-01 00:00 UTC
DAY_MS = 86400000


# ---------------------------------------------------------------------------
# _ms_to_gmdate
# ---------------------------------------------------------------------------

def test_ms_to_gmdate_nonpositive_returns_empty():
    assert qs._ms_to_gmdate(0) == ""
    assert qs._ms_to_gmdate(-1) == ""


def test_ms_to_gmdate_to_utc_date():
    assert qs._ms_to_gmdate(NOW_MS) == "2025-01-01"
    assert qs._ms_to_gmdate(NOW_MS + 23 * DAY_MS) == "2025-01-24"


# ---------------------------------------------------------------------------
# _uly_from_inst_id
# ---------------------------------------------------------------------------

def test_uly_from_futures_inst_id():
    assert qs._uly_from_inst_id("BTC-USD-260626") == "BTC-USD"


def test_uly_from_option_inst_id():
    assert qs._uly_from_inst_id("BTC-USD-260626-85000-C") == "BTC-USD"


def test_uly_from_bad_inst_id_empty():
    assert qs._uly_from_inst_id("") == ""
    assert qs._uly_from_inst_id("BTC") == ""


# ---------------------------------------------------------------------------
# aggregate_expiry_events
# ---------------------------------------------------------------------------

def test_aggregate_filters_past_and_out_of_window():
    rows = [
        {"instId": "BTC-USD-241227", "uly": "BTC-USD", "expTime": "1000000000000"},  # 过去 → 过滤
        {"instId": "BTC-USD-250124", "uly": "BTC-USD", "expTime": str(NOW_MS + 23 * DAY_MS)},   # 窗内
        {"instId": "BTC-USD-250331", "uly": "BTC-USD", "expTime": str(NOW_MS + 90 * DAY_MS)},   # 超窗 → 过滤
    ]
    events = qs.aggregate_expiry_events(rows, "FUTURES", NOW_MS, 30 * DAY_MS)
    assert [e["date"] for e in events] == ["2025-01-24"]


def test_aggregate_groups_same_ts_and_dedups_underlyings():
    ts = str(NOW_MS + 30 * DAY_MS)  # 2025-01-31（窗边界，含）
    rows = [
        {"instId": "BTC-USD-250131", "uly": "BTC-USD", "expTime": ts},
        {"instId": "ETH-USD-250131", "uly": "ETH-USD", "expTime": ts},
        {"instId": "BTC-USD-250131-B", "uly": "BTC-USD", "expTime": ts},  # 同 uly 去重
    ]
    events = qs.aggregate_expiry_events(rows, "FUTURES", NOW_MS, 30 * DAY_MS)
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "futures_delivery"
    assert e["underlyings"] == ["BTC-USD", "ETH-USD"]
    assert e["inst_count"] == 3


def test_aggregate_option_type_label():
    rows = [
        {"instId": "BTC-USD-250124-60000-C", "uly": "BTC-USD", "expTime": str(NOW_MS + 23 * DAY_MS)},
    ]
    events = qs.aggregate_expiry_events(rows, "OPTION", NOW_MS, 30 * DAY_MS)
    assert len(events) == 1
    assert events[0]["type"] == "option_expiry"


def test_aggregate_ignores_bad_exp_or_missing_uly():
    rows = [
        {"instId": "BTC-USD-250124", "uly": "BTC-USD", "expTime": "not-a-number"},
        {"instId": "XXX", "uly": "", "expTime": str(NOW_MS + 23 * DAY_MS)},
        {"instId": "", "uly": "", "expTime": str(NOW_MS + 23 * DAY_MS)},
        "not-a-dict",
    ]
    assert qs.aggregate_expiry_events(rows, "FUTURES", NOW_MS, 30 * DAY_MS) == []


def test_aggregate_non_list_returns_empty():
    assert qs.aggregate_expiry_events(None, "FUTURES", NOW_MS, 30 * DAY_MS) == []
    assert qs.aggregate_expiry_events("x", "FUTURES", NOW_MS, 30 * DAY_MS) == []


# ---------------------------------------------------------------------------
# _merge_by_date
# ---------------------------------------------------------------------------

def test_merge_by_date_combines_futures_and_option():
    day = NOW_MS + 23 * DAY_MS
    events = [
        {"ts_ms": day, "date": "2025-01-24", "type": "futures_delivery",
         "underlyings": ["BTC-USD", "ETH-USD"], "inst_count": 3},
        {"ts_ms": day, "date": "2025-01-24", "type": "option_expiry",
         "underlyings": ["BTC-USD"], "inst_count": 10},
    ]
    out = qs._merge_by_date(events)
    assert len(out) == 1
    m = out[0]
    assert m["types"] == ["futures_delivery", "option_expiry"]
    assert m["underlyings"] == ["BTC-USD", "ETH-USD"]
    assert m["inst_count"] == 13


def test_merge_by_date_keeps_distinct_days_ordered():
    a = NOW_MS + 2 * DAY_MS
    b = NOW_MS + 1 * DAY_MS
    events = [
        {"ts_ms": a, "date": "2025-01-03", "type": "futures_delivery", "underlyings": ["BTC-USD"], "inst_count": 1},
        {"ts_ms": b, "date": "2025-01-02", "type": "option_expiry", "underlyings": ["ETH-USD"], "inst_count": 1},
    ]
    out = qs._merge_by_date(events)
    assert [e["date"] for e in out] == ["2025-01-02", "2025-01-03"]


# ---------------------------------------------------------------------------
# fetch_okx_delivery_calendar（monkeypatch 网络层）
# ---------------------------------------------------------------------------

def _fake_instruments(inst_type, uly):
    if uly == "BTC-USD" and inst_type == "FUTURES":
        return [
            {"instId": "BTC-USD-250131", "uly": "BTC-USD", "expTime": str(NOW_MS + 30 * DAY_MS)},
        ]
    if uly == "ETH-USD" and inst_type == "OPTION":
        return [
            {"instId": "ETH-USD-250124-3000-C", "uly": "ETH-USD", "expTime": str(NOW_MS + 23 * DAY_MS)},
        ]
    return []


def test_fetch_calendar_all_fail_returns_none(monkeypatch):
    monkeypatch.setattr(qs, "fetch_okx_instruments", lambda it, uly="": None)
    assert qs.fetch_okx_delivery_calendar() is None


def test_fetch_calendar_builds_structure(monkeypatch):
    monkeypatch.setattr(qs, "fetch_okx_instruments", _fake_instruments)
    monkeypatch.setattr(qs.time, "time", lambda: NOW_MS / 1000.0)
    cal = qs.fetch_okx_delivery_calendar(horizon_days=30)
    assert cal is not None
    assert cal["source"] == "okx"
    assert [e["date"] for e in cal["events"]] == ["2025-01-24", "2025-01-31"]
    assert cal["next_event"]["date"] == "2025-01-24"
    assert cal["next_event"]["types"] == ["option_expiry"]
    assert set(cal["covered_underlyings"]) == {"BTC-USD", "ETH-USD"}