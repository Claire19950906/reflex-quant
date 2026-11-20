#!/usr/bin/env python3
"""公司「生意质量」读数（v488）—— 从三表能看到的第二层。

v405–v474 的公司基本面只有两把尺子：估值分位 + 盈利修正。
它能答"贵不贵、在变好还是变坏"，答不了"这门生意本身在变好还是变坏"：
毛利率是不是靠涨价撑的、经营杠杆有没有失效、赚的钱有没有变成自由现金流、
资产负债表是不是靠借钱维持。

本模块只做**能从已入库三表直接算出来**的四件事，零网络、零插值：

    毛利率趋势   TTM 毛利率 vs 上年同期（8 季）或同季毛利率（5 季）  pp
    经营利润率   TTM 经营利润率 vs 上年同期 / 同季                    pp
    自由现金流率 (TTM 经营现金流 − |资本开支|) / TTM 营收              %
    杠杆率       总负债 / 总资产（最近一期）；权益为负单列

设计纪律（与 v448/v454 一致）：
  · 只判"差"，不奖励"好" —— 毛利率上升不额外加分，避免同一份盈利证据用两遍；
  · 季数不够就不算，缺字段就 None，不拿相邻季度顶替；
  · 资本开支符号跨源不一致（AV 正 / yfinance 负），一律取绝对值再减；
  · 输出 gaps 明确写出"这层仍然看不到什么"（分部/地区/债务到期/指引/电话会/DCF），
    不让读者把"四张比率"误读成"看懂了这家公司"。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

_MIN_TREND_PP = 2.0        # 毛利率下滑 2pp 起算"变差"
_MIN_OPM_PP = 3.0          # 经营利润率下滑 3pp 起算"变差"
_NEG_FCF_MARGIN = -5.0     # 自由现金流率为负且低于 -5% 营收
_HIGH_LEVERAGE = 0.85      # 总负债/总资产 ≥85%（非金融企业）
_MAX_OPM = 200.0           # 经营利润率超过 ±200% 视为口径失真（公允价值/一次性损益）
_MAX_FCF = 300.0           # FCF 率超过 ±300% 视为口径失真（财库资产/极小营收）

_STATIC_GAPS = (
    "无分部/地区拆分：不知道增长来自哪条业务、哪个市场",
    "无债务到期结构与利息覆盖：不知道再融资压力",
    "无管理层指引/订单/backlog：不知道增长的可见度",
    "无财报电话会与 10-K 文本：读不到管理层原话与风险披露",
    "无卖方覆盖与一致预期序列：只有下次 EPS 一个点",
    "无 DCF/EV-EBITDA：估值只有历史分位与同业倍数",
)


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _sum_field(rows: List[Dict[str, Any]], field: str) -> Optional[float]:
    if not rows:
        return None
    vals = [_f(r.get(field)) for r in rows]
    if any(v is None for v in vals):
        return None
    return sum(vals)


def _day(value: Any) -> Optional[float]:
    try:
        return time.mktime(time.strptime(str(value or "")[:10], "%Y-%m-%d"))
    except (ValueError, TypeError, OverflowError):
        return None


def _prior_year_row(rows: List[Dict[str, Any]], cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """找约一年前的同季行（330~400 天，取最接近 365 天的一条）。"""
    cur_day = _day(cur.get("period_end"))
    if cur_day is None:
        return None
    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for rec in rows:
        if rec is cur:
            continue
        d = _day(rec.get("period_end"))
        if d is None:
            continue
        gap = (cur_day - d) / 86400.0
        if 330 <= gap <= 400:
            score = abs(gap - 365.0)
            if best is None or score < best[0]:
                best = (score, rec)
    return best[1] if best else None


def _margin(rows: List[Dict[str, Any]], num: str) -> Optional[float]:
    rev = _sum_field(rows, "revenue")
    val = _sum_field(rows, num)
    if rev is None or val is None or rev <= 0:
        return None
    return round(val / rev * 100.0, 2)


def _quarter_margin(rec: Dict[str, Any], num: str) -> Optional[float]:
    rev = _f(rec.get("revenue"))
    val = _f(rec.get(num))
    if rev is None or val is None or rev <= 0:
        return None
    return round(val / rev * 100.0, 2)


def readout(rows: Optional[List[Dict[str, Any]]],
            yoy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """算生意质量读数。rows 必须已按财季对齐、按 period_end 降序。"""
    rows = [r for r in (rows or []) if r]
    out: Dict[str, Any] = {
        "available": False,
        "gross_margin_ttm": None, "operating_margin_ttm": None,
        "fcf_margin_ttm": None, "leverage": None, "equity_ratio": None,
        "gm_delta_pp": None, "opm_delta_pp": None, "trend_basis": None,
        "flags": [], "evidence": [], "gaps": list(_STATIC_GAPS),
    }
    if not rows:
        out["gaps"] = ["无三表读数"] + out["gaps"]
        return out

    latest = rows[0]
    out["leverage"] = None
    assets = _f(latest.get("total_assets"))
    liab = _f(latest.get("total_liabilities"))
    equity = _f(latest.get("total_equity"))
    if assets not in (None, 0) and liab is not None:
        out["leverage"] = round(liab / assets, 4)
    if assets not in (None, 0) and equity is not None:
        out["equity_ratio"] = round(equity / assets, 4)
    if equity is not None and equity < 0:
        out["flags"].append("negative_equity")
        out["evidence"].append(f"净资产为负（{equity:,.0f}）→ 偿债与增发风险")
    elif out["leverage"] is not None and out["leverage"] >= _HIGH_LEVERAGE:
        out["flags"].append("high_leverage")
        out["evidence"].append(
            f"总负债/总资产 {out['leverage']:.2f}（≥{_HIGH_LEVERAGE:.2f}）→ 杠杆偏高"
        )

    # ── TTM 利润率（近 4 季）────────────────────────────────────────────
    opm_distorted = False
    if len(rows) >= 4:
        out["gross_margin_ttm"] = _margin(rows[:4], "gross_profit")
        opm = _margin(rows[:4], "operating_income")
        if opm is not None and abs(opm) > _MAX_OPM:
            opm_distorted = True
            out["gaps"].insert(0, f"经营利润率 {opm:+.0f}% 口径失真（公允价值/一次性损益），不作结论")
        else:
            out["operating_margin_ttm"] = opm

    # ── FCF 率：只认近 4 季 OCF 与 capex 齐全；capex 取绝对值（跨源符号不一）
    if len(rows) >= 4:
        ocf = _sum_field(rows[:4], "operating_cash_flow")
        capex = _sum_field(rows[:4], "capex")
        rev = _sum_field(rows[:4], "revenue")
        if ocf is not None and capex is not None and rev is not None and rev > 0:
            fcf = round((ocf - abs(capex)) / rev * 100.0, 2)
            if abs(fcf) > _MAX_FCF:
                out["gaps"].insert(0, f"自由现金流率 {fcf:+.0f}% 口径失真（财库资产/极小营收），不作结论")
            else:
                out["fcf_margin_ttm"] = fcf

    # ── 趋势：优先 TTM 对 TTM（8 季），不够则同季对同季（5 季）──────────
    if len(rows) >= 8:
        gm_now = _margin(rows[:4], "gross_profit")
        gm_prev = _margin(rows[4:8], "gross_profit")
        op_now = _margin(rows[:4], "operating_income")
        op_prev = _margin(rows[4:8], "operating_income")
        if gm_now is not None and gm_prev is not None:
            out["gm_delta_pp"] = round(gm_now - gm_prev, 2)
        if op_now is not None and op_prev is not None:
            out["opm_delta_pp"] = round(op_now - op_prev, 2)
        if out["gm_delta_pp"] is not None or out["opm_delta_pp"] is not None:
            out["trend_basis"] = "ttm_vs_ttm"
    if out["trend_basis"] is None:
        prev = _prior_year_row(rows, latest)
        if prev is not None:
            gm_now = _quarter_margin(latest, "gross_profit")
            gm_prev = _quarter_margin(prev, "gross_profit")
            op_now = _quarter_margin(latest, "operating_income")
            op_prev = _quarter_margin(prev, "operating_income")
            if gm_now is not None and gm_prev is not None:
                out["gm_delta_pp"] = round(gm_now - gm_prev, 2)
            if op_now is not None and op_prev is not None:
                out["opm_delta_pp"] = round(op_now - op_prev, 2)
            if out["gm_delta_pp"] is not None or out["opm_delta_pp"] is not None:
                out["trend_basis"] = "same_quarter"

    # 最新 TTM 经营利润率失真时，趋势也不能留（否则 MSTR 会显示 +2756pp）
    if opm_distorted:
        out["opm_delta_pp"] = None

    # ── 只判差 ─────────────────────────────────────────────────────────
    if out["gm_delta_pp"] is not None and out["gm_delta_pp"] <= -_MIN_TREND_PP:
        out["flags"].append("gm_down")
        out["evidence"].append(
            f"毛利率同比 {out['gm_delta_pp']:+.1f}pp（≤-{_MIN_TREND_PP:.0f}pp）→ 定价/成本在恶化"
        )
    if out["opm_delta_pp"] is not None and out["opm_delta_pp"] <= -_MIN_OPM_PP:
        out["flags"].append("opm_down")
        out["evidence"].append(
            f"经营利润率同比 {out['opm_delta_pp']:+.1f}pp（≤-{_MIN_OPM_PP:.0f}pp）→ 经营杠杆失效"
        )
    if out["fcf_margin_ttm"] is not None and out["fcf_margin_ttm"] <= _NEG_FCF_MARGIN:
        out["flags"].append("negative_fcf")
        out["evidence"].append(
            f"自由现金流率 {out['fcf_margin_ttm']:+.1f}% 营收（≤{_NEG_FCF_MARGIN:.0f}%）→ 增长在烧钱"
        )

    # 营收与 EPS 增速背离：EPS 涨而营收没涨，多半来自费用/税/回购，不是生意扩张
    rev_yoy = _f((yoy or {}).get("revenue"))
    eps_yoy = _f((yoy or {}).get("eps_diluted"))
    if rev_yoy is not None and eps_yoy is not None and rev_yoy < 0 <= eps_yoy:
        out["flags"].append("eps_without_revenue")
        out["evidence"].append(
            f"营收同比 {rev_yoy:+.1f}% 但 EPS 同比 {eps_yoy:+.1f}% → 盈利改善不是收入驱动"
        )

    metrics = [
        out["gross_margin_ttm"], out["operating_margin_ttm"],
        out["fcf_margin_ttm"], out["leverage"],
    ]
    out["available"] = sum(1 for m in metrics if m is not None) >= 2

    # 动态缺口：把"这层算不出来"的部分写清楚，不静默
    if out["fcf_margin_ttm"] is None:
        out["gaps"].insert(0, "自由现金流率不可得（OCF/capex 不足 4 季或营收缺失）")
    if out["gm_delta_pp"] is None and out["opm_delta_pp"] is None:
        out["gaps"].insert(0, "利润率趋势不可得（不足 5 个已对齐季度）")
    if out["leverage"] is None:
        out["gaps"].insert(0, "杠杆率不可得（最近一期资产/负债缺失）")
    out["evidence"] = out["evidence"][:4]
    out["gaps"] = out["gaps"][:7]
    return out
