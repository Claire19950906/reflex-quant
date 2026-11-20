# -*- coding: utf-8 -*-
"""品种命名唯一入口（v341）。

问题（实测 2026-11-15）：
  同一个标的在库里有**两套名字**，各写一半数据：
      ADA   vs ADA-USD      BNB vs BNB-USD     DOGE vs DOGE-USD
      SOL   vs SOL-USD      XRP vs XRP-USD     NVIDIA vs NVDA
  成因：取数端用「行情符号」（yfinance 的 BTC-USD），落库端用「品种代码」
  （BTC），两边都没做归一 → 权重、命中率、归因都按名字分组，
  等于同一个品种被拆成两份样本，统计直接对半。

做法：只在这里定"谁是规范名"，其他模块一律调 canonical()。
  · canonical(x)          → 规范代码（不认识的原样返回，绝不瞎猜）
  · aliases_of(canonical) → 该品种在库里可能出现的所有写法（迁移/查重用）

注意：这里的映射**不是**"看起来像就合并"。
  NVIDIA→NVDA 是同一家公司；ADA-USD→ADA 是同一枚币的行情符号；
  NGC 与 NG 不是一回事，不能合。拿不准的一律不写进表。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Set

# 规范名 → 库里出现过的其它写法
_ALIASES: Dict[str, tuple] = {
    # 加密：行情符号（yfinance/OKX 风格）
    "BTC": ("BTC-USD", "BTCUSDT", "XBTUSD"),
    "ETH": ("ETH-USD", "ETHUSDT"),
    "SOL": ("SOL-USD", "SOLUSDT"),
    "ADA": ("ADA-USD", "ADAUSDT"),
    "BNB": ("BNB-USD", "BNBUSDT"),
    "DOGE": ("DOGE-USD", "DOGEUSDT"),
    "XRP": ("XRP-USD", "XRPUSDT"),
    # 商品：期货连续合约符号
    "XAU": ("GC=F", "GOLD"),
    "XAG": ("SI=F", "SILVER"),
    "WTI": ("CL=F", "OIL", "CRUDE"),
    "HG": ("HG=F", "COPPER"),
    "DXY": ("DX-Y.NYB", "USDX"),
    # 股票：公司英文名/中文别名
    # v497 修正：US_HYNIX 原来被错挂在 MU（美光）下面 —— 把 SK 海力士的样本
    # 合并进了美光，两只票的命中率互相污染。海力士归海力士，美光归美光。
    "NVDA": ("NVIDIA",),
    "MU": ("MICRON",),
    "SKHYNIX": ("000660.KS", "US_HYNIX", "SK_HYNIX", "HYNIX"),
    "005930.KS": ("SAMSUNG", "SAMSUNG_ELEC"),
}

# 反向索引（小写比对，兼容大小写混写）
_REVERSE: Dict[str, str] = {}
for _canon, _alts in _ALIASES.items():
    _REVERSE[_canon.lower()] = _canon
    for _a in _alts:
        _REVERSE[_a.lower()] = _canon


def canonical(asset: str | None) -> str:
    """任意写法 → 规范代码。不认识的原样返回（保留大小写，只 strip）。"""
    if asset is None:
        return ""
    raw = str(asset).strip()
    if not raw:
        return ""
    return _REVERSE.get(raw.lower(), raw)


def aliases_of(canon: str) -> List[str]:
    """规范代码 → [规范代码, 其它写法...]，用于查重/迁移。"""
    c = canonical(canon)
    out: List[str] = [c]
    out.extend(a for a in _ALIASES.get(c, ()) if a != c)
    return out


def is_alias(name: str) -> bool:
    """这个名字是不是某个规范名的别名（用于扫描脏数据）。"""
    raw = str(name or "").strip()
    if not raw:
        return False
    hit = _REVERSE.get(raw.lower())
    return hit is not None and hit != raw


def all_canonical(names: Iterable[str]) -> Set[str]:
    """一批名字 → 规范名集合（去重后统计用）。"""
    return {canonical(n) for n in names if n}


def unknown(names: Iterable[str]) -> List[str]:
    """不在映射表里的名字，供人工确认后再补表（不自动扩表）。"""
    known = {k.lower() for k in _REVERSE}
    return sorted({n for n in names if n and n.lower() not in known})
