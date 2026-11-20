# -*- coding: utf-8 -*-
"""分析终态契约：五终态 / HOLD 原因枚举 / 长度预算。

为什么需要这个模块
------------------
现状（2026-11-16 实测 482 条决策）：
  · 430 条 `analysis_degraded=1`，其中 264 条其实是「不在服务范围」，
    另外 71 条 LLM 输出为空、62 条只给总结、33 条服务端兜底 —— 四种完全不同的
    事实挤在同一个布尔和同一个 reason 字符串里；
  · `no_trade_reason` 482/482 全空，导致 `reflection_ontology` 按 HOLD 原因
    归因的分支从不触发；
  · 于是「降级率 89.2%」「HOLD 99.4%」「置信度 0 占 92%」三个数字互为因果，
    谁也说不清到底是数据没到、模型没答，还是模型真的判了中性。

本模块只回答两个问题（纯函数，不碰数据库）：
  1. 这条决策的「分析」到底处于五种终态中的哪一种？   → derive_state()
  2. 它没有下单（HOLD）的可归因原因是什么？         → derive_no_trade_reason()

判定优先级写死在代码里，不做可配置项 —— 口径一旦可调，统计就会漂。
"""

from __future__ import annotations

# 只在注解里用（模块有 future annotations，运行时不求值），但 ruff F821 /
# typing.get_type_hints 都会看它 —— summarize_states 的返回类型需要真实可解析。
from typing import Any, Dict

# ── 五终态 ────────────────────────────────────────────────────────────────
# complete        分析完整（七段/16 字段成型），无论最后是 BUY/SELL/HOLD
# out_of_scope    品种不在服务范围（个股/桶标签/无标的），不该进主分析
# data_unavailable 证据没就绪（快照过期、无本地源、缺周期）——没烧模型
# llm_failed      调用了模型但没拿到可用输出（空输出/截断/服务端兜底）
# parse_failed    拿到了输出但结构不达标（只剩总结、字段缺失、目标缺失）
ANALYSIS_STATES = (
    "complete",
    "compressed",       # 模型给了摘要/方向，但八段没展开（可用、低质量，不算失败）
    "out_of_scope",
    "data_unavailable",
    "llm_failed",
    "parse_failed",
)

STATE_LABELS = {
    "complete": "分析完整",
    "compressed": "压缩输出（有方向）",
    "out_of_scope": "不在服务范围",
    "data_unavailable": "证据未就绪",
    "llm_failed": "模型未返回",
    "parse_failed": "输出结构不达标",
}

# ── HOLD 原因枚举 ─────────────────────────────────────────────────────────
# 与五终态一一对应，外加两条「分析完整但仍然 HOLD」的原因。
NO_TRADE_REASONS = (
    "out_of_scope",
    "data_unavailable",
    "llm_failed",
    "parse_failed",
    "compressed",          # 有方向但只给了摘要：只观察
    "stale_news",          # 新闻超龄（默认 ≥45 分钟），主动放弃
    "gate_blocked",        # 分析有结论但闸门拦下（置信度/方向/强约束）
    "direction_neutral",   # 分析完整且模型判中性，无外部拦截
)

NO_TRADE_REASON_LABELS = {
    "": "无需解释（已下单）",
    "out_of_scope": "不在服务范围",
    "data_unavailable": "证据未就绪",
    "llm_failed": "模型未返回",
    "parse_failed": "输出结构不达标",
    "compressed": "压缩输出（有方向）",
    "stale_news": "新闻超龄",
    "gate_blocked": "闸门拦截",
    "direction_neutral": "模型判中性",
}

# ── 范围常量（与 decision_provenance 的桶清单保持一致）──────────────────
# 有本地行情源、且系统真的会交易/观察的品种
CORE_ASSETS = frozenset({"BTC", "ETH", "SOL", "XAU", "WTI", "XAG"})
# 会向核心品种传导的邻近品种（宏观/指数/汇率/利率）——仍然允许进分析
ADJACENT_ASSETS = frozenset({
    "BRENT", "DXY", "USD", "EUR", "JPY", "CNY",
    "US10Y", "US2Y", "VIX", "SPX", "NDX", "DJI",
    "CN_INDEX", "HSI", "GOLD_ETF",
})
# 桶标签 / 个股：本地无源、系统不交易 —— 属于「不在服务范围」
STOCK_BUCKETS = frozenset({
    "US_STOCK", "CN_STOCK", "HK_STOCK", "INDIVIDUAL_STOCK",
    "ETF", "INDEX", "STOCK", "OTHER",
    "美股", "A股", "港股", "个股",
})
_NONE_ASSETS = frozenset({"", "NONE", "NULL", "UNKNOWN", "NA", "N/A"})
# A股 6 位 / 港股 5 位数字代码，可带 .SH/.SZ/.BJ/.HK 后缀
_NUMERIC_STOCK_CODE = __import__("re").compile(r"^\d{5,6}(\.(SH|SZ|BJ|HK))?$")

