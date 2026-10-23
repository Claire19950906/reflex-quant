#!/usr/bin/env python3
"""
Trident Agent MVP — Market Snapshot Module
===========================================

多源直连行情快照（不依赖代理）：
  - BTC: binance.vision 官方数据镜像 (api/v3 现货) — 国内可直连
  - XAU: 新浪/东方财富现货金价 (engine.prices._fetch_gold_price) — 国内直连

为什么不用 fapi.binance.com / api.binance.com：
  直连被墙 (ConnectTimeout)，走本地代理又依赖代理软件常开且端口匹配，
  实测不可靠。binance.vision 与国内现货源稳定直连。

注：均为现货数据，无永续资金费率 —— funding 标 N/A；
XAU 无 24h/7d 行情统计 —— 相关字段标 N/A，不造数。

Usage:
    from market_snapshot import get_snapshot
    snap = await get_snapshot()        # Async (engine 内调用)
    snap = get_snapshot_sync()         # Sync (调试/非 async 场景)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import quote_sources

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    requests = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 监控资产（快照品种；全品种统一币安永续 fapi K 线口径）
_MONITOR_ASSETS = ("BTC", "XAU", "WTI", "ETH")

_FAPI = "https://fapi.binance.com"             # 币安永续合约（直连被墙，走代理）

# 资产 → 展示 symbol（真实数据源合约）。XAU=OKX 永续、WTI=国内现货(无永续)，
# 实际数据源路由见 quote_sources.ASSET_ROUTE（单一事实来源）。
_ASSET_SYMBOL = {
    "BTC": "BTCUSDT",       # 币安永续
    "ETH": "ETHUSDT",       # 币安永续
    "XAU": "XAU-USDT-SWAP", # OKX 永续
    "WTI": "WTIUSD",        # 国内现货（无永续）
}

# 多周期配置：周期 → (K线数量, 趋势一致性根数)（1d=5根≈5天连续；15m=8根≈2小时）
_MTF_PERIODS = (("1d", 8, 5), ("4h", 42, 6), ("1h", 48, 8), ("15m", 32, 8))
_MTF_LABELS = {"1d": "日线", "4h": "4H", "1h": "1H", "15m": "15m"}

# Debug 日志开关 — 稳定后设为 False, 避免刷屏
_LOUD = False

def _debug(msg: str) -> None:
    if _LOUD:
        print(msg)

_FETCH_TIMEOUT_S = 8.0        # 单次请求超时 (秒)
_FETCH_OHLCV_TIMEOUT_S = 15.0  # OHLCV 拉取超时

# 趋势判定阈值
_TREND_BULL_THRESHOLD = 3.0    # 7日涨幅 > 3% 判定为 Bull
_TREND_BEAR_THRESHOLD = -3.0   # 7日涨幅 < -3% 判定为 Bear
_TREND_CONSISTENCY_DAYS = 5    # 连续上涨/下跌天数阈值 (强趋势判定)


# ---------------------------------------------------------------------------
# HTTP session (requests 直连, 懒加载)
# ---------------------------------------------------------------------------

_SESSION: Any = None


def _session() -> Any:
    """Return a requests.Session that ignores env proxies (强制直连)."""
    global _SESSION
    if _SESSION is None:
        from http_retry import new_retry_session
        # 强制直连 + connect-only 重试：缓解 LetsVPN 对 binance.vision 首握手抖动
        _SESSION = new_retry_session(trust_env=False)
    return _SESSION


def _get_json(url: str, timeout: float) -> Any:
    resp = _session().get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# 永续资金费率 — fapi.binance.com premiumIndex (直连被墙, 必须走代理)
# ---------------------------------------------------------------------------

# 模块级代理 Session 复用：多线程并发拉快照时，每请求新建 Session 会打爆
# 本地代理连接（20+ 并发 × 新连接 → TIME_WAIT 堆积/ProxyError）。
# requests.Session 线程安全，全局复用一个即可（trust_env 默认 True 走 env 代理）。
_SESSION_LOCK = threading.Lock()
_proxy_session: Any = None


def _get_proxy_session() -> Any:
    global _proxy_session
    if _proxy_session is None:
        with _SESSION_LOCK:
            if _proxy_session is None:
                from http_retry import new_retry_session
                # 走 env 代理（trust_env=True）+ connect-only 重试：缓解币安 fapi 首握手抖动
                _proxy_session = new_retry_session(
                    trust_env=True, pool_connections=12, pool_maxsize=24
                )
    return _proxy_session


def _fetch_fapi_premium_sync(symbol: str) -> Optional[Dict[str, Optional[float]]]:
    """币安永续 premiumIndex → {funding, mark, index} (走 env HTTP_PROXY, 尊重 NO_PROXY).

    一次请求同时拿资金费率 (lastFundingRate)、永续标记价 (markPrice)、指数价 (indexPrice):
    - funding 用于①费率背离
    - (mark/index - 1)*10000 用于②基差背离 (Phase 1.5 接入)
    代理未开/端口不对/网络失败 → 返回 None (降级, 不阻塞主流程)。
    """
    try:
        data = _get_proxy_session().get(
            f"{_FAPI}/fapi/v1/premiumIndex?symbol={symbol}",
            timeout=_FETCH_TIMEOUT_S,
        ).json()
        return {
            "funding": _safe_float(data.get("lastFundingRate"), None),
            "mark": _safe_float(data.get("markPrice"), None),
            "index": _safe_float(data.get("indexPrice"), None),
        }
    except Exception:
        return None


def _basis_bps(prem: Dict[str, Optional[float]]) -> Optional[float]:
    """永续基差 (bp) = (markPrice/indexPrice - 1) * 10000，正=升水，负=贴水。"""
    mark = prem.get("mark")
    index = prem.get("index")
    if mark is None or not index:
        return None
    return round((mark / index - 1.0) * 10000.0, 2)


def _fetch_funding_rate_sync(symbol: str) -> Optional[float]:
    """永续资金费率 via fapi premiumIndex (走 env HTTP_PROXY, 尊重 NO_PROXY)."""
    prem = _fetch_fapi_premium_sync(symbol)
    return prem["funding"] if prem else None


def _fetch_fapi_open_interest_sync(symbol: str) -> Optional[float]:
    """币安永续持仓量 (openInterest, 张数) — 走代理。失败 None。

    用于③持仓变化背离 (Phase 1.5)：进程内缓存上一时刻 OI 做环比。
    """
    try:
        data = _get_proxy_session().get(
            f"{_FAPI}/fapi/v1/openInterest?symbol={symbol}",
            timeout=_FETCH_TIMEOUT_S,
        ).json()
        return _safe_float(data.get("openInterest"), None)
    except Exception:
        return None


# OI 环比缓存：首次拉取无上一时刻值 → oi_change_pct 返回 None（不造数）；
# 超过 _OI_TTL_S 的旧值不用于对比（跨期太长会失真）。
_oi_cache: Dict[str, Dict[str, Any]] = {}
_oi_cache_lock = threading.Lock()
_OI_TTL_S = 15 * 60.0


def _oi_change_pct(asset_id: str, oi_now: Optional[float]) -> Optional[float]:
    """持仓环比 %（复用 technical_indicators.oi_change_pct 的纯计算）。"""
    import technical_indicators as _ti
    if oi_now is None:
        return None
    now = time.monotonic()
    with _oi_cache_lock:
        prev = _oi_cache.get(asset_id)
        _oi_cache[asset_id] = {"value": oi_now, "ts": now}
    if not prev or now - prev["ts"] > _OI_TTL_S:
        return None
    return _ti.oi_change_pct(oi_now, prev["value"])


async def _fetch_funding_rate_async(symbol: str) -> Optional[float]:
    return await asyncio.to_thread(_fetch_funding_rate_sync, symbol)


# ---------------------------------------------------------------------------
# BTC — binance.vision 现货
# ---------------------------------------------------------------------------

def _fetch_btc_ticker_sync() -> Optional[Dict[str, Any]]:
    """BTC 24h ticker — 币安现货镜像直连优先（fapi 被美国 IP 封禁）。"""
    try:
        tick = quote_sources.fetch_binance_spot_ticker("BTCUSDT")
        if tick and tick.get("last"):
            return tick
    except Exception as e:
        print(f"  [SNAPSHOT] spot_ticker(BTC) 失败: {type(e).__name__}: {str(e)[:80]}")
    return _fetch_fapi_ticker_sync("BTCUSDT")


def _fetch_eth_ticker_sync() -> Optional[Dict[str, Any]]:
    """ETH 24h ticker — 币安现货镜像直连优先（fapi 被美国 IP 封禁）。"""
    try:
        tick = quote_sources.fetch_binance_spot_ticker("ETHUSDT")
        if tick and tick.get("last"):
            return tick
    except Exception as e:
        print(f"  [SNAPSHOT] spot_ticker(ETH) 失败: {type(e).__name__}: {str(e)[:80]}")
    return _fetch_fapi_ticker_sync("ETHUSDT")


async def _fetch_btc_ticker_async() -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_fetch_btc_ticker_sync)


def _fetch_btc_ohlcv_sync() -> Optional[Tuple[List[List[float]], str]]:
    """BTC 日线 klines (4h fallback) for 7d trend/ATR — 统一币安永续 fapi。"""
    for tf, limit, label in (("1d", 8, "日线"), ("4h", 42, "4h(降级)")):
        try:
            rows = quote_sources.fetch_binance_spot_candles("BTCUSDT", tf, limit)
            if not rows or len(rows) < 2:
                rows = _fetch_fapi_klines_sync("BTCUSDT", tf, limit)
            if rows and len(rows) >= 2:
                return (rows, label)
        except Exception as e:
            print(f"  [SNAPSHOT] fetch_ohlcv(BTCUSDT {label}) 失败: {type(e).__name__}: {str(e)[:80]}")
    return None


# K 线短缓存：1d/4h/1h/15m 的 K 线在数十秒内几乎不变，而每次快照要拉
# 4 资产×4 周期=16 次请求（XAU/WTI/ETH 还走代理），是快照耗时的绝对大头。
# 加 30s 缓存后，同周期重复拉取直接命中内存，快照从 ~11s 降到秒级，
# 避免 15s 整体超时导致整批 AI 分析拿不到盘面数据。
_klines_cache: Dict[str, Dict[str, Any]] = {}
_KLINES_TTL_S = 30.0
_klines_lock = threading.Lock()


def _fetch_fapi_klines_sync(symbol: str, interval: str, limit: int) -> Optional[List[List[float]]]:
    """币安永续 K 线（BTCUSDT/ETHUSDT/SOLUSDT 等加密币）— 直连被墙，走 env HTTP_PROXY 代理。
    30s 内存缓存：同 symbol+interval 命中直接返回，不重复请求。

    代理未开/网络失败 → None（降级 N/A，不阻塞主流程）。
    """
    key = f"{symbol}:{interval}:{limit}"
    now = time.monotonic()
    with _klines_lock:
        hit = _klines_cache.get(key)
        if hit and now - hit["ts"] < _KLINES_TTL_S:
            return hit["data"]
    try:
        data = _get_proxy_session().get(
            f"{_FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}",
            timeout=_FETCH_OHLCV_TIMEOUT_S,
        ).json()
        if not data or len(data) < 2:
            return None
        rows = [
            [r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
            for r in data
        ]
        with _klines_lock:
            _klines_cache[key] = {"ts": time.monotonic(), "data": rows}
        return rows
    except Exception as e:
        print(f"  [SNAPSHOT] fetch_fapi_klines({symbol} {interval}) 失败: {type(e).__name__}: {str(e)[:80]}")
        return None


def _fetch_fapi_ticker_sync(symbol: str) -> Optional[Dict[str, Any]]:
    """币安永续 24h ticker（BTCUSDT/ETHUSDT）— 走代理。失败 → None。"""
    try:
        data = _get_proxy_session().get(
            f"{_FAPI}/fapi/v1/ticker/24hr?symbol={symbol}",
            timeout=_FETCH_TIMEOUT_S,
        ).json()
        last = float(data.get("lastPrice") or 0)
        pct = float(data.get("priceChangePercent") or 0)
        if last <= 0:
            return None
        return {"last": last, "percentage": pct, "has_change": True, "info": data}
    except Exception as e:
        print(f"  [SNAPSHOT] fetch_fapi_ticker({symbol}) 失败: {type(e).__name__}: {str(e)[:80]}")
        return None


def _fetch_asset_klines(asset_id: str, interval: str, limit: int) -> Optional[List[List[float]]]:
    """按资产拉 K 线：BTC/ETH 走币安 fapi，XAU 走 OKX；WTI/美股(无永续) 返回 None（标 N/A，不造数）。

    v2026.10.05：okx_swap 分支改用 fetch_asset_candles_with_yahoo_fallback —— OKX SNI 阻断时
    自动降级到 Yahoo Finance 期货合约（GC=F/SI=F/CL=F），避免 XAU/XAG/WTI 完全断流。
    """
    r = quote_sources.route(asset_id)
    if not r:
        return None
    if r["market"] == "okx_swap":
        # OKX 优先 → Yahoo 兜底（v2026.10.05）
        return quote_sources.fetch_asset_candles_with_yahoo_fallback(
            asset_id, quote_sources.okx_bar(interval), limit
        )
    if r["market"] == "binance_fapi":
        # fapi 被住宅代理(美国 IP)地区封禁 → 现货镜像直连优先（已实测国内稳定）
        rows = quote_sources.fetch_binance_spot_candles(r["symbol"], interval, limit)
        if rows:
            return rows
        return _fetch_fapi_klines_sync(r["symbol"], interval, limit)
    return None  # domestic_spot / yahoo：无永续 K 线


def _fetch_mtf_ohlcv(asset_id: str) -> Dict[str, List[List[float]]]:
    """拉资产多周期 K 线：{interval: candles}，失败的周期不进 dict（标 N/A）。

    4 个周期并行拉取（原串行 4×~2s=8s，并行后 ~2s）——否则叠加 ticker/funding
    后单资产 6 请求串行 8-12s，超过快照超时会被整体丢弃（快照 down 的主因之一）。
    """
    from concurrent.futures import ThreadPoolExecutor

    out: Dict[str, List[List[float]]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            interval: pool.submit(_fetch_asset_klines, asset_id, interval, limit)
            for interval, limit, _consistency in _MTF_PERIODS
        }
        for interval, fut in futures.items():
            try:
                candles = fut.result(timeout=8.0)
            except Exception:
                candles = None
            if candles:
                out[interval] = candles
    return out


async def _fetch_btc_ohlcv_async() -> Optional[Tuple[List[List[float]], str]]:
    return await asyncio.to_thread(_fetch_btc_ohlcv_sync)


# ---------------------------------------------------------------------------
# XAU — 新浪/东方财富 现货金价
# ---------------------------------------------------------------------------

def _fetch_xau_ticker_sync() -> Optional[Dict[str, Any]]:
    """XAU 黄金价格 — OKX 永续 XAU-USDT-SWAP（真实合约，含 24h 变化）。失败走国内现货兜底。"""
    try:
        tick = quote_sources.fetch_okx_ticker("XAU-USDT-SWAP")
        if tick and tick.get("last"):
            return tick
    except Exception as e:
        print(f"  [SNAPSHOT] fetch_ticker(XAU-USDT-SWAP) 失败: {type(e).__name__}: {str(e)[:80]}")
    try:
        # 兜底直接用 _fetch_gold_price_domestic（纯国内现货），不再经过 _fetch_gold_price：
        # 后者内部会再试一次 OKX，OKX TLS 阻断时会二次超时，导致 ticker 总耗时超过
        # _collect_asset 的 10s 上限被整体丢弃（XAU 快照 unavailable 的根因）。
        from engine.prices import _fetch_gold_price_domestic
        price = _fetch_gold_price_domestic()
        if price is not None and price > 0:
            return {"last": price, "percentage": None, "has_change": False, "info": {}}
    except Exception:
        pass
    return None


def _fetch_wti_ticker_sync() -> Optional[Dict[str, Any]]:
    """WTI 原油价格 — 国内现货（新浪/东财）。无永续合约 → 无 24h/funding，has_change=False。"""
    try:
        from engine.prices import _fetch_wti_price  # 延迟 import 避免循环
        price = _fetch_wti_price()
        if price is not None and price > 0:
            return {"last": price, "percentage": None, "has_change": False, "info": {}}
    except Exception as e:
        print(f"  [SNAPSHOT] fetch_ticker(WTI 现货) 失败: {type(e).__name__}: {str(e)[:80]}")
    return None


async def _fetch_xau_ticker_async() -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_fetch_xau_ticker_sync)


# ---------------------------------------------------------------------------
# Safe field parsers
# ---------------------------------------------------------------------------

def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _extract_price(ticker: Dict[str, Any]) -> Optional[float]:
    for key in ("last", "close"):
        val = ticker.get(key)
        if val is not None:
            return _safe_float(val)
    info = ticker.get("info") or {}
    if isinstance(info, dict):
        for key in ("lastPrice", "last", "price"):
            val = info.get(key)
            if val is not None:
                return _safe_float(val)
    bid = ticker.get("bid")
    ask = ticker.get("ask")
    if bid is not None and ask is not None:
        return round((_safe_float(bid) + _safe_float(ask)) / 2.0, 4)
    return None


# ---------------------------------------------------------------------------
# Trend & regime computation
# ---------------------------------------------------------------------------

def _compute_period_stats(candles: List[List[float]], consistency: int = 5) -> Dict[str, Any]:
    """From OHLCV candles compute return, ATR, and trend (period-agnostic).

    consistency = 连续同向 K 线数判定 Strong 趋势（按周期校准：
    1d=5 根≈5天，4h=6 根≈24h，1h=8 根≈8h，15m=8 根≈2h）。
    """
    if not candles or len(candles) < 2:
        return _empty_7d_stats()

    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]

    current = closes[-1]
    prior = closes[0]
    if prior <= 0 or current <= 0:
        return _empty_7d_stats()

    ret = (current - prior) / prior * 100.0
    highest = max(highs)
    lowest = min(lows)
    range_pct = (highest - lowest) / current * 100.0 if current > 0 else 0.0

    tr_values: List[float] = []
    for i in range(1, len(candles)):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_values.append(tr)
    atr_value = sum(tr_values) / len(tr_values) if tr_values else 0.0
    atr_pct = (atr_value / current * 100.0) if current > 0 else 0.0

    n_up = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    n_down = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])

    if n_up >= consistency and ret > 0:
        trend, trend_strength = "Strong Bull", "high"
    elif n_down >= consistency and ret < 0:
        trend, trend_strength = "Strong Bear", "high"
    elif ret > _TREND_BULL_THRESHOLD:
        trend, trend_strength = "Bull", "medium"
    elif ret < _TREND_BEAR_THRESHOLD:
        trend, trend_strength = "Bear", "medium"
    elif abs(ret) < 1.0:
        trend, trend_strength = "Ranging", "low"
    elif ret > 0:
        trend, trend_strength = "Mild Bull", "low"
    else:
        trend, trend_strength = "Mild Bear", "low"

    return {
        "price_ago": round(prior, 4),
        "return_pct": round(ret, 2),
        "return_str": _format_pct(ret),
        "atr_pct": round(atr_pct, 3),
        "atr_value": round(atr_value, 4),
        "atr_str": f"{atr_pct:.3f}%",
        "trend": trend,
        "trend_strength": trend_strength,
        "n_up": n_up,
        "n_down": n_down,
        "highest": round(highest, 4),
        "lowest": round(lowest, 4),
        "range_pct": round(range_pct, 2),
        # 盘面技术指标（本地计算，样本不足 → None，不造数）：RSI/ADX/VWAP/量能比
        "indicators": _compute_indicators(candles),
    }


# ---------------------------------------------------------------------------
# 盘面技术指标（本地计算，零外部依赖）：
#   RSI14 / ADX14 / 窗口VWAP（现价相对位置）/ 量能比（最新量 vs 前20均量）
# 样本不足（如 1d 只有 8 根，RSI 需 ≥15 根、ADX 需 ≥29 根）→ None，禁止造数。
# ---------------------------------------------------------------------------

def _rsi14(closes: List[float]) -> Optional[float]:
    """RSI(14) — Wilder 平滑。需要 ≥15 个收盘价。"""
    if len(closes) < 15:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains[:14]) / 14.0
    avg_l = sum(losses[:14]) / 14.0
    for i in range(14, len(gains)):
        avg_g = (avg_g * 13.0 + gains[i]) / 14.0
        avg_l = (avg_l * 13.0 + losses[i]) / 14.0
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def _adx14(candles: List[List[float]]) -> Optional[float]:
    """ADX(14) — Wilder 平滑方向指标。需要 ≥29 根 K 线（2×14+1）。"""
    if len(candles) < 29:
        return None
    trs: List[float] = []
    pdms: List[float] = []
    ndms: List[float] = []
    for i in range(1, len(candles)):
        h, l, c = candles[i][2], candles[i][3], candles[i][4]
        pc = candles[i - 1][4]
        up = h - candles[i - 1][2]
        dn = candles[i - 1][3] - l
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdms.append(up if (up > dn and up > 0) else 0.0)
        ndms.append(dn if (dn > up and dn > 0) else 0.0)

    def _wilder(arr: List[float]) -> List[float]:
        smoothed = [sum(arr[:14])]
        for v in arr[14:]:
            smoothed.append((smoothed[-1] * 13.0 + v) / 14.0)
        return smoothed

    atr = _wilder(trs)
    pdi = _wilder(pdms)
    ndi = _wilder(ndms)
    dxs: List[float] = []
    for i in range(len(atr)):
        if atr[i] <= 0:
            continue
        p = pdi[i] / atr[i] * 100.0
        n = ndi[i] / atr[i] * 100.0
        s = p + n
        if s > 0:
            dxs.append(abs(p - n) / s * 100.0)
    if len(dxs) < 14:
        return None
    adx = sum(dxs[:14]) / 14.0
    for v in dxs[14:]:
        adx = (adx * 13.0 + v) / 14.0
    return adx


def _window_vwap(candles: List[List[float]]) -> Optional[float]:
    """窗口 VWAP（可用 K 线全窗口累计）：典型价(high+low+close)/3 × volume。"""
    pv = 0.0
    vol = 0.0
    for c in candles:
        v = c[5] if len(c) > 5 else 0.0
        if v <= 0:
            continue
        typ = (c[2] + c[3] + c[4]) / 3.0
        pv += typ * v
        vol += v
    return pv / vol if vol > 0 else None


def _volume_ratio(candles: List[List[float]], lookback: int = 20) -> Optional[float]:
    """量能比 = 最新一根量 / 前 lookback 根平均量。需要 ≥ lookback+1 根。"""
    if len(candles) < lookback + 1:
        return None
    vols = [c[5] for c in candles]
    avg = sum(vols[-lookback - 1:-1]) / float(lookback)
    if avg <= 0:
        return None
    return vols[-1] / avg


def _compute_indicators(candles: List[List[float]]) -> Dict[str, Any]:
    """单周期盘面技术指标（样本不足的项为 None）。"""
    closes = [c[4] for c in candles] if candles else []
    vwap = _window_vwap(candles)
    last = closes[-1] if closes else None
    vwap_pos = None
    if vwap and last is not None and last > 0:
        vwap_pos = "above" if last >= vwap else "below"
    return {
        "rsi": round(_rsi14(closes), 1) if _rsi14(closes) is not None else None,
        "adx": round(_adx14(candles), 1) if _adx14(candles) is not None else None,
        "vwap": round(vwap, 2) if vwap is not None else None,
        "vwap_pos": vwap_pos,  # above=现价在 VWAP 上方 / below=下方
        "volume_ratio": round(_volume_ratio(candles), 2) if _volume_ratio(candles) is not None else None,
    }


def _compute_7d_stats(candles: List[List[float]]) -> Dict[str, Any]:
    """日线 7d 统计（兼容旧字段名，底层走通用 _compute_period_stats）。"""
    s = _compute_period_stats(candles, consistency=5)
    return {
        "price_7d_ago": s["price_ago"],
        "return_7d_pct": s["return_pct"],
        "return_7d_str": s["return_str"],
        "atr_pct": s["atr_pct"],
        "atr_value": s["atr_value"],
        "atr_str": s["atr_str"],
        "trend": s["trend"],
        "trend_strength": s["trend_strength"],
        "n_up_days": s["n_up"],
        "n_down_days": s["n_down"],
        "highest_7d": s["highest"],
        "lowest_7d": s["lowest"],
        "range_7d_pct": s["range_pct"],
    }


def _empty_7d_stats() -> Dict[str, Any]:
    return {
        "price_7d_ago": 0.0,
        "return_7d_pct": 0.0,
        "return_7d_str": "N/A",
        "atr_pct": 0.0,
        "atr_value": 0.0,
        "atr_str": "N/A",
        "trend": "Unknown",
        "trend_strength": "none",
        "n_up_days": 0,
        "n_down_days": 0,
        "highest_7d": 0.0,
        "lowest_7d": 0.0,
        "range_7d_pct": 0.0,
    }


def _format_pct(value: float, decimals: int = 2) -> str:
    return f"{value:+.{decimals}f}%"


def _format_price(value: float, decimals: int = 2) -> str:
    if value >= 1000:
        return f"{value:,.{decimals}f}"
    return f"{value:.{decimals}f}"


# ---------------------------------------------------------------------------
# Main snapshot API
# ---------------------------------------------------------------------------

def _collect_asset(asset_id: str) -> Dict[str, Any]:
    """Collect one asset's ticker/mtf-ohlcv/funding. Returns raw dict (or None ticker)."""
    from concurrent.futures import ThreadPoolExecutor

    symbol = _ASSET_SYMBOL.get(asset_id, "")

    def _fetch_ticker_funding():
        # 按资产绑定真实数据源：
        #   BTC/ETH → 币安 fapi（现货 ticker + premiumIndex 费率/基差 + openInterest 持仓）
        #   XAU    → OKX（永续 ticker/费率/持仓；OKX XAU 无 index 基差 → basis 恒 None）
        #   WTI    → 国内现货（无永续，funding/basis/oi 均 None）
        if asset_id == "BTC":
            prem = _fetch_fapi_premium_sync("BTCUSDT")
            return (
                _fetch_btc_ticker_sync(),
                prem["funding"] if prem else None,
                _basis_bps(prem) if prem else None,
                _fetch_fapi_open_interest_sync("BTCUSDT"),
            )
        if asset_id == "ETH":
            prem = _fetch_fapi_premium_sync("ETHUSDT")
            return (
                _fetch_eth_ticker_sync(),
                prem["funding"] if prem else None,
                _basis_bps(prem) if prem else None,
                _fetch_fapi_open_interest_sync("ETHUSDT"),
            )
        if asset_id == "XAU":
            return (
                _fetch_xau_ticker_sync(),
                quote_sources.fetch_okx_funding("XAU-USDT-SWAP"),
                None,
                quote_sources.fetch_okx_open_interest("XAU-USDT-SWAP"),
            )
        if asset_id == "WTI":
            return _fetch_wti_ticker_sync(), None, None, None  # 无永续合约
        return None, None, None, None

    # ticker+funding/basis/oi 与 多周期 K 线 并行（各自内部也已并行）：
    # 单资产从 6 串行请求(8-12s) → 2 路并行(~2-3s)，快照不再因超时被整体丢弃
    with ThreadPoolExecutor(max_workers=2) as pool:
        tf_fut = pool.submit(_fetch_ticker_funding)
        mtf_ohlcv = _fetch_mtf_ohlcv(asset_id)
        ticker, funding_rate, basis_bps, oi = tf_fut.result(timeout=10.0)

    mtf: Dict[str, Dict[str, Any]] = {}
    for interval, _limit, consistency in _MTF_PERIODS:
        candles = mtf_ohlcv.get(interval)
        if candles:
            mtf[interval] = _compute_period_stats(candles, consistency=consistency)
    # stats_7d 必须是 _compute_7d_stats 的 7d 键（lowest_7d/highest_7d/return_7d_str/
    # n_up_days…）：_build_summary 与前端按这些键读取。直接拿 mtf['1d']（通用键
    # highest/lowest/return_str）会导致区间恒 0、7d 恒 N/A——水位博弈无价格锚点。
    one_d_candles = mtf_ohlcv.get("1d") or []
    stats_7d = _compute_7d_stats(one_d_candles) if len(one_d_candles) >= 2 else _empty_7d_stats()

    # v226 P2-1: 从 external_indicators（yfinance_feed 5min cron 写入）抽
    # BTC/ETH 等替代价格序列的 7d 趋势因子，注入到 summary 供 decision_guard 读取。
    ext_ctx = _collect_external_indicators(asset_id)

    return {
        "ticker": ticker,
        "mtf_ohlcv": mtf_ohlcv,
        "mtf": mtf,
        "stats_7d": stats_7d,
        "funding_rate": funding_rate,
        "basis_bps": basis_bps,
        "oi_change_pct": _oi_change_pct(asset_id, oi),
        "external": ext_ctx,
    }


