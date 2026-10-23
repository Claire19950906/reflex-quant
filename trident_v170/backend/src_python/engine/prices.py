"""Unified price sources — Gold (Sina/EastMoney) + BTC (Binance REST) + WTI.

Leaf module: stdlib + requests (BTC 源须用 requests，Binance WAF 会重置 urllib 连接).
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple

import requests

# 直连 opener：绕过 env HTTP_PROXY。本地网络直连 Binance/Sina/EastMoney 通畅，
# 走代理反而超时/失败（代理节点对这些域名不友好）。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 币安系请求共享 Session（connect-only 重试，缓解 LetsVPN 首握手抖动）。
# fapi 走 env 代理（trust_env=True，币安 fapi 直连被墙）；vision 强制直连（trust_env=False）。
_FAPI_SESSION: Any = None
_VISION_SESSION: Any = None


def _fapi_session() -> Any:
    global _FAPI_SESSION
    if _FAPI_SESSION is None:
        from http_retry import new_retry_session  # 延迟 import 避免启动期 import 顺序问题
        _FAPI_SESSION = new_retry_session(trust_env=True)
    return _FAPI_SESSION


def _vision_session() -> Any:
    global _VISION_SESSION
    if _VISION_SESSION is None:
        from http_retry import new_retry_session
        _VISION_SESSION = new_retry_session(trust_env=False)
    return _VISION_SESSION


# ---------------------------------------------------------------------------
# Unified price sources — Gold (Sina/EastMoney) + BTC (Binance REST)
# ---------------------------------------------------------------------------

def _fetch_sina_xau() -> float | None:
    try:
        req = urllib.request.Request(
            "https://hq.sinajs.cn/list=hf_XAU",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        resp = _NO_PROXY_OPENER.open(req, timeout=5)
        text = resp.read().decode("gbk", errors="replace")
        if '="' in text:
            return float(text.split('="')[1].split(",")[0])
    except Exception:
        pass
    return None


def _fetch_eastmoney_xau() -> float | None:
    try:
        req = urllib.request.Request(
            "https://push2.eastmoney.com/api/qt/stock/get?secid=113.USDXAU&fields=f43",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        )
        resp = _NO_PROXY_OPENER.open(req, timeout=5)
        body = json.loads(resp.read().decode("utf-8"))
        return float(body["data"]["f43"]) / 100.0
    except Exception:
        return None


def _fetch_gold_price() -> float | None:
    """黄金价格 — OKX 永续 XAU-USDT-SWAP（真实合约，国内直连）。失败回退国内现货。"""
    try:
        import quote_sources  # 延迟 import 避免 engine 包内循环
        inst = quote_sources.symbol_of("XAU")
        if inst:
            t = quote_sources.fetch_okx_ticker(inst)
            p = t.get("last") if t else None
            if p is not None and 500 < p < 10000:
                return p
    except Exception:
        pass
    return _fetch_gold_price_domestic()


def _fetch_gold_price_domestic() -> float | None:
    for fn in [_fetch_sina_xau, _fetch_eastmoney_xau]:
        p = fn()
        if p is not None and 500 < p < 10000:
            return p
    return None


def _fetch_vision_price(symbol: str) -> float | None:
    """binance.vision 官方数据镜像 — 国内可直连（fapi/api.binance.com 直连被墙，
    且依赖代理不稳定）。用 requests + 强制直连，不读 env 代理。"""
    try:
        session = _vision_session()
        resp = session.get(
            f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        data = resp.json()
        p = float(data.get("price", 0))
        return p if p > 0 else None
    except Exception:
        return None


def _fetch_binance_btc() -> float | None:
    return _fetch_vision_price("BTCUSDT")


def _fetch_fapi_price(symbol: str) -> float | None:
    """币安永续最新价（fapi.binance.com，走 env HTTP_PROXY 代理）。

    统一全品种交易/行情取价用币安永续 —— 与图表 K 线同一市场口径，
    图上标注（入场/止损/出场）与真实成交价不再错位。
    """
    try:
        session = _fapi_session()
        resp = session.get(
            f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        data = resp.json()
        p = float(data.get("price", 0))
        return p if p > 0 else None
    except Exception:
        return None


def _fapi_or_fallback(symbol: str, asset_label: str, fallback_fn, valid_range=(0.0, 1e18)) -> float | None:
    """币安永续取价 + 醒目降级日志：fapi 失败 → 告警 + 回退源。

    symbol: 币安加密永续对（BTCUSDT/ETHUSDT/SOLUSDT…）
    asset_label: 日志用的品种名
    fallback_fn: 无参回退函数（国内金价/油价/现货镜像）
    """
    p = _fetch_fapi_price(symbol)
    lo, hi = valid_range
    if p is not None and lo < p < hi:
        return p
    fb = fallback_fn() if callable(fallback_fn) else None
    if fb is not None and lo < fb < hi:
        print(
            f"  [PRICE] ⚠️ 币安永续 {symbol} 不可用（代理/网络失败），"
            f"{asset_label} 降级用回退源报价 {fb} —— 与合约成交价有偏差，请检查代理",
            flush=True,
        )
        return fb
    if p is None:
        print(
            f"  [PRICE] ❌ 币安永续 {symbol} 与回退源都取不到 {asset_label} 价格",
            flush=True,
        )
    return None


def _fetch_wti_price() -> float | None:
    """WTI 原油价格 — 国内现货（新浪/东财）。币安/OKX 均无 WTI 永续合约，不再映射假对。"""
    return _fetch_wti_price_domestic()


def _fetch_wti_price_domestic() -> float | None:
    """回退：国内新浪/东财 WTI 连续合约价（币安永续不可用时的兜底）。"""
    # Tier 1: Sina Finance (hf_CL continuous contract, China-accessible)
    try:
        req = urllib.request.Request(
            "https://hq.sinajs.cn/list=hf_CL",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        resp = _NO_PROXY_OPENER.open(req, timeout=5)
        text = resp.read().decode("gbk", errors="replace")
        if '="' in text:
            p = float(text.split('="')[1].split(",")[0])
            if 20 < p < 200:
                return p
    except Exception:
        pass

    # Tier 2: EastMoney (try multiple known US futures secids for WTI)
    for secid in ("113.CL00Y", "113.CONC", "113.NYMEX_CL"):
        try:
            req = urllib.request.Request(
                f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            )
            resp = _NO_PROXY_OPENER.open(req, timeout=5)
            body = json.loads(resp.read().decode("utf-8"))
            data = body.get("data")
            if data and data.get("f43") is not None:
                p = float(data["f43"]) / 100.0
                if 20 < p < 200:
                    return p
        except Exception:
            continue

    return None


def _get_current_price(asset: str) -> float | None:
    asset_upper = asset.upper()
    # 按资产路由到真实数据源：XAU→OKX 永续 XAU-USDT-SWAP、BTC/ETH/SOL→币安永续、
    # WTI→国内现货（无永续合约）；各自失败回退国内/现货镜像，不再映射假币安对。
    if asset_upper in ("XAU", "GOLD", "XAUUSD"):
        return _fetch_gold_price()
    if asset_upper in ("BTC", "BTCUSDT"):
        return _fapi_or_fallback("BTCUSDT", "BTC", lambda: _fetch_vision_price("BTCUSDT"))
    if asset_upper in ("ETH", "ETHUSDT"):
        return _fapi_or_fallback("ETHUSDT", "ETH", lambda: _fetch_vision_price("ETHUSDT"))
    if asset_upper in ("SOL", "SOLUSDT"):
        return _fapi_or_fallback("SOLUSDT", "SOL", lambda: _fetch_vision_price("SOLUSDT"))
    if asset_upper in ("WTI", "WTI/USD", "OIL"):
        return _fetch_wti_price()
    # 美股（币安永续 fapi）：模拟盘若对美股建仓/结算，统一取永续价（与行情/K线口径一致）
    try:
        from stock_prices import asset_price as _stock_price
        sp = _stock_price(asset)
        if sp is not None:
            return sp
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 带 TTL 的缓存价格（高频轮询场景用：如 5s 移动止损检查，避免打爆行情源）
# ---------------------------------------------------------------------------

_PRICE_CACHE: Dict[str, Tuple[float, float]] = {}  # asset -> (price, fetched_at)
_PRICE_CACHE_TTL = 2.0


def _get_current_price_cached(asset: str, ttl: float = _PRICE_CACHE_TTL) -> Optional[float]:
    """TTL 秒内命中缓存，过期才重新抓取。同一资产在 ttl 窗口内最多一次网络请求。"""
    key = (asset or "").upper()
    now = time.time()
    hit = _PRICE_CACHE.get(key)
    if hit and now - hit[1] < ttl:
        return hit[0]
    price = _get_current_price(asset)
    if price is not None:
        _PRICE_CACHE[key] = (price, now)
    return price
