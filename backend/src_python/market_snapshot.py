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

from feeds import quote_sources

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
# v342-fix: 纳入本地库有日线的 SOL/NVDA/AMZN/META/SKHYNIX——外网拉不到时
# 仍可走本地 ohlcv_daily 兜底出 7d 结构/趋势，避免这些品种在闸门侧整体无行情上下文。
_MONITOR_ASSETS = ("BTC", "XAU", "WTI", "ETH", "SOL", "NVDA", "AMZN", "META", "SKHYNIX")

_FAPI = "https://fapi.binance.com"             # 币安永续合约（直连被墙，走代理）

# 资产 → 展示 symbol（真实数据源合约）。XAU=OKX 永续、WTI=国内现货(无永续)，
# 实际数据源路由见 quote_sources.ASSET_ROUTE（单一事实来源）。
_ASSET_SYMBOL = {
    "BTC": "BTCUSDT",       # 币安永续
    "ETH": "ETHUSDT",       # 币安永续
    "SOL": "SOLUSDT",       # 币安永续
    "XAU": "XAU-USDT-SWAP", # OKX 永续
    "WTI": "WTIUSD",        # 国内现货（无永续）
    "NVDA": "NVDA",         # Yahoo / 本地日线兜底
    "AMZN": "AMZN",
    "META": "META",
    "SKHYNIX": "000660.KS",
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


def _fetch_local_kline_bars(asset_id: str, interval: str, limit: int) -> Optional[List[List[float]]]:
    """v324：交易所拉不到时，退到本地 kline_bars（腾讯快照聚合出来的）。

    只给**确实在库里有点**的标的用；库里没有就返回 None，绝不补假 K 线。
    返回格式与交易所一致：[[ts_ms, open, high, low, close, volume], ...] 升序。
    """
    local_key = _local_asset_key(asset_id)
    try:
        import db
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM kline_bars"
                " WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
                (local_key, interval, limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    if not rows:
        return None
    return [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in reversed(rows)]


# v406 P4: 本地 K 线兜底的"新鲜度上限"——
# 不用远端数据就"标上一根几个月前的价格"同样是在造结论。按周期给可接受年龄。
_LOCAL_FALLBACK_MAX_AGE_S = {
    "1m": 2 * 3600, "3m": 4 * 3600, "5m": 6 * 3600, "15m": 12 * 3600,
    "30m": 24 * 3600, "1h": 48 * 3600, "2h": 72 * 3600,
    "4h": 7 * 86400, "1d": 30 * 86400,
}


def _fetch_local_recent(asset_id: str, interval: str, limit: int,
                        why: str = "") -> Optional[List[List[float]]]:
    """外网拉不到时的**本地兜底**（kline_bars，其次 ohlcv_intraday）。

    背景（2026-11-16 实测）：XAU 87 条、WTI 97 条决策被判 `missing:feed_stale`，
    而库里 `kline_bars` 的 XAU 1m 有 41,647 根、最新一根只在 1.1 小时前 ——
    **本地明明有新鲜数据**，只是 okx_swap 分支拉不到 OKX 就直接 `unavailable`，
    从来没往本地退一步。于是「有没有数据」被记成「没有数据」，决策被降级成 HOLD。

    兜底三条纪律：① 只读真实存在的 bar，不补造；② 超过周期允许年龄就不用（
    宁可标 N/A）；③ 打日志写清来源，事后能分清"活数据"和"盘上数据"。
    """
    limit = max(2, int(limit or 96))
    max_age = _LOCAL_FALLBACK_MAX_AGE_S.get(interval, 7 * 86400)
    now_s = time.time()
    try:
        import db as _db
        conn = _db.get_connection()
    except Exception:
        return None
    try:
        local_key = _local_asset_key(asset_id)
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM kline_bars"
            " WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
            (local_key, interval, limit),
        ).fetchall()
        src = "kline_bars"
        if not rows:
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM ohlcv_intraday"
                " WHERE asset=? AND interval=? ORDER BY ts DESC LIMIT ?",
                (local_key, interval, limit),
            ).fetchall()
            src = "ohlcv_intraday"
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not rows:
        return None
    def _ts_s(v):
        v = float(v or 0)
        return v / 1000.0 if v > 1e11 else v      # kline_bars 是毫秒，intraday 是秒
    newest = _ts_s(rows[0][0])
    age = now_s - newest
    if age < -300 or age > max_age:
        print("[KLINE-LOCAL] %s %s 本地数据不可用于兜底（age=%.1fh > %.1fh）"
              % (asset_id, interval, age / 3600.0, max_age / 3600.0), flush=True)
        return None
    out = [[_ts_s(r[0]) * 1000.0, r[1], r[2], r[3], r[4], r[5]] for r in reversed(rows)]
    _mark_local_fallback(asset_id, interval)
    print("[KLINE-LOCAL] %s %s 外网失败(%s) → 本地 %s %d 根（最新 %.1fh 前）"
          % (asset_id, interval, why or "n/a", src, len(out), age / 3600.0), flush=True)
    return out


# (asset, interval) → 最近一次"本地兜底"的时间戳。_fetch_asset_klines 内部可能
# 悄悄退回本地 kline_bars，返回值看不出区别；不标出来，上层就会把隔夜本地线
# 当真实交易所 K 线用（WTI 的 mtf 曾因此 source=None）。
_LOCAL_FALLBACK_MARKS: Dict[Tuple[str, str], float] = {}


def _mark_local_fallback(asset_id: str, interval: str) -> None:
    try:
        _LOCAL_FALLBACK_MARKS[(str(asset_id), str(interval))] = time.monotonic()
    except Exception:  # pragma: no cover
        pass


def _fetch_asset_klines(asset_id: str, interval: str, limit: int) -> Optional[List[List[float]]]:
    """按资产拉 K 线：BTC/ETH 走币安 fapi，XAU 走 OKX；WTI/美股(无永续) 返回 None（标 N/A，不造数）。

    v2026.10.05：okx_swap 分支改用 fetch_asset_candles_with_yahoo_fallback —— OKX SNI 阻断时
    自动降级到 Yahoo Finance 期货合约（GC=F/SI=F/CL=F），避免 XAU/XAG/WTI 完全断流。
    """
    r = quote_sources.route(asset_id)
    if not r:
        # 该标的没有交易所路由（美股/A股/港股）→ 直接用本地 kline_bars
        return _fetch_local_kline_bars(asset_id, interval, limit)
    if r["market"] == "okx_swap":
        # OKX 优先 → Yahoo 兜底（v2026.10.05）→ v406 本地 kline_bars 兜底
        rows = quote_sources.fetch_asset_candles_with_yahoo_fallback(
            asset_id, quote_sources.okx_bar(interval), limit
        )
        if rows:
            return rows
        return _fetch_local_recent(asset_id, interval, limit, why="okx/yahoo 均失败")
    if r["market"] == "binance_fapi":
        # fapi 被住宅代理(美国 IP)地区封禁 → 现货镜像直连优先（已实测国内稳定）
        rows = quote_sources.fetch_binance_spot_candles(r["symbol"], interval, limit)
        if rows:
            return rows
        rows = _fetch_fapi_klines_sync(r["symbol"], interval, limit)
        if rows:
            return rows
        return _fetch_local_recent(asset_id, interval, limit, why="binance 均失败")
    if r["market"] == "yahoo":
        # 美股/韩股：Yahoo 优先，失败退回本地 kline_bars / ohlcv 兜底（不造数）
        rows = quote_sources.fetch_asset_candles_with_yahoo_fallback(
            asset_id, quote_sources.okx_bar(interval), limit
        )
        if rows:
            return rows
        return _fetch_local_recent(asset_id, interval, limit, why="yahoo 失败")
    return _fetch_local_recent(asset_id, interval, limit, why="无交易所路由")


