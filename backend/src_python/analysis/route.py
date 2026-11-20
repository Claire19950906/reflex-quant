# -*- coding: utf-8 -*-
"""分析前范围路由：这条新闻值不值得走完整主分析。

背景（2026-11-16 实测，按五终态重算）
-------------------------------------
482 条决策里 176 条（36.5%）的 `target_asset` 落在 NONE/CN_STOCK/NVDA/AMZN…
它们全部走完了「7.5k 字符 system prompt + 七个注入块 + 约 25 秒调用」，
只为得出「不在核心清单，按 HOLD 兜底」，并被记成 `analysis_degraded`。

本模块把这一步提前：用门卫已经抽好的 `gate_main_asset` / `gate_assets`
+ 新闻文本里的品种别名 + `market_category`，在**调用模型之前**判出三档：

  core       —— 核心品种，必须走完整分析
  adjacent   —— 邻近品种（宏观/指数/汇率/利率），仍走分析（可以传导到核心）
  out_of_scope —— 个股/桶标签/无标的，不走主分析（写 out_of_scope 终态）

保守原则：判不准就进分析（unknown 也算 should_analyze=True）。
误杀一条核心新闻的代价，远大于多分析一条无关新闻。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract import (
    ADJACENT_ASSETS,
    CORE_ASSETS,
    STOCK_BUCKETS,
    _norm,
    canonical_asset,
    scope_of,
)

# 新闻文本里的品种别名 → 规范代码。只收「看到这个词基本能定性」的写法，
# 不做模糊匹配（模糊匹配会把「比特币ETF获批」里的 ETF 误判成桶标签）。
_ALIASES = {
    "BTC": ("BTC", "BITCOIN", "比特币", "大饼"),
    "ETH": ("ETH", "ETHEREUM", "以太坊", "以太"),
    "SOL": ("SOL", "SOLANA"),
    "XAU": ("XAU", "GOLD", "黄金", "金价"),
    "XAG": ("XAG", "SILVER", "白银", "银价"),
    "WTI": ("WTI", "CRUDE", "原油", "油价", "OPEC"),
    "BRENT": ("BRENT", "布伦特"),
    "DXY": ("DXY", "美元指数", "DOLLAR INDEX"),
    "US10Y": ("US10Y", "10年期美债", "美债收益率", "TREASURY YIELD"),
    "VIX": ("VIX", "恐慌指数"),
    "SPX": ("SPX", "S&P", "标普", "道琼斯", "道指", "美股指数"),
    "NDX": ("NDX", "NASDAQ", "纳指", "纳斯达克", "费城半导体", "费半"),
    "CN_INDEX": ("上证", "沪深300", "A股指数", "科创50"),
}

# 明确的「个股/板块/ETF 类」文本特征：出现这些词且没有任何核心别名 → 越界
_STOCK_HINTS = (
    "财报", "业绩", "净利润", "营收", "市值", "股价", "涨停", "跌停",
    "回购", "增发", "解禁", "减持", "IPO", "上市", "个股", "概念股",
    "ETF", "龙头股", "板块",
)

# 宏观/地缘特征词：本身不是品种，但会传导到核心品种。
# v412-G：加这一档是因为实测有 67 条正文写「伊朗战争 / 美联储 / 制裁」的新闻，
# 只因为门卫标了「个股/美股」就被整条判越界 —— 而这类恰恰是最该分析的传导源。
_MACRO_HINTS = (
    "美联储", "FED", "降息", "加息", "利率决议", "CPI", "非农", "通胀",
    "制裁", "关税", "战争", "冲突", "袭击", "OPEC", "减产", "中东", "地缘",
    "央行", "刺激", "流动性",
)

# 美股头部权重股：业绩/指引会带动 NDX/SPX，对「美股」这个核心资产有直接传导。
# 不含「苹果」（农产品新闻里也出现，有歧义）。
_US_MEGA_CAPS = (
    "英伟达", "NVDA", "特斯拉", "TSLA", "微软", "MSFT", "谷歌", "GOOGL",
    "亚马逊", "AMZN", "META", "台积电", "TSM", "AMD", "博通", "AVGO",
)

CATEGORY_SCOPE = {
    "CRYPTO": "core",
    "GOLD": "core",
    "OIL": "core",
    "MACRO": "adjacent",
    "STOCK": "stock",
}

# 门卫给的「类」而不是品种：这些词本身说明新闻落在核心/邻近赛道里，
# 不该因为 gate_main_asset 写的是「个股/加密主流」就被判越界。
_GATE_CLASS_WORDS = {
    "加密主流": "core", "加密货币": "core", "数字货币": "core", "虚拟货币": "core",
    "加密": "core", "CRYPTO": "core",
    "贵金属": "core", "商品": "core", "能源": "core",
    "美股指数": "adjacent", "美元": "adjacent", "美债": "adjacent", "宏观": "adjacent",
}


@dataclass(frozen=True)
class RouteResult:
    """路由结论。scope 决定要不要调模型，target 是路由认出的品种（可能为空）。"""

    scope: str        # core | adjacent | out_of_scope | unknown
    target: str       # 规范品种代码，认不出为空串
    reason: str       # gate_core / gate_adjacent / text_core / bucket / category_stock / no_evidence

    @property
    def should_analyze(self) -> bool:
        return self.scope != "out_of_scope"


def _split_assets(raw) -> list:
    """gate_assets 可能是 "XAU,WTI"、JSON 列表、或 "XAU|WTI"，统一拆开。

    实测门卫写进库的形态三种都有：`'["个股", "原油", "黄金"]'`（JSON 串）、
    `"NVDA,XAU"`、`"US10Y|WTI"`。JSON 串若不解析，每个元素都会带上方括号和引号，
    清单扫描永远命不中 —— 工作日 512 条里有 50 条就是这么被判成「桶」的。
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [_norm(x) for x in raw if x]
    text = str(raw).strip()
    if text.startswith("["):
        try:
            import json
            loaded = json.loads(text)
            if isinstance(loaded, list):
                return [_norm(x) for x in loaded if x]
        except Exception:  # 坏 JSON → 退回按分隔符拆
            pass
    for sep in ("|", "，", ";", "/"):
        text = text.replace(sep, ",")
    out = []
    for x in text.split(","):
        t = _norm(x).strip("[]\"' ")
        if t:
            out.append(t)
    return out


