#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部 K 线源适配（币安 / Bybit / OKX / Bitstamp / Yahoo / 金十）。

从 api_server 拆出来的原因：这是一整块"外部行情适配层"，只做 HTTP + 解析 + 短期
TTL 缓存，不碰决策链。放在 9k 行文件里，任何一次上游改字段都要在决策代码旁边改，
测起来也得把 FastAPI 全家 import 一遍。

约定（拆的时候刻意保留的）：
- **缓存只做短期 TTL**（币安 5s / 金十 30s / 分页 5min），过期不沿用旧值。
- 上游失败**返回 None**，由调用方降级；绝不在这里造数据。
- 币安 fapi 国内常被阻断：连续失败 2 次进 60s 熔断，由 `_fetch_binance_klines`
  走 spot 镜像兜底（熔断状态只在本模块内，别的模块不需要读它）。
"""

from __future__ import annotations

import time
import threading
from typing import Any, Dict, List, Optional, Tuple

from feeds import quote_sources
from feeds import jin10_mcp

_BINANCE_KLINE_CACHE: Dict[str, Dict[str, Any]] = {}
_BINANCE_KLINE_TTL_S = 5.0
_JIN10_KLINE_TTL_S = 30.0   # 金十 1m 级数据：缓存 30s，防高频打爆 MCP 配额


# 资产 → 真实数据源路由（见 quote_sources.ASSET_ROUTE，单一事实来源）：
#   加密 BTC/ETH/SOL → 币安永续 fapi；黄金 XAU/GOLD/XAG → OKX 永续；
#   原油 WTI/OIL、美股 → 无免费永续合约（实时价走国内现货 / Yahoo）。
# 币安 fapi 只上市加密币，XAUUSDT/CLUSDT/NVDAUSDT 等均不存在 —— 此前把它们硬编码到
# 币安 fapi 导致请求必 400，再被 mock/兜底造假数据，现已拆掉（缺真实 K 线 → 行情缺失）。
_BINANCE_BASE = {"fapi": "https://fapi.binance.com/fapi/v1"}


# ── 币安 fapi 熔断（仿 OKX 熔断模式）：fapi 国内常被 GFW 阻断，连续失败后短路 ──
# 短路时直接返回 None，让 _fetch_binance_klines 走 data-api.binance.vision spot 镜像兜底（国内稳定）。
# 根因诊断：fapi.binance.com:443 频繁 SSL/TLS 失败（GFW SNI 阻断），每次浪费 3s+ 才超时。
_binance_breaker_lock = threading.Lock()
_binance_fapi_fail_streak = 0
_binance_fapi_cooldown_until = 0.0

_BINANCE_FAPI_BREAKER_AFTER = 2           # 连续失败几次后打开熔断
_BINANCE_FAPI_BREAKER_COOLDOWN_S = 60.0   # 熔断冷却时长（秒）


def _binance_kline_session(market: str) -> Any:
    """币安合约请求会话（fapi 走 env HTTP_PROXY，与模拟盘交易/实时价同一通道）。"""
    import requests as _req
    s = _req.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def _aggregate_bars(bars_1m: List[Dict[str, Any]], period_sec: int) -> List[Dict[str, Any]]:
    """把连续 1m bar 按 period_sec（900/3600/14400）聚合为粗周期 OHLCV。

    bar 的 time 为秒级；按 time // period_sec 归桶，桶内开=首open、收=末close、高低=极值、量=累加。
    不完整桶也保留（最后一根未走完），保持与真实行情一致性。
    """
    if not bars_1m:
        return []
    out: Dict[int, List[Dict[str, Any]]] = {}
    for b in bars_1m:
        bucket = int(b["time"]) // period_sec
        out.setdefault(bucket, []).append(b)
    agg: List[Dict[str, Any]] = []
    for bucket in sorted(out):
        grp = out[bucket]
        agg.append({
            "time": bucket * period_sec,
            "open": grp[0]["open"],
            "high": max(g["high"] for g in grp),
            "low": min(g["low"] for g in grp),
            "close": grp[-1]["close"],
            "volume": sum(int(g.get("volume") or 0) for g in grp),
        })
    return agg


# 海力士 cap（v??）：股票类（Yahoo Finance 兜底）K 线统一入口
#   - 把 Yahoo v8 chart 的 [ts_ms, o, h, l, c, v] 形态转成与币安/OKX 一致的 {time, open, ...} dict 列表，
#     让 _history_sync 的存储/异常扫描/打点逻辑复用，不为股票另写一遍。
#   - Yahoo 限速+熔断由 quote_sources 内部处理；失败返回 []，调用方标"Yahoo Finance 不可用"。
# v231: 改走 multi_source_fetcher — yahoo 失败时自动切 yfinance / asia 兜底，
#   把"数据缺失"这个借口从反思系统里彻底拔掉。
def _fetch_yahoo_history_bars(asset: str, interval: str, limit: int) -> List[Dict[str, Any]]:
    """股票类（NVDA / TSLA / SKHYNIX / US_HYNIX / MU ...）K 线。

    v231 起走 multi_source_fetcher.fetch_stock_history_cached：
      yahoo → yfinance → asia 三级 fallback，每级失败都记 feed_health 表。
      全部失败才返回 [] — 调用方此时才能合理标"数据缺失"。

    返回 bars 形态与 _fetch_binance_klines 一致：[{time: 秒, open, high, low, close, volume}, ...]。
    """
    asset = (asset or "").upper()
    try:
        import multi_source_fetcher as _msf
        rows = _msf.fetch_stock_history_cached(asset, interval=interval, limit=limit)
    except Exception:
        return []
    if not rows:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            ts_ms, o, h, lo, c, v = r
        except (ValueError, TypeError):
            continue
        if c is None or o is None or h is None or lo is None or c <= 0:
            continue
        out.append({
            "time": int(int(ts_ms) // 1000),
            "open": round(float(o), 4),
            "high": round(float(h), 4),
            "low": round(float(lo), 4),
            "close": round(float(c), 4),
            "volume": int(float(v)) if v is not None else 0,
        })
    return out


# 海力士 cap（v??）：movers 板块用的"股票最新价 + 24h 涨跌%"读取器。
#   复用 quote_sources 的 Yahoo 数据展（5d 1d bar → 取末根收盘 / 24h 前 bar 收盘 → 百分比）。
#   Yahoo chart 直接提供 24h 价格区间，无需额外计算；失败返回 (None, None) 让前端降级。
#   进程内缓存 30s（避免每个 movers 拉取周期都打 Yahoo）。
# v231: 改走 multi_source_fetcher — yahoo 失败时自动切 yfinance / asia 兜底，
#   把"数据缺失"这个借口从反思系统里彻底拔掉。
def _yahoo_price_change24h(asset: str) -> Tuple[Optional[float], Optional[float]]:
    """从 Yahoo Finance v8 chart 取股票最新价 + 24h 涨跌%。失败返回 (None, None)。

    v231 起走 multi_source_fetcher.fetch_stock_price_cached：
      yahoo → yfinance → asia (→ equity for 美股) 多级 fallback。
      全部失败才返回 (None, None) — 调用方才能合理标"数据缺失"。
    """
    asset = (asset or "").upper()
    try:
        import multi_source_fetcher as _msf
        price, prev = _msf.fetch_stock_price_cached(asset)
    except Exception:
        return None, None
    if price is None or price <= 0:
        return None, None
    if prev is None or prev <= 0 or prev == price:
        # prev_close 拿不到或等于 price — 让前端按 0% 处理（不假装涨跌幅）
        return (price, None)
    chg_pct = (price / prev - 1.0) * 100.0
    return (price, chg_pct)


def _jin10_klines_bars(asset: str, interval: str) -> Optional[tuple]:
    """金十 MCP 分钟 K 线兜底：XAU/WTI/XAG 走 mcp.jin10.com（国内直连，不依赖 VPN）。

    金十 get_kline 仅提供 1m 级（近 100 根 / 24h 内）：
    - 1m 直接返回；
    - 15m/1h/4h 用 1m 聚合（数据量有限，仅作近端参考）；
    - 1d 金十不支持 → None（调用方回退库内历史）。
    返回 (bars, last_bar)，与币安/OKX bars 同构（time 秒级、volume 为成交量）。
    进程内缓存 30s（分钟级数据无需秒级刷新，且避免高频打爆金十 MCP 配额）。
    """
    code = jin10_mcp.JIN10_KLINE_CODE.get((asset or "").upper())
    if not code:
        return None
    if interval not in ("1m", "3m", "5m", "10m", "15m", "1h", "4h"):
        return None
    key = f"jin10:{code}:{interval}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _JIN10_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    # v2026.10.08: 硬截断 3s（jin10_mcp 内置 token 轮换，HTTP 20s timeout，3 token = 60s 累计）。
    # 用 ThreadPoolExecutor(1) + shutdown(wait=False) 真正立刻返回。
    import concurrent.futures as _cf
    def _do():
        try:
            return jin10_mcp.get_kline(code, count=100)
        except Exception:
            return None
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_do)
        try:
            payload = fut.result(timeout=3.0)
        except _cf.TimeoutError:
            return None
    except Exception:
        return None
    finally:
        ex.shutdown(wait=False)
    if payload is None:
        return None
    inner = payload.get("data") if payload.get("status") == "ok" else None
    kls = (inner or {}).get("klines") or []
    if not kls:
        return None
    bars_1m: List[Dict[str, Any]] = []
    for k in kls:
        try:
            o = float(k["open"]); h = float(k["high"])
            lo = float(k["low"]); c = float(k["close"])
        except (TypeError, ValueError, KeyError):
            continue
        if min(o, h, lo, c) <= 0:
            continue
        bars_1m.append({
            "time": int(k["time"]),
            "open": round(o, 4), "high": round(h, 4),
            "low": round(lo, 4), "close": round(c, 4),
            "volume": int(k.get("volume") or 0),
        })
    if not bars_1m:
        return None
    bars_1m.sort(key=lambda b: b["time"])
    if interval == "1m":
        bars = bars_1m
    else:
        bars = _aggregate_bars(bars_1m, {"3m": 180, "5m": 300, "10m": 600, "15m": 900, "1h": 3600, "4h": 14400}[interval])
    if not bars:
        return None
    _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": bars[-1]}
    return bars, bars[-1]


def _fetch_binance_klines(asset: str, interval: str) -> Optional[tuple]:
    """图表 K 线统一入口：按资产路由真实数据源（绝不 mock 凑数）。

    - 加密 BTC/ETH/SOL → 币安永续 fapi（走代理）→ 现货镜像直连 → Bybit 永续
                          → Bitstamp 现货 → 返回 None（caller 退库内历史）
    - 黄金 XAU/GOLD/XAG → OKX 永续（国内直连）→ 失败金十 MCP 1m 兜底
    - 原油 WTI/OIL → 金十 MCP 1m（USOIL，国内直连；无 OKX/Yahoo 免费源）
    - 美股 NVDA/TSLA/.../SKHYNIX/US_HYNIX/MU → Yahoo Finance v8 chart
                            （multi_source_fetcher 内部 yahoo→yfinance→asia 兜底）

    v192：BTC/ETH/SOL 加 Bybit + Bitstamp 多源链，规避 Binance 全家桶被 GFW
    阻断时只能退库内历史的问题（任意源成功 → 返回实时 K 线）。
    v?? 海力士 cap：yahoo 市场（美股/海力士）也接入真实 K 线，归因页 /review
    的 US_HYNIX/SKHYNIX/MU/NVDA/... 不再退化为「模拟反推」。
    返回 (bars, last_bar)。
    """
    r = quote_sources.route(asset)
    if not r:
        return None
    if r["market"] == "okx_swap":
        okx = _okx_klines_bars(r["symbol"], interval)
        if okx is not None:
            return okx
        # OKX 被墙（SSL_ERROR_SYSCALL）→ 金十 MCP 兜底（XAU/XAG 现货 1m 级，国内直连）
        return _jin10_klines_bars(asset, interval)
    if r["market"] == "binance_fapi":
        sym = r["symbol"]
        alt = _BINANCE_ALT_SYMBOLS.get(sym) or {}
        bybit_sym = alt.get("bybit")
        bs_sym = alt.get("bitstamp")

        # v2026.10.08: 4 源并行（每源 3s timeout）→ 总耗时 3s（以前 4×3=12s 串行）。
        # 谁先成功就用谁；全部超时/失败才 None。
        import concurrent.futures as _cf
        attempts = [
            ("fapi",     lambda: _binance_fapi_klines(sym, interval)),
            ("spot",     lambda: _binance_spot_klines(sym, interval)),
            ("bybit",    (lambda: _bybit_klines_bars(bybit_sym, interval)) if bybit_sym else (lambda: None)),
            ("bitstamp", (lambda: _bitstamp_klines_bars(bs_sym, interval)) if bs_sym else (lambda: None)),
        ]
        ex = _cf.ThreadPoolExecutor(max_workers=len(attempts))
        try:
            futs = {ex.submit(fn): name for name, fn in attempts}
            for fut in _cf.as_completed(futs, timeout=3.5):
                try:
                    res = fut.result()
                    if res is not None:
                        return res
                except Exception:
                    continue
        except _cf.TimeoutError:
            pass
        finally:
            ex.shutdown(wait=False)
        return None
    if r["market"] == "domestic_spot":
        # WTI/OIL 无免费永续 → 金十 MCP 兜底（USOIL 现货 1m，国内直连）
        return _jin10_klines_bars(asset, interval)
    if r["market"] == "yahoo":
        # 海力士 cap（v??）：股票类（Yahoo Finance 兜底）— 多源链 yahoo→yfinance→asia
        # 由 multi_source_fetcher.fetch_stock_history_cached 内部完成；这里只负责把
        # 前端 interval(1m/15m/1h/4h/1d) 翻译成 Yahoo chart 支持的 interval，并把
        # _fetch_yahoo_history_bars 返回的 bars 转成 (bars, last_bar) 元组，
        # 与币安/OKX 同构。归因页 /review 修 US_HYNIX 实时 K 线可用。
        iv_norm = {"1m": "1m", "15m": "15m", "1h": "60m", "4h": "60m", "1d": "1d"}.get(interval, "60m")
        limit = {"1m": 240, "15m": 96, "1h": 48, "4h": 36, "1d": 120}.get(interval, 48)
        asset_up = (asset or "").upper()
        bars = _fetch_yahoo_history_bars(asset_up, iv_norm, limit)
        if bars:
            return (bars, bars[-1])
        return None
    return None


def _binance_fapi_klines(symbol: str, interval: str) -> Optional[tuple]:
    """币安永续真实 K 线（fapi，走代理；5s 缓存）。volume=quoteVolume（USDT 成交额）。

    interval 支持 1m/3m/5m/15m/1h/4h/1d（币安原生）；10m 非原生 → 上层用 5m 聚合。

    v176.5f: 加 fapi 熔断器（仿 OKX 模式）。fapi 国内常被 GFW 阻断，连续失败后短路
    60s，节省每次浪费的 3s+ 超时；短路时直接 None，让调用方走 spot 镜像（data-api.binance.vision，
    国内稳定 0.6s 直连）兜底。
    """
    global _binance_fapi_fail_streak, _binance_fapi_cooldown_until
    b_interval = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}.get(interval, "1h")
    # v220: bumped 5m from 288 -> 1440 to support 10m x 720 bars (~5 days) on chart
    limit = {"1m": 720, "3m": 288, "5m": 1440, "15m": 96, "1h": 48, "4h": 36, "1d": 120}.get(interval, 48)
    key = f"{symbol}:{b_interval}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    # ── 熔断检查：冷却期内直接短路（返回 None → 调用方走 spot 镜像） ──
    with _binance_breaker_lock:
        if time.time() < _binance_fapi_cooldown_until:
            return None
    try:
        session = _binance_kline_session("fapi")
        url = f"{_BINANCE_BASE['fapi']}/klines?symbol={symbol}&interval={b_interval}&limit={limit}"
        # P1 修复: 国内网络 Binance fapi 常被 GFW 阻断(SNI/SSL 失败),15s 太长导致 dashboard
        # ticker 价格静止. 改 3s 快速失败 → 走 spot 镜像(国内稳定) → 再走 DB kline_bars 兜底.
        # 配合 _BINANCE_KLINE_TTL_S=5s 缓存, 成功请求命中缓存不再重试外网.
        resp = session.get(url, timeout=3)
        if resp.status_code != 200:
            with _binance_breaker_lock:
                _binance_fapi_fail_streak += 1
                if _binance_fapi_fail_streak >= _BINANCE_FAPI_BREAKER_AFTER:
                    _binance_fapi_cooldown_until = time.time() + _BINANCE_FAPI_BREAKER_COOLDOWN_S
                if _binance_fapi_fail_streak == _BINANCE_FAPI_BREAKER_AFTER:
                    print(
                        f"[BINANCE-FAPI-BREAKER] 连续失败 {_binance_fapi_fail_streak} 次（{symbol}），"
                        f"熔断 {_BINANCE_FAPI_BREAKER_COOLDOWN_S}s（status={resp.status_code}）",
                        flush=True,
                    )
            return None
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None
        bars: List[Dict[str, Any]] = []
        for k in data:
            o = float(k[1]); h = float(k[2]); lo = float(k[3]); c = float(k[4])
            if min(o, h, lo, c) <= 0:
                continue
            bars.append({
                "time": int(int(k[0]) // 1000),
                "open": round(o, 4), "high": round(h, 4),
                "low": round(lo, 4), "close": round(c, 4),
                "volume": int(float(k[7])),  # quoteVolume（USDT 成交额）
            })
        if not bars:
            return None
        last = bars[-1]
        _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
        # ── 成功后重置熔断 ──
        with _binance_breaker_lock:
            _binance_fapi_fail_streak = 0
            _binance_fapi_cooldown_until = 0.0
        return bars, last
    except Exception as _e:
        # ── 失败计数（超时/SSL 等） ──
        with _binance_breaker_lock:
            _binance_fapi_fail_streak += 1
            if _binance_fapi_fail_streak >= _BINANCE_FAPI_BREAKER_AFTER:
                _binance_fapi_cooldown_until = time.time() + _BINANCE_FAPI_BREAKER_COOLDOWN_S
            if _binance_fapi_fail_streak == _BINANCE_FAPI_BREAKER_AFTER:
                print(
                    f"[BINANCE-FAPI-BREAKER] 连续失败 {_binance_fapi_fail_streak} 次（{symbol}），"
                    f"熔断 {_BINANCE_FAPI_BREAKER_COOLDOWN_S}s | err={type(_e).__name__}: {str(_e)[:80]}",
                    flush=True,
                )
        return None


def _binance_spot_klines(symbol: str, interval: str) -> Optional[tuple]:
    """币安现货(USDT) 真实 K 线 → 与 fapi bars 同构（time 秒级、volume=quoteVolume）。

    国内直连（data-api.binance.vision），作为永续 fapi 代理失败时的兜底。5s 缓存。
    interval 支持 1m/3m/5m/15m/1h/4h/1d（币安原生）；10m 非原生 → 上层用 5m 聚合。
    """
    b_interval = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}.get(interval, "1h")
    # v220: bumped 5m from 288 -> 1440 to support 10m x 720 bars (~5 days) on chart
    limit = {"1m": 720, "3m": 288, "5m": 1440, "15m": 96, "1h": 48, "4h": 36, "1d": 120}.get(interval, 48)
    key = f"spot:{symbol}:{b_interval}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = False  # 强制直连，不走 env 代理
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={b_interval}&limit={limit}"
        # P1 修复: 同 _binance_fapi_klines, 国内网络 data-api.binance.vision 也偶发阻断.
        # 15s 太长, 改 3s 让前端 ticker 价格静止/加载中状态快速消失, 走 DB 兜底.
        resp = s.get(url, timeout=3)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None
        bars: List[Dict[str, Any]] = []
        for k in data:
            o = float(k[1]); h = float(k[2]); lo = float(k[3]); c = float(k[4])
            if min(o, h, lo, c) <= 0:
                continue
            bars.append({
                "time": int(int(k[0]) // 1000),
                "open": round(o, 4), "high": round(h, 4),
                "low": round(lo, 4), "close": round(c, 4),
                "volume": int(float(k[7])),  # quoteVolume（USDT 成交额）
            })
        if not bars:
            return None
        last = bars[-1]
        _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
        return bars, last
    except Exception:
        return None


# ── 多源 fallback 链（v192 — 国内服务器友好）────────────────────────────────────
# 国内服务器常见 GFW 阻断 Binance 全家桶（fapi/data-api/api）。在 _binance_fapi + _binance_spot
# 都连不上时，串接 Bybit（国内直连）/ Bitstamp（国际备用）作为实时兜底，最后才退到 DB。
# 任一源成功即返回，不再连环失败。OKX 已有专用路径（黄金/白银），这里只补 BTC/ETH/SOL。
_BINANCE_ALT_SYMBOLS: Dict[str, Dict[str, str]] = {
    "BTCUSDT": {"bybit": "BTCUSDT",  "bitstamp": "btcusd"},
    "ETHUSDT": {"bybit": "ETHUSDT",  "bitstamp": "ethusd"},
    "SOLUSDT": {"bybit": "SOLUSDT",  "bitstamp": "solusd"},
}


# ── v221.7 — 分页拉取（30 天 K 线 / 跨周历史）──────────────────────────────────────
# 币安 / Bybit 单次 K 线上限 1000，要拉 30d 10m（≈4320 bars）必须分页。
# 优先 Bybit（国内可直连，endTime cursor 支持好）→ 币安现货 → 库内回退。
# v221.7: _history_sync 外层缓存（含 anomalies 计算结果）— 30 天 scan_anomalies ~10s




def _bybit_paged(
    symbol: str, interval: str, total_bars: int, end_ts: Optional[int],
    interval_ms: int, seen_ts: set,
) -> List[Dict[str, Any]]:
    """Bybit v5 分页拉取，endTime cursor 向历史回溯。"""
    b_interval = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15",
        "1h": "60", "4h": "240", "1d": "D",
    }.get(interval, "60")
    PAGE = 1000
    bars: List[Dict[str, Any]] = []
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = True
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        cursor_end = int(end_ts * 1000) if end_ts else None
        attempts = 0
        while len(bars) < total_bars and attempts < 12:
            attempts += 1
            params = (f"category=linear&symbol={symbol}"
                      f"&interval={b_interval}&limit={PAGE}")
            if cursor_end is not None:
                params += f"&end={cursor_end}"
            url = f"https://api.bybit.com/v5/market/kline?{params}"
            resp = s.get(url, timeout=8)
            if resp.status_code != 200:
                break
            payload = resp.json() or {}
            rows = (payload.get("result") or {}).get("list") or []
            if not rows:
                break
            page_added = 0
            oldest_ms = cursor_end
            for k in rows:
                try:
                    ts_ms = int(k[0])
                    o = float(k[1]); h = float(k[2])
                    lo = float(k[3]); c = float(k[4])
                except (TypeError, ValueError, IndexError):
                    continue
                if min(o, h, lo, c) <= 0:
                    continue
                if ts_ms in seen_ts:
                    continue
                seen_ts.add(ts_ms)
                bars.append({
                    "time": ts_ms // 1000,
                    "open": round(o, 4), "high": round(h, 4),
                    "low": round(lo, 4), "close": round(c, 4),
                    "volume": int(float(k[5] or 0)),
                })
                page_added += 1
                if oldest_ms is None or ts_ms < oldest_ms:
                    oldest_ms = ts_ms
            bars.sort(key=lambda b: b["time"])
            if page_added == 0:
                break
            cursor_end = oldest_ms - interval_ms
        return bars[-total_bars:]
    except Exception:
        return bars[-total_bars:] if bars else []


def _binance_spot_paged(
    symbol: str, interval: str, total_bars: int, end_ts: Optional[int],
    interval_ms: int, seen_ts: set,
) -> List[Dict[str, Any]]:
    """Binance 现货分页拉取（data-api.binance.vision，国内直连）。"""
    b_interval = interval
    PAGE = 1000
    bars: List[Dict[str, Any]] = []
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        cursor_end = int(end_ts * 1000) if end_ts else None
        attempts = 0
        while len(bars) < total_bars and attempts < 12:
            attempts += 1
            params = f"symbol={symbol}&interval={b_interval}&limit={PAGE}"
            if cursor_end is not None:
                params += f"&endTime={cursor_end}"
            url = f"https://data-api.binance.vision/api/v3/klines?{params}"
            resp = s.get(url, timeout=8)
            if resp.status_code != 200:
                break
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            page_added = 0
            oldest_ms = cursor_end
            for k in data:
                try:
                    ts_ms = int(k[0])
                    o = float(k[1]); h = float(k[2])
                    lo = float(k[3]); c = float(k[4])
                except (TypeError, ValueError, IndexError):
                    continue
                if min(o, h, lo, c) <= 0:
                    continue
                if ts_ms in seen_ts:
                    continue
                seen_ts.add(ts_ms)
                bars.append({
                    "time": ts_ms // 1000,
                    "open": round(o, 4), "high": round(h, 4),
                    "low": round(lo, 4), "close": round(c, 4),
                    "volume": int(float(k[7] or 0)),
                })
                page_added += 1
                if oldest_ms is None or ts_ms < oldest_ms:
                    oldest_ms = ts_ms
            bars.sort(key=lambda b: b["time"])
            if page_added == 0:
                break
            cursor_end = oldest_ms - interval_ms
        return bars[-total_bars:]
    except Exception:
        return bars[-total_bars:] if bars else []


def _bybit_klines_bars(symbol: str, interval: str) -> Optional[tuple]:
    """Bybit v5 线性永续 K 线（api.bybit.com，国内可直连，3s 缓存）。

    interval 支持 1/3/5/15/60/240/D（Bybit 粒度：1/3/5/15/30/60/120/240/360/720/D/W）。
    返回与 Binance bars 同构的 (bars, last)。失败 → None，调用方继续下一源。
    """
    b_interval = {"1m": "1", "3m": "3", "5m": "5", "10m": "5", "15m": "15",
                  "1h": "60", "4h": "240", "1d": "D"}.get(interval, "60")
    limit = {"1m": 200, "3m": 200, "5m": 200, "10m": 200, "15m": 200,
             "1h": 200, "4h": 200, "1d": 200}.get(interval, 200)
    key = f"bybit:{symbol}:{b_interval}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = True
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        url = (f"https://api.bybit.com/v5/market/kline?category=linear"
               f"&symbol={symbol}&interval={b_interval}&limit={limit}")
        resp = s.get(url, timeout=3)
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            return None
        bars: List[Dict[str, Any]] = []
        for k in rows:
            try:
                o = float(k[1]); h = float(k[2]); lo = float(k[3]); c = float(k[4])
                ts_ms = int(k[0])
            except (TypeError, ValueError, IndexError):
                continue
            if min(o, h, lo, c) <= 0:
                continue
            bars.append({
                "time": ts_ms // 1000,
                "open": round(o, 4), "high": round(h, 4),
                "low": round(lo, 4), "close": round(c, 4),
                "volume": int(float(k[5] or 0)),
            })
        if not bars:
            return None
        bars.sort(key=lambda b: b["time"])  # Bybit 默认倒序，升序输出
        last = bars[-1]
        _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
        return bars, last
    except Exception:
        return None


def _bitstamp_klines_bars(symbol: str, interval: str) -> Optional[tuple]:
    """Bitstamp 现货 K 线（国际备用，3s 缓存，限频紧 600 req/10min）。

    step 取值：60/180/300/900/1800/3600/7200/14400/21600/43200/86400/259200。
    返回与 Binance bars 同构的 (bars, last)。失败 → None。
    """
    step = {"1m": 60, "3m": 180, "5m": 300, "10m": 300, "15m": 900,
            "1h": 3600, "4h": 14400, "1d": 86400}.get(interval, 3600)
    limit = {"1m": 60, "3m": 60, "5m": 60, "10m": 60, "15m": 60,
             "1h": 60, "4h": 60, "1d": 60}.get(interval, 60)
    key = f"bitstamp:{symbol}:{step}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = True
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        url = f"https://www.bitstamp.net/api/v2/ohlc/{symbol}/?step={step}&limit={limit}"
        resp = s.get(url, timeout=3)
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        rows = (payload.get("data") or {}).get("ohlc") or []
        if not rows:
            return None
        bars: List[Dict[str, Any]] = []
        for k in rows:
            try:
                ts_str = k.get("timestamp")
                o = float(k.get("open") or 0)
                h = float(k.get("high") or 0)
                lo = float(k.get("low") or 0)
                c = float(k.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if min(o, h, lo, c) <= 0:
                continue
            try:
                ts = int(ts_str)
            except (TypeError, ValueError):
                continue
            bars.append({
                "time": ts,  # Bitstamp timestamp 已是秒级
                "open": round(o, 4), "high": round(h, 4),
                "low": round(lo, 4), "close": round(c, 4),
                "volume": int(float(k.get("volume") or 0)),
            })
        if not bars:
            return None
        bars.sort(key=lambda b: b["time"])
        last = bars[-1]
        _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
        return bars, last
    except Exception:
        return None


def _okx_klines_bars(inst_id: str, interval: str) -> Optional[tuple]:
    """OKX 永续真实 K 线 → 与币安 bars 同构（time 秒级、volume=成交量）。5s 缓存。

    interval 支持 1m/3m/5m/15m/1h/4h/1d（OKX 原生）；10m 非原生 → 上层用 5m 聚合。
    """
    bar = quote_sources.okx_bar(interval)
    # v220: bumped 5m from 288 -> 1440 to support 10m x 720 bars (~5 days) on chart
    limit = {"1m": 720, "3m": 288, "5m": 1440, "15m": 96, "1h": 48, "4h": 36, "1d": 120}.get(interval, 48)
    key = f"okx:{inst_id}:{bar}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    rows = quote_sources.fetch_okx_candles(inst_id, bar, limit)
    if not rows:
        return None
    bars: List[Dict[str, Any]] = []
    for r in rows:
        # rows: [ts_ms, open, high, low, close, volume]
        bars.append({
            "time": int(int(r[0]) // 1000),
            "open": round(r[1], 4), "high": round(r[2], 4),
            "low": round(r[3], 4), "close": round(r[4], 4),
            "volume": int(r[5]),
        })
    last = bars[-1]
    _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
    return bars, last
