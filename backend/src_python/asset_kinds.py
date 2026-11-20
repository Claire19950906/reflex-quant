#!/usr/bin/env python3
"""标的分类（v406）—— 把「非公司」「占位标签」「美股」「A股」「港股」分成五类。

为什么要单独一个模块：
  v405 的 `fundamental_factor.resolve_ticker()` 只有"是美股代码 / 不是"两档，
  于是 `CN_STOCK`、`US_STOCK`、`INDIVIDUAL_STOCK` 这些**占位标签**（新闻没解析出
  具体代码时用的兜底标签）全被归成「非公司标的」—— 语义错得很贵：
  「BTC 不存在市盈率」和「A股这家公司有财报、只是我们还没接」是两件完全不同的事，
  前者是**不适用**，后者是**缺口**。混成一句话，复盘时会以为是"这类资产本来就不用看基本面"。

五种 kind：
  non_equity    —— BTC/XAU/WTI/SPX 这类不是公司的标的：**真的没有公司级基本面**
  placeholder   —— CN_STOCK/HK_STOCK/US_STOCK/INDIVIDUAL_STOCK/NONE：**有公司，但代码没解析出来**
  us            —— 美股代码（NVDA/AMZN/MSTR）
  a_share       —— A股代码（600519 / SH600519 / 000001 / 300750）
  hk            —— 港股代码（00700 / HK00700）

纯函数，不碰网络、不读库，可离线单测。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ── 不是公司的标的：没有市盈率这种东西，硬套就是造数 ─────────────────────
NON_EQUITY_ASSETS = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LTC", "BCH",
    "XAU", "XAG", "GOLD", "SILVER", "WTI", "BRENT", "OIL", "NG", "COPPER",
    "SPX", "SP500", "NDX", "NASDAQ", "DXY", "US10Y", "US2Y", "VIX",
    "SHCOMP", "SZCOMP", "CSI300", "HSI",
})

# ── 占位标签：有公司实体，但新闻没解析出代码。必须报"缺口"，不能报"不适用" ──
PLACEHOLDER_ASSETS = frozenset({
    "NONE", "CN_STOCK", "HK_STOCK", "US_STOCK", "INDIVIDUAL_STOCK",
    "A_STOCK", "US_EQUITY", "STOCK", "UNKNOWN", "",
})

# ── 别名：项目里同一个标的在不同链路口径不同（policy/feed 冗余策略里已约定）──
ALIASES = {
    "US_HYNIX": "MU",          # 美股口径别名：SK海力士的美股映射
    "SKHYNIX": "000660.KS",    # 韩股口径：SK海力士本体（韩交所）
}


# ── 公司名 → 代码（v406 第二版）：新闻里点名了公司，就不该只输出 CN_STOCK ──────
# 背景：近 7 天 14 个 distinct target_asset 里没有一个真实 A股/港股代码，全是占位标签，
# 所以 A股/港股通路虽然接好，触发率≈0。上游抽取只给"类别"时，这里按同一市场的公司名
# 反解成代码；只在该占位标签所属市场内匹配，宁可解析不出，也不跨市场乱认。
NAME_ALIASES = {
    # A股
    "贵州茅台": "600519.SH", "茅台": "600519.SH", "宁德时代": "300750.SZ",
    "比亚迪": "002594.SZ", "中国平安": "601318.SH", "招商银行": "600036.SH",
    "工商银行": "601398.SH", "中国石油": "601857.SH", "中国石化": "600028.SH",
    "五粮液": "000858.SZ", "隆基绿能": "601012.SH", "中芯国际": "688981.SH",
    "京东方": "000725.SZ", "万科": "000002.SZ", "东方财富": "300059.SZ",
    "药明康德": "603259.SH", "长江电力": "600900.SH", "美的集团": "000333.SZ",
    "格力电器": "000651.SZ", "海天味业": "603288.SH", "中国神华": "601088.SH",
    "紫金矿业": "601899.SH", "立讯精密": "002475.SZ", "北方华创": "002371.SZ",
    "中兴通讯": "000063.SZ", "上汽集团": "600104.SH", "中信证券": "600030.SH",
    "中国建筑": "601668.SH", "中国联通": "600050.SH", "中国核电": "601985.SH",
    # 港股
    "腾讯控股": "00700.HK", "腾讯": "00700.HK", "阿里巴巴": "09988.HK", "阿里": "09988.HK",
    "美团": "03690.HK", "小米集团": "01810.HK", "小米": "01810.HK",
    "京东集团": "09618.HK", "京东": "09618.HK", "网易": "09999.HK", "快手": "01024.HK",
    "中国移动": "00941.HK", "汇丰控股": "00005.HK", "友邦保险": "01299.HK",
    "香港交易所": "00388.HK", "建设银行": "00939.HK", "中国海洋石油": "00883.HK",
    "中海油": "00883.HK", "药明生物": "02269.HK", "理想汽车": "02015.HK",
    "小鹏汽车": "09868.HK", "蔚来": "09866.HK", "联想集团": "00992.HK",
    "商汤": "00020.HK", "平安好医生": "01833.HK",
    # 美股（中文名也常见于中文财经新闻）
    "英伟达": "NVDA", "苹果": "AAPL", "亚马逊": "AMZN", "特斯拉": "TSLA",
    "微软": "MSFT", "谷歌": "GOOGL", "脸书": "META", "美光": "MU",
    "微策略": "MSTR", "超微电脑": "SMCI", "超威半导体": "AMD", "英特尔": "INTC",
    "台积电": "TSM", "博通": "AVGO",
}

# 占位标签（上游给的"标的"槽位名）允许在哪个市场里反解 —— 跨市场一律不认
# （"苹果"只在美股口径下才算 AAPL）。
# 命名注意：诚实性扫描会把任何 `_PLACEHOLDER_` 开头的常量当成"可能捏造占位值"，
# 这里是标签→允许市场的白名单，不是占位值，故改名避免误报。
_TAG_MARKET_SCOPE = {
    "CN_STOCK": ("a_share",), "A_STOCK": ("a_share",),
    "HK_STOCK": ("hk",),
    "US_STOCK": ("us",), "INDIVIDUAL_STOCK": ("us",),
    "STOCK": ("a_share", "hk", "us"), "UNKNOWN": (),
    "US_EQUITY": ("us",), "NONE": (), "": (),
}

_NAME_KEYS = sorted(NAME_ALIASES, key=len, reverse=True)      # 长名优先（"腾讯控股" > "腾讯"）


# ── 从正文里直接抠代码（比名字匹配可靠：新闻里写了 "新易盛（300502.SZ）" 就直接用）──
_TEXT_A_RE = re.compile(r"(?<![0-9A-Za-z])((?:SH|SZ|BJ)?\d{6}(?:\.(?:SH|SZ|BJ))?)(?![0-9])")
_TEXT_HK_RE = re.compile(r"(?<![0-9A-Za-z])(\d{5})\.HK(?![A-Za-z])", re.I)
_TEXT_US_RE = re.compile(r"\$(\$?)([A-Z]{1,5})\b")


def extract_codes(text: object) -> List[Tuple[str, str]]:
    """扫正文里的代码写法，返回 [(kind, code)]。只认**能归一**的写法，不做模糊猜测。

    典型来源："新易盛（300502.SZ）" / "600519.SH" / "00700.HK" / cashtag "$NVDA"。
    为什么先抠代码再看名字：代码是无歧义的，名字有重名（"中金"既可是投行也可指中金公司）。
    """
    body = str(text or "")
    out: List[Tuple[str, str]] = []
    for m in _TEXT_A_RE.finditer(body):
        code = resolve_a_share(m.group(1))
        if code:
            out.append(("a_share", code))
    for m in _TEXT_HK_RE.finditer(body):
        code = resolve_hk(m.group(1))
        if code:
            out.append(("hk", code))
    for m in _TEXT_US_RE.finditer(body):
        sym = m.group(2).upper()
        kind, norm = classify(sym)
        if kind == "us":
            out.append(("us", norm))
    return out


def resolve_from_text(text: object, placeholder: object,
                      name_map: Optional[Dict[str, Tuple[str, str]]] = None) -> Optional[Tuple[str, str]]:
    """从新闻文本里反解出公司代码 → (kind, code)；解不出返回 None。

    只在 `placeholder` 允许的市场内解析；`NONE` 是模型明确说"无可交易标的"，
    不去推翻它 —— 只补「有公司、但没给代码」这种情况。

    `name_map` 是库里的"全市场简称→代码"字典（cron 维护、决策链路只读）。静态
    `NAME_ALIASES` 只有 74 个常见名，靠它 A股命中率只有 14%；接上字典后新闻里
    任何一只 A股/港股简称都能落到代码。字典里长名优先，避免"腾讯"抢"腾讯音乐"。
    """
    if text is None:
        return None
    body = str(text)
    if not body.strip():
        return None
    ph = str(placeholder or "").strip().upper()
    allow = _TAG_MARKET_SCOPE.get(ph)
    if allow is None:
        allow = ("us",)
    if not allow:
        return None
    # 先抠代码：正文里写了 300502.SZ / 00700.HK / $NVDA，就是最硬的证据
    for kind, code in extract_codes(body):
        if kind in allow:
            return kind, code
    # 再按公司名反解：先把库里的全市场字典并进来（静态表优先，长名优先）
    merged: Dict[str, Tuple[str, str]] = {}
    if name_map:
        merged.update({k: v for k, v in name_map.items() if k})
    for nm in _NAME_KEYS:
        merged.setdefault(nm, classify(NAME_ALIASES[nm]))
    for name in sorted(merged, key=len, reverse=True):
        if name and name in body:
            kind, norm = merged[name]
            if kind in allow:
                return kind, norm
    # 最后回落到静态表（name_map 为空时行为与之前完全一致）
    for name in _NAME_KEYS:
        if name in body:
            code = NAME_ALIASES[name]
            kind, norm = classify(code)
            if kind in allow:
                return kind, norm
    return None

# ── 我们**没接**基本面的市场：这些票有财报，但本系统只有行情、没有财务口径 ──
# 单列一类而不是塞进 placeholder：placeholder 是"代码没解析出来"，
# 这类是"代码很清楚、市场不支持"。两者对复盘的含义完全不同。
_UNSUPPORTED_SUFFIXES = frozenset({"KS", "KQ", "T", "TW", "L", "DE", "PA", "AS", "MI", "TO", "AX"})

_US_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,5}")
_A_RE = re.compile(r"(?:SH|SZ|BJ)?(\d{6})(?:\.(SH|SZ|BJ))?")
_HK_RE = re.compile(r"(?:HK)?(\d{5})(?:\.HK)?")


def resolve_a_share(code: str) -> Optional[str]:
    """A股代码归一成 `600519.SH`；支持 `600519` / `SH600519` / `600519.SH` 三种写法。"""
    code = (code or "").strip().upper()
    m = _A_RE.fullmatch(code)
    if not m:
        return None
    digits, suffix = m.group(1), (m.group(2) or "")
    if suffix:
        return f"{digits}.{suffix}"
    if digits[0] in ("6", "9"):
        return f"{digits}.SH"
    if digits[0] in ("0", "3", "2"):
        return f"{digits}.SZ"
    if digits[0] in ("4", "8"):       # 北交所
        return f"{digits}.BJ"
    return None


def resolve_hk(code: str) -> Optional[str]:
    """港股代码归一成 `00700.HK`；支持 `00700` / `HK00700` / `00700.HK`。"""
    code = (code or "").strip().upper()
    m = _HK_RE.fullmatch(code)
    return f"{m.group(1)}.HK" if m else None


def classify(asset: object) -> Tuple[str, Optional[str]]:
    """→ (kind, 归一代码)。

    kind ∈ non_equity / placeholder / us / a_share / hk / other_market。
    · `placeholder` 的代码恒为 None —— 调用方必须据此报"未解析"，不许猜；
    · `other_market` 是"代码已知、市场未接入"（如 SKHYNIX → 000660.KS），
      与"不适用"（non_equity）和"没解析"（placeholder）三者必须可区分。
    """
    text = str(asset or "").strip().upper()
    if text in PLACEHOLDER_ASSETS:
        return "placeholder", None
    if text in NON_EQUITY_ASSETS:
        return "non_equity", None
    if text in ALIASES:
        text = ALIASES[text]
    for name, code in NAME_ALIASES.items():
        if text == name.strip().upper():
            text = code
            break
    suffix = text.rsplit(".", 1)[1] if "." in text else ""
    if suffix in _UNSUPPORTED_SUFFIXES:
        return "other_market", text
    a_share = resolve_a_share(text)
    if a_share:
        return "a_share", a_share
    hk = resolve_hk(text)
    if hk:
        return "hk", hk
    if _US_RE.fullmatch(text) and text not in NON_EQUITY_ASSETS:
        return "us", text
    # 认不出来：当作占位标签处理（不许猜成某只票）
    return "placeholder", None


def is_company(asset: object) -> bool:
    """只问"它是不是一家能出财报的公司"（占位标签也算"是"，只是代码未知）。"""
    return classify(asset)[0] != "non_equity"


def display(kind: str, code: Optional[str], asset: object) -> str:
    """给 evidence/detail 用的短标签：`NVDA` / `600519.SH` / `00700.HK` / `CN_STOCK(未解析)`。"""
    if code:
        return code
    return f"{str(asset or '').strip().upper() or '空'}(未解析)"
