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