# 资产 → 候选 yfinance indicator（按顺序匹配，第一个有数据者胜出）
_EXTERNAL_INDICATOR_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "BTC": ("BTC-USD",),
    "ETH": ("ETH-USD",),
    # SKHYNIX/SOL/DOGE/XRP/ADA/BNB 暂无 market_snapshot asset_id 映射
    # （market_snapshot 当前仅跟踪 BTC/ETH/XAU/WTI），
    # 若将来扩展 _ASSET_SYMBOL 可在此追加。
}


def _collect_external_indicators(asset_id: str) -> Dict[str, Any]:
    """v226 P2-1：从 external_indicators 表读取 yfinance 价格 → 计算 7d trend_factor。

    返回 dict：
      · trend_factor_7d_pct : float | None — (latest - 7d_ago) / 7d_ago * 100
      · trend_factor_30d_pct : float | None — (latest - 30d_ago) / 30d_ago * 100
      · latest_value : float | None
      · latest_ts : int | None
      · n_bars : int — 最近 60 天的 K 线数
      · source : str — yfinance symbol（如 "BTC-USD"）
      · has_data : bool — 是否有足够数据计算 7d 因子

    数据缺失或读取失败返回 has_data=False，其它字段 None。
    """
    out: Dict[str, Any] = {
        "trend_factor_7d_pct": None,
        "trend_factor_30d_pct": None,
        "latest_value": None,
        "latest_ts": None,
        "n_bars": 0,
        "source": None,
        "has_data": False,
    }

    candidates = _EXTERNAL_INDICATOR_CANDIDATES.get(asset_id, ())
    if not candidates:
        return out  # 该资产不在覆盖列表（XAU/WTI 等）

    try:
        import db
        conn = db.get_connection()
        try:
            # 逐个候选 indicator 尝试，找第一个有数据的
            rows = []
            chosen_indicator = None
            for indicator in candidates:
                rs = conn.execute(
                    "SELECT value, ts FROM external_indicators "
                    "WHERE asset IN (?, ?) AND indicator=? "
                    "ORDER BY ts DESC LIMIT 60",
                    (asset_id, indicator.split("-")[0], indicator),
                ).fetchall()
                if rs:
                    rows = rs
                    chosen_indicator = indicator
                    if len(rows) >= 8:  # 足够算 7d 因子 → 停止搜索
                        break
            if not rows:
                return out

            n = len(rows)
            latest = float(rows[0][0])
            latest_ts = int(rows[0][1])
            v7 = float(rows[min(7, n - 1)][0]) if n >= 8 else None
            v30 = float(rows[min(30, n - 1)][0]) if n >= 31 else None

            tf7 = ((latest - v7) / v7 * 100.0) if v7 and v7 > 0 else None
            tf30 = ((latest - v30) / v30 * 100.0) if v30 and v30 > 0 else None

            out.update({
                "trend_factor_7d_pct": round(tf7, 2) if tf7 is not None else None,
                "trend_factor_30d_pct": round(tf30, 2) if tf30 is not None else None,
                "latest_value": round(latest, 6),
                "latest_ts": latest_ts,
                "n_bars": n,
                "source": chosen_indicator,
                "has_data": tf7 is not None,
            })
        finally:
            conn.close()
    except Exception:
        # 不抛，保持 summary 渲染不中断
        pass
    return out


