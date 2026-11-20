# -*- coding: utf-8 -*-
"""v491 交易规则层：交易时段、整股、能否做空。

模拟盘最容易"看起来能交易、其实不能"的三件事：
  1. 美股收盘后照样按隔夜价成交 —— 现实里下不进去；
  2. 股票下 0.37 股 —— 现实里没有碎股可挂；
  3. 直接空 NVDA —— 现实里要先融券。

这里只做**规则判断**，不碰账户、不下单；preflight 与 executor 共用同一份口径。
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ET = ZoneInfo("America/New_York") if ZoneInfo else None
KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None

# 2026 美股休市（NYSE 全日休市日；半天市按正常时段处理，宁可放过不误杀）
US_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}

KR_HOLIDAYS_2026 = {
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-03-02", "2026-05-05",
    "2026-05-25", "2026-08-15", "2026-09-24", "2026-09-25", "2026-09-26",
    "2026-10-03", "2026-10-05", "2026-10-09", "2026-12-25",
}


def classify(asset: str) -> str:
    """资产 → 规则类别：crypto / perp / us_stock / kr_stock / commodity。"""
    up = (asset or "").upper()
    try:
        from feeds import quote_sources as qs
        mkt = qs.market_of(up)
        sym = qs.symbol_of(up) or ""
    except Exception:
        mkt, sym = "", ""
    if mkt == "binance_fapi":
        return "crypto"
    if mkt == "okx_swap":
        return "perp"
    if mkt == "domestic_spot":
        return "commodity"
    if mkt == "yahoo":
        if up in ("SKHYNIX", "HYNIX", "SAMSUNG") or sym.endswith(".KS"):
            return "kr_stock"
        return "us_stock"
    return "unknown"


def _local_date(ts: int, tz) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(int(ts), tz)


def session_open(asset: str, ts: Optional[int] = None) -> Tuple[bool, str]:
    """该资产此刻能否下单；返回 (是否开市, 原因)。"""
    ts = int(ts or _dt.datetime.now().timestamp())
    kind = classify(asset)
    if kind in ("crypto", "perp", "unknown"):
        return True, "24/7"
    if kind == "us_stock":
        t = _local_date(ts, ET)
        if t.weekday() >= 5:
            return False, "美股周末休市"
        if t.strftime("%Y-%m-%d") in US_HOLIDAYS_2026:
            return False, "美股假日休市"
        hm = t.hour * 60 + t.minute
        if 9 * 60 + 30 <= hm < 16 * 60:
            return True, "美股盘中"
        return False, f"美股盘外（美东 {t:%H:%M}）"
    if kind == "kr_stock":
        t = _local_date(ts, KST)
        if t.weekday() >= 5:
            return False, "韩股周末休市"
        if t.strftime("%Y-%m-%d") in KR_HOLIDAYS_2026:
            return False, "韩股假日休市"
        hm = t.hour * 60 + t.minute
        if 9 * 60 <= hm < 15 * 60 + 30:
            return True, "韩股盘中"
        return False, f"韩股盘外（首尔 {t:%H:%M}）"
    if kind == "commodity":
        # NYMEX 原油：周日 18:00 ET 开，周五 17:00 ET 收，日内 17:00-18:00 结算暂停
        t = _local_date(ts, ET)
        wd, hm = t.weekday(), t.hour * 60 + t.minute
        if wd == 5:
            return False, "原油周末休市"
        if wd == 6 and hm < 18 * 60:
            return False, "原油周末休市"
        if wd == 4 and hm >= 17 * 60:
            return False, "原油周五收盘后"
        if 17 * 60 <= hm < 18 * 60:
            return False, "原油每日结算暂停"
        return True, "原油盘中"
    return True, "未知品种（不阻断）"


def allows_short(asset: str) -> Tuple[bool, str]:
    """股票没有券源就不允许裸空；加密/贵金属永续可双向。"""
    kind = classify(asset)
    if kind in ("us_stock", "kr_stock"):
        return False, "股票模拟盘无券源，禁止裸空（只能先买后卖）"
    return True, "允许"


def lot_size(asset: str) -> float:
    """最小交易单位：股票 1 股；加密/商品允许小数。"""
    return 1.0 if classify(asset) in ("us_stock", "kr_stock") else 0.0


def round_qty(asset: str, qty: float) -> float:
    """把数量按最小单位向下取整（股票 3.7 股 → 3 股；不足 1 股 → 0）。"""
    step = lot_size(asset)
    q = float(qty or 0.0)
    if step <= 0:
        return q
    return float(int(q / step) * step)
