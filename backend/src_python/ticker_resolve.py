# -*- coding: utf-8 -*-
"""新闻 → 真实代码（v409）。

问题：模型经常只给出 `CN_STOCK` / `US_STOCK` / `INDIVIDUAL_STOCK` 这种**桶标签**
（"有公司，但代码没解析出来"），于是硬数据永远取不到，还会被误记成"没有数据源"。
换数据源解决不了这件事 —— 缺的是代码。

做法（刻意保守）：只认**显式**写法，不猜公司名。
  · `$NVDA` / `$0700.HK`
  · `（600519）` `（0700.HK）` 中文括号里的代码
  · `600519.SH` / `00700.HK` 带交易所后缀
  · `NASDAQ: NVDA` / `NYSE:AMZN` / `HKEX: 0700`
免费语里的裸六位数（"营收 600519 万元"这种）**一律不认** —— 报错的代价比漏报高。

拿到的代码必须能被 `asset_kinds.classify` 认成 us / a_share / hk，否则返回 None。
纯函数，不联网不读库。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# $NVDA / $0700.HK
_DOLLAR_RE = re.compile(r"\$([A-Z0-9][A-Z0-9.\-]{0,8})")
# 中文/英文括号里的代码：（600519）/ (600519.SH) / （0700.HK）
_BRACKET_RE = re.compile(r"[（(【\[]\s*([A-Z0-9]{4,9}(?:\.[A-Z]{2})?)\s*[）)】\]]")
# 交易所前缀：NASDAQ: NVDA / NYSE:AMZN / HKEX：0700
_EXCHANGE_RE = re.compile(
    r"\b(?:NASDAQ|NYSE|AMEX|NYSEARCA|HKEX|SSE|SZSE|SHSE)\s*[:：]\s*([A-Z0-9][A-Z0-9.\-]{0,8})",
    re.IGNORECASE)
# 带后缀的裸代码：600519.SH / 00700.HK
_SUFFIX_RE = re.compile(r"\b(\d{5,6}\.(?:SH|SZ|BJ|HK))\b", re.IGNORECASE)


def _normalize(raw: str) -> Optional[str]:
    """交给 asset_kinds 判：只接受 us / a_share / hk 三种，其余一律 None。

    HK 代码习惯写 4 位（0700.HK），而 `asset_kinds` 认 5 位（00700.HK）——
    这里先补零，别让"写法不同"变成"解析不出来"。
    """
    r = str(raw or "").strip().upper()
    m = re.fullmatch(r"(\d{1,4})\.HK", r)
    if m:
        r = m.group(1).zfill(5) + ".HK"
    try:
        from asset_kinds import classify
        kind, code = classify(r)
    except Exception:
        return None
    if kind in ("us", "a_share", "hk") and code:
        return code
    return None


def resolve_from_text(text: str) -> Tuple[Optional[str], str]:
    """→ (代码, 命中方式)。没命中的代码为 None，方式为空串。

    顺序按可信度排：交易所前缀 > 带后缀 > $代码 > 括号代码。
    括号那种最弱（容易括到年份/金额），放最后。
    """
    t = str(text or "")
    if not t:
        return None, ""

    for m in _EXCHANGE_RE.finditer(t):
        code = _normalize(m.group(1))
        if code:
            return code, "exchange_prefix"

    for m in _SUFFIX_RE.finditer(t):
        code = _normalize(m.group(1))
        if code:
            return code, "suffix"

    for m in _DOLLAR_RE.finditer(t):
        code = _normalize(m.group(1))
        if code:
            return code, "dollar"

    for m in _BRACKET_RE.finditer(t):
        raw = m.group(1)
        # 括号里必须"像代码"：纯 5–6 位数字，或带交易所后缀；排除 2026/500 这类
        if re.fullmatch(r"\d{5,6}(?:\.(?:SH|SZ|BJ|HK))?", raw, re.IGNORECASE) \
                or "." in raw:
            code = _normalize(raw)
            if code:
                return code, "bracket"

    return None, ""


def maybe_resolve(asset: str, text: str) -> Tuple[str, str, str]:
    """给落库路径用：桶标签时尝试解析。

    → (最终 asset, 解析方式, 说明)。非桶标签原样返回，不做任何改动。
    """
    a = str(asset or "").strip().upper()
    try:
        from asset_kinds import classify
        kind, _ = classify(a)
    except Exception:
        kind = "other"
    if kind != "placeholder":
        return a, "", ""
    code, how = resolve_from_text(text)
    if not code:
        return a, "", "新闻里没有显式代码"
    return code, how, f"桶标签 {a} → {code}（{how}）"
