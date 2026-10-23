#!/usr/bin/env python3
"""需求 8 —— 免费公开数据源统一聚合层（主源 → 备用源，3 秒超时自动切换）。

原则：
  1. 所有接口优先用无付费权限的公开地址；
  2. 单个源请求延迟超过 ``FALLBACK_TIMEOUT_S``（3 秒）自动切到下一个备用源；
  3. 对没有免费公开 API / 需注册 / 需付费的源，如实登记状态，绝不伪造接口、不做
     违反服务条款的爬虫。

失败一律返回 None 并调用 ``source_status.report`` 记录真实状态，供「数据源面板」标色。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    _HAS_REQUESTS = False

import source_status

# 需求 8：延迟超 3 秒自动切备用免费源
FALLBACK_TIMEOUT_S = 3.0

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 源接入状态：free=免费公开 / existing=既有模块已接入 / needs_key=需免费key /
# needs_registration=需账户 / no_public_api=无公开API / premium_tier=仅付费tier
SOURCE_STATUS_ENUM = (
    "free", "existing", "needs_key", "needs_registration", "no_public_api", "premium_tier",
)


def _registry() -> List[Dict[str, Any]]:
    rows = [
        # ── 行情 ──
        ("quote_binance", "行情", "Binance(现货/永续)", "existing", "加密价格/K线，data-api.binance.vision 国内直连"),
        ("quote_okx", "行情", "OKX 永续", "existing", "XAU/XAG/BTC 价格/K线/费率/OI，直连（GFW 阻断时熔断）"),
        ("quote_kucoin", "行情", "KuCoin 现货", "free", "加密价格/K线备用源，api.kucoin.com 免费公开"),
        ("quote_gate", "行情", "Gate.io 现货", "existing", "加密价格备用源（market_feeds 已接入）"),
        ("quote_bitget", "行情", "Bitget 现货", "existing", "加密价格备用源（market_feeds 已接入）"),
        ("quote_yahoo", "行情", "Yahoo Finance", "existing", "美股价格/估值兜底（stock_prices 已接入）"),
        ("quote_domestic", "行情", "新浪/东财现货", "existing", "黄金/原油现货价兜底（国内直连）"),
        ("quote_tradingview", "行情", "TradingView", "no_public_api", "无免费公开 API，仅网页/爬虫，不入库"),
        ("quote_oanda", "行情", "Oanda", "needs_registration", "需注册账户获得 token，无匿名公开接口"),
        ("quote_cme", "行情", "CME", "needs_registration", "期货实时/历史行情需注册，仅延迟日结算数据公开"),
        ("quote_investing", "行情", "Investing.com", "no_public_api", "无公开 API，爬虫违反 ToS，不入库"),

        # ── 资金流 / 持仓 ──
        ("flow_cftc", "资金流", "CFTC COT 持仓", "existing", "黄金/原油/日元期货持仓周报（已有接入）"),
        ("flow_bis", "资金流", "BIS 国际清算银行", "free", "汇率/跨境资金统计，stats.bis.org 免费 REST"),
        ("flow_coinglass", "资金流", "CoinGlass 衍生品", "premium_tier", "全网爆仓/OI/资金费率，V4 仅付费 plan，免费 tier 降级"),
        ("flow_spdr", "资金流", "SPDR GLD 持仓", "existing", "黄金 ETF 每日持仓吨数（已接入）"),
        ("flow_dtcc", "资金流", "DTCC 存管结算", "no_public_api", "核心结算/回购数据付费，无免费公开接口"),
        ("flow_blockchain", "资金流", "Blockchain.com 链上", "existing", "BTC 哈希率/难度（blockchain_feed 已接入）"),

        # ── 情绪 ──
        ("sent_vix", "情绪", "VIX 恐慌指数", "existing", "CBOE VIX（fred_feed 已接入）"),
        ("sent_cnn", "情绪", "CNN 恐惧贪婪指数", "free", "Fear & Greed 免费公开接口"),
        ("sent_marketwatch", "情绪", "MarketWatch 情绪", "no_public_api", "无公开 API，爬虫违反 ToS，不入库"),

        # ── 快讯 / 新闻 ──
        ("news_jin10", "快讯", "金十数据", "existing", "快讯/财经日历（jin10 已接入）"),
        ("news_fed", "快讯", "美联储/FRED", "existing", "利率/CPI/非农/FOMC（fred_feed 已接入）"),
        ("news_reuters", "快讯", "路透", "existing", "英文头条经翻译入库（reuters_news 已接入）"),
        ("news_bloomberg", "快讯", "彭博", "no_public_api", "无免费公开 API，仅终端/订阅，不入库"),
    ]
    return [
        {"source_id": sid, "category": cat, "label": label, "status": status, "note": note}
        for sid, cat, label, status, note in rows
    ]


def register_known_sources() -> None:
    """把免费源聚合清单登记进 source_status（幂等，含不可用源的如实标注）。"""
    for row in _registry():
        source_status.register(
            row["source_id"],
            category=row["category"],
            label=row["label"],
            description=f"[{row['status']}] {row['note']}",
        )


def fetch_with_fallback(
    endpoints: List[str],
    *,
    labels: Optional[List[str]] = None,
    timeout: float = FALLBACK_TIMEOUT_S,
    headers: Optional[Dict[str, str]] = None,
    parser: Optional[Callable[[Any], Any]] = None,
) -> Optional[Any]:
    """按顺序尝试 endpoints，单个超时 ``timeout`` 秒即切下一个；全部失败返回 None。

    ``parser`` 非空时对 ``resp.json()`` 解析；否则返回 json 本身。
    """
    if not _HAS_REQUESTS:
        return None
    if labels is None:
        labels = [f"fallback#{i}" for i in range(len(endpoints))]
    for url, sid in zip(endpoints, labels):
        t0 = time.time()
        try:
            resp = requests.get(url, timeout=timeout, headers=headers or {"User-Agent": _UA})
            if resp.status_code != 200:
                source_status.report(sid, False, error=f"HTTP {resp.status_code}")
                continue
            data = resp.json()
            out = parser(data) if parser else data
            if out is None:
                source_status.report(sid, False, error="解析为空")
                continue
            source_status.report(sid, True, latency_ms=round((time.time() - t0) * 1000))
            return out
        except Exception as exc:
            source_status.report(
                sid, False, error=f"{type(exc).__name__}: {str(exc)[:60]}",
                latency_ms=round((time.time() - t0) * 1000),
            )
            continue
    return None


# ── KuCoin 现货（免费公开，加密行情备用源）──────────────────────────────
_KUCOIN = "https://api.kucoin.com"


def kucoin_ticker(symbol: str) -> Optional[Dict[str, Any]]:
    """KuCoin 现货 24h 行情 → {price, change24h}（24h 涨跌幅已 ×100）。失败 None。"""
    def _p(d: Any) -> Optional[Dict[str, Any]]:
        if isinstance(d, dict) and d.get("code") == "200000" and d.get("data"):
            t = d["data"]
            return {"price": float(t["last"]), "change24h": float(t.get("changeRate", 0)) * 100}
        return None
    return fetch_with_fallback(
        [f"{_KUCOIN}/api/v1/market/stats?symbol={symbol}"],
        labels=[f"quote_kucoin:{symbol}"], parser=_p,
    )


def kucoin_candles(symbol: str, interval: str = "5min", limit: int = 96) -> Optional[List[List[float]]]:
    """KuCoin K 线 → [[ts_s, o, h, l, c, vol], ...] 升序；interval 用 KuCoin 口径(1min/5min/...)。"""
    def _p(d: Any) -> Optional[List[List[float]]]:
        if not isinstance(d, dict) or d.get("code") != "200000":
            return None
        rows = d.get("data") or []
        out: List[List[float]] = []
        # KuCoin candles 最新在前（倒序）→ 反转升序；行 = [ts_s, o, c, h, l, vol, turnover]
        for r in reversed(rows):
            try:
                out.append([float(r[0]), float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5])])
            except (TypeError, ValueError, IndexError):
                continue
        return out if len(out) >= 2 else None
    return fetch_with_fallback(
        [f"{_KUCOIN}/api/v1/market/candles?type={interval}&symbol={symbol}&limit={limit}"],
        labels=[f"quote_kucoin:candles:{symbol}"], parser=_p,
    )


# ── CNN 恐惧贪婪指数（免费公开情绪接口）──────────────────────────────────
_CNN_FG = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


def cnn_fear_greed() -> Optional[Dict[str, Any]]:
    """CNN Fear & Greed 最新读数 → {value, rating}。失败 None。"""
    def _p(d: Any) -> Optional[Dict[str, Any]]:
        data = d.get("fear_and_greed") or {}
        cur = data.get("now") or {}
        if "value" not in cur or cur.get("value") in (None, ""):
            return None
        return {"value": float(cur.get("value")), "rating": str(cur.get("rating") or "")}
    return fetch_with_fallback([_CNN_FG], labels=["sent_cnn"], parser=_p)


# ── BIS 统计数据（免费公开 REST）─────────────────────────────────────────
_BIS = "https://stats.bis.org/api/v2"


def bis_locations() -> Optional[Any]:
    """BIS 元数据探测（轻量探测免费公开接口可达性）。"""
    return fetch_with_fallback(
        [f"{_BIS}/locations"], labels=["flow_bis"], parser=lambda d: d or None,
    )


def free_source_registry() -> Dict[str, Any]:
    """返回登记表快照（供面板/审计展示数据源全景与接入状态）。"""
    return {
        "timeout_s": FALLBACK_TIMEOUT_S,
        "policy": "优先无付费公开地址；单源超时 3s 自动切备用源；无公开 API 源如实标注，不伪造",
        "sources": _registry(),
    }