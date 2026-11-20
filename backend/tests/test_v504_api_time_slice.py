#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v504：时间口径 helper 的单测（api_time，从 api_server 下沉）。

这是一次真切片：api_server 从 9292 行减到 9263 行，四个纯函数搬进 api_time，
调用点靠同名别名保持不变，行为不变。

为什么要给它单测：这三套时间串的**分隔符和时区语义必须与列一致**，
SQLite 的 TEXT 比较是字节序，' '(0x20) < 'T'(0x54) ——
只要分隔符错一个字符，窗口过滤就会把整天的数据漏进来（v302 就真出过）。
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import api_time as at


SHANGHAI = timezone(timedelta(hours=8))


# ── 'T' 分隔 UTC ─────────────────────────────────────────────
def test_utc_ts_to_iso_uses_T_separator_and_utc():
    # 2026-11-20 00:00:00 UTC
    ts = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
    assert at._utc_ts_to_iso(ts) == "2026-11-20T00:00:00"


def test_utc_ts_to_iso_has_no_suffix_or_microseconds():
    ts = int(datetime(2026, 11, 20, 12, 34, 56, 789000, tzinfo=timezone.utc).timestamp())
    out = at._utc_ts_to_iso(ts)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", out)


def test_utc_ts_to_iso_is_lexicographically_monotonic():
    # 这是整个 helper 存在的理由：字符串序 == 时间序
    a = at._utc_ts_to_iso(1763616000)
    b = at._utc_ts_to_iso(1763616000 + 3600)
    assert a < b


# ── 空格分隔 UTC ────────────────────────────────────────────
def test_utc_ts_to_space_iso_uses_space_separator():
    ts = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
    assert at._utc_ts_to_space_iso(ts) == "2026-11-20 00:00:00"


def test_T_and_space_helpers_differ_and_do_not_interchange():
    ts = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
    t_sep = at._utc_ts_to_iso(ts)
    sp = at._utc_ts_to_space_iso(ts)
    assert t_sep != sp
    # 字节序：空格 < 'T' —— 混用会把整天数据漏进窗口
    assert "2026-11-20 00:00:00" < "2026-11-20T00:00:00"


def test_space_form_sorts_before_T_form_for_same_instant():
    ts = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
    assert at._utc_ts_to_space_iso(ts) < at._utc_ts_to_iso(ts)


def test_both_helpers_are_eight_hours_apart_in_wall_clock():
    # 同一个 epoch，'T' 串是 UTC 口径；北京时间串必须晚 8 小时
    ts = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
    beijing = datetime.fromtimestamp(ts, tz=SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    assert beijing == "2026-11-20 08:00:00"


# ── 北京“现在” ──────────────────────────────────────────────
def test_now_iso_is_beijing_with_offset_and_second_precision():
    out = at._now_iso()
    assert out.endswith("+08:00")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00", out)


def test_now_iso_is_within_a_minute_of_real_time():
    now = datetime.now(SHANGHAI)
    parsed = datetime.fromisoformat(at._now_iso())
    assert abs((now - parsed).total_seconds()) < 60


# ── 新鲜度换算 ──────────────────────────────────────────────
def test_age_hours_naive_string_is_read_as_beijing():
    # 无时区串按北京时间解读：北京 1 小时前 ≈ 1.0 小时
    dt = datetime.now(SHANGHAI) - timedelta(hours=1)
    s = dt.strftime("%Y-%m-%d %H:%M:%S")
    assert at._age_hours_of(s) == pytest.approx(1.0, abs=0.1)


def test_age_hours_tz_aware_is_converted_not_naive():
    # 带 +00:00 的串按 UTC 换算：同样是"1 小时前"，不能差 8 小时
    dt = (datetime.now(SHANGHAI) - timedelta(hours=1)).astimezone(timezone.utc)
    s = dt.isoformat(timespec="seconds")
    assert at._age_hours_of(s) == pytest.approx(1.0, abs=0.1)


def test_age_hours_garbage_returns_none_meaning_fresh():
    assert at._age_hours_of("不是时间") is None
    assert at._age_hours_of(None) is None
    assert at._age_hours_of("") is None


def test_age_hours_future_time_is_negative():
    dt = datetime.now(SHANGHAI) + timedelta(hours=2)
    assert at._age_hours_of(dt.strftime("%Y-%m-%d %H:%M:%S")) == pytest.approx(-2.0, abs=0.1)


# ── 切片本身：api_server 仍是同名别名，调用点不受影响 ─────────
def test_api_server_still_exports_aliases():
    src = (BACKEND / "src_python" / "api_server.py").read_text(encoding="utf-8")
    assert "from api_time import _utc_ts_to_iso" in src
    assert "from api_time import _now_iso" in src
    assert "from api_time import _age_hours_of" in src
    # 原来的函数体不应再留在 api_server 里（否则就是两份定义）
    assert "def _utc_ts_to_iso(" not in src
    assert "def _now_iso(" not in src
    assert "def _age_hours_of(" not in src


def test_api_server_shrank():
    lines = (BACKEND / "src_python" / "api_server.py").read_text(encoding="utf-8").count("\n")
    assert lines < 9292, "切片后 api_server 应该真的变短"