def _fetch_asset_klines_unused(asset_id: str, interval: str, limit: int):
    return None


def _fetch_mtf_ohlcv(asset_id: str, total_timeout: float = 8.0) -> Dict[str, Any]:
    """拉资产多周期 K 线：{interval: candles}，失败的周期不进 dict（标 N/A）。

    4 个周期并行拉取（原串行 4×~2s=8s，并行后 ~2s）——否则叠加 ticker/funding
    后单资产 6 请求串行 8-12s，超过快照超时会被整体丢弃（快照 down 的主因之一）。

    v322 修复：**总预算**而不是「每周期各 8 秒」。
    原来 4 个周期**串行** `fut.result(timeout=8.0)` —— 虽然提交是并行的，
    但每个 `result(timeout=8)` 的计时**从被调用那一刻起算** → 最坏 4×8=32 秒；
    再叠加 `_fetch_ticker_funding` 的 10 秒 → 单资产最坏 42 秒，
    4 个资产再串行 await → **实测 97.6 秒**（日志：SNAPSHOT PARTIAL in 97.609s）。
    外层 `asyncio.wait_for(timeout=8)` 只是不再等，**线程仍在后台跑**
    （Python 无法强杀线程）→ 线程池被占满 → 后续请求排队 → 越来越慢。
    现在：整组 K 线共享一个总预算（默认 8 秒），超时即放弃剩余周期。
    """
    from concurrent.futures import ThreadPoolExecutor

    out: Dict[str, List[List[float]]] = {}
    # 必须在提交之前取：无交易所路由的标的（WTI）会**立刻**走本地兜底，
    # 时间戳比提交后取的还早，漏标 → mtf 的 1d 又变回 source=None。
    _started = time.monotonic()
    deadline = _started + max(1.0, float(total_timeout))
    # ⚠️ 不用 with 块：with 退出时默认 wait=True → 会等慢线程跑完
    # → 又变回"主线程阻塞"。慢周期是"尽力而为"的补充数据，超时就算了。
    _pool = ThreadPoolExecutor(max_workers=4)
    try:
        futures = {
            interval: _pool.submit(_fetch_asset_klines, asset_id, interval, limit)
            for interval, limit, _consistency in _MTF_PERIODS
        }
        for interval, fut in futures.items():
            _left = deadline - time.monotonic()
            if _left <= 0:
                break                      # 预算用尽 → 剩余周期直接放弃
            try:
                candles = fut.result(timeout=_left)
            except Exception:
                candles = None
            if candles:
                out[interval] = candles
                if _LOCAL_FALLBACK_MARKS.get((str(asset_id), str(interval)), -1) >= _started:
                    out.setdefault("_local_intervals", []).append(interval)
    finally:
        try:
            _pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            _pool.shutdown(wait=False)
        except Exception:
            pass
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