# 常见别名 → 规范代码。只收「模型/门卫可能吐出来的同义写法」，
# 避免把 GOLD / OIL 这类写法判成「未知品种」进而误判范围。
_ASSET_ALIASES = {
    "GOLD": "XAU", "XAUUSD": "XAU", "黄金": "XAU",
    "SILVER": "XAG", "XAGUSD": "XAG", "白银": "XAG",
    "OIL": "WTI", "USOIL": "WTI", "CRUDE": "WTI", "原油": "WTI",
    "BITCOIN": "BTC", "比特币": "BTC",
    "ETHEREUM": "ETH", "以太坊": "ETH",
    "USDT": "USDT",
    "BRENT": "BRENT", "UKOIL": "BRENT",
    "DOLLAR": "DXY", "USDX": "DXY",
}


# ── 长度预算 ─────────────────────────────────────────────────────────────
# 与 engine/ai_worker.py 的 [:4000] 截断、以及「过度分析惩罚」阈值是两套口径。
# 这里给出唯一预算：七段推演正文 1,200 字；超预算只告警（看重复率），不扣分。
# 枚举值 → 复盘维度分支认得的同义中文。
# 运行时 HOLD 行写的是枚举（gate_blocked / out_of_scope / ...），而
# `reflection_ontology` 的 6 个分支只认「数据不可用 / 延迟 / 源降级」这类中文词，
# 结果枚举值一条都匹配不上 —— 复盘维度长期空转。这里做一次翻译，不改写库口径。
NO_TRADE_REASON_TEXT = {
    "out_of_scope": "不在服务范围",
    "data_unavailable": "数据不可用",
    "llm_failed": "模型未返回",
    "parse_failed": "输出结构不达标",
    "compressed": "压缩输出（有方向）",
    "stale_news": "新闻延迟",
    "gate_blocked": "闸门拦截",
    "direction_neutral": "模型判中性",
}

REASONING_CHAR_BUDGET = 1200
# 新闻时效：超过该分钟数，HOLD 的原因记为 stale_news（与 prompt 里「~45 分钟按过时处理」对齐）
STALE_NEWS_MINUTES = 45

# 闸门拦截的特征片段（trade_gate_reason 是自由文本，按片段判，不猜语义）
_GATE_BLOCK_HINTS = (
    "低于闸门",
    "无可证据",
    "证据充分",
    "禁止",
    "方向不一致",
    "矛盾",
    "惩罚",
    "强约束",
)


def _norm(asset) -> str:
    return str(asset or "").strip().upper()


def canonical_asset(asset) -> str:
    """别名归一：GOLD→XAU、SILVER→XAG、OIL→WTI……认不出原样返回（大写）。

    门卫的 gate_main_asset 常写 "GOLD"/"OIL" 这类叫法，直接拿去比对
    CORE_ASSETS 会掉进「未知品种」，于是核心新闻被当越界丢掉。
    """
    a = _norm(asset)
    return _ASSET_ALIASES.get(a, a)


def scope_of(asset) -> str:
    """品种归属：core / adjacent / stock / none / unknown。

    只做分类，不判断「该不该分析」——那是 route.should_analyze 的事。
    """
    a = _norm(asset)
    if a in _NONE_ASSETS:
        return "none"
    a = canonical_asset(a)
    if a in CORE_ASSETS:
        return "core"
    if a in ADJACENT_ASSETS:
        return "adjacent"
    if a in STOCK_BUCKETS:
        return "stock"
    # v453: A股/港股代码（600519 / 000001.SZ / 00700.HK）常被门卫直接写成数字，
    # 旧逻辑认不出 → unknown → 落到 parse_failed，把"越界"记成"模型失败"。
    if _NUMERIC_STOCK_CODE.match(a):
        return "stock"
    return "unknown"


