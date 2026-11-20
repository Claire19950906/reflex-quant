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
    # ── v367 重排: 把已验证可达的源放第一, 删掉确认不可达/需 key 的源 ──────────
    #   移除: yahoo / yfinance (本机 SSL 恒断), asia (韩股系, 不在交易范围),
    #         equity(Finnhub) / tiingo / coinglass_lsr (需 key, 从未成功)
    #   保留: cn(腾讯 qt.gtimg.cn + 新浪 hq.sinajs.cn, 实测 0.3-0.6s 全成功)
    # 加密主力
    "BTC_PRICE": ["binance", "coinbase", "kraken", "coingecko"],
    "ETH_PRICE": ["binance", "coinbase", "kraken", "coingecko"],
    "SOL_PRICE": ["binance", "coinbase", "kraken", "coingecko", "coinpaprika"],
    "XRP_PRICE": ["binance", "coinbase", "kraken"],
    "DOGE_PRICE": ["binance", "coinbase", "kraken"],
    "ADA_PRICE": ["binance", "coinbase", "kraken"],
    "BNB_PRICE": ["binance", "coingecko"],
    # 贵金属/能源 —— 国内直连优先
    "XAU_PRICE": ["cn", "binance"],
    "XAG_PRICE": ["cn", "binance"],
    "WTI_PRICE": ["cn", "eia"],
    "BRENT_PRICE": ["cn", "eia"],
    "GOLD_PRICE": ["cn", "binance"],
    "OIL_PRICE": ["cn", "eia"],
    # 外汇/国债
    "DXY_PRICE": ["cn"],
    "US10Y_YIELD": ["us10y", "cn"],
    "US02Y_YIELD": ["us10y", "cn"],
    # 美股/股指
    "SPX_PRICE": ["cn"],
    "NDX_PRICE": ["cn", "nq"],
    "DJI_PRICE": ["cn"],
    **{f"{t}_PRICE": ["cn"] for t in (
        "AAPL", "AMZN", "MSFT", "GOOGL", "META", "NVDA", "TSLA", "COIN",
        "PLTR", "MSTR", "SMCI", "MARA", "RIOT", "MU",
    )},
    # SKHYNIX 是韩股 → 不在交易范围, 不再挂链(避免持续失败记录)
    # 加密衍生品
    "BTC_FUNDING_RATE": ["funding_rate", "binance"],
    "BTC_LSR": ["binance"],
    # 新闻
    "NEWS_GENERAL": ["jin10", "reuters", "treenews", "stock_news", "qinghong"],
    "NEWS_CRYPTO": ["reuters", "treenews", "jin10", "crypto_news"],
    "NEWS_MACRO": ["reuters", "jin10", "treenews"],
    "NEWS_STOCK": ["stock_news", "reuters", "treenews", "rss", "qinghong"],
    # 链上
    "ONCHAIN_BTC": ["blockchain", "coinpaprika"],
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