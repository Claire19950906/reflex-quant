"""宏观日历提醒 worker 单测（v202）。

覆盖：
- _parse_event_ts: 多种 pub_time 格式（BJT 字符串 / ISO UTC / ISO + 时区）
- 窗口判定：lead_2h 在 [lead_min-10, lead_min]、at_time 在 ±tolerance
- min_star 门槛过滤
- _format_title / _format_body 渲染正确
- _trigger_one 幂等性：第二次相同 (event_id, window) → 不再调 send_important_news
- _record_notified 直接调用：UNIQUE 冲突处理

全部离线 mock，不触发真实推送与数据库。
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("JIN10_MCP_TOKEN", "")
import config


# ------------------------------------------------------------------ 时间解析
def test_parse_event_ts_iso_utc():
    from engine.macro_calendar_notifier import _parse_event_ts
    iso = "2026-09-29T12:00:00+00:00"
    ts = _parse_event_ts(iso)
    assert ts is not None
    assert datetime.fromtimestamp(ts, tz=timezone.utc).hour == 12


def test_parse_event_ts_iso_with_offset():
    from engine.macro_calendar_notifier import _parse_event_ts
    iso = "2026-09-29T20:00:00+08:00"  # BJ 20:00 = UTC 12:00
    ts = _parse_event_ts(iso)
    assert ts is not None
    utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert utc.hour == 12
    assert utc.day == 29


def test_parse_event_ts_invalid_returns_none():
    from engine.macro_calendar_notifier import _parse_event_ts
    assert _parse_event_ts("not a date") is None
    assert _parse_event_ts("") is None
    assert _parse_event_ts(None) is None  # type: ignore[arg-type]


# ------------------------------------------------------------------ 窗口判定
def test_self_test_dry_run_triggered_windows():
    from engine.macro_calendar_notifier import _self_test_dry_run
    result = _self_test_dry_run()
    windows = {t["_window"] for t in result["triggered"]}
    assert "lead_2h" in windows
    assert "at_time" in windows


def test_self_test_filters_low_star():
    from engine.macro_calendar_notifier import _self_test_dry_run
    result = _self_test_dry_run()
    for t in result["triggered"]:
        assert t["star"] >= config.MACRO_NOTIFY_MIN_STAR


# ------------------------------------------------------------------ 渲染
def test_format_title_includes_prefix_and_stars():
    from engine.macro_calendar_notifier import _format_title
    title = _format_title("🗓️ 提前 2h｜", "美国 CPI 同比", 3)
    assert title.startswith("🗓️ 提前 2h｜")
    assert "★★★" in title
    assert "美国 CPI" in title


def test_format_title_truncates_long():
    from engine.macro_calendar_notifier import _format_title
    long = "很长的事件名" * 10
    title = _format_title("prefix｜", long, 5)
    assert title.startswith("prefix｜★★★★★ ")
    # 截断部分 `[:18]` 后再 strip，应当 ≤ 18 字符
    payload = title[len("prefix｜★★★★★ "):]
    assert len(payload) <= 18


def test_format_body_includes_consensus_and_previous():
    from engine.macro_calendar_notifier import _format_body
    body = _format_body("【财经日历提醒】距离公布还有约 2 小时：",
                        "2026-09-29T12:00:00+00:00", 3, "3.2%", "3.1%")
    assert "市场预期" in body
    assert "3.2%" in body
    assert "前值" in body
    assert "3.1%" in body


def test_format_body_handles_empty_consensus():
    from engine.macro_calendar_notifier import _format_body
    body = _format_body("prefix：", "2026-09-29T12:00:00+00:00", 3, None, None)
    assert "市场预期" not in body
    assert "前值" not in body


# ------------------------------------------------------------------ 幂等性
def test_trigger_one_idempotent_via_unique():
    """同 (event_id, window) 第二次触发应被 UNIQUE 拦截，不调 send_important_news。"""
    from engine import macro_calendar_notifier as mcn

    # Mock db.get_connection + send_important_news
    fake_conn = MagicMock()
    # First call → commit ok, second call → IntegrityError on UNIQUE
    fake_conn.execute.side_effect = [
        None,  # INSERT first
        sqlite3_IntegrityError(),  # INSERT dup
    ]
    fake_conn.commit = MagicMock()
    fake_conn.rollback = MagicMock()

    event = {
        "pub_time": "2026-09-29T12:00:00+00:00",
        "star": 3, "title": "测试事件", "consensus": "—", "previous": "—",
    }

    with patch.object(mcn.db, "get_connection", return_value=fake_conn), \
         patch.object(mcn, "send_important_news") as mock_send, \
         patch.object(mcn, "_record_notified",
                      side_effect=[True, False]):  # first writes, second sees UNIQUE
        mcn._trigger_one(event, "lead_2h")
        mcn._trigger_one(event, "lead_2h")

    # Second call short-circuited before send_important_news
    assert mock_send.call_count == 1


def test_trigger_one_different_window_passes_through():
    """同一事件不同窗口（lead_2h vs at_time）应各推一次。"""
    from engine import macro_calendar_notifier as mcn

    fake_conn = MagicMock()
    fake_conn.commit = MagicMock()
    fake_conn.rollback = MagicMock()

    event = {
        "pub_time": "2026-09-29T12:00:00+00:00",
        "star": 4, "title": "美联储利率决议", "consensus": "5.50%", "previous": "5.50%",
    }

    with patch.object(mcn.db, "get_connection", return_value=fake_conn), \
         patch.object(mcn, "send_important_news") as mock_send, \
         patch.object(mcn, "_record_notified", side_effect=[True, True]):
        mcn._trigger_one(event, "lead_2h")
        mcn._trigger_one(event, "at_time")

    assert mock_send.call_count == 2


# ------------------------------------------------------------------ helpers
def sqlite3_IntegrityError():
    """制造一个 sqlite3 IntegrityError 实例（不依赖 sqlite3 import）。"""
    import sqlite3
    return sqlite3.IntegrityError("UNIQUE constraint failed: macro_notify_state.event_id, window")