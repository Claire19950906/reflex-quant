# -*- coding: utf-8 -*-
"""chain_keywords.py — 因果链触发词净化 + 品种绑定（v400 P0-1/P0-2，纯函数）

两个已实测的入库缺陷（2026-11-16 实测，752 条链）
--------------------------------------------------
1. **触发词污染**：722/751（96%）的 `trigger_words` 混进了系统 token ——
   `verdict / prompt / Asset / BUY / SELL / ai_worker / checklist / market_event /
   partial / geopolitical_conflict`。来源是 LLM 输出 JSON 的**键名与枚举**被当成
   "关键词"抓进词表（平均 14.7 词、最多 30 词）。后果：任何一段 AI 文本都能命中
   任意一条链，先验被稀释成噪声，权重再准也没意义。
2. **品种绑定错**：旧规则是"trigger_words 里第一个有行情的 ticker"。
   一条讲 XAU 的链，只要词表里 WTI 恰好排在前面就被绑成 WTI；319/752（42%）落
   到 `UNKNOWN`，而 bridge / chain_prior 的匹配都带 `asset=?` 过滤 —— 这些链的
   命中恒为 0，因果闭环静默空转。

本模块把两件事收敛成**唯一口径**，三个调用点共用（不连库、不写库、可单测）：
  · `causal_chains.add_chain / update_chain`             —— 入库时净化
  · `causal_chain_evaluator.derive_strategy_columns`    —— 派生 asset/trigger_keywords
  · `scripts/backfill_chain_keywords_asset.py`          —— 存量回填（默认 dry-run）

设计原则
--------
* **只删不造**：不猜作者没写的词，不补品种；判不准就留 UNKNOWN（诚实优先）。
* **宁少不滥**：词表上限 12 个；超出的截断并计入 `truncated`，便于看污染程度。
* **品种按语义打分**：链的 `effect`（果落在哪个资产）权重最高，其次 `name`，
  再 `cause`，最后才是 `trigger_words`（词表可以复述多次，不能定身份）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

# ── 词表切分：历史数据里 , / ， / 、 / ; / ; / | / / / 空白 混用 ─────────────
_SPLIT_RE = re.compile(r"[,，、;；|/\s]+")

# 单条链保留的关键词上限（超过部分是 LLM 复读，不是信息）
MAX_KEYWORDS = 12
# 单个关键词长度上限（>20 字基本是一句被切碎的话）
MAX_TOKEN_LEN = 20
# 判定"污染严重"的阈值：被删词数 ≥ 4 且 不短于保留词数
SUSPECT_MIN_REMOVED = 4

# ── 系统 token：LLM 输出的键名/枚举/口径词，一律不是事件关键词 ────────────
SYSTEM_TOKENS: FrozenSet[str] = frozenset({
    # JSON 键 / schema
    "verdict", "prompt", "asset", "assets", "partial", "checklist", "score",
    "confidence", "signal", "reason", "reasoning", "action", "side", "size",
    "notional", "json", "summary", "snapshot", "evidence", "hypothesis",
    "event", "eventtype", "event_type", "buysell", "direction", "horizon",
    "window", "threshold", "structure", "field", "fields",
    # 引擎/事件类型代码（是分类名，不是事件词）
    "ai_worker", "market_event", "geopolitical_conflict", "regulatory_event",
    "monetary_policy", "trade_settlement", "limit_up", "structural",
    # 布尔 / 空值 / 交易动作枚举
    "true", "false", "null", "none", "unknown", "n/a", "na",
    "buy", "sell", "hold", "long", "short", "hedge", "entry", "exit",
    "win", "loss", "pnl", "avg", "min", "max",
    # 中文口径词
    "样本", "样本量", "胜率", "盈亏", "置信度", "权重", "决策", "信号", "判断",
    "结论", "复盘", "评测", "验证", "未验证", "命中", "未命中", "方向", "幅度",
    "资产", "品种", "标的", "多头", "空头", "持仓", "消息", "市场", "行情",
    "事件", "框架", "结构", "代码", "字段", "格式", "输出", "输入", "提示词",
    # 英文虚词：按空白切词时 "he said" 会被切成 he/said，这类词不是事件关键词
    "the", "and", "or", "but", "if", "then", "than", "as", "at", "by", "for",
    "from", "in", "on", "of", "to", "with", "he", "she", "it", "they", "we",
    "said", "says", "say", "will", "may", "might", "could", "would", "should",
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "do", "does",
    "did", "not", "yes", "this", "that", "these", "those", "there", "here",
    "when", "where", "why", "how", "who", "what", "which", "also", "more", "most",
    "very", "just", "now", "still", "yet", "per", "amid",
    # 复盘口径词：来自 effect 里的实现结果（WIN/LOSS/hit_rate），不是事件词
    "win", "loss", "hit", "hit_rate", "miss", "pnl", "profit", "realized",
})

# ── 品种别名：canonical → 文本里可能出现的写法（lowercase）────────────────
# 只收录系统真正有行情/能结算的品种；调用方还会用 priced_assets 二次过滤。
TICKER_ALIASES: Dict[str, Tuple[str, ...]] = {
    "XAU":  ("xau", "xauusd", "gold", "黄金", "金价", "现货黄金"),
    "XAG":  ("xag", "silver", "白银"),
    "WTI":  ("wti", "oil", "原油", "crude", "brent", "布伦特", "油价"),
    "BTC":  ("btc", "bitcoin", "比特币"),
    "ETH":  ("eth", "ethereum", "以太坊", "以太"),
    "SOL":  ("sol", "solana"),
    "BNB":  ("bnb",),
    "XRP":  ("xrp", "ripple"),
    "DOGE": ("doge", "dogecoin", "狗狗币"),
    "ADA":  ("ada", "cardano", "艾达"),
    "DXY":  ("dxy", "美元指数"),
    "US10Y": ("us10y", "10y", "美债收益率", "十年期美债"),
    "US_STOCK": ("us_stock", "美股", "纳指", "标普", "sp500"),
    "CN_STOCK": ("cn_stock", "a股", "沪深", "上证"),
    "SPX":  ("spx",),
    "NDX":  ("ndx",),
    "NVDA": ("nvda", "nvidia", "英伟达"),
    "TSLA": ("tsla", "tesla", "特斯拉"),
    "MU":   ("mu", "micron", "美光"),
    "SKHYNIX": ("skhynix", "hynix", "海力士"),
}

# 反向索引：alias → canonical
_ALIAS_TO_TICKER: Dict[str, str] = {}
for _ticker, _aliases in TICKER_ALIASES.items():
    for _a in _aliases:
        _ALIAS_TO_TICKER[_a.lower()] = _ticker

ALL_TICKER_ALIASES: FrozenSet[str] = frozenset(_ALIAS_TO_TICKER.keys())


# ---------------------------------------------------------------------------
# 触发词净化
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CleanResult:
    """一次词表净化的结果（不可变，便于直接写审计日志）。"""

    kept: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()

    @property
    def joined(self) -> str:
        return ",".join(self.kept)

    @property
    def removed_ratio(self) -> float:
        total = len(self.kept) + len(self.removed)
        return (len(self.removed) / total) if total else 0.0

    @property
    def suspect(self) -> bool:
        """污染严重 → 需要人工复核（不等于"没救"，只是提示来源不可信）。"""
        if not self.kept:
            return bool(self.removed)
        return (len(self.removed) >= SUSPECT_MIN_REMOVED
                and len(self.removed) >= len(self.kept))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kept": list(self.kept),
            "removed": list(self.removed),
            "removed_ratio": round(self.removed_ratio, 3),
            "suspect": self.suspect,
        }


def split_tokens(raw: Any) -> List[str]:
    """按历史数据的全部分隔符切词（保序、去空白）。"""
    if not raw:
        return []
    return [p.strip() for p in _SPLIT_RE.split(str(raw)) if p and p.strip()]


def _is_ticker_token(token: str) -> bool:
    return token.strip().lower() in ALL_TICKER_ALIASES


def _is_droppable(token: str) -> Optional[str]:
    """返回丢弃原因；None 表示保留。原因是给审计日志看的。"""
    low = token.lower().strip()
    if not low:
        return "empty"
    # 去掉包裹的引号/括号（LLM 常写 "BUY" / [verdict]）
    core = low.strip("\"'“”‘’[]()【】{}<>`")
    if not core:
        return "punctuation"
    if len(core) < 2:
        return "too_short"
    if core.isascii() and len(core) < 3:
        return "too_short"
    if len(core) > MAX_TOKEN_LEN:
        return "too_long"
    if core in SYSTEM_TOKENS:
        return "system_token"
    # 注意：ticker 不在这里判死 —— `drop_tickers` 是 clean_keywords 的开关，
    # trigger_words 要保留品种证据（见 causal_chains._clean_trigger_words）。
    # 纯数字 / 纯符号 / JSON 残片
    if re.fullmatch(r"[-+.\d%]+", core):
        return "numeric"
    if re.fullmatch(r"#[\d\-]+", core):
        return "ref_id"          # effect 里的 "#47" 这类链号引用
    if not re.search(r"[\w\u4e00-\u9fff]", core):
        return "punctuation"
    if any(ch in core for ch in "{}[]\"\\"):
        return "json_fragment"
    return None


def clean_keywords(
    raw: Any,
    *,
    max_keywords: int = MAX_KEYWORDS,
    max_token_len: int = MAX_TOKEN_LEN,
    drop_tickers: bool = True,
    extra_drop: Iterable[str] = (),
) -> CleanResult:
    """净化触发词表：删系统 token / 品种代码 / 超长短语，去重，截断。

    Args:
        raw: 原始 trigger_words（逗号/顿号/空白混排均可）。
        max_keywords: **非品种词**的保留上限，超出截断（截断部分记入 removed）。
            品种代码不占名额 —— 否则 "…,ETH,XAU,SEC,ETP" 里的 ETH/XAU 会把
            SEC/ETP 挤掉（实测链 #1 就这样丢了两个真事件词）。
        drop_tickers: 是否剔除品种代码（匹配侧用默认 True；trigger_words 落库用 False）。
        extra_drop: 调用方额外要屏蔽的词（如某些部署特有的噪声）。
    """
    extra = {str(x).strip().lower() for x in (extra_drop or ()) if str(x).strip()}
    seen: Set[str] = set()
    kept: List[str] = []
    removed: List[str] = []
    kept_non_ticker = 0
    for tok in split_tokens(raw):
        core = tok.strip("\"'“”‘’[]()【】{}<>`")
        reason = _is_droppable(core)
        if reason is None and core.lower() in extra:
            reason = "extra_drop"
        if reason is None and len(core) > max_token_len:
            reason = "too_long"
        is_ticker = _is_ticker_token(core)
        if reason is None and drop_tickers and is_ticker:
            reason = "ticker"
        if reason is not None:
            removed.append(tok)
            continue
        key = core.lower()
        if key in seen:
            removed.append(tok)
            continue
        if not is_ticker and kept_non_ticker >= max_keywords:
            removed.append(tok)   # 截断：记在 removed 里，看污染率时不算"干净"
            continue
        seen.add(key)
        kept.append(core)
        if not is_ticker:
            kept_non_ticker += 1
    return CleanResult(kept=tuple(kept), removed=tuple(removed))


# ---------------------------------------------------------------------------
# 品种绑定（P0-2）
# ---------------------------------------------------------------------------

# 字段权重：果落在哪个资产 > 标题 > 因（上下文）> 词表（可复述，不定身份）
_FIELD_WEIGHTS: Tuple[Tuple[str, float], ...] = (
    ("effect", 3.0),
    ("name", 2.0),
    ("cause", 1.0),
    ("trigger_words", 1.0),
)


@dataclass(frozen=True)
class AssetDecision:
    """一次品种判定的结果。strength 说明这条判断有多硬。"""

    asset: str = "UNKNOWN"
    # effect / name / cause / trigger_only / ambiguous / none / filtered
    strength: str = "none"
    scores: Dict[str, float] = field(default_factory=dict)
    secondary: Tuple[str, ...] = ()
    matched_alias: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "asset": self.asset,
            "strength": self.strength,
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
            "secondary": list(self.secondary),
            "matched_alias": self.matched_alias,
        }


def _alias_positions(text: str, alias: str) -> List[int]:
    """返回 alias 在 text 里所有出现位置。

    英文别名必须**词边界**匹配：否则 "oil" 命中 "boil"、"10y" 命中 "2010y"。
    中文按子串匹配（中文没有词边界）。
    """
    if not text or not alias:
        return []
    low = text.lower()
    if alias.isascii():
        pat = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"
        return [m.start() for m in re.finditer(pat, low)]
    out: List[int] = []
    start = low.find(alias)
    while start >= 0:
        out.append(start)
        start = low.find(alias, start + 1)
    return out


def detect_tickers(text: str, priced_assets: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """text 里出现过的 canonical ticker → 命中的别名（任一）。"""
    priced = {str(a).upper() for a in priced_assets} if priced_assets else None
    hit: Dict[str, str] = {}
    for alias, ticker in _ALIAS_TO_TICKER.items():
        if priced is not None and ticker not in priced:
            continue
        if _alias_positions(text, alias):
            hit.setdefault(ticker, alias)
    return hit


def decide_asset(
    name: str = "",
    cause: str = "",
    effect: str = "",
    trigger_words: str = "",
    *,
    priced_assets: Optional[Iterable[str]] = None,
    aliases: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> AssetDecision:
    """按语义权重判定这条链属于哪个品种。

    规则（打分相加，每个字段对每个 ticker 最多计一次，防止词表复述刷分）：
        effect +3 | name +2 | cause +1 | trigger_words +1

    判定：
        1. 无任何候选 → UNKNOWN / "none"；
        2. 唯一最高分 → 该 ticker；
        3. 并列最高分：
           - 分值 ≤1（只在词表里出现过）→ UNKNOWN / "ambiguous"（宁可不绑）；
           - 否则取在 effect 里出现更早的那个（果的落点通常是链主资产）；
        4. 有 priced_assets 时，无行情的品种直接丢弃（评估不了就不绑）。
    返回的 secondary 是其余候选，供报告看"多资产链"占比，不写入库表。
    """
    alias_map = aliases or TICKER_ALIASES
    priced = {str(a).upper() for a in priced_assets} if priced_assets else None
    counts: Dict[str, float] = {}
    first_alias: Dict[str, str] = {}
    effect_pos: Dict[str, int] = {}

    def _scan(text: str, weight: float, field_name: str) -> None:
        if not text:
            return
        for ticker, alias_list in alias_map.items():
            if priced is not None and ticker not in priced:
                continue
            positions: List[int] = []
            matched = ""
            for alias in alias_list:
                pos = _alias_positions(text, alias)
                if pos:
                    matched = matched or alias
                    positions.extend(pos)
            if not positions:
                continue
            counts[ticker] = counts.get(ticker, 0.0) + weight
            first_alias.setdefault(ticker, matched)
            if field_name == "effect":
                effect_pos[ticker] = min(effect_pos.get(ticker, 10**9), min(positions))

    for field_name, weight in _FIELD_WEIGHTS:
        _scan({"name": name, "cause": cause, "effect": effect,
               "trigger_words": trigger_words}.get(field_name, ""), weight, field_name)

    if not counts:
        return AssetDecision(asset="UNKNOWN", strength="none")

    top = max(counts.values())
    winners = [t for t, s in counts.items() if s == top]
    if len(winners) > 1:
        if top <= 1.0:
            return AssetDecision(asset="UNKNOWN", strength="ambiguous", scores=counts,
                                 secondary=tuple(sorted(winners)))
        # 果里出现更早的那个当主资产
        winners.sort(key=lambda t: effect_pos.get(t, 10**9))
        chosen = winners[0]
    else:
        chosen = winners[0]

    strength = "trigger_only"
    for field_name, _ in _FIELD_WEIGHTS:
        text = {"name": name, "cause": cause, "effect": effect,
                "trigger_words": trigger_words}.get(field_name, "")
        if any(_alias_positions(text, a) for a in alias_map.get(chosen, ())):
            strength = field_name
            if field_name == "trigger_words":
                strength = "trigger_only"
            break

    others = tuple(sorted((t for t in counts if t != chosen), key=lambda t: -counts[t]))
    return AssetDecision(asset=chosen, strength=strength, scores=counts,
                         secondary=others, matched_alias=first_alias.get(chosen, ""))


def asset_for_row(row: Any, *, priced_assets: Optional[Iterable[str]] = None) -> AssetDecision:
    """便捷入口：吃 sqlite3.Row / dict，字段名按 causal_chains 的列名。"""
    def _get(k: str) -> str:
        try:
            return str(row[k] or "")
        except (KeyError, IndexError, TypeError):
            return ""
    return decide_asset(
        name=_get("name"), cause=_get("cause"), effect=_get("effect"),
        trigger_words=_get("trigger_words"), priced_assets=priced_assets,
    )
