#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v521：宏观发布时点模块（news_release_timing）的行为测试。

这块从 api_server 里拆出来的目的有两个，测试也按这两条写：
  ① 水位语义：首启只建基线、不推存量；同一 series 的观测期变了才算真发布；
     没有 id 的事件不带水位，原样放行。
  ② 窗口语义：只在美东工作日 8:30 前后密集轮询，周末不发布 → 不进窗口；
     四档间隔（5/20/90/1800）的边界必须卡死，否则要么漏发布、要么烧配额。

另有一条**防腐**断言：api_server 里的 `_bls_*` / `_fred_*` 私有名必须仍然指向
本模块的实现（老调用点和测试都按 `api._xxx` 取），拆完不能变成两份代码。
"""
import datetime as dt
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import news_release_timing as nrt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_watermarks():
    nrt._FRED_SEEN.clear()
    nrt._BLS_SEEN.clear()
    yield
    nrt._FRED_SEEN.clear()
    nrt._BLS_SEEN.clear()


def _utc(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)


# ── ① 水位：只推真发布 ─────────────────────────────────────────
def test_fred_keys_split_series_and_observation_date():
    assert nrt.fred_date_key("fred-cpi-2026-08-01") == "2026-08-01"
    assert nrt.fred_date_key("no-date") == "no-date"
    assert nrt.fred_date_key("") == ""
    assert nrt.fred_series_key("fred-CPIAUCSL-2026-08-01") == "fred-CPIAUCSL"
    assert nrt.fred_series_key("fred-trend-T10Y2Y-2026-07-05") == "fred-trend-T10Y2Y"


def test_fred_first_sight_builds_baseline_without_pushing(capsys):
    ev = {"id": "fred-CPI-2026-08-01"}
    assert nrt.fred_new_only([ev]) == []          # 首启只建基线
    assert "基线" in capsys.readouterr().out
    assert nrt._FRED_SEEN["fred-CPI"] == "2026-08-01"


def test_fred_same_observation_is_silent_and_new_one_pushes():
    base = {"id": "fred-CPI-2026-08-01"}
    nrt.fred_new_only([base])                     # 建基线
    assert nrt.fred_new_only([base]) == []        # 同一观测日：不是新发布
    nxt = {"id": "fred-CPI-2026-09-01"}
    assert nrt.fred_new_only([nxt]) == [nxt]      # 观测日变了 → 真发布
    assert nrt._FRED_SEEN["fred-CPI"] == "2026-09-01"
    # 上游回发旧观测（数据修订）也算一次变化：宁可多推一次，也不静默吞掉修订
    assert nrt.fred_new_only([base]) == [base]
    assert nrt.fred_new_only([base]) == []        # 同一值连发两次则静默


def test_fred_series_are_independent():
    nrt.fred_new_only([{"id": "fred-CPI-2026-08-01"}])       # CPI 建基线
    jobs = {"id": "fred-PAYEMS-2026-08-01"}
    assert nrt.fred_new_only([jobs]) == []                  # 另一个 series 也先建基线
    assert nrt.fred_new_only([{"id": "fred-CPI-2026-09-01"}]) != []
    assert nrt.fred_new_only([jobs]) == []                  # 不受 CPI 影响


def test_events_without_id_always_pass_through():
    ev = {"title": "没有 id 的手工事件"}
    assert nrt.fred_new_only([ev]) == [ev]        # 不带水位，谈不上"旧"
    assert nrt.bls_new_only([ev]) == [ev]


def test_bls_period_change_is_the_only_signal():
    aug = {"id": "bls-CUUR0000SA0-2026-M07"}
    assert nrt.bls_new_only([aug]) == []
    assert nrt.bls_new_only([aug]) == []
    sep = {"id": "bls-CUUR0000SA0-2026-M08"}
    assert nrt.bls_new_only([sep]) == [sep]
    assert nrt._BLS_SEEN["bls-CUUR0000SA0"] == "2026-M08"


def test_bls_series_are_independent_and_malformed_ids_are_stable():
    nrt.bls_new_only([{"id": "bls-A-2026-M07"}])
    b = {"id": "bls-B-2026-M07"}
    assert nrt.bls_new_only([b]) == []            # B 首启也建基线
    assert nrt.bls_new_only([b]) == []            # 同 id 再来不是新发布
    assert nrt.bls_new_only([{"id": "bls-A-2026-M08"}]) != []


# ── ② 美东时间与四档窗口 ───────────────────────────────────────
def test_us_eastern_offset_flips_on_the_dst_boundaries():
    assert nrt.us_eastern_offset(_utc(2026, 1, 15)) == -5
    assert nrt.us_eastern_offset(_utc(2026, 7, 15)) == -4
    # 2026-03-08 是 3 月第二个周日：之前 EST，当天起 EDT
    assert nrt.us_eastern_offset(_utc(2026, 3, 7, 12)) == -5
    assert nrt.us_eastern_offset(_utc(2026, 3, 9, 12)) == -4
    # 2026-11-01 是 11 月第一个周日：之前 EDT，当天起 EST
    assert nrt.us_eastern_offset(_utc(2026, 10, 31, 12)) == -4
    assert nrt.us_eastern_offset(_utc(2026, 11, 2, 12)) == -5


def test_windows_are_workday_only():
    sat = dt.datetime(2026, 11, 21, 8, 30)       # 周六
    assert nrt.is_us_workday(sat) is False
    assert nrt.bls_core_window_active(sat) is False
    assert nrt.bls_mid_window_active(sat) is False
    assert nrt.bls_window_active(sat) is False
    # 周末也不该给"密集间隔"，否则白烧配额
    assert nrt.bls_sleep_seconds(_utc(2026, 11, 21, 13, 30)) == 1800


def test_window_boundaries_are_exact():
    wed = dt.datetime(2026, 11, 18)
    assert nrt.bls_window_active(wed.replace(hour=7, minute=29)) is False
    assert nrt.bls_window_active(wed.replace(hour=7, minute=30)) is True
    assert nrt.bls_window_active(wed.replace(hour=10, minute=30)) is True
    assert nrt.bls_window_active(wed.replace(hour=10, minute=31)) is False

    assert nrt.bls_core_window_active(wed.replace(hour=8, minute=29)) is False
    assert nrt.bls_core_window_active(wed.replace(hour=8, minute=30)) is True
    assert nrt.bls_core_window_active(wed.replace(hour=8, minute=50)) is True
    assert nrt.bls_core_window_active(wed.replace(hour=8, minute=51)) is False

    assert nrt.bls_mid_window_active(wed.replace(hour=8, minute=15)) is True
    assert nrt.bls_mid_window_active(wed.replace(hour=8, minute=20)) is True
    assert nrt.bls_mid_window_active(wed.replace(hour=8, minute=30)) is False  # 黄金窗接管
    assert nrt.bls_mid_window_active(wed.replace(hour=8, minute=50)) is False  # 黄金窗尾部
    assert nrt.bls_mid_window_active(wed.replace(hour=9, minute=5)) is True
    assert nrt.bls_mid_window_active(wed.replace(hour=9, minute=6)) is False


def test_sleep_seconds_maps_summer_and_winter_750_clock():
    # 夏令时：美东 08:30 = UTC 12:30；冬令时 = UTC 13:30
    assert nrt.bls_sleep_seconds(_utc(2026, 7, 15, 12, 30)) == 5     # 黄金窗
    assert nrt.bls_sleep_seconds(_utc(2026, 7, 15, 12, 20)) == 20    # 中频
    assert nrt.bls_sleep_seconds(_utc(2026, 7, 15, 13, 30)) == 90    # 外围
    assert nrt.bls_sleep_seconds(_utc(2026, 7, 15, 20, 0)) == 1800   # 窗口外
    assert nrt.bls_sleep_seconds(_utc(2026, 1, 15, 13, 30)) == 5     # 冬令时同一时刻
    assert nrt.bls_sleep_seconds(_utc(2026, 1, 15, 12, 30)) == 90    # 冬令时 07:30 ET：外围窗刚开


# ── ③ 防腐：拆出去之后 api_server 的名字必须仍指向这一份实现 ─────────
def test_api_server_reexports_the_same_implementation():
    import api_server as api

    assert api._bls_sleep_seconds is nrt.bls_sleep_seconds
    assert api._us_eastern_offset is nrt.us_eastern_offset
    assert api._bls_window_active is nrt.bls_window_active
    assert api._fred_new_only is nrt.fred_new_only
    assert api._bls_new_only is nrt.bls_new_only
    # 水位必须是同一个 dict 对象，否则 watcher 清空的和测试断言的不是同一个池
    assert api._FRED_SEEN is nrt._FRED_SEEN
    assert api._BLS_SEEN is nrt._BLS_SEEN
