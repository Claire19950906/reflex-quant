# -*- coding: utf-8 -*-
"""标的别名层（v422，只读诊断用）。

背景：`ai_decisions.decision_kind='routing_miss'` 常年占一半（11-18 实测 115/228）。
含义是「新闻看懂了，但没落到任何可交易品种」—— 这行决策不可能变成交易，
所以信号数上不去。根因之一是模型只能从固定枚举里挑（XAU|XAG|WTI|BTC|ETH|SOL|
CN_STOCK|US_STOCK|HK_STOCK|INDIVIDUAL_STOCK|SKHYNIX|NONE），
而新闻里写的是「英伟达」「海力士」「金价」「油价」这类自然说法。

本模块只做一件事：**从原文里把品种认出来**，用来回答两个问题：
  1) 这些 routing_miss 的新闻，到底提到了哪些品种？（未识别 TOP 榜）
  2) 如果补了别名，最多能救回多少条？（上限估计，不当承诺）

**不写库、不改决策链**：任何"把命中结果回灌成决策"的动作都不在这里做。
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

# 与 market/market_classifier.py 的 TARGET_TO_MARKET 键保持一致（单一真理源那边不动）。
DEFAULT_ALIASES: Dict[str, List[str]] = {
    # ── 加密 ──
    "BTC": ["比特币", "btc", "bitcoin", "大饼"],
    "ETH": ["以太坊", "eth", "ethereum"],
    "SOL": ["solana", "sol币"],
    # ── 贵金属 / 工业金属 / 原油 ──
    "XAU": ["黄金", "金价", "现货黄金", "伦敦金", "gold", "xauusd", "comex黄金"],
    "XAG": ["白银", "银价", "silver", "xagusd"],
    "COPPER": ["铜价", "精铜", "copper", "lme铜", "沪铜"],
    "WTI": ["原油", "油价", "wti", "布伦特", "brent", "crude", "opec", "欧佩克"],
    # ── 个股（新闻里最常被写成中文名或 ticker）──
    "NVDA": ["英伟达", "nvidia", "nvda", "黄仁勋"],
    "TSMC": ["台积电", "tsmc", "台积电公司"],
    "SKHYNIX": ["海力士", "sk海力士", "sk hynix", "hynix", "000660"],
    "SAMSUNG": ["三星电子", "samsung", "005930"],
    "MU": ["美光", "micron", "mu"],
    "INTC": ["英特尔", "intel", "intc"],
    "TSLA": ["特斯拉", "tesla", "tsla", "马斯克", "musk"],
    "AAPL": ["苹果公司", "apple", "aapl", "库克"],
    "MSFT": ["微软", "microsoft", "msft", "纳德拉"],
    "AMZN": ["亚马逊", "amazon", "amzn"],
    "GOOGL": ["谷歌", "alphabet", "google", "googl"],
    "META": ["脸书", "facebook", "meta", "扎克伯格"],
    "COIN": ["coinbase", "coin"],
    "PLTR": ["palantir", "pltr"],
    "MSTR": ["microstrategy", "strategy", "mstr"],
    "SMCI": ["超微电脑", "supermicro", "smci"],
    "MARA": ["marathon digital", "mara"],
    "RIOT": ["riot platforms", "riot"],
    # ── 指数 / 板块（聚合标的）──
    "CN_STOCK": ["a股", "沪深", "上证指数", "深证", "创业板", "科创板", "沪指", "中国股市"],
    "HK_STOCK": ["港股", "恒生指数", "恒指", "恒生科技"],
    "US_STOCK": ["美股", "标普", "纳斯达克", "道琼斯", "纳指", "s&p", "nasdaq", "dow jones"],
    "INDIVIDUAL_STOCK": ["个股"],
    # ── 利率 / 汇率（MACRO）──
    "US_BOND": ["美债", "美国国债", "国债收益率", "treasury", "收益率曲线"],
    "CN_BOND": ["中债", "中国国债"],
    "USD": ["美元指数", "dxy", "美元走强", "美元走弱"],
    "CNY": ["人民币", "离岸人民币", "cny", "usdcny"],
    "EUR": ["欧元", "eurusd", "欧央行", "ecb"],
    "JPY": ["日元", "usdjpy", "日本央行", "boj"],
}

# 看起来像"标的"但其实是宏观名词，不当成命中（避免"美元/美联储"满天飞）
_WEAK_HINTS = {"individual_stock", "个股"}

_ASCII_RE_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _ascii_pat(alias: str) -> "re.Pattern[str]":
    """ASCII 别名用词边界匹配，避免 'mu' 命中 'must'、'sol' 命中 'solid'。"""
    pat = _ASCII_RE_CACHE.get(alias)
    if pat is None:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", re.I)
        _ASCII_RE_CACHE[alias] = pat
    return pat


def _hit(text: str, alias: str) -> bool:
    if not alias:
        return False
    if alias.isascii():
        return bool(_ascii_pat(alias).search(text))
    return alias in text


def load_aliases(path: Optional[str] = None) -> Dict[str, List[str]]:
    """默认表 + 可选 JSON 覆盖（`backend/config/target_aliases.json`）。

    JSON 形状：{"XAU": ["黄金", "gold"], ...}；同名键的列表会与默认表**合并**，
    不会整体替换 —— 覆盖文件写错一个键不该把别的品种的别名全清掉。
    """
    aliases = {k: list(v) for k, v in DEFAULT_ALIASES.items()}
    p = path or os.getenv("TARGET_ALIAS_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "target_aliases.json"
    )
    if p and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                extra = json.load(f)
            for k, v in (extra or {}).items():
                if not isinstance(v, list):
                    continue
                key = str(k).upper().strip()
                cur = aliases.setdefault(key, [])
                for a in v:
                    a = str(a).strip()
                    if a and a not in cur:
                        cur.append(a)
        except Exception:
            # 配置文件坏了不该让诊断接口挂掉：退回默认表，并把问题留给调用方从
            # /api/targets/aliases 的 source 字段看出来
            pass
    return aliases


def resolve(text: str, aliases: Optional[Dict[str, List[str]]] = None) -> List[Tuple[str, List[str]]]:
    """从一段文本里认出品种。

    返回 [(canonical_target, [命中的别名...]), ...]，按命中个数降序。
    **不做加权、不做歧义消解**：一条新闻同时提"黄金"和"原油"就把两个都返回，
    是否只用第一个由调用方决定。
    """
    if not text:
        return []
    a = aliases or load_aliases()
    out: List[Tuple[str, List[str]]] = []
    for target, words in a.items():
        hits = [w for w in words if _hit(text, w)]
        if hits:
            out.append((target, hits))
    out.sort(key=lambda t: (-len(t[1]), t[0]))
    return out


def resolve_one(text: str, aliases: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """只取一个最可能的品种（命中别名最多的那个），没有则 None。"""
    hits = resolve(text, aliases)
    return hits[0][0] if hits else None


# 聚合类标签不是“具体品种”，不能靠别名回退把一条新闻硬塞进某个桶。
_AGGREGATE_TARGETS = {"CN_STOCK", "HK_STOCK", "US_STOCK", "INDIVIDUAL_STOCK"}

# 诊断层的具体 ticker → 决策层 target_asset enum。
# 只映射到 ai_worker._sanitize_llm_result 认得的枚举，避免回退后又被判非法。
_DECISION_TARGET = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL",
    "XAU": "XAU", "XAG": "XAG", "COPPER": "COPPER",
    "WTI": "WTI", "BRENT": "BRENT",
    "SKHYNIX": "SKHYNIX",
    "NVDA": "US_STOCK", "TSLA": "US_STOCK", "AAPL": "US_STOCK",
    "MSFT": "US_STOCK", "AMZN": "US_STOCK", "GOOGL": "US_STOCK",
    "META": "US_STOCK", "COIN": "US_STOCK", "PLTR": "US_STOCK",
    "MSTR": "US_STOCK", "SMCI": "US_STOCK", "MARA": "US_STOCK",
    "RIOT": "US_STOCK", "MU": "US_STOCK", "INTC": "US_STOCK",
    "TSMC": "US_STOCK", "SAMSUNG": "US_STOCK",
    "USD": "DXY", "CNY": "FX", "EUR": "FX", "JPY": "FX",
}


def resolve_specific_one(text: str,
                         aliases: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """只回退“唯一且具体”的品种；含糊或多品种时返回 None。

    规则：
      1. 排除 CN_STOCK/HK_STOCK/US_STOCK/INDIVIDUAL_STOCK 这类聚合桶；
      2. 必须恰好命中 1 个具体品种（同品种多个别名只算 1 个）；
      3. 映射到决策层 enum；没有映射的（如 US_BOND）不强行回退。

    “黄金和原油同时上涨”会命中 2 个 → 返回 None，不猜。
    """
    hits = resolve(text, aliases)
    specific = [
        t for t, _ in hits
        if t not in _AGGREGATE_TARGETS and t in _DECISION_TARGET
    ]
    if len(specific) != 1:
        return None
    return _DECISION_TARGET[specific[0]]


def summarize_unresolved(rows: Iterable[Tuple[int, str, str, str]],
                         aliases: Optional[Dict[str, List[str]]] = None,
                         sample_limit: int = 3) -> Dict[str, object]:
    """把"未落到品种"的新闻聚合成未识别 TOP 榜。

    入参 rows：[(news_id, source, text, created_at), ...]（**已经**是 router 从库里
    捞出来的 routing_miss / unclassified 行，本函数不连库）。
    出参：
      total            本次分析的条数
      with_candidate   原文里能认出品种的条数 → "补别名最多能救回的上限"
      targets          按品种聚合：[{target, count, share, aliases, samples}]
      no_clue          一个字都没认出来的条数
      no_clue_samples  没认出来的样本（供人工看是不是真没有标的）
    """
    a = aliases or load_aliases()
    total = 0
    per_target: Dict[str, Dict[str, object]] = {}
    no_clue_samples: List[Dict[str, str]] = []
    with_candidate = 0
    for news_id, source, text, created_at in rows:
        total += 1
        head = (text or "").strip().replace("\n", " ")
        hits = resolve(head, a)
        if not hits:
            if len(no_clue_samples) < sample_limit * 3:
                no_clue_samples.append({
                    "news_id": news_id,
                    "source": source or "",
                    "at": created_at or "",
                    "head": head[:100],
                })
            continue
        with_candidate += 1
        # 一条新闻最多记 2 个品种（避免"美股/纳指/标普"把榜单灌满）
        for target, hw in hits[:2]:
            slot = per_target.setdefault(target, {"target": target, "count": 0,
                                                  "aliases": [], "samples": []})
            slot["count"] = int(slot["count"]) + 1
            for w in hw:
                if w not in slot["aliases"]:
                    slot["aliases"].append(w)
            if len(slot["samples"]) < sample_limit:
                slot["samples"].append({
                    "news_id": news_id,
                    "source": source or "",
                    "at": created_at or "",
                    "head": head[:90],
                })
    items = sorted(per_target.values(), key=lambda x: -int(x["count"]))
    for it in items:
        it["share"] = round(int(it["count"]) / total, 4) if total else None
    return {
        "total": total,
        "with_candidate": with_candidate,
        "recover_rate_upper": round(with_candidate / total, 4) if total else None,
        "targets": items,
        "no_clue": total - with_candidate,
        "no_clue_samples": no_clue_samples,
    }
