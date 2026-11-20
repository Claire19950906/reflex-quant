#!/usr/bin/env python3
"""A股 / 港股深度读数（v489）—— 用已入库的多期财报算趋势与盈利位置。

与美股 `company_depth.py` 的分工：
  · 美股：三表齐全，算毛利率/经营利润率/FCF/杠杆；
  · A股/港股：东财只有主要指标，没有营业成本/经营现金流明细，
    所以只能算「TTM 同比 + 毛利率趋势 + ROE 趋势 + 盈利位置 + 资产负债率」。
  · 两边都只读入、不改方向分；先让复盘看见，再谈权重。

口径三条：
  · TTM 必须由累计值还原（A股 `_a_ttm`、港股 `_hk_ttm`），不能把三季报当全年；
  · 同比只跟「上一年同一报告期」比，不跨季比（Q3 对 Q2 会把季节性读成增长；
  · 样本不足不下结论：趋势要 ≥3 期、盈利位置要 ≥8 个 TTM 点，缺就写 gap。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 这些缺口不是"没接数据的借口"，是每次输出都要带上的边界，避免把 16 只美股的深度
# 误读成 A股/港股也有同口径覆盖。
_STATIC_GAPS = [
    "无完整三表：只有东财主要指标，看不到营业成本/资产负债表明细",
    "无分部/地区拆分：不知道增长来自哪条业务、哪个市场",
    "无同业可比与财报日历：8 票跨 8 个行业，样本不足不造数",
    "无管理层指引/电话会：看不到增长的可见度",
    "无宏观到个股的传导：政策/汇率/商品只到行业层",
]


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _label_key(label: Any) -> str:
    """去掉港股报告类型里的年份前缀：`2026年三季报` → `三季报`。"""
    return re.sub(r"^\s*\d{4}\s*年?\s*", "", str(label or "")).strip()


def _prior_year(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """上一年同一报告期；找不到就 None（不拿相邻季凑同比）。"""
    if len(history) < 2:
        return None
    latest = history[0]
    date = str(latest.get("report_date") or "")
    if len(date) < 4 or not date[:4].isdigit():
        return None
    prev_year = str(int(date[:4]) - 1)
    key = _label_key(latest.get("period_label"))
    for rec in history[1:]:
        rd = str(rec.get("report_date") or "")
        if rd[:4] == prev_year and _label_key(rec.get("period_label")) == key:
            return rec
    return None


def _yoy(now: Optional[float], base: Optional[float]) -> Optional[float]:
    if now is None or base is None or base <= 0:
        return None
    return round((now / base - 1.0) * 100.0, 2)


def _position(series: List[float]) -> Dict[str, Any]:
    """盈利位置：0 = 历史最低，1 = 历史最高；<8 个点不下结论。"""
    if len(series) < 8:
        return {}
    now = series[-1]
    lo, hi = min(series), max(series)
    if hi <= lo:
        return {}
    pos = (now - lo) / (hi - lo)
    zone = "peak" if pos >= 0.85 else ("trough" if pos <= 0.15 else "mid")
    return {"position": round(pos, 4), "zone": zone,
            "min": round(lo, 4), "max": round(hi, 4), "points": len(series)}


def readout(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """一只 A股/港股的多期读数 → 深度字段 + 护栏 + 缺口。"""
    out: Dict[str, Any] = {
        "available": False, "points": len(history or []),
        "revenue_ttm_yoy": None, "net_profit_ttm_yoy": None, "eps_ttm_yoy": None,
        "gross_margin_delta_pp": None, "roe_delta_pp": None,
        "earnings_position": None, "earnings_zone": None,
        "leverage": None, "flags": [], "gaps": list(_STATIC_GAPS),
    }
    if not history:
        out["gaps"].insert(0, "无季度历史：库里只有最新一期，趋势与盈利位置算不出来")
        return out

    latest = history[0]
    prior = _prior_year(history)
    out["available"] = True
    out["revenue_ttm_yoy"] = _yoy(_num(latest.get("revenue_ttm")),
                                  _num(prior.get("revenue_ttm")) if prior else None)
    out["net_profit_ttm_yoy"] = _yoy(_num(latest.get("net_profit_ttm")),
                                     _num(prior.get("net_profit_ttm")) if prior else None)
    out["eps_ttm_yoy"] = _yoy(_num(latest.get("eps_ttm")),
                              _num(prior.get("eps_ttm")) if prior else None)

    gm_now, gm_base = _num(latest.get("gross_margin")), (
        _num(prior.get("gross_margin")) if prior else None)
    if gm_now is not None and gm_base is not None:
        out["gross_margin_delta_pp"] = round(gm_now - gm_base, 2)

    # ROE 用同一报告期的 YTD 值比（不是年化值），Q3 对 Q3 才可比。
    roe_now, roe_base = _num(latest.get("roe")), (
        _num(prior.get("roe")) if prior else None)
    if roe_now is not None and roe_base is not None:
        out["roe_delta_pp"] = round(roe_now - roe_base, 2)

    lev = _num(latest.get("debt_asset_ratio"))
    if lev is not None:
        out["leverage"] = round(lev / 100.0 if lev > 1 else lev, 4)

    # 盈利位置：按报告期从旧到新排，只用非空 TTM 净利
    series: List[float] = []
    for rec in reversed(history):
        value = _num(rec.get("net_profit_ttm"))
        if value is not None:
            series.append(value)
    pos = _position(series)
    if pos:
        out["earnings_position"] = pos["position"]
        out["earnings_zone"] = pos["zone"]

    flags = out["flags"]
    if out["revenue_ttm_yoy"] is not None and out["revenue_ttm_yoy"] <= -10:
        flags.append("rev_down")
    if out["net_profit_ttm_yoy"] is not None and out["net_profit_ttm_yoy"] <= -20:
        flags.append("np_down")
    if out["gross_margin_delta_pp"] is not None and out["gross_margin_delta_pp"] <= -2:
        flags.append("gm_down")
    if out["roe_delta_pp"] is not None and out["roe_delta_pp"] <= -3:
        flags.append("roe_down")
    if out["leverage"] is not None and out["leverage"] >= 0.85:
        flags.append("high_leverage")
    # 盈利位置高本身不是风险（成长股可以一直在高位），只放进 earnings_zone，不打标签

    if len(history) < 3:
        out["gaps"].insert(0, f"季度历史仅 {len(history)} 期，趋势样本不足（需 ≥3 期）")
    if prior is None:
        out["gaps"].insert(0, "缺上一年同期：TTM 同比与毛利率趋势无法判断")
    if out["earnings_position"] is None:
        out["gaps"].insert(0, f"TTM 净利仅 {len(series)} 个点，盈利位置不下结论（需 ≥8）")
    if out["leverage"] is None:
        out["gaps"].insert(0, "无资产负债率：A股主要指标接口不返回，杠杆读数缺失")
    return out
