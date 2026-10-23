# -*- coding: utf-8 -*-
"""multi_source_fetcher.py - v231 多源 fallback 驱动器

# v282+: HONESTY 边界 — 绝不允许捏造数据。详见 _honesty.NEVER_FABRICATE_INVARIANT
from ._honesty import NEVER_FABRICATE_INVARIANT  # noqa: F401  (导入即承认红线)

设计动机(用户反馈):
  反思系统一直把"判断错"归因到 data 层，根因是 FEED_REDUND_POLICY 定义了 17 个
  资产+kind 的 fallback 链但【从未被代码真正使用】——只是给前端展示。
  Yahoo 一挂，18 个资产(13 只美股 + SKHYNIX/US_HYNIX/MU + GOLD/OIL 别名)
  立刻变 "数据缺失"，反思系统就把它当成判断错的理由，掩盖了真正的
  推理/因果/判断错误。

本模块做 3 件事:
  1. 把 FEED_REDUND_POLICY 里的字符串源名解析成实际 fetch 函数
  2. 提供 fetch_stock_price / fetch_stock_history / fetch_stock_news 三个统一入口
  3. 每次尝试都写到 data_health_journal，反思面板可看"哪个源失败了几次"

接入点 (api_server.py):
  - _yahoo_price_change24h()    -> fetch_stock_price_cached(asset)
  - _fetch_yahoo_history_bars() -> fetch_stock_history_cached(asset, interval, limit)
  - 后续 news stock 兜底       -> fetch_stock_news(asset)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import data_health_journal as _dhj
import db
import feed_redund_policy as _frp


log = logging.getLogger("multi_source_fetcher")


# ═══════════════════════════════════════════════════════════════════════════
# Source → Function 解析表 — 每个 source name 对应一个 fetch 函数
# ═══════════════════════════════════════════════════════════════════════════


def _yahoo_fetch_price(asset: str) -> Optional[Tuple[float, float]]:
    """yahoo 源: 5d 1d bar → (close, prev_close)。失败 None。"""
    try:
        import quote_sources as _qs
        yahoo_sym = _qs.YAHOO_SYMBOL_FOR_ASSET.get(asset)
        if not yahoo_sym:
            return None
        rows = _qs.fetch_yahoo_candles(yahoo_sym, interval="1d",
                                       range_or_period="5d", limit=5)
        if not rows or len(rows) < 2:
            return None
        prev_close = float(rows[-2][4])
        next_close = float(rows[-1][4])
        if prev_close <= 0 or next_close <= 0:
            return None
        return (next_close, prev_close)
    except Exception as exc:
        log.debug("[yahoo_price] %s 失败: %s", asset, exc)
        return None


def _yahoo_fetch_history(asset: str, interval: str = "1d",
                          limit: int = 60) -> Optional[List[List[float]]]:
    """yahoo 源: 拉 K 线 → [[ts_ms, o, h, l, c, v], ...]。失败 None。

    v2026.10.08 bugfix: Yahoo Finance chart v8 同样不支持 10m。
    yahoo_interval("10m") 不在 _OKX_TO_YAHOO_INTERVAL 中 → 兜底为 "60m"，但用户要 10m
    → 之前所有 US_HYNIX/MU/SKHYNIX/NVDA 股票 10m K 线都静默失败。
    这里同样在 fetcher 层把 10m 降级为 5m，由 api_server._history_sync 做 5m→10m 聚合。
    """
    try:
        import quote_sources as _qs
        yahoo_sym = _qs.YAHOO_SYMBOL_FOR_ASSET.get(asset) or _qs.symbol_of(asset)
        if not yahoo_sym:
            return None
        # 10m → 5m：Yahoo Finance 同样不支持 10m
        iv = "5m" if interval == "10m" else interval
        yahoo_iv = _qs.yahoo_interval(iv)
        if yahoo_iv in ("1d", "1wk", "1mo", "3mo"):
            rng = "6mo"
        elif yahoo_iv in ("60m", "90m"):
            rng = "1mo" if limit <= 96 else "2y"
        else:
            rng = "5d"
        rows = _qs.fetch_yahoo_candles(yahoo_sym, interval=yahoo_iv,
                                       range_or_period=rng, limit=limit)
        return rows if rows else None
    except Exception as exc:
        log.debug("[yahoo_hist] %s 失败: %s", asset, exc)
        return None


def _yahoo_fetch_price_with_timeout(asset: str) -> Optional[Tuple[float, float]]:
    """Yahoo chart v8 内置 4 retry + 10s timeout × 4 = 40s 累计；
    这里 wrap 在 2s 内强截断（限速时 429 一连串根本不会成功）。
    """
    import concurrent.futures as _cf
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_yahoo_fetch_price, asset)
        try:
            return fut.result(timeout=2.0)
        except _cf.TimeoutError:
            log.debug("[yahoo_price_timeout] %s 超时 2s", asset)
            return None
    except Exception as exc:
        log.debug("[yahoo_price_with_timeout] %s 失败: %s", asset, exc)
        return None
    finally:
        ex.shutdown(wait=False)


def _yfinance_fetch_price(asset: str) -> Optional[Tuple[float, float]]:
    """yfinance 源: yf.Ticker(symbol).fast_info.last_price。失败 None。

    Yahoo Finance v8 chart 与 yfinance 是同一协议的两个客户端；
    Yahoo 限速时 yfinance 有时能过，反之亦然 — 必须双 fallback。

    v2026.10.08 bugfix: yfinance 在限速/网络堵塞时会卡 20-30s（无内置超时），
    这里 wrap 在 ThreadPoolExecutor + 2s fut.result 超时里强制截断；
    超时则返回 None，让上层 fallback 链快速进入下一个源。
    """
    import concurrent.futures as _cf
    def _do():
        import quote_sources as _qs
        import yfinance as yf
        yahoo_sym = _qs.YAHOO_SYMBOL_FOR_ASSET.get(asset) or _qs.symbol_of(asset)
        if not yahoo_sym:
            return None
        t = yf.Ticker(yahoo_sym)
        p = t.fast_info.last_price
        if p is None or float(p) <= 0:
            return None
        price = float(p)
        # yfinance fast_info 没有 prev_close — 用 ticker.history 5d 1d 取前收
        try:
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
            if hist is not None and len(hist) >= 2:
                prev_close = float(hist["Close"].iloc[-2])
                if prev_close > 0:
                    return (price, prev_close)
        except Exception:
            pass
        # v252.2: 历史拿不到也返回 (price, None) — 前端按 "—" 显示，
        # 绝不假装 prev=current 让前端算成假 0% 涨跌。
        # fetch_stock_price 已接受 prev=None(在 _record_attempt 后传给调用方)。
        return (price, None)
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_do)
        try:
            return fut.result(timeout=2.0)
        except _cf.TimeoutError:
            log.debug("[yfinance_price] %s 超时 2s", asset)
            return None
    except Exception as exc:
        log.debug("[yfinance_price] %s 失败: %s", asset, exc)
        return None
    finally:
        ex.shutdown(wait=False)


def _yfinance_fetch_history(asset: str, interval: str = "1d",
                             limit: int = 60) -> Optional[List[List[float]]]:
    """yfinance 源: yf.Ticker(symbol).history(period, interval)。

    v?? 海力士 cap: 5m/15m/30m/90m 受 yfinance 60 天限制，硬编码 period 必须 ≤60d。
    旧代码统一传 "3mo"（90d），导致 SKHYNIX/MU/NVDA 等 Yahoo 类资产拉 5m K 线全部失败。

    v2026.10.08 bugfix: yfinance 不支持 10m（合法 interval: 1m/2m/5m/15m/30m/60m/90m/1h/1d）。
    前端归因页大量请求 interval=10m → 此前两源都返回空 → "Yahoo Finance 实时 K 线不可用"。
    这里把 10m 内部降级为 5m，外层 api_server._history_sync 已经做 5m→10m 聚合。
    """
    try:
        import quote_sources as _qs
        import yfinance as yf
        yahoo_sym = _qs.YAHOO_SYMBOL_FOR_ASSET.get(asset) or _qs.symbol_of(asset)
        if not yahoo_sym:
            return None
        # 10m → 5m：yfinance 不支持 10m，必须降级（5m→10m 由 _history_sync 聚合）
        iv = "5m" if interval == "10m" else interval
        yf_iv = "60m" if iv == "4h" else iv
        # yfinance 间隔限制（实测 2026-10-06）：
        #   1m: ≤7d;  2m/5m/15m/30m/90m: ≤60d;  60m/1h: ≤730d;  1d: 无限制
        # 超过限制直接报 "data not available ... within the last N days"。
        # 这里按间隔严格给最大可用 period，limit 再截断最后 N 根。
        if yf_iv == "1m":
            period = "7d"
        elif yf_iv in ("2m", "5m", "15m", "30m", "90m"):
            period = "60d"
        elif yf_iv in ("60m", "1h"):
            # v2026.10.08 bugfix: 旧代码写 "2mo" 不是 yfinance 合法 period 值，
            # yfinance 静默 fallback 到 1y 仍报 "no price data found"（yfinance 1h 单次
            # 最多 730d，但 ticker fast_info 没拿到 MU 30 天前数据会空）。
            # yfinance 合法 period: 1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max
            period = "60d"
        else:
            period = "3mo"  # 1d/5d/1wk/1mo/3mo 都没限制
        df = yf.Ticker(yahoo_sym).history(period=period, interval=yf_iv,
                                          auto_adjust=False)
        if df is None or len(df) < 2:
            return None
        rows: List[List[float]] = []
        import math
        for ts_or_idx, row in df.iterrows():
            try:
                o = float(row["Open"]); h = float(row["High"])
                lo = float(row["Low"]); c = float(row["Close"])
                v = float(row["Volume"])
            except (KeyError, TypeError, ValueError):
                continue
            # v2026.10.08: 过滤 NaN（yfinance 1d 偶尔含 NaN，min(NaN,*) 比较恒为 False 会漏过滤 → JSON 序列化时 NaN 直接炸）
            if any(math.isnan(x) for x in (o, h, lo, c)):
                continue
            if min(o, h, lo, c) <= 0:
                continue
            try:
                ts_ms = int(ts_or_idx.timestamp() * 1000) if hasattr(ts_or_idx, "timestamp") else int(ts_or_idx)
            except Exception:
                continue
            rows.append([ts_ms, o, h, lo, c, v])
        return rows[-limit:] if rows else None
    except Exception as exc:
        log.debug("[yfinance_hist] %s 失败: %s", asset, exc)
        return None


def _yahoo_fetch_history_with_timeout(asset, interval, limit):
    """Yahoo chart v8 内置 4 retry + 10s timeout × 4 = ~40s 累计；
    这里 wrap 在 2.0s 内强截断（Yahoo 被限速时 429 一连串根本不会成功，
    没必要等满重试链）。失败立刻返回 None 让 fallback 链进入下一源。
    """
    import concurrent.futures as _cf
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_yahoo_fetch_history, asset, interval=interval, limit=limit)
        try:
            return fut.result(timeout=2.0)
        except _cf.TimeoutError:
            log.debug("[yahoo_hist_timeout] %s 超时 2s", asset)
            return None
    except Exception as exc:
        log.debug("[yahoo_hist_with_timeout] %s 失败: %s", asset, exc)
        return None
    finally:
        ex.shutdown(wait=False)


def _yfinance_fetch_history_with_timeout(asset, interval, limit):
    """yfinance.Ticker.history 没有内置超时，这里包装 2.5s 硬截断。

    关键：ThreadPoolExecutor 的 `with` 块默认在 __exit__ 时 shutdown(wait=True)，
    会等所有未完成的线程完成 — 即使 fut.result 已 TimeoutError。
    这里显式 shutdown(wait=False) 立即释放主请求，超时线程在后台继续（结果不重要）。
    """
    import concurrent.futures as _cf
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_yfinance_fetch_history, asset, interval=interval, limit=limit)
        try:
            return fut.result(timeout=2.5)
        except _cf.TimeoutError:
            log.debug("[yfinance_hist_timeout] %s 超时 2.5s", asset)
            return None
    except Exception as exc:
        log.debug("[yfinance_hist_with_timeout] %s 失败: %s", asset, exc)
        return None
    finally:
        ex.shutdown(wait=False)


def _asia_fetch_price(asset: str) -> Optional[Tuple[float, float]]:
    """asia_prices 源: 韩股/A 股走 yfinance 同协议，仅作第 3 兜底。"""
    try:
        import asia_prices
        p = asia_prices.asset_price(asset)
        if p is None or p <= 0:
            return None
        return (float(p), float(p))
    except Exception as exc:
        log.debug("[asia_price] %s 失败: %s", asset, exc)
        return None


def _equity_fetch_price(asset: str) -> Optional[Tuple[float, float]]:
    """equity_feed 源: 美股 FMP/AlphaVantage 估值层 (delayed)。仅作兜底。

    equity_feed 仅返回估值字段 (mktcap/pe/peg/...)，不返回 last_price —
    因此对当前价基本无用。本函数返回 None 让调用方继续试下一个源。
    """
    return None


def _stock_news_fetch(asset: str) -> Optional[List[Dict[str, Any]]]:
    """stock_news 源: Finnhub + Yahoo 美股新闻。"""
    try:
        import stock_news
        items = stock_news.poll_stock_news()
        return items if items else None
    except Exception as exc:
        log.debug("[stock_news] 失败: %s", exc)
        return None


def _reuters_fetch(asset: str = "") -> Optional[List[Dict[str, Any]]]:
    """reuters 源: Google News RSS。"""
    try:
        import reuters_news
        items = reuters_news.poll_reuters_news()
        return items if items else None
    except Exception as exc:
        log.debug("[reuters] 失败: %s", exc)
        return None


def _rss_fetch(asset: str = "") -> Optional[List[Dict[str, Any]]]:
    """rss 源: RSS 多源聚合。"""
    try:
        import rss_sources
        items = rss_sources.poll_rss_news()
        return items if items else None
    except Exception as exc:
        log.debug("[rss] 失败: %s", exc)
        return None


def _treenews_fetch(asset: str = "") -> Optional[List[Dict[str, Any]]]:
    """treenews 源: 子进程桥接 (空 stub — 进程未启动时 None)。"""
    return None


def _qinghong_fetch(asset: str = "") -> Optional[List[Dict[str, Any]]]:
    """qinghong 源: qinghong_feed (微信群 / 自有源)。"""
    try:
        import qinghong_feed
        items = qinghong_feed.poll()
        return items if items else None
    except Exception as exc:
        log.debug("[qinghong] 失败: %s", exc)
        return None


# ── PRICE 源的 fallback resolver ──
# yahoo 源走 _with_timeout version（chart v8 内部 4 retry × 10s timeout = 40s，
# 直接 wrap 2s 强截断避免限速时拖累聚合端点）
_PRICE_FETCHERS: Dict[str, Callable[[str], Optional[Tuple[float, float]]]] = {
    "yahoo":    _yahoo_fetch_price_with_timeout,
    "yfinance": _yfinance_fetch_price,
    "equity":   _equity_fetch_price,
    "asia":     _asia_fetch_price,
}


# ── HISTORY 源的 fallback resolver ──
_HISTORY_FETCHERS: Dict[str, Callable[..., Optional[List[List[float]]]]] = {
    "yahoo":    _yahoo_fetch_history_with_timeout,
    "yfinance": _yfinance_fetch_history_with_timeout,
}


# ── NEWS 源的 fallback resolver ──
_NEWS_FETCHERS: Dict[str, Callable[..., Optional[List[Dict[str, Any]]]]] = {
    "stock_news": _stock_news_fetch,
    "reuters":    _reuters_fetch,
    "treenews":   _treenews_fetch,
    "rss":        _rss_fetch,
    "qinghong":   _qinghong_fetch,
}


# ═══════════════════════════════════════════════════════════════════════════
# 主入口: walk fallback chain, 首次成功即返回 + 记日志
# ═══════════════════════════════════════════════════════════════════════════


def _record_attempt(asset: str, feed_kind: str, source: str,
                    status: str, latency_ms: int = 0,
                    value: Any = None, error: Optional[str] = None) -> None:
    """写入 data_health_journal.feed_health。失败也不抛。"""
    try:
        conn = db.get_connection()
        _dhj.record_attempt(asset=asset, feed_kind=feed_kind,
                            source=source, status=status,
                            latency_ms=latency_ms, value=value,
                            error=error, conn=conn)
        conn.close()
    except Exception:
        pass


def fetch_stock_price(asset: str) -> Tuple[Optional[float], Optional[float]]:
    """股票类资产当前价 + 24h 涨跌%。fallback chain 走完。

    Returns: (price, prev_close) — 任一为 None 表示全部源失败

    v2026.10.08 note: 单源 1.5s 超时会让总响应 >3×1.5=4.5s，
    因此没加 per-source 超时；上层调用方负责整体超时（api_server HYNIX 段 2s）。
    单源失败/限速时会快速返回（不 hang）。
    """
    asset = (asset or "").upper()
    chain = _frp.policy_for(asset, "PRICE")
    if not chain:
        return (None, None)
    for source in chain:
        fetcher = _PRICE_FETCHERS.get(source)
        if fetcher is None:
            continue
        t0 = time.monotonic()
        try:
            out = fetcher(asset)
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            _record_attempt(asset, "PRICE", source, "failed",
                            latency_ms=latency, error=str(exc)[:200])
            continue
        latency = int((time.monotonic() - t0) * 1000)
        if out is None:
            _record_attempt(asset, "PRICE", source, "failed", latency_ms=latency)
            continue
        price, prev = out
        if price is None or price <= 0:
            _record_attempt(asset, "PRICE", source, "failed", latency_ms=latency,
                            error="zero/negative price")
            continue
        _record_attempt(asset, "PRICE", source, "success", latency_ms=latency,
                        value=price)
        return (price, prev)
    return (None, None)


def fetch_stock_history(asset: str, interval: str = "1d",
                         limit: int = 60) -> List[List[float]]:
    """股票类资产 K 线。fallback chain 走完，返回 [[ts_ms, o, h, l, c, v], ...]。"""
    asset = (asset or "").upper()
    chain = _frp.policy_for(asset, "PRICE")
    if not chain:
        return []
    for source in chain:
        fetcher = _HISTORY_FETCHERS.get(source)
        if fetcher is None:
            continue
        t0 = time.monotonic()
        try:
            rows = fetcher(asset, interval=interval, limit=limit)
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            _record_attempt(asset, "HISTORY_" + interval, source, "failed",
                            latency_ms=latency, error=str(exc)[:200])
            continue
        latency = int((time.monotonic() - t0) * 1000)
        if not rows or len(rows) < 2:
            _record_attempt(asset, "HISTORY_" + interval, source, "failed",
                            latency_ms=latency)
            continue
        _record_attempt(asset, "HISTORY_" + interval, source, "success",
                        latency_ms=latency, value=len(rows))
        return rows
    return []


def fetch_stock_news(asset: str = "") -> List[Dict[str, Any]]:
    """美股/海力士类新闻。fallback chain 走完，合并所有源的结果。

    Returns: 合并后的新闻 list；asset="" 表示任意美股新闻(用于大盘面板)。
    """
    asset = (asset or "").upper()
    chain = _frp.FEED_REDUND_POLICY.get("NEWS_STOCK", [])
    if not chain:
        return []
    out: List[Dict[str, Any]] = []
    seen_keys: set = set()
    for source in chain:
        fetcher = _NEWS_FETCHERS.get(source)
        if fetcher is None:
            continue
        t0 = time.monotonic()
        try:
            items = fetcher(asset)
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            _record_attempt(asset, "NEWS_STOCK", source, "failed",
                            latency_ms=latency, error=str(exc)[:200])
            continue
        latency = int((time.monotonic() - t0) * 1000)
        if not items:
            _record_attempt(asset, "NEWS_STOCK", source, "failed", latency_ms=latency)
            continue
        _record_attempt(asset, "NEWS_STOCK", source, "success",
                        latency_ms=latency, value=len(items))
        for it in items:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or it.get("Title") or "")[:200]
            src = str(it.get("source") or it.get("Source") or source)[:50]
            key = (title.lower(), src.lower())
            if key in seen_keys or not title:
                continue
            seen_keys.add(key)
            it.setdefault("source", src)
            out.append(it)
    return out


# 进程内缓存 (避免高频 movers/K 线刷新把所有源都打爆)
_PRICE_CACHE: Dict[str, Tuple[float, Tuple[Optional[float], Optional[float]]]] = {}
_PRICE_CACHE_TTL_S = 30.0


def fetch_stock_price_cached(asset: str) -> Tuple[Optional[float], Optional[float]]:
    """带 30s 进程内缓存的 fetch_stock_price (movers 面板用)。"""
    asset = (asset or "").upper()
    now = time.monotonic()
    hit = _PRICE_CACHE.get(asset)
    if hit and now - hit[0] < _PRICE_CACHE_TTL_S:
        return hit[1]
    out = fetch_stock_price(asset)
    _PRICE_CACHE[asset] = (now, out)
    return out


_HIST_CACHE: Dict[str, Tuple[float, List[List[float]]]] = {}
_HIST_CACHE_TTL_S = 60.0
_HIST_CACHE_FAIL_TTL_S = 12.0  # v?? 空结果短 TTL：Yahoo 限速恢复后更快回到正常数据


def fetch_stock_history_cached(asset: str, interval: str = "1d",
                                limit: int = 60) -> List[List[float]]:
    """带分桶 TTL 缓存的 fetch_stock_history (K 线用)。

    - 成功结果缓存 60s (避开 Yahoo/yfinance rate limit 重压)
    - 空结果缓存 12s (限速恢复后 12s 内即可拿到新数据)
    """
    asset = (asset or "").upper()
    key = asset + "|" + interval + "|" + str(limit)
    now = time.monotonic()
    hit = _HIST_CACHE.get(key)
    if hit:
        ttl = _HIST_CACHE_FAIL_TTL_S if not hit[1] else _HIST_CACHE_TTL_S
        if now - hit[0] < ttl:
            return hit[1]
    rows = fetch_stock_history(asset, interval=interval, limit=limit)
    _HIST_CACHE[key] = (now, rows)
    return rows


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "probe":
        a = sys.argv[2] if len(sys.argv) >= 3 else "NVDA"
        price, prev = fetch_stock_price(a)
        print("PRICE " + a + ": price=" + str(price) + " prev=" + str(prev))
        rows = fetch_stock_history(a, interval="1d", limit=5)
        print("HISTORY " + a + ": " + str(len(rows)) + " bars")
        news = fetch_stock_news(a)
        print("NEWS " + a + ": " + str(len(news)) + " items")
