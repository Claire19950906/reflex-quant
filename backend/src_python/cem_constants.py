# -*- coding: utf-8 -*-
"""cem_constants.py - v291+
calc_event_move_attribution 的中文同义词常量表
与主逻辑解耦, 便于维护和扩展
"""

ASSET_SYNONYMS = {
    "BTC":     ["BTC", "比特币", "bitcoin"],
    "ETH":     ["ETH", "以太坊", "ethereum"],
    "SOL":     ["SOL", "索拉纳", "solana"],
    "XAU":     ["XAU", "黄金", "金价", "金条", "gold"],
    "WTI":     ["WTI", "原油", "油价", "美油", "crude oil", "crude"],
    "NVDA":    ["NVDA", "英伟达", "老黄", "nvidia"],
    "TSLA":    ["TSLA", "特斯拉", "马斯克", "tesla", "elon musk"],
    "MU":      ["MU", "美光", "micron"],
    "SKHYNIX": ["SKHYNIX", "海力士", "sk hynix", "000660.KS"],
    "ADA":     ["ADA", "艾达", "cardano"],
    "DOGE":    ["DOGE", "狗狗币", "dogecoin"],
    "XRP":     ["XRP", "瑞波", "ripple"],
    "BNB":     ["BNB", "币安币", "binance coin"],
    "AAPL":    ["AAPL", "苹果", "apple"],
    "MSFT":    ["MSFT", "微软", "microsoft"],
    "GOOGL":   ["GOOGL", "谷歌", "google", "alphabet"],
    "AMZN":    ["AMZN", "亚马逊", "amazon"],
    "META":    ["META", "脸书", "facebook"],
}

CROSS_ASSET_GROUPS = {
    "crypto":  ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"],
    "metal":   ["XAU"],
    "energy":  ["WTI"],
    "us_tech": ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META"],
    "memory":  ["MU", "SKHYNIX"],
}

# ── v332: 新闻资产标签 → 平台资产代码 的归一表 ────────────────────────────────
# 背景: 新闻侧按**交易所原始代码**打标 (韩国 000660), 异动侧按**平台统一代码**
# (SKHYNIX)。两边字符串永不相等, 于是"品种直配"一直命中不了, 只剩标题弱匹配
# (semantic), 而 v321.5 又把弱匹配默认挡在主表外 —— 实测 SKHYNIX 82 条异动、
# 窗口内有 45 条带真新闻, 归因结果却是 0 条, 直接拖垮 #11「按资产覆盖 ≥10」。
# 这里只做**代码归一**, 不新增任何启发式: 归一后仍是原来的 "asset 直配" 通道。
ASSET_TAG_ALIASES = {
    "000660": "SKHYNIX", "000660.KS": "SKHYNIX", "000660.ks": "SKHYNIX",
    "SK HYNIX": "SKHYNIX", "HYNIX": "SKHYNIX",
    "005930": "SAMSUNG", "005930.KS": "SAMSUNG",
    "INDIVIDUAL_STOCK": "", "US_STOCK": "", "个股": "",   # 资产"桶", 不是具体标的
}


def normalize_asset_tag(tag: str) -> str:
    """把新闻侧的资产标签归一到平台资产代码 (空串 = 不是具体标的, 丢弃)。"""
    if not tag:
        return ""
    t = str(tag).strip()
    if not t:
        return ""
    return ASSET_TAG_ALIASES.get(t, ASSET_TAG_ALIASES.get(t.upper(), t.upper()))
