# -*- coding: utf-8 -*-
"""事件结构化 — 从新闻文本抽取「事件原子」（根因 / 行动 / 量级 / 方向 / 标的）。

目的：把自然语言新闻抽成可检索、可推演的量化事件原子，例如：

    "美联储加息 25 个基点"  →
        {
          "event_type": "加息",
          "actor": "美联储",
          "action": "加息",
          "magnitude_value": 25,
          "magnitude_unit": "bp",
          "target_assets": ["XAU", "BTC"],
          "raw": "美联储加息25个基点",
        }

定位（与现有 8 段式 / [因果链分析] 互补，不做重复）：
  - 本模块是**纯规则 / 词典**的确定性前置抽取：不联网、不调 LLM，可离线单测。
  - LLM 负责「机理拆解 + 生效前提 + 失效边界」；本模块负责把**事件客观字段抽准**。
  - 两者交叉校验：规则抽出的量级与 LLM 结构化输出不一致时，由上游标黄给人复核。

约束：只依赖 stdlib（re / typing），无重依赖，保证离线可测、engine 可安全 import。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 事件词典：正则 → 事件原子模板（顺序即优先级，一个新闻可命中多条）
# ---------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"


def _re(pat: str) -> re.Pattern:
    return re.compile(pat, re.IGNORECASE)


# (event_type, regex, actor_guess, target_assets)
_EVENT_RULES: List[Dict[str, Any]] = [
    {"event_type": "加息", "regex": _re(r"(加息|升息|上调.{0,6}(基准)?利率|rate\s*hike?|fed\s*rate\s*up)"),
     "actor": "美联储", "target_assets": ["XAU", "BTC", "ETH"]},
    {"event_type": "降息", "regex": _re(r"(降息|下调.{0,6}(基准)?利率|rate\s*cut|fed\s*rate\s*down)"),
     "actor": "美联储", "target_assets": ["XAU", "BTC", "ETH"]},
    {"event_type": "CPI", "regex": _re(r"(CPI|消费者物价|核心通胀|通胀数据|inflation\s*data)"),
     "actor": None, "target_assets": ["XAU", "BTC", "ETH"]},
    {"event_type": "非农", "regex": _re(r"(非农|NFP|失业率|就业数据|nonfarm|unemployment)"),
     "actor": None, "target_assets": ["XAU", "BTC", "ETH"]},
    {"event_type": "OPEC减产", "regex": _re(r"(OPEC|欧佩克).{0,12}(减产|削减|减供|cut\s*production|lower\s*output)"),
     "actor": "OPEC", "target_assets": ["WTI"]},
    {"event_type": "OPEC增产", "regex": _re(r"(OPEC|欧佩克).{0,12}(增产|增加供应|raise\s*production|increase\s*output)"),
     "actor": "OPEC", "target_assets": ["WTI"]},
    {"event_type": "原油库存", "regex": _re(r"(EIA|API).{0,10}(原油)?库存|crude\s*inventor"),
     "actor": None, "target_assets": ["WTI"]},
    {"event_type": "地缘冲突", "regex": _re(r"(战争|武装冲突|军事打击|导弹|空袭|红海|霍尔木兹|伊朗|乌克兰|俄罗斯|袭击|打击|边境冲突|核设施|无人机.{0,4}(袭击|攻击))"),
     "actor": None, "target_assets": ["XAU", "WTI"]},
    {"event_type": "地缘缓和", "regex": _re(r"(停火|和谈|和平协议|缓和|撤军)"),
     "actor": None, "target_assets": ["XAU", "WTI"]},
    {"event_type": "央行购金", "regex": _re(r"(央行|各国央行).{0,10}(购金|增持黄金|黄金储备)|gold\s*reserve"),
     "actor": None, "target_assets": ["XAU"]},
    {"event_type": "加密ETF", "regex": _re(r"(比特币|BTC|以太坊|ETH).{0,10}(现货\s*ETF|ETF.{0,4}(获批|通过|批准))"),
     "actor": None, "target_assets": ["BTC", "ETH"]},
    {"event_type": "美元指数", "regex": _re(r"(美元指数|DXY|美元走强|美元走弱|美元.{0,4}(上涨|下跌))"),
     "actor": None, "target_assets": ["XAU", "BTC"]},
    {"event_type": "TIPS", "regex": _re(r"(TIPS|实际利率|10年期.{0,6}(美债|国债)收益率|美债收益率)"),
     "actor": None, "target_assets": ["XAU", "BTC"]},
    # ── v181 P2: 扩展词典 — 覆盖产业/科技/金融衍生新闻 ──
    {"event_type": "technology_breakthrough", "regex": _re(r"(技术突破|科技突破|新技术.{0,6}(量产|发布|面世)|技术平台|工艺.{0,4}(量产|升级))"),
     "actor": None, "target_assets": ["000300.SS", "NDX"]},
    {"event_type": "chip_capacity", "regex": _re(r"(存储芯片|芯片.{0,6}(量产|扩产|突破)|长鑫|长江存储|DRAM|NAND)"),
     "actor": None, "target_assets": ["000300.SS", "NDX"]},
    {"event_type": "earnings_release", "regex": _re(r"(财报|业绩|净利润|营收|业绩超预期|业绩不及预期|EPS|earnings)"),
     "actor": None, "target_assets": ["000300.SS", "NDX"]},
    {"event_type": "merger_acquisition", "regex": _re(r"(并购|收购|股权转让|重组|M&A|merger|acquisition|takeover)"),
     "actor": None, "target_assets": ["000300.SS", "NDX"]},
    {"event_type": "hack_attack", "regex": _re(r"(黑客|被攻击|漏洞|密钥泄露|钱包.{0,6}(被盗|停用)|hack|exploit|stolen)"),
     "actor": None, "target_assets": ["BTC", "ETH"]},
    {"event_type": "regulation_policy", "regex": _re(r"(监管|合规|政策.{0,6}(出台|发布|落地)|牌照|牌照|合规.{0,4}(要求|标准))"),
     "actor": None, "target_assets": ["BTC", "ETH"]},
    {"event_type": "ipo_listing", "regex": _re(r"(IPO|上市|挂牌|首次公开|新股发行)"),
     "actor": None, "target_assets": ["000300.SS", "NDX"]},
    {"event_type": "macro_speech", "regex": _re(r"(鲍威尔|美联储主席|央行行长|财长|讲话|表态|新闻发布会|press conference)"),
     "actor": None, "target_assets": ["XAU", "BTC"]},
    {"event_type": "natural_disaster", "regex": _re(r"(地震|台风|海啸|洪水|山火|灾害|飓风|灾难)"),
     "actor": None, "target_assets": ["WTI", "000300.SS"]},
    {"event_type": "pandemic_outbreak", "regex": _re(r"(疫情|传染病|病毒|感染病例|封控|lockdown|outbreak)"),
     "actor": None, "target_assets": ["WTI", "000300.SS"]},
    {"event_type": "supply_chain", "regex": _re(r"(供应链|物流|港口|航运|集装箱|缺货|断供)"),
     "actor": None, "target_assets": ["WTI", "000300.SS"]},
    {"event_type": "trade_tariff", "regex": _re(r"(关税|制裁|实体清单|出口管制|贸易战|tariff|sanction)"),
     "actor": None, "target_assets": ["XAU", "000300.SS"]},
]

# 量级单位归一：顺序即优先级。
_UNIT_PATTERNS: List[tuple] = [
    (_re(_NUM + r"\s*(?:个)?\s*基点"), "bp"),
    (_re(_NUM + r"\s*bp"), "bp"),
    (_re(_NUM + r"\s*(?:个)?\s*百分点"), "pct"),
    (_re(_NUM + r"\s*%"), "pct"),
    (_re(_NUM + r"\s*万桶"), "万桶"),
    (_re(_NUM + r"\s*亿美元"), "亿美元"),
]

# 事件 → 标的 → 通用方向（利多/利空/中性）。None/缺省 = 规则不抢答，交给 AI。
# 方向语义：该事件对「对应资产」的直觉定价压力（用于交叉校验，不替代 AI 机理推演）。
_DIRECTION_BIAS: Dict[str, Dict[str, str]] = {
    "加息": {"XAU": "利空", "BTC": "利空", "ETH": "利空", "WTI": "利空"},
    "降息": {"XAU": "利多", "BTC": "利多", "ETH": "利多", "WTI": "利多"},
    "CPI": {"XAU": "利空", "BTC": "利空"},
    "非农": {"XAU": "利空", "BTC": "利空"},
    "OPEC减产": {"WTI": "利多"},
    "OPEC增产": {"WTI": "利空"},
    "原油库存": {},
    "地缘冲突": {"XAU": "利多", "WTI": "利多"},
    "地缘缓和": {"XAU": "利空", "WTI": "利空"},
    "央行购金": {"XAU": "利多"},
    "加密ETF": {"BTC": "利多", "ETH": "利多"},
    "美元指数": {"XAU": "利空", "BTC": "利空"},
    "TIPS": {"XAU": "利空", "BTC": "利空"},
    # ── v181 P2: 新词条方向 bias ──
    "technology_breakthrough": {"000300.SS": "利多", "NDX": "利多"},
    "chip_capacity": {"000300.SS": "利多", "NDX": "利多"},
    "earnings_release": {"000300.SS": "中性", "NDX": "中性"},
    "merger_acquisition": {"000300.SS": "中性", "NDX": "中性"},
    "hack_attack": {"BTC": "利空", "ETH": "利空"},
    "regulation_policy": {"BTC": "中性", "ETH": "中性"},
    "ipo_listing": {"000300.SS": "中性", "NDX": "中性"},
    "macro_speech": {"XAU": "中性", "BTC": "中性"},
    "natural_disaster": {"WTI": "利空", "000300.SS": "利空"},
    "pandemic_outbreak": {"WTI": "利空", "000300.SS": "利空"},
    "supply_chain": {"WTI": "利空", "000300.SS": "利空"},
    "trade_tariff": {"XAU": "利多", "000300.SS": "利空"},
}

def _extract_magnitude(text: str) -> Dict[str, Optional[float]]:
    """扫出第一个可解析的量级：{value, unit}，无则 {value: None, unit: None}。"""
    for pat, unit in _UNIT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return {"value": float(m.group(1)), "unit": unit}
            except (TypeError, ValueError):
                continue
    return {"value": None, "unit": None}


def direction_for(event_type: str, asset: str) -> Optional[str]:
    """返回该事件对指定标的的规则默认方向；未覆盖返回 None（规则不抢答）。"""
    return _DIRECTION_BIAS.get(event_type, {}).get(asset.upper())


def extract_events(text: str) -> List[Dict[str, Any]]:
    """从文本抽取全部事件原子（按词典顺序；同 event_type 只保留首个命中）。"""
    if not text:
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for rule in _EVENT_RULES:
        m = rule["regex"].search(text)
        if not m:
            continue
        event_type = rule["event_type"]
        if event_type in seen:
            continue
        seen.add(event_type)
        mag = _extract_magnitude(text)
        out.append({
            "event_type": event_type,
            "actor": rule["actor"] or "",
            "action": event_type,
            "magnitude_value": mag["value"],
            "magnitude_unit": mag["unit"],
            "target_assets": list(rule["target_assets"]),
            "raw": text.strip()[:120],
        })
    return out


def to_prompt_block(events: List[Dict[str, Any]]) -> str:
    """把事件原子转成注入 AI 的紧凑文本块；无事件返回空串（不改变输出格式）。"""
    if not events:
        return ""
    lines = ["═══ 事件结构化抽取（规则确定性抽出，供八段/因果链分析交叉校验）═══"]
    for e in events:
        mag = ""
        if e.get("magnitude_value") is not None:
            mag = f"，量级={e['magnitude_value']:g}{e.get('magnitude_unit') or ''}"
        actor = f"；施动者={e['actor']}" if e.get("actor") else ""
        assets = "、".join(e.get("target_assets") or [])
        lines.append(f"  · 事件={e['event_type']}{actor}；标的=[{assets}]{mag}")
    return "\n".join(lines)


def extract_data_points(text: str) -> Dict[str, Optional[float]]:
    """抽经济数据发布新闻的三值锚点（actual/forecast/prev），供数据不全时交叉验证。

    场景：FRED/BLS/EIA 等官数源缺数据时，用新闻文本里明确报出的「实际值/预期值/前值」
    作锚点核对，避免单源缺失导致无结论。纯规则、只依赖 stdlib，抽不到返回 None。
    """
    if not text:
        return {"actual": None, "forecast": None, "prev": None}

    def _grab(keys: tuple) -> Optional[float]:
        for kw in keys:
            m = re.search(
                kw + r"[^\d+\-]{0,12}(" + _NUM + r")\s*(?:%|bp|基点|百分点)?",
                text, re.IGNORECASE,
            )
            if m:
                try:
                    return float(m.group(1))
                except (TypeError, ValueError):
                    continue
        return None
    def _grab_comparative() -> Optional[float]:
        """兜底：数据发布常写作「同比/环比 上涨 3.2%」而无「实际值」锚词，抓第一个幅度数作 actual。"""
        m = re.search(
            r"(?:同比|环比)\s*(?:上涨|上升|增长|增加|回落|下降|下跌|放缓|加速|收窄|扩大|为|升|降)\s*("
            + _NUM
            + r")\s*(?:%|bp|基点|百分点)?",
            text, re.IGNORECASE,
        )
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                return None
        return None

    actual = _grab(("实际值", "公布值", "实际", "录得"))
    if actual is None:
        actual = _grab_comparative()
    return {
        "actual": actual,
        "forecast": _grab(("预期值", "预测值", "市场预期", "预期", "预估")),
        "prev": _grab(("前值", "前次", "上期")),
    }


def to_data_points_block(points: Dict[str, Optional[float]]) -> str:
    """三值锚点 → 注入 AI 的紧凑片段；无任何值返回空串。"""
    if not points or all(v is None for v in points.values()):
        return ""

    def fmt(v: Optional[float]) -> str:
        return "N/A" if v is None else f"{v:g}"

    return (
        "  数据三值锚点（规则抽，供交叉验证，缺失标 N/A）："
        f"实际值={fmt(points.get('actual'))}，预期值={fmt(points.get('forecast'))}，前值={fmt(points.get('prev'))}"
    )


if __name__ == "__main__":
    samples = [
        "美联储宣布加息 25 个基点，黄金承压",
        "OPEC 决定每日减产 50 万桶，原油应声上涨",
        "美国 10 月 CPI 同比上涨 3.2%，高于预期",
        "红海航运冲突升级，避险资金涌入黄金",
    ]
    for s in samples:
        evs = extract_events(s)
        print("新闻:", s)
        print(to_prompt_block(evs) if evs else "  （未命中任何事件词典）")
        print()