def derive_state(*, degraded, reason="", asset="", hard_status="") -> str:
    """把落库字段还原成五终态之一。

    Args:
        degraded:     ai_decisions.analysis_degraded（1 = 没形成有效分析）
        reason:       analysis_degraded_reason（target_none / llm_empty /
                      summary_only / server_fallback / "" …）
        asset:        target_asset（用于范围判定）
        hard_status:  hard_data_status（ok / missing:feed_stale / missing:no_local_feed …）

    Returns:
        ANALYSIS_STATES 之一。判定顺序（固定，不随配置变）：
          1. 未降级 → complete
          2. 品种是桶标签/无标的 → out_of_scope（系统不覆盖这一类，
             即使 reason 写的是 llm_empty 也不是「模型失败」）
          3. 证据缺失（hard_status 以 missing: 开头）且 reason 不是模型侧失败
             → data_unavailable
          4. reason 是模型/解析侧失败 → llm_failed / parse_failed
          5. 其余降级（含 target_none 落在核心品种这种异常组合）→ parse_failed
             （核心品种却拿不到目标，属于输出契约失败，不是越界）
    """
    if not degraded:
        return "complete"

    why = str(reason or "").strip().lower()
    scope = scope_of(asset)
    hard = str(hard_status or "").strip().lower()

    # 2) 越界优先：个股/桶标签/无标的，本来就不该进主分析
    if scope in ("stock", "none"):
        return "out_of_scope"
    # 2b) 认不出、且明确的「本地无源」标的（如 NVDA/AMZN 这类个股代码）
    #     —— 有源才会是 core/adjacent 或 feed_stale，所以这里可以安全判越界
    if scope == "unknown" and hard in ("missing:no_local_feed", "missing:unknown_symbol"):
        return "out_of_scope"

    # 4) 模型侧/解析侧失败的判定先于证据判定：
    #    这几条 reason 都意味着「已经调用过模型」，与数据缺不缺失是两件事。
    if why == "llm_empty":
        return "llm_failed"
    if why == "server_fallback":
        return "llm_failed"
    # 4b) 只给了摘要、但模型确实给了方向/分数 —— 这是「压缩判断」，可用但低质量。
    #     实测今天 62 条 summary_only 里 22 条带 BUY/SELL，其中 21 条被降级路径
    #     强制改成 HOLD 后直接丢弃。把它们单列一档，既不冒充「分析完整」，
    #     也不冒充「结构失败」——否则失败率永远算不准，方向也永远进不了观察池。
    if why == "summary_only":
        return "compressed"
    if why == "target_none" and scope in ("core", "adjacent"):
        return "parse_failed"
    # v409: 模型把标的判成 NONE、路由事后补了代码 —— 分析仍是降级产物，
    # 但归因是"输出契约没给标的"，不是"世界没有标的"。
    if why == "asset_late_bound":
        return "parse_failed"

    # 3) 有本地源但断档 / 无源 → 证据未就绪
    if hard.startswith("missing:"):
        return "data_unavailable"

    # 5) 兜底：降级但没有可用 reason，按输出契约失败记
    return "parse_failed"


def derive_no_trade_reason(*, action, state, gate_reason="", news_age_min=None) -> str:
    """HOLD 的可归因原因（0-1 个枚举值）。

    已下单（BUY/SELL）返回空串 —— 空串是有意义的：表示「不需要解释」。
    """
    act = str(action or "").strip().upper()
    if act in ("BUY", "SELL"):
        return ""

    st = str(state or "").strip()
    if st in ("out_of_scope", "data_unavailable", "llm_failed", "parse_failed",
              "compressed"):
        return st

    # 分析完整但没下单：先看时效，再看闸门，最后才是「模型判中性」
    if news_age_min is not None:
        try:
            if float(news_age_min) >= STALE_NEWS_MINUTES:
                return "stale_news"
        except (TypeError, ValueError):
            pass

    gate = str(gate_reason or "")
    if any(h in gate for h in _GATE_BLOCK_HINTS):
        # 「AI 主动观望 (evidence=…)」是模型自己的结论，不是闸门拦截
        if "主动观望" in gate:
            return "direction_neutral"
        return "gate_blocked"

    return "direction_neutral"