def _ticker_from_ticks(asset_id: str) -> Optional[Dict[str, Any]]:
    """v323 兜底：外网 ticker 拉不到时，用本系统 market_ticks 表的最新价。

    为什么必须有这条：OKX/Yahoo 在本机连不通（实测 721 次 SNAPSHOT DOWN），
    快照一挂，prompt 里就会写"市场数据不可用，请仅基于新闻文本判断"，
    模型于是统一回 HOLD/0 分 —— 分析链路全通也产不出方向。
    而 market_ticks 每个品种都有 8–27 万行、秒级新鲜（BTC/ETH/SOL/WTI/XAU），
    完全够当"当前价 + 24h 涨跌"的锚。
    """
    try:
        import sqlite3

        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "trident_event_bus.db",
        )
        # 只读 + 1.2s：引擎自己每批都在写，锁争用下 3s 会变成"每个资产都等满"，
        # 9 个资产就能把快照拖到 20s+（实测 22.4s）。读不到就返回 None，不硬等。
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
        local_key = _local_asset_key(asset_id)
        try:
            row = conn.execute(
                "SELECT price, ts FROM market_ticks WHERE symbol = ? "
                "ORDER BY ts DESC LIMIT 1",
                (asset_id,),
            ).fetchone()
            ticker_source_table = "market_ticks"
            if not row or not row[0]:
                # v342: 二级兜底 —— market_ticks 本身也可能因网络整夜断档。
                # 这时退到 ohlcv_intraday / ohlcv_daily 的最后一根收盘价，
                # 让决策快照至少有"当前价 + 24h 涨跌"，而不是直接写"数据不可用"
                # （实测 07:26 的决策快照里 BTC/WTI 全是 unavailable，模型只能回 HOLD）。
                ticker_source_table = "ohlcv"
                for _tbl in ("ohlcv_intraday", "ohlcv_daily"):
                    row = conn.execute(
                        f"SELECT close, ts FROM {_tbl} WHERE asset = ? "
                        "ORDER BY ts DESC LIMIT 1", (local_key,)).fetchone()
                    if row and row[0]:
                        break
                if not row or not row[0]:
                    return None
            last = float(row[0])
            # 24h 前最近一条：算涨跌幅，让模型看到方向而非只有一个孤立价格
            prev = None
            if ticker_source_table == "market_ticks":
                prev = conn.execute(
                    "SELECT price FROM market_ticks WHERE symbol = ? "
                    "AND ts <= ? ORDER BY ts DESC LIMIT 1",
                    (asset_id, int(row[1]) - 86400),
                ).fetchone()
            # v382: 本地 ticks 无 24h 前价时（美股/韩股常只有日线），
            # 用 ohlcv_daily 找前一根收盘价补涨跌，避免 change=+0.00% 占位。
            if prev is None:
                prev = conn.execute(
                    "SELECT close, ts FROM ohlcv_daily WHERE asset = ? "
                    "AND ts < ? ORDER BY ts DESC LIMIT 1",
                    (local_key, int(row[1])),
                ).fetchone()
            change_pct = None
            if prev and prev[0] and float(prev[0]):
                change_pct = round((last - float(prev[0])) / float(prev[0]) * 100, 2)
                # v382: 日线相邻两根可能同价（停牌/重复落库），0% 是真实零差；
                # 若上一根等于最新价但更早一根有差异，取更早一根，避免把持平误当方向缺失。
                if change_pct == 0.0:
                    prev2 = conn.execute(
                        "SELECT close FROM ohlcv_daily WHERE asset = ? "
                        "AND ts < ? ORDER BY ts DESC LIMIT 1",
                        (local_key, int(prev[1])),
                    ).fetchone()
                    if prev2 and prev2[0] and abs(float(prev2[0]) - last) > 1e-9:
                        change_pct = round((last - float(prev2[0])) / float(prev2[0]) * 100, 2)
        finally:
            conn.close()
        ticker: Dict[str, Any] = {"last": last, "close": last, "source": ticker_source_table}
        if change_pct is not None:
            ticker["change_pct_24h"] = change_pct
        return ticker
    except Exception:
        return None


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
        # v342: volume 可能为 None（本地 tick 聚合/部分远端源会留空），
        #       None <= 0 会抛 TypeError 把整张快照打挂，这里统一按"无成交量"处理
        v = c[5] if (len(c) > 5 and c[5] is not None) else 0.0
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
    vols = [(c[5] or 0.0) for c in candles]
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
        # SOL → 币安 fapi；美股/韩股(NVDA/AMZN/META/SKHYNIX)无永续，
        # ticker 交由本地 ticks/日线兜底，funding/basis/oi 均 None。
        if asset_id == "SOL":
            prem = _fetch_fapi_premium_sync("SOLUSDT")
            return (
                quote_sources.fetch_binance_spot_ticker("SOLUSDT"),
                prem["funding"] if prem else None,
                _basis_bps(prem) if prem else None,
                _fetch_fapi_open_interest_sync("SOLUSDT"),
            )
        return None, None, None, None

    # ticker+funding/basis/oi 与 多周期 K 线 并行（各自内部也已并行）：
    # 单资产从 6 串行请求(8-12s) → 2 路并行(~2-3s)，快照不再因超时被整体丢弃
    #
    # v322 修复：**两路都必须在总预算内结束**。
    # 原来 `_fetch_mtf_ohlcv` 是**主线程同步调用**（阻塞），而 `tf_fut.result(timeout=10)`
    # 超时后**线程不会被杀掉**（Python 无法强杀线程）→ 慢请求持续占用线程池
    # → 快照越跑越慢（实测 97.6s → 57.8s），且线程无限膨胀。
    # 现在：两路都放进同一个 pool，各自带 timeout；主线程只等一个**总预算**。
    #
    # ⚠️ pool 必须**不等待**退出（shutdown(wait=False)）：with 块退出时
    # 默认 wait=True → 会等慢线程跑完 → 又变回"同步阻塞"。
    _pool = ThreadPoolExecutor(max_workers=2)
    try:
        tf_fut = _pool.submit(_fetch_ticker_funding)
        mtf_fut = _pool.submit(_fetch_mtf_ohlcv, asset_id)
        try:
            ticker, funding_rate, basis_bps, oi = tf_fut.result(timeout=8.0)
        except Exception:
            ticker, funding_rate, basis_bps, oi = (None, None, None, None)
        try:
            mtf_ohlcv = mtf_fut.result(timeout=2.0)
        except Exception:
            mtf_ohlcv = {}
    finally:
        # 不等待剩余线程 —— 它们是"尽力而为"的补充数据，超时就算了
        try:
            _pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            _pool.shutdown(wait=False)
        except Exception:
            pass

    # v323：外网 ticker 失败 → 用本地 market_ticks 兜底，别让整张快照 DOWN
    if (ticker is None) or (_extract_price(ticker) is None):
        _fallback = _ticker_from_ticks(asset_id)
        if _fallback is not None:
            ticker = _fallback

    mtf: Dict[str, Dict[str, Any]] = {}
    _local_ivs = set(mtf_ohlcv.get("_local_intervals") or ())
    for interval, _limit, consistency in _MTF_PERIODS:
        candles = mtf_ohlcv.get(interval)
        if candles:
            _st = _compute_period_stats(candles, consistency=consistency)
            # 来源必须显式：本地兜底 = local_kline，真实外网 K 线 = remote_kline。
            # 之前远端周期一律不带 source（读出来是 None），和"没来源"分不开。
            _st = dict(_st, source="local_kline" if interval in _local_ivs else "remote_kline")
            mtf[interval] = _st
    # v342: 外网多周期全失败 → 用本地库算，别让模型在"没有方向先验"的状态下拍 HOLD。
    # v342-fix: 本地兜底不再受 `if not mtf` 门控——外网哪怕只回来一根残缺周期，
    # 也不能因此跳过本地日线/小时线；否则 stats_7d 区间/ATR/趋势恒 0 占位，
    # structure/trend 因子在闸门侧全部不可得（实测 474 条决策结构可用率 0%）。
    local_mtf = _local_mtf(asset_id)
    if local_mtf:
        for k in ("1d", "4h", "1h"):
            # 外网已有该周期时优先外网；缺的周期才用本地补，不覆盖远端读数
            if k not in mtf and k in local_mtf:
                mtf[k] = local_mtf[k]
    # stats_7d 必须是 _compute_7d_stats 的 7d 键（lowest_7d/highest_7d/return_7d_str/
    # n_up_days…）：_build_summary 与前端按这些键读取。直接拿 mtf['1d']（通用键
    # highest/lowest/return_str）会导致区间恒 0、7d 恒 N/A——水位博弈无价格锚点。
    # 日线优先级：外网 1d K 线 > 本地 ohlcv_daily 兜底；两者都缺才给空占位。
    one_d_candles = mtf_ohlcv.get("1d") or (local_mtf.get("_daily_candles") or [])
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
        # v324: 资金/杠杆/情绪面。此前 external_indicators 里的 bybit 与
        # tencent 数据只写不读 —— 采了 500+ 条，决策侧一条都看不到，
        # 等于标准 14（资金进出、成交量、杠杆）没有入口。这里统一收口。
        "flow": _collect_market_flow(asset_id),
    }


# 资产 → 候选 yfinance indicator（按顺序匹配，第一个有数据者胜出）
_EXTERNAL_INDICATOR_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "BTC": ("BTC-USD",),
    "ETH": ("ETH-USD",),
    # SKHYNIX/SOL/DOGE/XRP/ADA/BNB 暂无 market_snapshot asset_id 映射
    # （market_snapshot 当前仅跟踪 BTC/ETH/XAU/WTI），
    # 若将来扩展 _ASSET_SYMBOL 可在此追加。
}


def _collect_market_flow(asset_id: str) -> Dict[str, Any]:
    """v324：从 external_indicators 读资金/杠杆/情绪/量能，供决策侧看到。

    只读三分钟内的最新值，超过窗口就明确标 stale —— 不许把过期数字当现价用。
      · 杠杆与情绪（Bybit，加密）：资金费率 / 持仓量 / 多空比
      · 量能（腾讯，A股/美股/港股）：成交量、成交额、当日涨跌
    返回 {"stale": bool, "items": {...}}，取不到就给空 dict，不抛。
    """
    import time
    out: Dict[str, Any] = {"items": {}, "stale": False, "window_min": 30}
    now = int(time.time())
    specs = (
        ("bybit_funding_rate", "资金费率", 8),
        ("bybit_open_interest", "持仓量", 0),
        ("bybit_long_short_ratio", "多空比", 2),
        ("tencent_volume", "成交量", 0),
        ("tencent_change_pct", "当日涨跌%", 2),
        ("tencent_amount", "成交额", 0),
    )
    try:
        import db
        conn = db.get_connection()
        try:
            for indicator, label, nd in specs:
                row = conn.execute(
                    "SELECT value, ts FROM external_indicators"
                    " WHERE asset=? AND indicator=? ORDER BY ts DESC LIMIT 1",
                    (asset_id, indicator),
                ).fetchone()
                if not row:
                    continue
                age_min = (now - int(row[1])) / 60.0
                if age_min > 30:
                    out["stale"] = True
                    continue
                val = row[0] if nd == 0 else round(float(row[0]), nd)
                out["items"][label] = val
        finally:
            conn.close()
    except Exception:
        pass
    return out


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


# 本地日线表的品种别名：决策侧代码 → ohlcv_daily / kline_bars 里实际存的 asset 名
_LOCAL_ASSET_ALIAS = {
    "SKHYNIX": "000660.KS",
}


def _local_asset_key(asset_id: str) -> str:
    return _LOCAL_ASSET_ALIAS.get(asset_id.upper(), asset_id.upper())


