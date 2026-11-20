# -*- coding: utf-8 -*-
"""v399: 决策落库的两处"来源留痕"—— 路由来源 + 硬数据缺口原因。

为什么需要
----------
评估报告里两条最大的"说不清"：

  1. `target_asset=NONE` 占 29.7%（142/479）。可它们到底是"新闻真的没有可交易标的"，
     还是"路由漏了"？`asset_route` 只有一次性脚本回填的 36 行，**运行时从不写** →
     永远分不清。
  2. 硬数据证据覆盖只有 44.2%（非降级），而 `_hard_data_block()` 对 BTC/ETH/XAU/WTI/SOL
     明明都能出数。缺的那一半是谁？查出来是个股（A股/港股/美股个股）与桶标签
     （US_STOCK/CN_STOCK/INDIVIDUAL_STOCK）**本地没有行情源**，以及 XAU/WTI 偶发断档。
     但库里只留了"空字符串"，看不出原因。

本模块只做分类，不写库、不抓数据、不猜价格：
  · `route_tag(asset)`：这条决策的标的从哪来（`target_none` / `decided:<class>`）；
  · `hard_status(asset, evidence)`：硬数据是 `ok` 还是缺，缺在哪一类。
两个标签都是"描述事实"，不参与任何打分。
"""
from __future__ import annotations

# 有本地行情源的品种（与 `_hard_data_block` 的实测行为对齐：这些能出数）
_LOCAL_FEED = {"BTC", "ETH", "SOL", "XAU", "WTI", "XAG", "USDT"}

# 桶标签 / 个股类：没有本地行情源，硬数据只能缺
_NO_FEED_BUCKETS = {
    "US_STOCK", "CN_STOCK", "HK_STOCK", "INDIVIDUAL_STOCK", "ETF", "INDEX",
    "STOCK", "OTHER", "美股", "A股", "港股", "个股",
}

# v409: 其中这些是「公司实体存在、只是新闻没解析出代码」——修法不是接数据源，
# 是把新闻里的代码/公司名解析出来。ETF/INDEX/OTHER 不属于这一类（它们本来就没有公司级代码）。
_UNRESOLVED_BUCKETS = {
    "US_STOCK", "CN_STOCK", "HK_STOCK", "INDIVIDUAL_STOCK", "STOCK",
    "美股", "A股", "港股", "个股",
}

# 主要品种 → 资产类别（只用于 route 标签的可读性）
_CLASS = {
    "BTC": "crypto", "ETH": "crypto", "SOL": "crypto", "XRP": "crypto",
    "XAU": "metal", "XAG": "metal",
    "WTI": "oil", "BRENT": "oil",
    "USD": "fx", "CNY": "fx", "EUR": "fx", "JPY": "fx",
}


def _norm(asset: str) -> str:
    return str(asset or "").strip().upper()


def route_tag(asset: str) -> str:
    """标的是怎么定下来的（事实描述，不打分）。

    · `target_none`      —— 决策层没有定出可交易标的
    · `decided:<class>`  —— 定出了具体品种（class 只是可读性标签）
    """
    a = _norm(asset)
    if a in ("", "NONE", "OTHER", "NULL"):
        return "target_none"
    return "decided:" + _CLASS.get(a, "stock" if a in _NO_FEED_BUCKETS else "symbol")


def hard_status(asset: str, evidence: str, ts=None) -> str:
    """硬数据证据的状态：`ok` 或缺在哪一类（只描述，不补造数据）。

    v409: 加 `ts`（决策时刻，秒）。休市时段取不到价是**市场事实**，记
    `missing:market_closed`；不传 ts 则按当前时间判。这条分支只对 CME 系
    与 7×24 品种生效，其它品种 session_state 返回 unknown，仍按旧口径走。
    """
    ev = str(evidence or "").strip()
    if ev and ev.upper() not in ("N/A", "NA", "NULL"):
        return "ok"
    a = _norm(asset)
    if a in ("", "NONE", "OTHER", "NULL"):
        return "missing:none_target"
    if a in _UNRESOLVED_BUCKETS:
        # v409: 桶标签（CN_STOCK/INDIVIDUAL_STOCK…）不是"本地没有源"，
        # 而是"公司认出来了、代码没解析出来"。这两件事的修法完全不同：
        # 前者要接数据源，后者要修新闻→代码的路由。分开记，才看得见真缺口。
        # 顺序坑：这一段必须排在 `no_local_feed` 之前，否则永远走不到（死分支）。
        return "missing:unresolved_target"
    if a in _NO_FEED_BUCKETS:
        return "missing:no_local_feed"
    if a in _LOCAL_FEED:
        # v409: 先判休市 —— 休市没有新价是正常的，不是"管道断档"。
        try:
            from market_session import session_state
            if session_state(a, ts) == "closed":
                return "missing:market_closed"
        except Exception:
            pass
        return "missing:feed_stale"      # 有源但取数失败/断档
    # v409: 真实代码但本地没接这路行情（NVDA/600519.SH/00700.HK…）——是"无源"，
    # 与"符号压根不认识"必须分开：前者要接源，后者要修解析。
    try:
        from asset_kinds import classify as _classify
        _kind, _code = _classify(a)
        if _kind in ("us", "a_share", "hk", "other_market"):
            return "missing:no_local_feed"
    except Exception:
        pass
    return "missing:unknown_symbol"      # 既不在源清单也不在桶清单


def tags(asset: str, evidence: str, ts=None) -> tuple:
    return route_tag(asset), hard_status(asset, evidence, ts=ts)