def is_over_budget(reasoning_path) -> bool:
    """七段正文是否超预算（只用于告警与看板，不作为扣分依据）。"""
    return len(str(reasoning_path or "")) > REASONING_CHAR_BUDGET


def humanize_no_trade_reason(reason) -> str:
    """枚举 → 中文短语；本来就是中文/自由文本的原样返回（不动老数据）。

    只用于**读取侧**（复盘、看板），不改变写入值：写库仍是严格枚举，
    这样机器可统计、人也能读懂，两套口径不再打架。
    """
    r = str(reason or "").strip()
    if not r:
        return ""
    return NO_TRADE_REASON_TEXT.get(r, r)


def classify_no_trade_reason(reason) -> str:
    """自由文本 → 枚举（尽力而为）。认不出来返回空串，绝不猜。

    用于把历史中文原因（"新闻已发布 87 分钟，超过时效门槛 45 分钟"）归位到枚举，
    让统计能按同一套口径合并新旧数据。
    """
    r = str(reason or "").strip()
    if not r:
        return ""
    if r in NO_TRADE_REASONS:
        return r
    low = r.lower()
    rules = (
        ("stale_news", ("时效", "stale", "延迟", "超龄")),
        ("data_unavailable", ("数据不可用", "数据缺失", "无 K 线", "行情源不可用", "源降级")),
        ("gate_blocked", ("闸门", "置信度不足", "信号不匹配", "不在模拟盘允许", "策略", "未运行", "无激活")),
        ("direction_neutral", ("主动观望", "中性")),
        ("llm_failed", ("模型未返回", "空输出", "超时")),
        ("parse_failed", ("结构不达标", "解析失败")),
        ("compressed", ("压缩", "摘要")),
        ("out_of_scope", ("不在服务范围", "越界")),
    )
    for code, keys in rules:
        if any(k in r or k in low for k in keys):
            return code
    return ""


def validate_state(state) -> str:
    """非法终态一律抛错：宁可炸在测试里，也不要把脏标签写进账本。"""
    st = str(state or "")
    if st not in ANALYSIS_STATES:
        raise ValueError(f"unknown analysis state: {st!r}")
    return st


def validate_no_trade_reason(reason) -> str:
    r = str(reason or "")
    if r and r not in NO_TRADE_REASONS:
        raise ValueError(f"unknown no_trade_reason: {r!r}")
    return r


def summarize_states(rows) -> Dict[str, Any]:
    """v453: 降级率的**正确口径** —— 把"越界"从"失败"里摘出去。

    `ai_decisions.analysis_degraded=1` 只说明"没形成完整分析"，里面混着两类
    完全不同的东西：新闻本来就不该进主分析（out_of_scope），和真该分析却
    失败了（llm_failed / parse_failed）。混在一起算，会得出"70% 降级"这种
    吓人但失真的数。

    Args:
        rows: 可迭代的 (degraded, reason, asset, hard_status) 四元组。

    Returns:
        {
          "total", "complete", "compressed", "out_of_scope",
          "failed",                      # llm_failed + parse_failed + data_unavailable
          "raw_degraded_rate",           # 旧口径（含越界），保留以便对照
          "effective_analysis_rate",     # (complete + compressed) / total
          "failure_rate_in_scope",       # failed / (total - out_of_scope)
        }
    """
    from collections import Counter

    states = Counter()
    for r in rows:
        degraded, reason, asset, hard_status = (list(r) + ["", "", ""])[:4]
        states[derive_state(
            degraded=int(degraded or 0),
            reason=reason or "",
            asset=asset or "",
            hard_status=hard_status or "",
        )] += 1

    total = sum(states.values()) or 1
    complete = states.get("complete", 0)
    compressed = states.get("compressed", 0)
    oos = states.get("out_of_scope", 0)
    failed = states.get("llm_failed", 0) + states.get("parse_failed", 0) \
        + states.get("data_unavailable", 0)
    in_scope = max(1, total - oos)
    return {
        "total": total,
        "complete": complete,
        "compressed": compressed,
        "out_of_scope": oos,
        "failed": failed,
        "raw_degraded_rate": round((total - complete) / total, 4),
        "effective_analysis_rate": round((complete + compressed) / total, 4),
        "failure_rate_in_scope": round(failed / in_scope, 4),
    }