def _local_mtf(asset_id: str) -> Dict[str, Any]:
    """v342: 外网多周期 K 线拿不到时，用本地库算 1d/1h/4h 趋势。

    为什么必须有：外网 ticker 一失败，快照里 4H/1H/15m 全是 N/A、日线 Unknown，
    模型只能看到"一个孤立价格 + 24h 涨跌"，等于没有方向先验，于是统一回 HOLD。
    而本地 ohlcv_daily（日线，08:00 更新）和 ohlcv_intraday 1h（tick 每 10 分钟
    聚合）都是活的，足够算趋势/ATR/连涨连跌。
    """
    try:
        import sqlite3

        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "trident_event_bus.db",
        )
        # 只读 + 1.2s：引擎自己每批都在写，锁争用下 3s 会变成"每个资产都等满"，
        # 9 个资产就能把快照拖到 20s+（实测 22.4s）。读不到就返回 None，不硬等。
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
        try:
            out: Dict[str, Any] = {}
            local_key = _local_asset_key(asset_id)
            daily = conn.execute(
                "SELECT ts,open,high,low,close,volume FROM ohlcv_daily "
                "WHERE asset = ? ORDER BY ts DESC LIMIT 30", (local_key,)
            ).fetchall()
            daily_c = [[r[0], r[1], r[2], r[3], r[4], r[5] or 0.0] for r in reversed(daily)]
            if len(daily_c) >= 2:
                # 与远端口径一致：1d 只取最近 8 根（closes[0] 是 8 根前），
                # 不是把 30 天涨幅当成"日线趋势"（那样会把 -21% 说成日线跌 21%）
                out["1d"] = dict(_compute_period_stats(daily_c[-8:], consistency=5),
                                source="local_kline")

            hourly = conn.execute(
                "SELECT ts,open,high,low,close,volume FROM ohlcv_intraday "
                "WHERE asset = ? AND interval = '1h' ORDER BY ts DESC LIMIT 96",
                (local_key,),
            ).fetchall()
            hourly_c = [[r[0], r[1], r[2], r[3], r[4], r[5] or 0.0] for r in reversed(hourly)]
            # v342-fix: 只统计**连续**的一段。本地小时 bar 是逐步攒的，
            # 中间常有整段空洞（例如 07:00 有、08:00–11:00 缺）；跨越空洞算出的
            # "4H -11.9%" 是假的（实测 BTC 近 4 小时真实振幅只有 1.1%），
            # 宁可返回"数据不足"也不能给模型一个假方向。
            if hourly_c:
                cut = 0
                for i in range(len(hourly_c) - 1, 0, -1):
                    if int(hourly_c[i][0]) - int(hourly_c[i - 1][0]) > 2 * 3600:
                        cut = i
                        break
                hourly_c = hourly_c[cut:]
            if len(hourly_c) >= 24:
                out["1h"] = dict(_compute_period_stats(hourly_c[-48:], consistency=8),
                                source="local_kline")
                buckets: Dict[int, list] = {}
                for r in hourly_c:
                    b = (int(r[0]) // 14400) * 14400
                    cur = buckets.get(b)
                    if cur is None:
                        buckets[b] = [b, r[1], r[2], r[3], r[4], r[5] or 0.0]
                    else:
                        cur[2] = max(cur[2], r[2])
                        cur[3] = min(cur[3], r[3])
                        cur[4] = r[4]
                        cur[5] = (cur[5] or 0.0) + (r[5] or 0.0)
                if len(buckets) >= 8:
                    # 与远端 _MTF_PERIODS 的 4h=42 根对齐（42×4h≈7天），
                    # 否则本地把 7 天的 168 根全算进去，会把"4H 跌 12%"说成几天的事
                    out["4h"] = dict(_compute_period_stats(
                        [buckets[k] for k in sorted(buckets)][-42:], consistency=6),
                        source="local_kline")
            out["_daily_candles"] = daily_c
            return out
        finally:
            conn.close()
    except Exception:
        return {}


def _technical_state(asset_id: str) -> Dict[str, Any]:
    """六维技术状态读数（趋势/波动/价值区/杠杆/主动性/持仓分歧）。

    只带精简字段：快照每批都要序列化进 decision_context，塞整份 state 会白白撑大
    JSON；decision_guard 只需要 bias/coverage/usable 三个判据 + 一句 readout。
    读数算不出来就返回 available=False——不补造中性值。
    """
    try:
        import technical_state as _tsst
        state = _tsst.for_asset(asset_id, timeframe="4h")
    except Exception as _e:
        return {"available": False, "error": f"{type(_e).__name__}: {str(_e)[:80]}"}
    if not isinstance(state, dict) or "error" in state:
        return {"available": False, "error": (state or {}).get("error", "empty")}
    bd = state.get("bias_detail") or {}
    rk = state.get("risk") or {}
    return {
        "available": True,
        "timeframe": (state.get("_meta") or {}).get("timeframe", "4h"),
        "readout": state.get("readout") or "",
        "bias": bd.get("bias"),
        "coverage": bd.get("coverage"),
        "usable": bool(bd.get("usable")),
        "missing": list(state.get("missing") or []),
        "trend_stage": (state.get("trend") or {}).get("stage"),
        "vol_state": (state.get("volatility") or {}).get("state"),
        "lev_label": (state.get("leverage") or {}).get("label"),
        # v412：风险档位（仓位系数 + 止损 ATR 倍数）。只给执行建议，不参与方向分。
        "risk": ({
            "level": rk.get("level"),
            "size_factor": rk.get("size_factor"),
            "stop_atr_multiple": rk.get("stop_atr_multiple"),
            "atr_pct": rk.get("atr_pct"),
            "expected_adverse_pct": rk.get("expected_adverse_pct"),
            "drivers": list(rk.get("drivers") or []),
            "note": rk.get("note"),
        } if rk.get("available") else None),
    }


# v412: 远端全失后的"本地直通车"窗口。实测本机 OKX DNS 不通 / Binance TLS 被重置 /
# Yahoo 429，每资产远端调用都要耗满 8s 超时；而库里 market_ticks 与 kline 是活的。
# 一旦确认"远端全失"，接下来 120s 内直接本地重建，不再逐资产去等超时。
_REMOTE_BREAKER_S = float(os.getenv("SNAPSHOT_REMOTE_BREAKER_S", "120") or 120)
_REMOTE_BREAKER_UNTIL = 0.0


def _remote_breaker_active() -> bool:
    return time.time() < _REMOTE_BREAKER_UNTIL


def _trip_remote_breaker(reason: str) -> None:
    global _REMOTE_BREAKER_UNTIL
    _REMOTE_BREAKER_UNTIL = time.time() + _REMOTE_BREAKER_S
    print(f"  [SNAPSHOT] 远端全失（{reason}）→ 本地直通车 {int(_REMOTE_BREAKER_S)}s", flush=True)


def _rebuild_asset_from_local(asset_id: str) -> Optional[Dict[str, Any]]:
    """v412：远端超时之后，用库里已有的 tick / K 线重建一份资产数据。

    为什么必须有：`_collect_asset` 里的本地兜底写在**远端调用之后**，
    而远端一旦挂住（OKX DNS 失败 / Binance TLS 重置 / Yahoo 429），
    `asyncio.wait_for(to_thread(...), 8)` 会把线程整个丢弃 ——
    本地兜底根本没机会执行，9 个资产全部 ticker=None → 快照 down → 决策降级 HOLD。
    这里在 gather 之后补跑一次纯本地重建：只读库、不联网、毫秒级。

    纪律：只用库里真实存在的 tick/bar；取不到就返回 None（不补造、不用 0 占位）。
    """
    try:
        ticker = _ticker_from_ticks(asset_id)
    except Exception:
        ticker = None
    if not ticker:
        return None
    try:
        lm = _local_mtf(asset_id) or {}
    except Exception:
        lm = {}
    mtf: Dict[str, Any] = {}
    for k in ("1d", "4h", "1h"):
        if k in lm:
            mtf[k] = dict(lm[k], source="local_kline")   # 来源可追溯：本地库 = 隔夜参考
    one_d = lm.get("_daily_candles") or []
    stats_7d = _compute_7d_stats(one_d) if len(one_d) >= 2 else _empty_7d_stats()
    return {
        "ticker": ticker, "mtf": mtf, "stats_7d": stats_7d,
        "funding_rate": None, "basis_bps": None, "oi_change_pct": None,
    }


# v412: 国内源用**独立线程池**。默认 executor 会被 `_collect_asset` 里那些挂死的
# 远端线程占满（DNS 卡住的请求 cancel 不掉），国内源排在后面就会错过预算。
try:
    from concurrent.futures import ThreadPoolExecutor as _TPE
    _DOM_EXECUTOR = _TPE(max_workers=2, thread_name_prefix="domestic")
except Exception:  # pragma: no cover
    _DOM_EXECUTOR = None


def _domestic_tickers() -> Dict[str, Dict[str, Any]]:
    """v412: 国内可访问源（Gate.io 加密 / 新浪外盘 / 腾讯美韩股）。

    本机外盘三源全失，但这几条路是通的；抓不到就返回空 dict（上层按缺失处理）。
    每条都带 source 与 note，供 `_assemble_asset` 区分"交易所直连"和"代理/收盘价"。
    """
    try:
        from feeds import domestic_quotes as _dq
    except Exception:
        try:
            from feeds import domestic_quotes as _dq  # type: ignore
        except Exception:
            return {}
    _sub = _domestic_via_subproc("quotes", list(_MONITOR_ASSETS), 7.5)
    if _sub:
        return _sub
    try:
        return _dq.fetch_all(list(_MONITOR_ASSETS))
    except Exception:
        return {}


try:
    _DOM_MTF_EXECUTOR = _TPE(max_workers=6, thread_name_prefix="gate-mtf")
except Exception:  # pragma: no cover
    _DOM_MTF_EXECUTOR = None

# K 线来源分两类，别混说：
#   Gate 现货（BTC/ETH/SOL）→ 1h×200 聚合成 1d/4h/1h，三个周期同一份序列；
#   新浪外盘日线（XAU/WTI）→ 只有 1d（真实交易日，替代本地隔夜线）；
#   4h/1h 走东财国际期货 1h（COMEX 101.GC00Y / NYMEX 102.CL00Y，约 118 根≈5 个交易日），
#   本地聚合成 4h。东财是**机会源**：每品种独立连接、递进退避；拿不到就回落本地线
#   并如实标 local_kline，绝不拿日线硬凑小时线。
_GATE_MTF_ASSETS = ("BTC", "ETH", "SOL")
_SINA_DAILY_MTF_ASSETS = ("XAU", "WTI")
_MTF_ASSETS = _GATE_MTF_ASSETS + _SINA_DAILY_MTF_ASSETS
_MTF_CONSISTENCY = {"1d": 5, "4h": 6, "1h": 8}


try:
    _TS_EXECUTOR = _TPE(max_workers=3, thread_name_prefix="tech-state")
except Exception:  # pragma: no cover
    _TS_EXECUTOR = None

# 宏观单独一个线程：与技术读数共用池时，库锁会把 3 个 tech 线程全占满，
# 宏观任务排在后面轮不到 → 装配末尾 _macro_defaults 回头同步查库，白付 4.4s。
try:
    _MACRO_EXECUTOR = _TPE(max_workers=1, thread_name_prefix="macro")
except Exception:  # pragma: no cover
    _MACRO_EXECUTOR = None


def _collect_technical_states(assets, deadline: float) -> Dict[str, Dict[str, Any]]:
    """并行预取六维技术读数。

    为什么不放在装配里逐个算：库被引擎自己写锁住时单次读数能到 2s+，
    9 个资产串行就是十几秒，只能整批跳过（等于把技术面白扔）。
    读数与报价无关，完全可以和远端/国内取价并行跑。
    """
    import concurrent.futures as _cf

    out: Dict[str, Dict[str, Any]] = {}
    todo = [a for a in assets]
    if not todo or _TS_EXECUTOR is None:
        return out
    futs = {}
    for a in todo:
        if time.time() > deadline:
            break
        futs[_TS_EXECUTOR.submit(_technical_state, a)] = a
    for fut in _cf.as_completed(futs):
        a = futs[fut]
        try:
            st = fut.result()
        except Exception:
            continue
        if isinstance(st, dict):
            out[a] = st
    return out


# v412-F: 国内源抓取走**独立子进程**。
# 引擎进程跑久之后对 gate/新浪/腾讯/东财的 TLS 连接会整体失效（同机独立脚本能取
# 8–9/9，引擎里连 qt.gtimg.cn 都"预算内未返回"，err.log 里 SSLError 三千多条）。
# 在同一个进程里重试/加预算都治不好，只能换一个干净的进程。
_DOM_SUBPROC_ENABLED = (os.getenv("SNAPSHOT_DOMESTIC_SUBPROCESS", "1") or "1") != "0"
_DOM_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "feeds", "domestic_runner.py")