def _find_alias(text: str) -> str:
    """在新闻正文里找最强的品种别名（长别名优先，避免 "ETH" 命中 "ETHEREUM" 的重复）。"""
    upper = text.upper()
    best = ""
    best_len = 0
    for code, words in _ALIASES.items():
        for w in words:
            if w in upper and len(w) > best_len:
                best, best_len = code, len(w)
    return best


def route_news(*, gate_main_asset="", gate_assets="", news_text="", category="") -> RouteResult:
    """分析前路由。输入全部来自 raw_news（门卫已抽好的字段）+ 新闻正文。"""
    # 门卫常写 GOLD/OIL/CRUDE 这类叫法 —— 先归一到规范代码再比对清单，
    # 否则「金价新闻」会因为叫法不同掉进未知品种（v400 实测 46 条 XAU 就是这么丢的）。
    main = canonical_asset(_norm(gate_main_asset))

    # 1) 门卫已经定性了主品种 —— 直接用它，不用再猜
    if main in CORE_ASSETS:
        return RouteResult("core", main, "gate_core")
    if main in ADJACENT_ASSETS:
        return RouteResult("adjacent", main, "gate_adjacent")

    # 2) 门卫给了多品种清单 —— 只要含核心/邻近，就还值得分析
    #    两遍扫描：核心优先于邻近（"US10Y|WTI" 不能因为先遇到 US10Y 就降级成邻近）
    #    ⚠️ 必须排在「桶标签判定」之前：实测 28181/29976 的 gate_main_asset 写「个股」，
    #    但 gate_assets 里带着「原油/黄金/加密主流」——先判桶就会把核心新闻整条丢掉。
    _gated = [canonical_asset(a) for a in _split_assets(gate_assets)]
    for a in _gated:
        if a in CORE_ASSETS:
            return RouteResult("core", a, "gate_assets_core")
    for a in _gated:
        if a in ADJACENT_ASSETS:
            return RouteResult("adjacent", a, "gate_assets_adjacent")
    for a in _gated:
        if a in _GATE_CLASS_WORDS:
            return RouteResult(_GATE_CLASS_WORDS[a], "", "gate_class_word")

    # 3) 正文别名 —— v412-G 修正：必须排在「桶标签判定」之前。
    #    原顺序是先看 main 是不是桶，是就直接判越界，正文压根不看；实测 14 天里有
    #    126 条正文明写「比特币 / 白银 / 美联储」，却因为 main='个股' 被整条丢掉
    #    （route.py 自己的保守原则是"误杀核心新闻的代价远大于多分析一条无关新闻"，
    #    原顺序违反了这条原则）。现在先看正文，认不出核心/邻近才退回桶判定。
    hit = _find_alias(str(news_text or ""))
    if hit:
        scope = scope_of(hit)
        return RouteResult(scope, hit, f"text_{scope}")

    # 4) 门卫定性为桶/个股，且清单与正文都没有核心品种 → 越界
    #    但先放行"宏观传导类"：美联储/战争/制裁/OPEC 这类文本本身不是品种，
    #    却会传导到核心品种（降息→BTC/XAU、中东冲突→WTI）。判 adjacent（仍进分析），
    #    因为 route 的保守原则是"判不准就进分析"。
    _upper = str(news_text or "").upper()
    if any(h.upper() in _upper for h in _MACRO_HINTS):
        return RouteResult("adjacent", "", "text_macro")

    # 4b) 美股头部权重股 —— v412-G 补：实测一条「道琼斯期货：科技股上涨，英伟达业绩
    #     超预期」因为门卫标「个股」被整条丢；这类票的业绩/指引会带动 NDX/SPX，
    #     对「美股」这个核心资产有直接传导，判 adjacent 仍进分析。
    if any(h.upper() in _upper for h in _US_MEGA_CAPS):
        return RouteResult("adjacent", "", "text_mega_cap")

    if main in STOCK_BUCKETS:
        return RouteResult("out_of_scope", main, "bucket")

    # 4) 正文只有个股特征词 → 越界
    upper = str(news_text or "").upper()
    if any(h in upper for h in (h.upper() for h in _STOCK_HINTS)):
        return RouteResult("out_of_scope", "", "stock_hints")

    # 5) 门卫给了 category（CRYPTO/GOLD/OIL/MACRO/STOCK）
    cat = _norm(category).replace("CATEGORY", "").strip()
    if cat in CATEGORY_SCOPE:
        scope = CATEGORY_SCOPE[cat]
        if scope == "stock":
            return RouteResult("out_of_scope", "", "category_stock")
        return RouteResult(scope, "", f"category_{cat.lower()}")

    # 6) 什么都判不出 —— 保守进分析
    return RouteResult("unknown", "", "no_evidence")


def should_analyze(result: RouteResult) -> bool:
    return result.should_analyze
