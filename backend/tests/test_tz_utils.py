"""v171 时区 / ISO-8601 / 时间窗口 — 单元测试。

覆盖：
- validate_iso8601 三态（精确时间 / AMBIGUOUS / 空串）+ 拒绝模糊文本
- format_dual 四时区（UTC / 北京 / 美东 / 美西）+ display_tz 切换
- get_window_label 5m / 30m / 2h / 1d / extended
- parse_iso8601 strict / 非严格
- detect_timezone_hint
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import tz_utils


# ────────────────────────────────────────────────────────────────────────────
# validate_iso8601 三态
# ────────────────────────────────────────────────────────────────────────────

def test_validate_iso8601_accepts_naive_strict():
    r = tz_utils.validate_iso8601("2025-01-15 09:30:00")
    assert r["ok"] is True
    assert r["normalized"].endswith("Z")


def test_validate_iso8601_accepts_utc_z():
    r = tz_utils.validate_iso8601("2025-01-15T09:30:00Z")
    assert r["ok"] is True
    assert r["normalized"] == "2025-01-15T09:30:00Z"


def test_validate_iso8601_accepts_offset():
    r = tz_utils.validate_iso8601("2025-01-15T17:30:00+08:00")
    assert r["ok"] is True
    # 标准化为 UTC
    assert r["normalized"] == "2025-01-15T09:30:00Z"


def test_validate_iso8601_accepts_ambiguous_marker():
    r = tz_utils.validate_iso8601("AMBIGUOUS")
    assert r["ok"] is True
    assert r["normalized"] == "AMBIGUOUS"


def test_validate_iso8601_accepts_empty():
    r = tz_utils.validate_iso8601("")
    assert r["ok"] is True
    assert r["normalized"] == ""


def test_validate_iso8601_rejects_vague_text():
    """'上周五' / '昨晚' / '2 hours ago' 全部拒绝。"""
    for vague in ["上周五", "昨晚", "2 hours ago", "前天", "刚刚", "a moment ago"]:
        r = tz_utils.validate_iso8601(vague)
        assert r["ok"] is False, f"应被拒: {vague!r}"
        assert r["normalized"] is None


def test_validate_iso8601_rejects_short_format():
    """'YYYY-MM-DD HH:MM' 缺秒应被拒。"""
    r = tz_utils.validate_iso8601("2025-01-15 09:30")
    assert r["ok"] is False
    assert "not ISO-8601 strict" in r["reason"]


def test_validate_iso8601_rejects_pre_1990():
    r = tz_utils.validate_iso8601("1989-12-31 23:59:59")
    assert r["ok"] is False
    assert "1990" in r["reason"]


def test_validate_iso8601_rejects_non_string():
    r = tz_utils.validate_iso8601(2025011509)
    assert r["ok"] is False
    assert "type" in r["reason"]


def test_validate_iso8601_with_disallow_empty():
    r = tz_utils.validate_iso8601("", allow_empty=False)
    assert r["ok"] is False
    assert "empty" in r["reason"]


# ────────────────────────────────────────────────────────────────────────────
# format_dual 四时区
# ────────────────────────────────────────────────────────────────────────────

def test_format_dual_epoch_to_dual():
    """epoch 1757923800 → UTC 2025-09-15 14:50 UTC, +8 → 22:50 北京。"""
    epoch = int(datetime(2025, 9, 15, 14, 50, 0, tzinfo=timezone.utc).timestamp())
    d = tz_utils.format_dual(epoch)
    assert d["epoch"] == epoch
    assert d["iso"] == "2025-09-15T14:50:00Z"
    assert d["utc"] == "2025-09-15 14:50:00"
    assert d["cn"] == "2025-09-15 22:50:00"
    assert d["us_eastern"] == "2025-09-15 09:50:00"
    assert d["us_pacific"] == "2025-09-15 06:50:00"
    assert d["display_tz"] == "Asia/Shanghai"
    assert d["display"] == d["cn"]


def test_format_dual_aware_dt():
    dt = datetime(2025, 9, 15, 14, 50, 0, tzinfo=timezone(timedelta(hours=8)))
    d = tz_utils.format_dual(dt)
    assert d["iso"] == "2025-09-15T06:50:00Z"
    assert d["cn"] == "2025-09-15 14:50:00"


def test_format_dual_display_tz_us_eastern():
    """切到美东 display。"""
    os.environ["TRIDENT_TZ"] = "US/Eastern"
    try:
        epoch = int(datetime(2025, 9, 15, 14, 50, 0, tzinfo=timezone.utc).timestamp())
        d = tz_utils.format_dual(epoch)
        assert d["display_tz"] == "US/Eastern"
        assert d["display"] == "2025-09-15 09:50:00"
        # cn / us 字段不受影响
        assert d["cn"] == "2025-09-15 22:50:00"
    finally:
        del os.environ["TRIDENT_TZ"]


def test_format_dual_invalid_input_returns_empty():
    d = tz_utils.format_dual("上周五")
    assert d["epoch"] == 0
    assert d["iso"] == ""


def test_format_dual_with_string_iso_input():
    d = tz_utils.format_dual("2025-09-15T14:50:00Z")
    assert d["iso"] == "2025-09-15T14:50:00Z"
    assert d["cn"] == "2025-09-15 22:50:00"


# ────────────────────────────────────────────────────────────────────────────
# parse_iso8601 strict / 非严格
# ────────────────────────────────────────────────────────────────────────────

def test_parse_iso8601_strict_rejects_epoch():
    """strict 模式下 epoch 数字应被拒（不视为 ISO-8601）。"""
    assert tz_utils.parse_iso8601("1757923800", strict=True) is None


def test_parse_iso8601_nonstrict_accepts_epoch():
    """非严格模式接受 epoch 数字。"""
    dt = tz_utils.parse_iso8601("1757923800", strict=False)
    assert dt is not None
    assert dt.tzinfo is not None  # tz-aware


# ────────────────────────────────────────────────────────────────────────────
# get_window_label 时间窗口
# ────────────────────────────────────────────────────────────────────────────

def test_window_label_boundaries():
    assert tz_utils.get_window_label(0) == "5m"
    assert tz_utils.get_window_label(5) == "5m"
    assert tz_utils.get_window_label(6) == "30m"
    assert tz_utils.get_window_label(30) == "30m"
    assert tz_utils.get_window_label(31) == "2h"
    assert tz_utils.get_window_label(120) == "2h"
    assert tz_utils.get_window_label(121) == "1d"
    assert tz_utils.get_window_label(1440) == "1d"
    assert tz_utils.get_window_label(1441) == "extended"
    assert tz_utils.get_window_label(99999) == "extended"


def test_window_label_handles_invalid():
    assert tz_utils.get_window_label("") == "extended"
    assert tz_utils.get_window_label(None) == "extended"
    assert tz_utils.get_window_label("not-a-number") == "extended"


# ────────────────────────────────────────────────────────────────────────────
# detect_timezone_hint
# ────────────────────────────────────────────────────────────────────────────

def test_detect_tz_hint():
    assert tz_utils.detect_timezone_hint("2025-01-15T09:30:00Z") == "UTC"
    assert tz_utils.detect_timezone_hint("2025-01-15T17:30:00+08:00") == "Asia/Shanghai"
    assert tz_utils.detect_timezone_hint("New York EST time") == "US/Eastern"
    assert tz_utils.detect_timezone_hint("PST 09:30") == "US/Pacific"
    assert tz_utils.detect_timezone_hint("") is None
    assert tz_utils.detect_timezone_hint("普通文字") is None


# ────────────────────────────────────────────────────────────────────────────
# DISPLAY_TZ_OPTIONS 完整性
# ────────────────────────────────────────────────────────────────────────────

def test_display_tz_options_contains_all_keys():
    for name in ("Asia/Shanghai", "US/Eastern", "US/Pacific", "UTC"):
        assert name in tz_utils.DISPLAY_TZ_OPTIONS


def test_get_display_tz_default_is_shanghai():
    os.environ.pop("TRIDENT_TZ", None)
    tz = tz_utils.get_display_tz()
    assert tz == tz_utils.TZ_SHANGHAI


# ─── v171+：DST 边界测试 ──────────────────────────────────────────────────
# Python stdlib 的 EST（UTC-5）固定偏移无 DST；
# 但格式化时分 offset=−05:00 不变（验证文档化行为）。
# 如要真实 DST 切换，需用 America/New_York（通过 _tz_for 或 Intl）。
# 此处主要验证：跨 DST 边界 epoch 的 format_dual 输出结构稳定。

def test_format_dual_across_us_eastern_spring_forward_2025():
    """2025-03-09 美东春季跳跃：02:00→03:00 → 跨过 02:00~03:00 不存在。"""
    # 02:30 EST (UTC-5) 实际不存在 → 这个 epoch 对应 07:30 UTC
    # epoch: 2025-03-09 07:30:00 UTC (= 02:30 EST 标准时)
    from datetime import datetime, timezone
    epoch = int(datetime(2025, 3, 9, 7, 30, 0, tzinfo=timezone.utc).timestamp())
    d = tz_utils.format_dual(epoch, display_tz="US/Eastern")
    # 我们的固定偏移 → 输出 02:30:00（不是 03:30）；这是文档化行为
    assert d["display"] == "2025-03-09 02:30:00"
    assert d["display_tz"] == "US/Eastern"
    assert d["utc"] == "2025-03-09 07:30:00"


def test_format_dual_across_us_eastern_fall_back_2025():
    """2025-11-02 美东秋季回退：02:00 重复 → 验证两小时都给 EST。"""
    from datetime import datetime, timezone
    # 04:30 UTC = 23:30 EST（前一天） 或 05:30 UTC = 00:30 EDT（重复）
    # 用 06:30 UTC = 01:30 EST（fall back 后）
    epoch = int(datetime(2025, 11, 2, 6, 30, 0, tzinfo=timezone.utc).timestamp())
    d = tz_utils.format_dual(epoch, display_tz="US/Eastern")
    assert d["utc"] == "2025-11-02 06:30:00"
    # EST (UTC-5) → 01:30
    assert d["display"] == "2025-11-02 01:30:00"


def test_format_dual_shanghai_has_no_dst():
    """北京时区无 DST → 跨任意日 → UTC offset 恒为 +8。"""
    from datetime import datetime, timezone
    for month in (1, 4, 7, 10):
        e = int(datetime(2025, month, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        d = tz_utils.format_dual(e, display_tz="Asia/Shanghai")
        # UTC 12:00 + 北京 8h = 20:00
        assert d["display"] == f"2025-{month:02d}-15 20:00:00"


def test_format_dual_epoch_zero_returns_full_dual():
    """epoch=0 → 1970-01-01 00:00:00 UTC（合法 epoch, 返回完整 dict）。"""
    d = tz_utils.format_dual(0, display_tz="Asia/Shanghai")
    assert d["epoch"] == 0
    assert d["iso"] == "1970-01-01T00:00:00Z"
    assert d["utc"] == "1970-01-01 00:00:00"
    assert d["display_tz"] == "Asia/Shanghai"


def test_format_dual_negative_epoch_returns_full_dual():
    """负 epoch（如 epoch=-1）→ 1969-12-31 23:59:59 UTC（合法 epoch）。"""
    d = tz_utils.format_dual(-1, display_tz="Asia/Shanghai")
    assert d["epoch"] == -1
    assert d["iso"] == "1969-12-31T23:59:59Z"
    assert d["display_tz"] == "Asia/Shanghai"


def test_format_dual_string_epoch_converts():
    """字符串 epoch（数字）→ 解析为 int。"""
    d = tz_utils.format_dual("1758400000", display_tz="UTC")
    assert d["epoch"] == 1758400000
    assert d["utc"] != ""
    assert d["display_tz"] == "UTC"


def test_format_dual_all_tz_present():
    """所有 4 时区字段都被填（非空 epoch 时）。"""
    d = tz_utils.format_dual(1758400000, display_tz="Asia/Shanghai")
    assert d["utc"] != ""
    assert d["cn"] != ""
    assert d["us_eastern"] != ""
    assert d["us_pacific"] != ""
    assert d["display"] != ""
    # display 应等于 cn（因为默认 Asia/Shanghai）
    assert d["display"] == d["cn"]