# -*- coding: utf-8 -*-
"""亚洲股票行情（SK 海力士 + A 股宽基指数）——供新闻分析的价格上下文 / 统一取价。

数据源：Yahoo Finance（yfinance，与 stock_prices 美股同源）：
  - SK 海力士  000660.KS（韩股）
  - 上证指数    000001.SS
  - 深证成指    399001.SZ
  - 沪深300     000300.SS

设计约束（与 stock_prices.py 一致）：
  - 拉不到返回 None——行情只是上下文，绝不阻塞 AI 分析。
  - 10 分钟缓存，同标的高频新闻只拉一次。
  - 不接入 Polygon 兜底（A股/韩股 Polygon 覆盖不全且免费额度有限）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

# 资产代码（LLM target_asset）→ Yahoo ticker
_ASSET_SYMBOLS: Dict[str, str] = {
    "SKHYNIX": "000660.KS",
    "CN_STOCK": "000001.SS",  # A股兜底用上证指数
    "SHCOMP": "000001.SS",    # 上证指数
    "SZCOMP": "399001.SZ",    # 深证成指
    "CSI300": "000300.SS",    # 沪深300
}

# 展示中文名
_ASSET_LABELS: Dict[str, str] = {
    "SKHYNIX": "SK海力士",
    "CN_STOCK": "上证指数",
    "SHCOMP": "上证指数",
    "SZCOMP": "深证成指",
    "CSI300": "沪深300",
}

# 关键词识别（中文/英文/代码）→ 命中即认为是相关新闻
# 强命中：海力士专属（精准触发提醒用，防 HBM/DRAM 等存储泛词误报）
_HYNIX_STRONG_KEYWORDS = (
    "海力士", "sk hynix", "skhynix", "hynix", "000660",
    # v226+：海力士特有产品词（H200 / HBM3 系列是 SK Hynix 独有的 AI 存储产品线）
    "hbm3", "hbm3e", "h200", "hbm4", "h100 hynix",
)
# 弱命中：存储产业泛词（仅行情上下文注入用，不单独判海力士）
_HYNIX_WEAK_KEYWORDS = (
    "hbm", "高带宽内存", "存储芯片", "dram", "nand", "内存芯片",
)
_HYNIX_KEYWORDS = _HYNIX_STRONG_KEYWORDS + _HYNIX_WEAK_KEYWORDS
_ASHARE_KEYWORDS = (
    "a股", "上证", "深证", "沪深300", "沪指", "深成指", "创业板",
    "科创板", "北交所", "大盘指数", "券商", "中国股市",
)


def _norm(asset: str) -> str:
    return (asset or "").strip().upper()


def _ticker(asset: str) -> Optional[str]:
    return _ASSET_SYMBOLS.get(_norm(asset))


_cache_lock = threading.Lock()
_price_cache: Dict[str, Tuple[float, float]] = {}  # yahoo_ticker -> (price, ts)
_CACHE_TTL_S = 600.0


def fetch_price(ticker: str) -> Optional[float]:
    """拉单只标的当前价（yfinance），10 分钟缓存。失败 None。

    v2026.10.08 bugfix: yfinance 无超时——wrap 在 2s ThreadPoolExecutor 截断里，
    防止单 ticker 卡 20-30s 拖累 movers 聚合端点。
    """
    if not ticker:
        return None
    with _cache_lock:
        hit = _price_cache.get(ticker)
        if hit and time.time() - hit[1] < _CACHE_TTL_S:
            return hit[0]
    price: Optional[float] = None

    def _do():
        import yfinance as yf
        p = yf.Ticker(ticker).fast_info.last_price
        if p is not None and float(p) > 0:
            return float(p)
        return None

    try:
        import concurrent.futures as _cf
        ex = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_do)
            try:
                price = fut.result(timeout=2.0)
            except _cf.TimeoutError:
                price = None
        finally:
            ex.shutdown(wait=False)
    except Exception:
        price = None
    if price is not None:
        with _cache_lock:
            _price_cache[ticker] = (price, time.time())
    return price


def asset_price(asset_or_ticker: str) -> Optional[float]:
    """按资产代码取价：入参可能是 'SKHYNIX' / 'CN_STOCK' / '000660.KS'。"""
    s = _norm(asset_or_ticker)
    ticker = _ticker(s) or (s if "." in s else None)
    return fetch_price(ticker)


def detect_hynix(text: str) -> bool:
    """文本是否命中海力士相关关键词（强+弱）。用于行情上下文注入，宽松无害。"""
    low = (text or "").lower()
    return any(k in low for k in _HYNIX_KEYWORDS)


def detect_hynix_strict(text: str) -> bool:
    """只认海力士专属关键词（海力士/SK Hynix/HYNIX/000660），HBM/DRAM 等存储泛词不算。

    用于「海力士强买入/重要提醒」的精准触发，避免美光/三星的 HBM 新闻误报为海力士。
    """
    low = (text or "").lower()
    return any(k in low for k in _HYNIX_STRONG_KEYWORDS)


def detect_ashare(text: str) -> bool:
    """文本是否命中 A 股相关关键词。"""
    low = (text or "").lower()
    return any(k in low for k in _ASHARE_KEYWORDS)


def _hynix_context_line() -> str:
    p = fetch_price("000660.KS")
    if not p:
        return ""
    return f"[SK海力士行情] 000660.KS ₩{p:,.0f}（来源:Yahoo Finance·10分钟缓存）"


def hynix_price_line() -> str:
    """SK 海力士实时行情行（供推送等场景直接取，10 分钟缓存；拉不到返回空串）。"""
    return _hynix_context_line()


def _ashare_context_line() -> str:
    p = fetch_price("000001.SS")
    if not p:
        return ""
    return f"[A股行情] 上证指数 000001.SS {p:,.2f}（来源:Yahoo Finance·10分钟缓存）"


def market_context_line(text: str) -> str:
    """若新闻提到海力士 / A股 → 返回行情上下文行；否则空串。"""
    if not text:
        return ""
    if detect_hynix(text):
        return _hynix_context_line()
    if detect_ashare(text):
        return _ashare_context_line()
    return ""