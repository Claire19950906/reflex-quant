# -*- coding: utf-8 -*-
"""外部指标的“坐标系”（v341）。

问题：`external_indicators` 用 (asset, indicator) 两列装所有东西，但 asset 有时
**不是品种**，而是个分类抽屉。实测：

    asset='*'         indicator='GLD'/'GDX'/'TLT'  → 这是 ETF 的价格，
                      真正的品种在 indicator 列里（25,080 行全塞在 '*' 下）
    asset='FX'        indicator='fx_eurusd'        → 汇率，品种是 EURUSD
    asset='CRYPTO'    indicator='cp_total_mcap_usd'→ 全市场市值（没有品种）
    asset='STABLE'    indicator='sc_usdt_mcap'     → 稳定币存量（不是价格）
    asset='US_MACRO'  indicator='us_vix_cboe'      → 指数点位（VIX）
                      indicator='us_dgs10_calc'    → 收益率（不是价格）
    asset='fred'      indicator='VIX'              → VIX 点位

后果：凡是按 asset 分组的统计（异动、归因、权重、命中率），
聚合量会被当成“一个品种的价格”，和真品种混在一张表里算。

这里只做**分类**，不改数据：resolve() 给每行一个 (namespace, series, kind)。
  namespace = asset | market | macro | supply
  kind      = price | yield | supply | aggregate | ratio | change | flow | macro
"""
from __future__ import annotations

from dataclasses import dataclass

import asset_alias as aa


@dataclass(frozen=True)
class Series:
    namespace: str
    series: str        # 分组用的键（asset 空间用规范代码，其它用指标族名）
    kind: str

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.namespace, self.series, self.kind)


# 这些 asset 本身就是“抽屉”，不是品种
_DRAWERS = {"*", "MARKET", "CRYPTO", "STABLE", "US_MACRO", "US_EQ",
            "USA-LABOR-ICS", "FRED", "FX"}

# 收益率/利率类指标（是水平值，但不是价格）
_YIELD_HINT = ("ust2y", "ust5y", "ust10y", "ust30", "sofr", "dgs", "yield", "rate_")
_CHANGE_HINT = ("_change", "chg", "pct", "_yoy", "mom")
_SUPPLY_HINT = ("supply", "mcap", "cap_usd", "share", "dominance", "reserve")
_FLOW_HINT = ("inflow", "outflow", "netflow", "net_flow")
_RATIO_HINT = ("ratio",)
# 同一个品种名下的“非价格指标”（量、持仓、资金费、活跃地址、离散度…）
_METRIC_HINT = ("volume", "_vol", "hashrate", "open_interest", "long_short",
                "active_addr", "dispersion", "median", "_std", "_z", "funding",
                "premium", "basis", "depth", "spread", "trades", "count",
                "share", "oi_", "pool", "issuance")


# 指数/收益率代码 → 库里的规范代码
_INDEX_ALIAS = {"^GSPC": "SPX", "^IXIC": "IXIC", "^NDX": "NDX",
                "^DJI": "DJI", "^VIX": "VIX", "^RUT": "RUT", "^SOX": "SOX"}
_INDEX_YIELD = {"^TNX": "UST10Y", "^TYX": "UST30Y", "^FVX": "UST5Y", "^IRX": "UST13W"}


def _is_ticker_like(ind: str) -> bool:
    """'GLD' / 'TLT' / '^GSPC' 这种：看起来就是代码，而不是描述性指标名。"""
    up = ind.upper()
    if up in _INDEX_ALIAS or up in _INDEX_YIELD:
        return True
    return (up.isascii() and up.lstrip("^").replace(".", "").replace("-", "").isalnum()
            and not any(h in ind.lower() for h in
                        _SUPPLY_HINT + _FLOW_HINT + _RATIO_HINT + ("_", " ")))


def resolve(asset: str, indicator: str) -> Series:
    """(asset, indicator) → 该行的坐标。不认识就按“该品种的价格”处理。"""
    a = (asset or "").strip()
    ind = (indicator or "").strip()
    au = a.upper()
    low = ind.lower()

    # `*` 是 ETF 价格抽屉：真正的品种在 indicator 里
    if a == "*":
        if any(h in low for h in _RATIO_HINT):
            return Series("market", "ratio:" + low, "ratio")
        up = ind.upper()
        if up in _INDEX_YIELD:                       # ^TNX = 10Y 收益率，是水平值不是价格
            return Series("macro", _INDEX_YIELD[up], "yield")
        if up in _INDEX_ALIAS:
            return Series("asset", _INDEX_ALIAS[up], "price")
        if low.startswith("fed_"):                   # 美联储资产负债表，宏观存量
            return Series("macro", low, "macro")
        if _is_ticker_like(ind):
            return Series("asset", aa.canonical(ind), "price")
        return Series("market", ind, "aggregate")

    # 汇率抽屉
    if au == "FX":
        if low.startswith("dxy") or low == "usdx":
            return Series("asset", "DXY", "price")
        child = ind.split("_", 1)[-1].upper() if "_" in ind else ind.upper()
        return Series("asset", child, "price")

    # 全市场 / 稳定币 / 宏观抽屉
    if au == "CRYPTO" or au == "MARKET":
        # 全市场市值/成交量是聚合量；占有率是比率 —— 都不是某个品种的余额
        if "dominance" in low or "share" in low:
            return Series("market", low, "ratio")
        return Series("market", low, "aggregate")
    if au == "STABLE":
        return Series("supply", low, "supply")
    if au == "US_EQ":
        return Series("market", low, "aggregate")

    if au in ("US_MACRO", "USA-LABOR-ICS", "FRED"):
        # VIX 点位是指数价格，能当行情用；*_change 是变化率
        if any(h in low for h in _CHANGE_HINT):
            return Series("macro", low, "change")
        if low.startswith("us_vix") or low in ("vix", "vvix", "vix9d"):
            return Series("asset", "VIX", "price")
        if any(h in low for h in _YIELD_HINT):
            return Series("macro", low, "yield")
        return Series("macro", low, "macro")

    canon = aa.canonical(a)
    if any(h in low for h in _YIELD_HINT):
        return Series("macro", f"{canon}:{low}", "yield")
    if any(h in low for h in _SUPPLY_HINT):
        return Series("supply", f"{canon}:{low}", "supply")
    if any(h in low for h in _FLOW_HINT):
        return Series("market", f"{canon}:{low}", "flow")
    if any(h in low for h in _CHANGE_HINT):
        return Series("market", f"{canon}:{low}", "change")
    # 资金费/未平仓/成交量/哈希率… 都是"这个品种的另一个指标"，不是它的价格
    if any(h in low for h in _METRIC_HINT):
        return Series("market", f"{canon}:{low}", "metric")
    return Series("asset", canon, "price")


def is_drawer(asset: str) -> bool:
    return (asset or "").strip().upper() in _DRAWERS