def _assemble_asset(asset_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Build the public asset entry from collected raw data."""
    ticker = raw.get("ticker")
    stats_7d = raw.get("stats_7d") or _empty_7d_stats()
    funding_rate = raw.get("funding_rate")
    basis_bps = raw.get("basis_bps")
    oi_change_pct = raw.get("oi_change_pct")
    mtf = raw.get("mtf") or {}

    if ticker is None:
        return {
            "symbol": _ASSET_SYMBOL.get(asset_id, ""),
            "price": 0.0,
            "price_str": "获取失败",
            "change_24h_pct": 0.0,
            "change_24h_str": "N/A",
            "funding_rate_pct": 0.0,
            "funding_rate_str": "N/A",
            "basis_bps": None,
            "oi_change_pct": None,
            "status": "unavailable",
            "status_note": "网络请求失败",
            "stats_7d": stats_7d,
            "mtf": mtf,
        }

    price = _extract_price(ticker) or 0.0
    has_change = bool(ticker.get("has_change", True))
    change_pct = _safe_float(ticker.get("percentage"), 0.0) if has_change else None

    fr_pct = 0.0
    fr_str = "N/A"
    if funding_rate is not None:
        fr_pct = funding_rate * 100.0
        fr_str = f"{fr_pct:+.4f}%"

    return {
        "symbol": _ASSET_SYMBOL.get(asset_id, ""),
        "price": round(price, 4),
        "price_str": _format_price(price, 2),
        "change_24h_pct": round(change_pct, 2) if change_pct is not None else 0.0,
        "change_24h_str": _format_pct(change_pct) if change_pct is not None else "N/A",
        "funding_rate_pct": round(fr_pct, 4),
        "funding_rate_str": fr_str,
        "basis_bps": basis_bps,
        "oi_change_pct": oi_change_pct,
        "status": "ok",
        "stats_7d": stats_7d,
        "mtf": mtf,
    }


async def get_snapshot() -> Dict[str, Any]:
    """
    Async — pull a multi-asset market snapshot (BTC via binance.vision,
    XAU via Sina/EastMoney). Never raises; degrades gracefully.
    """
    if not HAS_REQUESTS:
        return _empty_snapshot("requests 未安装 (pip install requests)")

    t0 = time.time()
    ts = int(t0 * 1000)

    # 并行采集各资产；每资产独立 8s 超时——代理抖动时只丢慢资产，
    # 不让单个资产拖死整批（15s 整体超时 → 全 down 的历史问题）
    tasks = {
        asset_id: asyncio.create_task(
            asyncio.wait_for(asyncio.to_thread(_collect_asset, asset_id), timeout=8.0)
        )
        for asset_id in _MONITOR_ASSETS
    }
    raw_results: Dict[str, Dict[str, Any]] = {}
    for asset_id, task in tasks.items():
        try:
            raw_results[asset_id] = await task
        except Exception:
            raw_results[asset_id] = {"ticker": None, "mtf": {}, "stats_7d": None, "funding_rate": None, "basis_bps": None, "oi_change_pct": None}

    assets: Dict[str, Dict[str, Any]] = {}
    ok_count = 0
    for asset_id in _MONITOR_ASSETS:
        entry = _assemble_asset(asset_id, raw_results[asset_id])
        assets[asset_id] = entry
        if entry["status"] == "ok":
            ok_count += 1

    total = len(_MONITOR_ASSETS)
    if ok_count == total:
        overall_status = "ok"
    elif ok_count == 0:
        overall_status = "down"
    else:
        overall_status = "partial"

    macro: Dict[str, Any] = _macro_defaults()

    summary = _build_summary(assets, macro, overall_status)

    elapsed = round(time.time() - t0, 3)
    print(f"  [SNAPSHOT] {overall_status.upper()} ({ok_count}/{total} OK) "
          f"in {elapsed}s — {summary.split(chr(10))[0]}")

    # ── 持久化 tick：每次 snapshot 拉成功的品种写一行 market_ticks(ts=秒)
    # 让 _price_at_ts 在 news 时刻 ±5min 窗口能查到真实成交价
    # （WTI/XAU 之前 0 条的根本原因：snapshot 拉成功但不写库）。
    try:
        tick_items = []
        for asset_id, entry in assets.items():
            if entry.get("status") != "ok":
                continue
            price = float(entry.get("price") or 0.0)
            if price <= 0:
                continue
            tick_items.append({
                "asset": asset_id,
                "price": price,
                "change24h": float(entry.get("change_24h_pct") or 0.0),
                "source": "snapshot",
                "sourceCount": ok_count,
            })
        if tick_items:
            try:
                import timeseries
                written = timeseries.record_market_snapshot({"items": tick_items})
                if written and _LOUD:
                    print(f"  [SNAPSHOT] 持久化 tick: {written} 行 ({', '.join(t['asset'] for t in tick_items)})")
            except Exception as e:
                # 落库失败不影响主流程
                if _LOUD:
                    print(f"  [SNAPSHOT] tick 持久化失败: {type(e).__name__}: {str(e)[:80]}")
    except Exception:
        pass  # 持久化是 best-effort，绝不抛

    return {
        "timestamp": _fmt_ts(),
        "epoch_ms": ts,
        "assets": assets,
        "macro": macro,
        "summary": summary,
        "status": overall_status,
    }


def get_snapshot_sync() -> Dict[str, Any]:
    """Synchronous wrapper — for debugging, REPL, or non-async scripts."""
    if not HAS_REQUESTS:
        return _empty_snapshot("requests 未安装 (pip install requests)")

    t0 = time.time()
    ts = int(t0 * 1000)

    assets: Dict[str, Dict[str, Any]] = {}
    ok_count = 0
    for asset_id in _MONITOR_ASSETS:
        entry = _assemble_asset(asset_id, _collect_asset(asset_id))
        assets[asset_id] = entry
        if entry["status"] == "ok":
            ok_count += 1

    total = len(_MONITOR_ASSETS)
    overall_status = "ok" if ok_count == total else ("down" if ok_count == 0 else "partial")

    macro: Dict[str, Any] = _macro_defaults()

    summary = _build_summary(assets, macro, overall_status)
    elapsed = round(time.time() - t0, 3)
    print(f"  [SNAPSHOT] {overall_status.upper()} ({ok_count}/{total} OK) in {elapsed}s")

    # ── 持久化 tick（与 get_snapshot 同步写库）
    try:
        tick_items = []
        for asset_id, entry in assets.items():
            if entry.get("status") != "ok":
                continue
            price = float(entry.get("price") or 0.0)
            if price <= 0:
                continue
            tick_items.append({
                "asset": asset_id,
                "price": price,
                "change24h": float(entry.get("change_24h_pct") or 0.0),
                "source": "snapshot-sync",
                "sourceCount": ok_count,
            })
        if tick_items:
            try:
                import timeseries
                timeseries.record_market_snapshot({"items": tick_items})
            except Exception:
                pass
    except Exception:
        pass

    return {
        "timestamp": _fmt_ts(),
        "epoch_ms": ts,
        "assets": assets,
        "macro": macro,
        "summary": summary,
        "status": overall_status,
    }


# ---------------------------------------------------------------------------
# 宏观区（DXY/US10Y/VIX）：读 macro_snapshots 表里最近一次 FRED 落库值。
# 不自己拉 FRED（避免重复请求）：macro_context.build_context 已把 fred 层
# （DGS10/T10Y2Y/DFII10/DTWEXBGS/VIXCLS）落库到 macro_snapshots，这里读缓存。
# FRED 为日度指标，上批缓存（几秒前）足够新。表空/无 Key → 维持 unavailable。
# ---------------------------------------------------------------------------

def _fred_macro_latest() -> Dict[str, Dict[str, Any]]:
    """从 macro_snapshots 读最近一次 fred 层 ok 值，返回 {metric_key: {"value", "unit"}}。"""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        import db
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT metric_key, value, unit FROM macro_snapshots"
                " WHERE category='fred' AND status='ok'"
                " AND ts = (SELECT MAX(ts) FROM macro_snapshots WHERE category='fred' AND status='ok')"
            ).fetchall()
            for r in rows:
                out[str(r[0])] = {"value": r[1], "unit": str(r[2] or "")}
        finally:
            conn.close()
    except Exception:
        pass
    return out


# macro 区字段 → (FRED metric_key, 标签)。fred 无 24h 变化 → change 标 N/A（日度指标）。
_MACRO_FRED_MAP = {
    "us10y": ("dgs10", "US10Y"),
    "tips10y": ("tips10y", "TIPS10Y"),
    "dxy": ("dxy", "DXY"),
    "vix": ("vix", "VIX"),
}


def _macro_defaults() -> Dict[str, Dict[str, Any]]:
    """宏观指标块：us10y/tips10y/dxy/vix 优先用 FRED 落库真值；oil 不可用（无免费源）。"""
    macro: Dict[str, Dict[str, Any]] = {
        "dxy":   {"value": None, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "FRED 未提供或未配置"},
        "us10y": {"value": None, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "FRED 未提供或未配置"},
        "tips10y": {"value": None, "change_24h_str": "N/A", "status": "unavailable",
                     "note": "FRED 未提供或未配置"},
        "oil":   {"value": None, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "无免费原油资金数据源"},
        "vix":   {"value": None, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "FRED 未提供或未配置"},
    }
    fred = _fred_macro_latest()
    for field, (metric_key, _label) in _MACRO_FRED_MAP.items():
        hit = fred.get(metric_key)
        if hit is not None and hit.get("value") is not None:
            macro[field] = {
                "value": hit["value"],
                "unit": hit.get("unit") or "",
                "change_24h_str": "N/A",
                "status": "ok",
                "source": "FRED",
            }
    return macro




def _fmt_ts() -> str:
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S CST")


def _build_summary(assets: Dict[str, Dict], macro: Dict[str, Any], overall_status: str) -> str:
    if overall_status == "down":
        return "⚠️ 市场数据不可用 — 所有标的拉取失败。LLM 请仅基于新闻文本做方向性判断。"

    lines = ["── 市场快照 ──"]

    for asset_id in ["BTC", "XAU", "WTI", "ETH"]:
        a = assets.get(asset_id)
        if a is None:
            continue
        if a.get("status") == "unavailable":
            lines.append(f"{asset_id}: 数据不可用")
            continue

        stats = a.get("stats_7d", {})
        line = (
            f"{asset_id} ${a['price']:,.2f} | "
            f"24h {a['change_24h_str']} | "
            f"7d {stats.get('return_7d_str', 'N/A')} | "
            f"资金费率 {a['funding_rate_str']}"
        )
        lines.append(line)

        trend = stats.get("trend", "Unknown")
        atr = stats.get("atr_str", "N/A")
        strength = stats.get("trend_strength", "none")
        lines.append(
            f"  日线: {trend} (强度={strength}, "
            f"连涨{stats.get('n_up_days', 0)}/连跌{stats.get('n_down_days', 0)}根) | "
            f"ATR={atr} | "
            f"区间: {stats.get('lowest_7d', 0):,.2f}–{stats.get('highest_7d', 0):,.2f}"
        )

        # 多周期结构（1d 已在上方，这里补 4h/1h/15m；缺失周期标 N/A）
        mtf = a.get("mtf") or {}
        mtf_parts = []
        for interval, label in (("4h", "4H"), ("1h", "1H"), ("15m", "15m")):
            s = mtf.get(interval)
            if s:
                mtf_parts.append(
                    f"{label}: {s.get('trend', '?')} {s.get('return_str', '')} ATR{s.get('atr_str', '')}"
                )
            else:
                mtf_parts.append(f"{label}: N/A")
        if mtf_parts:
            lines.append("  " + " | ".join(mtf_parts))

        # 盘面技术指标（本地计算，样本不足标 N/A，不造数）：
        # RSI14/ADX14/VWAP 位置/量能比 —— 归 [水位博弈] 段引用（超买超卖/趋势强度/价格 vs VWAP）
        ind_parts = []
        for interval, label in (("4h", "4H"), ("1h", "1H"), ("15m", "15m")):
            s = (mtf.get(interval) or {}).get("indicators") or {}
            if not s.get("rsi") and not s.get("adx") and not s.get("vwap"):
                continue
            bit = f"{label}:"
            bit += f"RSI{s.get('rsi', 'N/A')}" if s.get("rsi") is not None else ""
            bit += f" ADX{s.get('adx', 'N/A')}" if s.get("adx") is not None else ""
            bit += f" {('VWAP' + ('上' if s.get('vwap_pos') == 'above' else '下')) if s.get('vwap_pos') else ''}"
            bit += f" 量{s.get('volume_ratio', '')}x" if s.get("volume_ratio") is not None else ""
            ind_parts.append(bit)
        if ind_parts:
            lines.append("  技术: " + " | ".join(ind_parts))

    lines.append("── 宏观指标（来源标注）──")
    macro_line_parts = []
    # oil 无免费源恒 unavailable；其余项 FRED 落库 ok → 显示真值，否则 N/A
    for key, label in [("us10y", "US10Y"), ("tips10y", "TIPS10Y"), ("dxy", "DXY"), ("vix", "VIX"), ("oil", "Oil")]:
        m = macro.get(key, {})
        if m.get("status") == "unavailable" or m.get("value") is None:
            macro_line_parts.append(f"{label}: N/A")
            continue
        value = float(m["value"])
        unit = str(m.get("unit") or "")
        if unit == "%":
            text = f"{value:.2f}%"
        elif unit == "index" or value >= 1000:
            text = f"{value:,.2f}"
        else:
            text = f"{value:.2f}"
        src = str(m.get("source") or "")
        macro_line_parts.append(f"{label}: {text}（{src}）" if src else f"{label}: {text}")
    lines.append(" | ".join(macro_line_parts))

    lines.append(f"数据状态: {overall_status}")
    return "\n".join(lines)


def _empty_snapshot(reason: str = "requests 未安装") -> Dict[str, Any]:
    assets = {}
    for asset_id in _MONITOR_ASSETS:
        assets[asset_id] = {
            "symbol": _ASSET_SYMBOL.get(asset_id, ""),
            "price": 0.0, "price_str": "N/A",
            "change_24h_pct": 0.0, "change_24h_str": "N/A",
            "funding_rate_pct": 0.0, "funding_rate_str": "N/A",
            "basis_bps": None,
            "oi_change_pct": None,
            "status": "unavailable", "status_note": reason,
            "stats_7d": _empty_7d_stats(),
            "mtf": {},
        }
    macro = _macro_defaults()
    for key in macro:
        if macro[key].get("status") != "ok":
            macro[key]["note"] = reason
    return {
        "timestamp": _fmt_ts(),
        "epoch_ms": int(time.time() * 1000),
        "assets": assets,
        "macro": macro,
        "summary": f"⚠️ 市场数据不可用: {reason}",
        "status": "down",
    }


async def close() -> None:
    """释放 requests session (engine shutdown 时调用)"""
    global _SESSION
    if _SESSION is not None:
        try:
            _SESSION.close()
        except Exception:
            pass
        _SESSION = None


# ---------------------------------------------------------------------------
# 独立运行：python3 market_snapshot.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("═" * 60)
    print("[SNAPSHOT] Trident Market Snapshot — 独立测试")
    print("═" * 60)

    if not HAS_REQUESTS:
        print("❌ requests 未安装. 请运行: pip install requests")
        sys.exit(1)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            snap = get_snapshot_sync()
        else:
            snap = asyncio.run(get_snapshot())
    except RuntimeError:
        snap = get_snapshot_sync()

    print()
    print("── 市场摘要 (可直接注入 LLM Prompt) ──")
    print(snap["summary"])
    print()
    print("── 完整 JSON ──")
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    print()
    print(f"整体状态: {snap['status']}")
