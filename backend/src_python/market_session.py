# -*- coding: utf-8 -*-
"""交易时段判定（v409）—— 把"休市没价"与"管道断了"分开。

为什么必须分开：`hard_data_status` 只有 `feed_stale` 一种"有源但取不到数"，
于是 XAU/WTI 每天 UTC 22:00–23:00 的例行休市（以及周末）全被记成"断档"，
S20 / 错误码里这类缺口永远修不掉，还会被当成管道故障去查。
休市是**市场事实**，断档是**我们的故障**，混在一起两边都看不清。

口径（刻意保守，只覆盖确定知道的）：
  · 加密：7×24，永远 open；
  · CME 系（XAU/XAG/WTI/BRENT/OIL/NG）：每日 22:00–23:00 UTC 维护休市；
    周末从周五 22:00 UTC 到周日 23:00 UTC 停盘；
  · 其它品种：返回 unknown，**不猜**（宁可继续用旧口径，也不编一张假日历）。

纯函数，不读库不联网，可离线单测。
"""
from __future__ import annotations

import time
from typing import Optional

UTC = 0

# 7×24 的品种
_ALWAYS_OPEN = frozenset({"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LTC", "BCH", "USDT"})

# CME 系：每日维护窗口（UTC 小时，左闭右开）
_CME_DAILY_CLOSE_UTC = (22, 23)

# 周末：周五 22:00 UTC 收盘 → 周日 23:00 UTC 开盘
_CME_WEEKEND_CLOSE_HOUR = 22      # 周五
_CME_WEEKEND_OPEN_HOUR = 23       # 周日

_CME_ASSETS = frozenset({"XAU", "XAG", "WTI", "BRENT", "OIL", "NG", "COPPER"})


def _utc_parts(ts: Optional[float]):
    t = time.gmtime(float(ts) if ts else time.time())
    return t.tm_wday, t.tm_hour       # wday: 周一=0 … 周日=6


def session_state(asset: str, ts: Optional[float] = None) -> str:
    """→ 'open' / 'closed' / 'unknown'（unknown = 我们没有这个品种的日历，不猜）。"""
    a = str(asset or "").strip().upper()
    if a in _ALWAYS_OPEN:
        return "open"
    if a not in _CME_ASSETS:
        return "unknown"
    wday, hour = _utc_parts(ts)
    if wday == 5:                                   # 周六整天停盘
        return "closed"
    if wday == 6 and hour < _CME_WEEKEND_OPEN_HOUR:  # 周日晚 23:00 UTC 前
        return "closed"
    if wday == 4 and hour >= _CME_WEEKEND_CLOSE_HOUR:  # 周五 22:00 之后
        return "closed"
    lo, hi = _CME_DAILY_CLOSE_UTC
    if lo <= hour < hi:
        return "closed"
    return "open"


def closed_reason(asset: str, ts: Optional[float] = None) -> str:
    """人话原因，写进错误码备注用。"""
    if session_state(asset, ts) != "closed":
        return ""
    wday, hour = _utc_parts(ts)
    if wday == 5 or (wday == 6 and hour < _CME_WEEKEND_OPEN_HOUR) \
            or (wday == 4 and hour >= _CME_WEEKEND_CLOSE_HOUR):
        return "周末休市"
    return "每日维护休市(UTC 22:00-23:00)"
