#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宏观发布时点：官方数据（BLS / FRED）的"水位记忆"与发布窗口调度。

这两件事为什么放在 api_server 之外：

① 它们是**纯函数 + 两个进程级水位**，不碰数据库、不发请求。放在 9k 行的
   api_server 里，既测不细（连带 import FastAPI 全家），也看不见边界。
② 口径必须唯一：FRED 与 BLS 的"只推新发布、首启建基线"是同一条规则的两个实现，
   写在一起才能一眼看出它们是不是还在守同一条规则。

两个水位都是**进程级**：首次见到某 series 只建基线、不推送存量（月度指标观测
滞后一期，首启全推会把旧观测当新事件刷屏）。

窗口调度（美东 8:30 发布，工作日）：
  黄金窗 08:30–08:50 → 5s      发布瞬间捕获
  中频窗 08:15–08:30 / 08:50–09:05 → 20s
  外围窗 07:30–10:30 → 90s
  窗口外 → 1800s              配额 ~460 次/日 < 免费额度 500
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List

# ── FRED 水位：键 = series 前缀，值 = 已见观测日 ────────────────────────
_FRED_SEEN: Dict[str, str] = {}
_FRED_SEEN_LOCK = threading.Lock()

# ── BLS 水位：键 = series 前缀，值 = 已见数据期（YYYY-Mxx） ─────────────
_BLS_SEEN: Dict[str, str] = {}
_BLS_SEEN_LOCK = threading.Lock()


def fred_date_key(eid: str) -> str:
    """从事件 id 提取观测日期：'fred-cpi-2026-08-01' → '2026-08-01'。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", eid or "")
    return m.group(1) if m else (eid or "")


def fred_series_key(eid: str) -> str:
    """事件 id → series 水位键：去掉尾部观测日期，保留前缀。

    'fred-CPIAUCSL-2026-08-01' → 'fred-CPIAUCSL'
    'fred-trend-T10Y2Y-2026-07-05' → 'fred-trend-T10Y2Y'
    """
    return re.sub(r"-?\d{4}-\d{2}-\d{2}$", "", eid or "") or (eid or "")


def fred_new_only(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一 series 的观测日**发生变化** → 才算真发布。

    注意是"变化"不是"变大"：FRED 会回填修订历史观测，回发旧日期等于一次数据修订，
    值得推一次；连发两次同一个值才静默。

    首轮（某 series 首次见到）只建基线、不推送；没有 id 的事件原样放行
    （它们不带水位，谈不上"旧"）。
    """
    out: List[Dict[str, Any]] = []
    with _FRED_SEEN_LOCK:
        for ev in events:
            eid = str(ev.get("id") or "")
            if not eid:
                out.append(ev)
                continue
            series_key = fred_series_key(eid)
            date_key = fred_date_key(eid)
            prev = _FRED_SEEN.get(series_key)
            if prev is None:
                _FRED_SEEN[series_key] = date_key
                print(f"[FRED] 基线 {series_key[:40]} @ {date_key}（不推存量，等下次真发布）",
                      flush=True)
                continue
            if date_key != prev:
                _FRED_SEEN[series_key] = date_key
                out.append(ev)
    return out


def bls_new_only(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一 series 的数据期（YYYY-Mxx）**变化** → 才算官方发布。首启只建基线。

    同 FRED：按"变化"判，不按"变大"判 —— 官方修订上一期数据时也要推出去。"""
    out: List[Dict[str, Any]] = []
    with _BLS_SEEN_LOCK:
        for ev in events:
            eid = str(ev.get("id") or "")
            if not eid:
                out.append(ev)
                continue
            m = re.search(r"-(\d{4}-M\d{2})$", eid)
            period_key = m.group(1) if m else (eid or "")
            series_key = re.sub(r"-?\d{4}-M\d{2}$", "", eid) or eid
            prev = _BLS_SEEN.get(series_key)
            if prev is None:
                _BLS_SEEN[series_key] = period_key
                print(f"[BLS] 基线 {series_key[:40]} @ {period_key}（不推存量，等下次真发布）",
                      flush=True)
                continue
            if period_key != prev:
                _BLS_SEEN[series_key] = period_key
                out.append(ev)
    return out


# ── 美东时间换算：DST = 3 月第二个周日 ~ 11 月第一个周日 ────────────────
def us_eastern_offset(dt: datetime) -> int:
    """按给定 UTC 时刻判断美国东部当前偏移（-4 EDT / -5 EST）。"""
    year = dt.year
    march_first = datetime(year, 3, 1, tzinfo=dt.tzinfo)
    days_to_second_sun = (7 - march_first.weekday()) % 7 + 7
    dst_start = march_first + timedelta(days=days_to_second_sun)
    nov_first = datetime(year, 11, 1, tzinfo=dt.tzinfo)
    days_to_first_sun = (7 - nov_first.weekday()) % 7
    dst_end = nov_first + timedelta(days=days_to_first_sun)
    return -4 if dst_start <= dt < dst_end else -5


def is_us_workday(dt: datetime) -> bool:
    return dt.weekday() < 5


def bls_window_active(dt: datetime) -> bool:
    """外围窗口：美东工作日 07:30–10:30。"""
    if not is_us_workday(dt):
        return False
    minutes = dt.hour * 60 + dt.minute
    return 7 * 60 + 30 <= minutes <= 10 * 60 + 30


def bls_core_window_active(dt: datetime) -> bool:
    """黄金窗口：美东工作日 08:30–08:50（BLS 发布后 1–2 分钟即更新）。"""
    if not is_us_workday(dt):
        return False
    minutes = dt.hour * 60 + dt.minute
    return 8 * 60 + 30 <= minutes <= 8 * 60 + 50


def bls_mid_window_active(dt: datetime) -> bool:
    """中频窗口：美东工作日 08:15–08:30 与 08:50–09:05（黄金窗两侧）。"""
    if not is_us_workday(dt):
        return False
    minutes = dt.hour * 60 + dt.minute
    return (8 * 60 + 15 <= minutes < 8 * 60 + 30) or (8 * 60 + 50 < minutes <= 9 * 60 + 5)


def bls_sleep_seconds(now_utc: datetime) -> int:
    """下一次轮询间隔：黄金窗 5s / 中频 20s / 外围 90s / 窗口外 1800s。"""
    et = now_utc + timedelta(hours=us_eastern_offset(now_utc))
    if bls_core_window_active(et):
        return 5
    if bls_mid_window_active(et):
        return 20
    if bls_window_active(et):
        return 90
    return 1800