_DOM_CACHE_TTL_S = 180.0     # 缓存超过 3 分钟就当过期（刷新器每 45s 写一次）
_DOM_CACHE_DIR = os.path.expanduser("~/.trident/cache")


def _domestic_from_cache(mode: str, assets=None) -> Optional[Dict[str, Any]]:
    """读独立刷新器（launchd 每 45s 跑一次）写出的缓存。

    不在引擎进程里 fork 抓取：实测 fork 出来的子进程抓取会**一直挂到 timeout 才被杀**
    （限 5s / 6.5s / 8s 都是恰好在限制点被砍），而同一脚本在终端 4.8s 就完成 ——
    这是 macOS 多线程进程 fork 后做网络 IO 的死锁形态，加预算治不好。
    所以抓取交给独立进程，引擎只读文件、只判新鲜度。
    """
    import json as _json

    path = os.path.join(_DOM_CACHE_DIR, f"domestic_{'mtf' if mode == 'mtf' else 'quotes'}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = _json.load(f)
        age = time.time() - float(payload.get("ts") or 0)
        data = payload.get("data") or {}
        if age > _DOM_CACHE_TTL_S or not isinstance(data, dict):
            return None
        if assets:
            data = {k: v for k, v in data.items() if k in set(assets)}
        return data or None
    except Exception:
        return None


def _domestic_via_subproc(mode: str, assets, timeout: float) -> Optional[Dict[str, Any]]:
    """保留接口名（调用点不变）：现在只读缓存，缓存缺/过期返回 None → 调用方回落进程内抓取。"""
    return _domestic_from_cache(mode, assets)


def _domestic_mtf() -> Dict[str, Dict[str, Any]]:
    """国内可访问 K 线：Gate 现货 1d/4h/1h + 新浪外盘日线（商品 1d）。取不到就空。"""
    try:
        from feeds import domestic_quotes as _dq
    except Exception:
        try:
            from feeds import domestic_quotes as _dq  # type: ignore
        except Exception:
            return {}
    _assets = [a for a in _MTF_ASSETS if a in _MONITOR_ASSETS]
    _sub = _domestic_via_subproc("mtf", _assets, 8.0)
    if _sub:
        return _sub
    try:
        return _dq.fetch_mtf(_assets)
    except Exception:
        return {}


def _raw_empty() -> Dict[str, Any]:
    return {"ticker": None, "mtf": {}, "stats_7d": None,
            "funding_rate": None, "basis_bps": None, "oi_change_pct": None}


def _quick_entry(asset_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """时间已经不够时的最小可用 entry（字段与 `_assemble_asset` 同构）。

    装配在引擎负载重时会被 GIL 抢占（实测单资产内部 2.37s，纯等待），9 个叠加
    就把总时长推到 17s，外层 15s 一砍，整份快照连价格都没了。这里宁可少给
    六维技术读数，也要保住价格、来源、mtf——它们才是决策真正要用的。
    """
    ticker = raw.get("ticker") or {}
    price = _extract_price(ticker) or 0.0
    chg = ticker.get("percentage")
    if chg is None:
        chg = ticker.get("change_pct_24h")
    src = str(ticker.get("source") or "")
    return {
        "symbol": _ASSET_SYMBOL.get(asset_id, ""),
        "price": price,
        "price_str": (f"${price:,.2f}" if price else "获取失败"),
        "change_24h_pct": float(chg or 0.0),
        "change_24h_str": ("N/A" if chg is None else f"{float(chg):+.2f}%"),
        "funding_rate_pct": 0.0,
        "funding_rate_str": "N/A",
        "basis_bps": None,
        "oi_change_pct": None,
        "status": "partial" if price else "unavailable",
        "status_note": (f"时间预算耗尽，精简装配（来源 {src or '未知'}）"
                        if price else "网络请求失败"),
        "stats_7d": raw.get("stats_7d") or _empty_7d_stats(),
        "mtf": raw.get("mtf") or {},
        "technical_state": {"available": False,
                            "error": "快照时间预算耗尽，跳过技术读数"},
    }


def _assemble_asset(asset_id: str, raw: Dict[str, Any],
                    lightweight: bool = False,
                    tech: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the public asset entry from collected raw data."""
    ticker = raw.get("ticker")
    stats_7d = raw.get("stats_7d") or _empty_7d_stats()
    funding_rate = raw.get("funding_rate")
    basis_bps = raw.get("basis_bps")
    oi_change_pct = raw.get("oi_change_pct")
    mtf = raw.get("mtf") or {}
    # 本地 ticks / 本地 K 线兜底取到的价（source=market_ticks）与外网直连要区分：
    # 价格锚可信，但 24h 涨跌/资金费率可能仍是本地推算或缺失，故标 partial 而非 ok。
    ticker_source = str((ticker or {}).get("source") or "")

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
            # 轻装模式：时间预算已耗尽时跳过六维技术读数（每个约 0.2–0.3s，
        # DB 忙时更久）。宁可这一批没有技术读数，也不能让整份快照超 15s 被判 down。
        "technical_state": (tech if tech else
                            ({"available": False, "error": "快照总预算不足，跳过技术读数"}
                             if lightweight else _technical_state(asset_id))),
        }

    price = _extract_price(ticker) or 0.0
    has_change = bool(ticker.get("has_change", True))
    # v382: 本地兜底（ticks/ohlcv）没有 percentage 键，不能给 0.0 占位——
    # 否则下游读到 +0.00% 会被误当成"平盘"。统一取 percentage，缺则回落 change_pct_24h。
    change_pct = None
    if has_change:
        change_pct = _safe_float(ticker.get("percentage"), None)
    if change_pct is None:
        change_pct = _safe_float(ticker.get("change_pct_24h"), None)

    fr_pct = 0.0
    fr_str = "N/A"
    if funding_rate is not None:
        fr_pct = funding_rate * 100.0
        fr_str = f"{fr_pct:+.4f}%"

    # v342-fix: 本地兜底取到价时不再标 unavailable——否则闸门的 market_ok=False，
    # confirmation 代理和 structure 因子会被整体挡掉，明明有本地价格/日线也判不可得。
    # 有真实现价即视为可用；本地来源（ticks/ohlcv）标 partial，提示行情非外网直连。
    _tk_note = str((ticker or {}).get("note") or "")
    if price <= 0:
        status = "unavailable"
        status_note = "价格为 0，行情不可用"
    elif ticker_source in ("market_ticks", "ohlcv"):
        status = "partial"
        status_note = "本地行情兜底（外网直连失败）"
    elif ticker_source.startswith(("sina_", "tencent_")):
        # v412: 国内源能取到真价，但有的是期货代理（hf_GC）或收盘价（美股/韩股），
        # 不能和"交易所直连"混说成同一件事 → partial + 明确标的差异
        status = "partial"
        status_note = f"国内源·{_tk_note}" if _tk_note else "国内源（非交易所直连）"
    else:
        status = "ok"
        status_note = _tk_note

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
        "status": status,
        "status_note": status_note,
        "stats_7d": stats_7d,
        "mtf": mtf,
        "technical_state": (tech or _technical_state(asset_id)),
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
    # v412-C: 总时限。引擎外层是 `wait_for(get_snapshot(), 15.0)`，一旦超时整份快照
    # 被判 down → 降级 HOLD。实测单次耗时方差很大（10.9s / 12.1s / 18.9s），
    # 根因是远端挂死线程抢 GIL + 装配本身约 1.5s；这里用"总预算 - 剩余"给每段等待
    # 动态限量，保证必然留出装配时间，不再出现"前段吃满、后段超时"。
    _deadline = t0 + float(os.getenv("SNAPSHOT_TOTAL_BUDGET_S", "12.5") or 11)

    def _remain(reserve: float) -> float:
        return max(0.0, _deadline - time.time() - reserve)

    # 并行采集各资产；每资产独立 8s 超时——代理抖动时只丢慢资产，
    # 不让单个资产拖死整批（15s 整体超时 → 全 down 的历史问题）
    #
    # v322 修复：**并发 await**（原来串行 for-await，虽然 task 已并发启动，
    # 但第一个 task 若耗满 8s 才返回，后面几个的 wait_for 计时早已开始跑完
    # → 表现为「资产 N 耗时 = N×8s」。实测单次快照 **97.6 秒**）。
    # v412: **本地优先打底** —— 先用库里真实 tick/K 线把 9 个资产建出来（毫秒级），
    # 再给远端一个硬预算去补齐。这样无论远端怎么挂（本机实测 OKX DNS 不通、
    # Binance TLS 重置、Yahoo 429，getaddrinfo 还能卡几十秒），整次快照都能在
    # 5s 预算内收口，不会再被引擎的 15s wait_for 整段取消 → 判 down → 降级 HOLD。
    raw_results: Dict[str, Dict[str, Any]] = {}
    # 打底阶段也要有预算：库被锁时单次只读能等满 1.2s，9 个资产 × 2 次查询仍然很长。
    # 超过 2.5s 的资产直接给空壳（后面国内源/远端仍可补），绝不把整份快照拖过 15s。
    _local_deadline = t0 + 3.0
    for asset_id in _MONITOR_ASSETS:
        if time.time() > _local_deadline:
            raw_results[asset_id] = _raw_empty()
            continue
        try:
            raw_results[asset_id] = _rebuild_asset_from_local(asset_id) or _raw_empty()
        except Exception:
            raw_results[asset_id] = _raw_empty()
    _local_n = sum(1 for a in _MONITOR_ASSETS
                   if (raw_results[a].get("ticker") or {}).get("source"))
    _local_s = time.time() - t0

    # v412: 国内源与远端并行发起 —— 不要串行叠加，否则 3s+5s 会把引擎 15s 预算吃光
    # run_in_executor 返回的是 Future（不是 coroutine），不能再套 create_task
    _dom_loop = asyncio.get_running_loop()
    _dom_task = _dom_loop.run_in_executor(_DOM_EXECUTOR, _domestic_tickers)
    _dom_mtf_task = _dom_loop.run_in_executor(_DOM_MTF_EXECUTOR, _domestic_mtf)
    _ts_task = _dom_loop.run_in_executor(
        _TS_EXECUTOR, _collect_technical_states, list(_MONITOR_ASSETS), _deadline - 2.0)
    _macro_task = _dom_loop.run_in_executor(_MACRO_EXECUTOR, _fred_macro_latest)

    if _remote_breaker_active():
        # 熔断窗口内跳过远端：不再逐资产去等超时
        print(f"  [SNAPSHOT] 远端熔断中 → 全本地重建（本地 {_local_n}/{len(_MONITOR_ASSETS)} 有价）",
              flush=True)
    else:
        _budget = min(float(os.getenv("SNAPSHOT_REMOTE_BUDGET_S", "3") or 3),
                      _remain(6.5))     # 给国内源(≤4s) + 装配(~3s) 留 7s
        tasks = [asyncio.create_task(asyncio.to_thread(_collect_asset, a))
                 for a in _MONITOR_ASSETS]
        _done, _pending = await asyncio.wait(tasks, timeout=_budget)
        for _t in _pending:
            _t.cancel()
        _remote_n = 0
        for asset_id, _t in zip(_MONITOR_ASSETS, tasks):
            if _t in _done and not _t.cancelled():
                try:
                    _r = _t.result()
                except BaseException:
                    _r = None
                if isinstance(_r, dict) and _r.get("ticker") is not None:
                    raw_results[asset_id] = _r   # 远端宁可优先，本地已打底
                    _remote_n += 1
        if _remote_n == 0:
            _trip_remote_breaker(f"远端 0 个资产可用（预算 {_budget}s），全部走本地")
        print(f"  [SNAPSHOT] 远端补齐 {_remote_n}/{len(_MONITOR_ASSETS)}（预算 {_budget}s，"
              f"本地打底 {_local_n}）", flush=True)

    _remote_s = time.time() - t0 - _local_s

    # v412: 远端（交易所/Yahoo）之外再走一层**国内可访问源**：Gate.io / 新浪外盘 / 腾讯。
    # 只补"还没有远端报价"的资产，绝不覆盖远端直连结果（同源优先级必须可追溯）。
    # 两个国内任务（报价 / K 线）**同时等**，不串行叠加预算：
    # 串行等 = 6s + 6s，算上远端 3s 就顶到引擎 15s 上限。
    _dom_budget = min(float(os.getenv("SNAPSHOT_DOMESTIC_BUDGET_S", "8.5") or 5),
                      _remain(3.5))     # 装配阶段至少留 3s
    await asyncio.wait({_dom_task, _dom_mtf_task, _ts_task, _macro_task},
                       timeout=_dom_budget)

    def _pick(fut):
        try:
            return fut.result() if fut.done() and not fut.cancelled() else {}
        except Exception:
            return {}

    _dom = _pick(_dom_task) or {}
    _dom_mtf = _pick(_dom_mtf_task) or {}
    _dom_s = time.time() - t0 - _local_s - _remote_s
    _ts_map = _pick(_ts_task) or {}
    _fred_rows = _pick(_macro_task)   # None = 没取到，直接用 N/A，别再查库
    _dom_n = 0
    if _dom:
        for asset_id in _MONITOR_ASSETS:
            _d = _dom.get(asset_id) or {}
            _p = _d.get("price")
            if not _p:
                continue
            _tk = (raw_results[asset_id].get("ticker") or {})
            _src = str(_tk.get("source") or "")
            # 已有远端交易所直连的资产不动；本地兜底(market_ticks/ohlcv)才让国内源顶上
            if _src and _src not in ("market_ticks", "ohlcv"):
                continue
            raw_results[asset_id]["ticker"] = {
                "last": float(_p),
                "percentage": _d.get("change_pct"),
                "change_pct_24h": _d.get("change_pct"),
                "has_change": _d.get("change_pct") is not None,
                "source": _d.get("source") or "domestic",
                "note": _d.get("note") or "",
            }
            _dom_n += 1
    # 注意：这两个 print 必须在 `if _dom:` 之外。否则"_dom 为空"这一最常见的失败形态
    # 一行日志都不打，看起来就像代码没生效（08:02 那轮的真实误判）。
    if _dom_n:
        print(f"  [SNAPSHOT] 国内源补齐 {_dom_n}/{len(_MONITOR_ASSETS)}"
              f"（Gate/新浪/腾讯；其余走本地）", flush=True)
    else:
        print("  [SNAPSHOT] 国内源本轮 0 补齐（源全部失败或任务超预算）→ 保持远端/本地",
              flush=True)

    # v412-B: Gate 真实 K 线补进 mtf。优先于本地库的隔夜线，低于远端直连。
    # 只覆盖 `source=local_kline` 或缺失的周期：远端拿到的 K 线不动（口径不可跨源拼）。
    _mtf_n = 0
    _mtf_by_src: Dict[str, int] = {}
    for asset_id, periods in (_dom_mtf or {}).items():
        _raw = raw_results.get(asset_id)
        if not _raw:
            continue
        # 来源按周期取：同一资产可能 1d 走新浪日线、4h/1h 走东财（商品），
        # 也可能三个周期都走 Gate（加密）。统一写成一个 source 会污染口径。
        _src_1d = (periods or {}).get("_source_1d") or "gate_klines"
        _src_intra = (periods or {}).get("_source_intraday") or "gate_klines"
        _mtf = _raw.setdefault("mtf", {})
        for interval, candles in (periods or {}).items():
            if interval.startswith("_"):
                continue
            _src = _src_1d if interval == "1d" else _src_intra
            _cons = _MTF_CONSISTENCY.get(interval, 6)
            if not candles or len(candles) < 2:
                continue
            _old = _mtf.get(interval)
            if _old and str(_old.get("source") or "") not in ("local_kline", ""):
                continue    # 远端直连的周期不动
            _stats = _compute_period_stats(candles, consistency=_cons)
            if not _stats or (_stats.get("return_pct") is None):
                continue
            _stats["source"] = _src
            _mtf[interval] = _stats
            _mtf_n += 1
            _mtf_by_src[_src] = _mtf_by_src.get(_src, 0) + 1
    if _mtf_n:
        _desc = "、".join(f"{k} {v} 个" for k, v in _mtf_by_src.items())
        print(f"  [SNAPSHOT] K线补齐 {_mtf_n} 个周期（{_desc}，替代本地隔夜线）",
              flush=True)
    else:
        print("  [SNAPSHOT] K线本轮 0 补齐（Gate K 线抓取失败或超预算）→ mtf 仍用本地线",
              flush=True)

    assets: Dict[str, Dict[str, Any]] = {}
    ok_count = 0       # 外网直连拿到（status=ok）
    usable_count = 0   # 有任何真实价格（ok + partial 本地兜底）
    _light_n = 0
    _quick_n = 0
    _ts_slow = False          # 第一个技术读数变慢 → 后面全部轻装，绝不逐个去等
    _asm_t0 = time.time()
    for asset_id in _MONITOR_ASSETS:
        # DB 被锁时技术读数会从 0.25s 涨到十几秒；提前 4s切轻装 + 首次读数变慢即熔断
        # 硬保护：引擎负载重时装配会被 GIL 抢占（实测单资产"内部 2.37s"，纯等待），
        # 9 个资产叠加能把总时长推到 17s+，整份快照会被外层 15s 砍掉 → 全部白做。
        # 所以超过 12.5s 就放弃精装，直接给最小可用 entry（价格/来源/mtf 原样）。
        if time.time() > (_deadline + 1.5):
            assets[asset_id] = _quick_entry(asset_id, raw_results[asset_id])
            _quick_n += 1
            continue
        _tech = _ts_map.get(asset_id)
        _light = (not _tech) or _ts_slow or (time.time() > (_deadline - 4.0))
        _t1 = time.time()
        entry = _assemble_asset(asset_id, raw_results[asset_id],
                                lightweight=_light, tech=_tech)
        _asm_cost = time.time() - _t1
        # 只有"因为我们主动跳过"才算跳过；源本来就没数据的不算（那是真的没有）
        if str(((entry.get("technical_state") or {}).get("error") or "")).startswith("快照总预算不足"):
            _light_n += 1
        if (not _light) and (time.time() - _t1 > 1.2):
            _ts_slow = True   # 兜底：预取没覆盖到的资产读数慢，就不再逐个算
        _dt = time.time() - _t1
        if _dt > 0.5:
            print(f"  [SNAPSHOT] 装配慢 {asset_id} {_dt:.2f}s"
                  f"（light={_light} tech={'预取' if _tech else '无'}"
                  f" 内部={_asm_cost:.2f}s 事后={_dt - _asm_cost:.2f}s）", flush=True)

        assets[asset_id] = entry
        if entry["status"] == "ok":
            ok_count += 1
        if entry["status"] in ("ok", "partial") and float(entry.get("price") or 0.0) > 0:
            usable_count += 1

    total = len(_MONITOR_ASSETS)
    _asm_loop_s = time.time() - _asm_t0   # 装配循环本身
    # v412: "partial"（本地兜底有真实价）不能再被当成"全部失败"。
    # 之前 ok_count==0 就判 down，导致 9 个资产全有价、总结却写"所有标的拉取失败"
    # → 引擎 raise snapshot down/empty → 决策被迫降级 HOLD。
    if ok_count == total:
        overall_status = "ok"
    elif usable_count == 0:
        overall_status = "down"
        _trip_remote_breaker("9 个资产全部不可用")
    elif usable_count < total:
        overall_status = "partial"
    else:
        overall_status = "partial"   # 全部有价但含本地兜底 → 如实标 partial

    _macro_t0 = time.time()
    macro: Dict[str, Any] = _macro_defaults(_fred_rows)
    _macro_s = time.time() - _macro_t0

    summary = _build_summary(assets, macro, overall_status)

    elapsed = round(time.time() - t0, 3)
    print(f"  [SNAPSHOT] {overall_status.upper()} ({ok_count}/{total} 直连 OK, "
          f"{usable_count}/{total} 有价) in {elapsed}s "
          f"（打底 {_local_s:.1f}s / 远端 {_remote_s:.1f}s / 国内 {_dom_s:.1f}s / "
          f"装配 {_asm_loop_s:.1f}s+宏观 {_macro_s:.1f}s）— {summary.split(chr(10))[0]}")
    if _light_n:
        print(f"  [SNAPSHOT] 其中 {_light_n} 个资产跳过技术读数"
              f"（{'库锁导致读数变慢' if _ts_slow else '总预算 11s 已接近耗尽'}）", flush=True)

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
                # v489：None = 取不到，写 NULL；绝不落成 0.0（否则前端把未知当"没动"）
                "change24h": (float(entry["change_24h_pct"])
                              if entry.get("change_24h_pct") is not None else None),
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
    usable_count = 0
    for asset_id in _MONITOR_ASSETS:
        entry = _assemble_asset(asset_id, _collect_asset(asset_id))
        assets[asset_id] = entry
        if entry["status"] == "ok":
            ok_count += 1
        if entry["status"] in ("ok", "partial") and float(entry.get("price") or 0.0) > 0:
            usable_count += 1

    total = len(_MONITOR_ASSETS)
    # v412: 口径与 get_snapshot 一致 —— partial（本地兜底价）不算 down
    overall_status = "ok" if ok_count == total else ("down" if usable_count == 0 else "partial")

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
                # v489：None = 取不到，写 NULL；绝不落成 0.0（否则前端把未知当"没动"）
                "change24h": (float(entry["change_24h_pct"])
                              if entry.get("change_24h_pct") is not None else None),
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

_FRED_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}
_FRED_CACHE_TTL_S = 3600.0  # 宏观是日度指标。TTL 曾设 300s，正好等于引擎的快照冷却
                            # 300s → 每次快照时缓存必然过期，等于没缓存（实测每批都付 4.4s）


def _fred_macro_latest() -> Dict[str, Dict[str, Any]]:
    """从 macro_snapshots 读最近一次 fred 层 ok 值，返回 {metric_key: {"value", "unit"}}。

    注意两件事：
    1. 连接不能用 `db.get_connection()`：它开的是**可写连接 + timeout=5s**，
       引擎正在写库时 SELECT 会等满 5s（实测"装配"因此从 0.7s 涨到 4.8s）。
    2. 这条 SQL 本身在 786MB 的库上要 4s+（`MAX(ts)` 子查询 + 无覆盖索引）。
       宏观是**日度**数据，每 5 分钟一批去查一遍纯属浪费 → 这里加 300s 进程内缓存。
       读不到也缓存空值，避免每批都去付这 4s。
    """
    now = time.time()
    if now - float(_FRED_CACHE.get("ts") or 0.0) < _FRED_CACHE_TTL_S:
        return dict(_FRED_CACHE.get("data") or {})

    out: Dict[str, Dict[str, Any]] = {}
    try:
        import sqlite3

        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "trident_event_bus.db",
        )
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
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
    _FRED_CACHE["ts"] = now
    _FRED_CACHE["data"] = out
    return dict(out)


# macro 区字段 → (FRED metric_key, 标签)。fred 无 24h 变化 → change 标 N/A（日度指标）。
_MACRO_FRED_MAP = {
    "us10y": ("dgs10", "US10Y"),
    "tips10y": ("tips10y", "TIPS10Y"),
    "dxy": ("dxy", "DXY"),
    "vix": ("vix", "VIX"),
}


def _macro_defaults(fred: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
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
    # fred 为 None/空 = 本次预取没拿到 → 直接给 N/A，**绝不回头同步查库**。
    # 之前这里 `if fred is None: fred = _fred_macro_latest()`，一旦预取任务被
    # 技术读数挤掉（同池 3 线程全被库锁占住），就会在装配末尾同步补一次 4.4s，
    # 把整份快照顶到 11.9s。宏观是日度数据，缺一批不影响决策。
    if not fred:
        fred = {}
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

        # 状态读数（六维合成，见 technical_state.py）：趋势阶段/波动/价值区/杠杆/
        # 主动性/持仓分歧 → 一行结论。缺的维度随读数一起标出，不让 LLM 把"没数据"
        # 当成"中性"。这是状态描述，不是入场信号。
        ts_state = a.get("technical_state") or {}
        if ts_state.get("available") and ts_state.get("readout"):
            txt = f"  状态: {ts_state['readout']}"
            if ts_state.get("bias") is not None:
                txt += f" [偏置 {ts_state['bias']:+.2f}/覆盖 {ts_state.get('coverage', 0):.0%}]"
            lines.append(txt)

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
            "technical_state": {"available": False, "error": reason},
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
