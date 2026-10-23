# -*- coding: utf-8 -*-
"""feed_redund_policy.py - P2 多源数据 fallback 策略

核心问题：单源数据缺失/失败 → 决策质量直接下降，且 E001a 系列错误高居榜首。
本模块定义「同一数据点，按优先级依次试多源」，并对每次尝试记入 data_health_journal，
让数据缺失作为「系统判断」项被列入反思面板（不是硬门禁）。
"""
from __future__ import annotations
from typing import Dict, List

# key = (asset, feed_kind)，value = [source1, source2, ...]
# 顺序即优先级。前一源成功 200ms 内 + 原值正常，则强烈 (a) 跳过 (b) 不再记录。
# 前一源超时/失败/None/异常值，立即跳下一源，全部失败 → 状态为 None。
FEED_REDUND_POLICY: Dict[str, List[str]] = {
    # 加密主力
    "BTC_PRICE": ["binance", "coinbase", "kraken", "yahoo", "coingecko"],
    "ETH_PRICE": ["binance", "coinbase", "kraken", "yahoo", "coingecko"],
    "SOL_PRICE": ["binance", "coinbase", "kraken", "coingecko", "coinpaprika"],
    "XRP_PRICE": ["binance", "coinbase", "kraken", "yahoo"],
    "DOGE_PRICE": ["binance", "coinbase", "kraken", "yahoo"],
    # 贵金属/能源
    "XAU_PRICE": ["yahoo", "binance", "tiingo", "fred", "asia"],
    "XAG_PRICE": ["yahoo", "binance", "tiingo", "asia"],
    "WTI_PRICE": ["eia", "yahoo", "asia"],
    "BRENT_PRICE": ["eia", "yahoo", "asia"],
    # 黄金/原油别名 → 复用主代码的 fallback 链
    "GOLD_PRICE": ["yahoo", "binance", "tiingo", "fred", "asia"],
    "OIL_PRICE":  ["eia", "yahoo", "asia"],
    # 外汇/国债
    "DXY_PRICE": ["fred", "yahoo", "asia"],
    "US10Y_YIELD": ["fred", "yahoo", "us10y"],
    "US02Y_YIELD": ["fred", "yahoo"],
    # 美股/股指 — 13 只 + 3 个指数
    "SPX_PRICE": ["yahoo", "equity", "asia"],
    "NDX_PRICE": ["yahoo", "nq", "asia"],
    "DJI_PRICE": ["yahoo", "equity"],
    # v231: 美股 13 只 fallback 链 — yahoo 挂时切 equity_feed (Finnhub) / yfinance / asia
    "AAPL_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "AMZN_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "MSFT_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "GOOGL_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "META_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "NVDA_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "TSLA_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "COIN_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "PLTR_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "MSTR_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "SMCI_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "MARA_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    "RIOT_PRICE": ["yahoo", "equity", "yfinance", "asia"],
    # 海力士 cap: 韩股 SK Hynix + 美股 Micron (中文圈"美国海力士")
    # Yahoo Finance 唯一支持 000660.KS 的免费源；失败兜底 yfinance (同协议) → asia_prices
    "SKHYNIX_PRICE":  ["yahoo", "yfinance", "asia"],
    "US_HYNIX_PRICE": ["yahoo", "yfinance", "asia"],   # MU
    "MU_PRICE":       ["yahoo", "yfinance", "asia"],
    # 加密衍生品数据
    "BTC_FUNDING_RATE": ["funding_rate", "binance", "coinglass_lsr"],
    "BTC_LSR": ["coinglass_lsr", "binance", "blockchain"],
    # 新闻 — 4 个细分领域，避免美股/海力士新闻被钉死在 jin10 单源
    "NEWS_GENERAL": ["jin10", "reuters", "treenews", "stock_news", "qinghong"],
    "NEWS_CRYPTO":  ["reuters", "treenews", "jin10", "crypto_news"],
    "NEWS_MACRO":   ["reuters", "jin10", "treenews", "fred"],
    # v231: 美股新闻 fallback — stock_news (Finnhub/Yahoo) 主 + reuters + rss 兜底
    # 避免 13 只美股/海力士 新闻缺失被反思误归为"程序错"
    "NEWS_STOCK":   ["stock_news", "reuters", "treenews", "rss", "qinghong"],
    # 链上/Glassnode 类
    "ONCHAIN_BTC": ["blockchain", "coinglass", "coinpaprika"],
}

# 同一品种，跨源价格差异若 > X% 则视作「源不一致」—— 计入 degraded。
EXOG_MAX_PRICE_DRIFT_PCT: float = 1.0

# 单源超时阈值（毫秒）—— 超时即跳下一源。
SOURCE_TIMEOUT_MS: int = 1500


def policy_for(asset: str, kind: str = "PRICE") -> List[str]:
    """取 (asset, kind) 的 fallback 列表 — 无则返回空列表。"""
    key = "{}_{}".format(asset.upper(), kind.upper())
    return FEED_REDUND_POLICY.get(key, [])


def known_assets() -> List[str]:
    """返回所有 policy 中出现过的 asset。"""
    out = set()
    for k in FEED_REDUND_POLICY.keys():
        if "_" in k:
            out.add(k.split("_", 1)[0])
    return sorted(out)


def known_kinds_for(asset: str) -> List[str]:
    """返回某 asset 涉及的所有 feed_kind。"""
    out = set()
    for k in FEED_REDUND_POLICY.keys():
        if k.startswith(asset.upper() + "_"):
            out.add(k.split("_", 1)[1])
    return sorted(out)