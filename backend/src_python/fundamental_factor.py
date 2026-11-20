#!/usr/bin/env python3
"""基本面方向因子（v405）—— 第一个真正进方向判定的公司级基本面。

为什么需要它：v342 之前 DIRECTIONAL_WEIGHTS 六项全是价格/情绪/资金派生，
`news_sentiment 0.40 + market_confirmation 0.10 + structure 0.15 + trend 0.15 +
funding 0.10 + price_fund_divergence 0.10`，公司级基本面权重 **0**。
「超级低估（基本面+技术面）」的评分函数里没有一个财务指标，prompt 还主动
把财报降级为「不构成方向」。于是这个系统对基本面的全部"了解"，其实只有
五只票的 5 个 TTM 比率摆放着当背景板。

本模块把它补成一条**有符号、可解释、能被复盘**的因子：

    估值历史分位  —— 自身 PE 在近一年快照里的分位（越低越偏多）
    盈利修正方向  —— EPS 同比 + 最近一次预期差 + 下次一致预期相对上次实际的抬升

score ∈ [-1, 1]，只有两个分量都拿不到时才 available=False（此时闸门会把
0.08 权重按「取不到≠中性」的规则让回其它因子，不制造假信号）。

数据来源严格分源标注：
  估值分位  ← macro_snapshots（本系统自己每轮落库的 eq.pe，样本数随轮次增长）
  盈利/预期 ← equity_fundamentals 两张表（由 cron 脚本抓取落库）
零网络：全部读库，不阻塞决策链。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import asset_kinds as ak
import cn_fundamentals as cf
import config
import equity_fundamentals as ef

# v406: 分类从「是美股代码 / 不是」两档，拆成 asset_kinds 的五档。
# 原因：`CN_STOCK` / `US_STOCK` / `INDIVIDUAL_STOCK` 这些**占位标签**此前被归成
# 「非公司标的」，等于把「A股这家公司有财报、只是没接」说成「这类资产本来就不用看基本面」。
# 现在：non_equity（真的不适用）/ placeholder（缺口）/ us / a_share / hk。
_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,5}")
_PE_WINDOW_DAYS = 365
_PE_MIN_SAMPLES = 60

_PEER_WEIGHT = 0.30

# v454 两个增量的幅度上限（都**在** 0.08 这条既有权重内部，不新增因子）：
#   同业可比：占"估值"那半边的 30%，即总分里的 0.5×0.30 = 15%
#   宏观倾斜：直接加到分值上，幅度上限对最终方向分的影响上限 0.08×cap
#
# v459 回测校准把 cap 从 0.15 降到 **0.05**：用 FRED 5 年日频 + 板块 ETF 做检验
# （`scripts/macro_driver_calibration.py`），复合 tilt 对同桶 ETF 前瞻 10/20 日收益的
# 中位 IC ≈ 0.00（多空差也在 ±1% 内），四个驱动单拎出来也全部 |对齐 IC| < 0.10 ——
# 也就是说这 0.15 的调整值当时**没有任何可验证的预测力**，却按满额进打分。
# 拉长到 60 日，各驱动与收益的相关性变得很强、但 `risk` 项符号与先验**相反**
# （低波动/低利差之后 60 日收益反而更弱，典型的均值回复），而 60 日非重叠样本仅 ~12 个，
# 不足以拟合权重 —— 所以本轮只做"减配"，不做"翻符号"。
# 想临时恢复原幅度：`TRIDENT_MACRO_TILT_CAP=0.15`。
_MACRO_TILT_CAP = float(os.environ.get("TRIDENT_MACRO_TILT_CAP", "0.05") or 0.05)

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 1800   # 季报级数据，30 分钟足够；批内多条新闻共用同一份读数


def resolve_ticker(asset: Any) -> Optional[str]:
    """把 target_asset 归一成**美股**代码；非美股/占位标签一律 None（不猜）。"""
    kind, code = ak.classify(asset)
    return code if kind == "us" else None


def universe(conn: Optional[sqlite3.Connection] = None, days: int = 30) -> Tuple[str, ...]:
    """要维护基本面的美股清单（基线名单 ∪ 近 N 天决策过的票）。"""
    return ef.universe(conn, days=days)


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _percentile_readout(ticker: str, metric_key: str, field: str,
                        conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """自身某个 TTM 估值倍数（eq.pe / eq.pb）在近一年的历史分位。

    两条取数路径，PE 有倒推序列优先走它：
    1) **倒推序列（主）**：`ef.pe_history` = 日线收盘 ÷ 当时已公布的 EPS TTM。
       每个交易日 1 个点，一年 ~250 个点，窗口和时间权重都是真的。
    2) **快照序列（备）**：macro_snapshots 里每轮落库的值。它的问题是不均匀 ——
       实测 NVDA 45 天攒了 2512 条、九成挤在同一价位，算出来的"97 分位"其实是采样
       密度凑出来的假信号（当时当前 PE 恰好是窗口最低值）。所以走备份时**按日去重**，
       每天只取最后一条，把采样密度拉回"一天一次"。

    v406 补 PB 的原因：COIN/RIOT/MSTR 这类亏损或无 EPS 的票，PE 分位根本算不出来，
    只剩"盈利修正"一个分量在打分，稳健性差。PB 对亏损公司仍然有意义，两条都有时取均值。
    """
    # ── 路径 1：倒推序列（PE 用日线÷EPS；PB 用市值÷净资产）──────────────
    if metric_key == "eq.pe":
        try:
            import equity_fundamentals as _efq
            hist = _efq.pe_history(ticker, days=_PE_WINDOW_DAYS, conn=conn)
        except Exception:
            hist = []
        if len(hist) >= _PE_MIN_SAMPLES:
            values = sorted(v for _, v in hist)
            current = hist[-1][1]
            n = len(values)
            below = sum(1 for v in values if v <= current)
            return {
                field: round(current, 2),
                f"{field}_position": round(below / n * 100.0, 1),
                f"{field}_samples": n,
                f"{field}_samples_ok": True,
                f"{field}_p25": round(values[int(n * 0.25)], 2),
                f"{field}_p75": round(values[min(n - 1, int(n * 0.75))], 2),
                f"{field}_source": "backfill(price/eps_ttm)",
            }

    # v448：PB 倒推（市值快照 ÷ 当时已公布净资产）。样本到 60 天自动越过门槛，
    # 期间只回报"有多少样本"，绝不在不足 60 时给分位结论。
    if metric_key == "eq.pb":
        try:
            import equity_fundamentals as _efq
            pb_hist = _efq.pb_history(ticker, days=_PE_WINDOW_DAYS, conn=conn)
        except Exception:
            pb_hist = []
        if pb_hist:
            values = sorted(v for _, v in pb_hist)
            current = pb_hist[-1][1]
            n = len(values)
            below = sum(1 for v in values if v <= current)
            return {
                field: round(current, 2),
                f"{field}_position": round(below / n * 100.0, 1),
                f"{field}_samples": n,
                f"{field}_samples_ok": n >= _PE_MIN_SAMPLES,
                f"{field}_p25": round(values[int(n * 0.25)], 2),
                f"{field}_p75": round(values[min(n - 1, int(n * 0.75))], 2),
                f"{field}_source": f"backfill(mktcap/equity,{_efq.pb_basis(ticker, conn)})",
            }

    # ── 路径 2：快照序列，按日去重 ────────────────────────────────────────
    owned = conn is None
    try:
        if owned:
            conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
        cutoff = int(time.time()) - _PE_WINDOW_DAYS * 86400
        rows = conn.execute(
            f"""SELECT ts, value FROM macro_snapshots
               WHERE metric_key=? AND value IS NOT NULL AND value > 0 AND ts >= ?
                 AND payload LIKE ? ORDER BY ts""",
            (metric_key, cutoff, f'%"label": "{ticker}"%'),
        ).fetchall()
    except Exception:
        return {}
    finally:
        if owned and conn is not None:
            conn.close()

    daily: Dict[int, float] = {}
    for ts, value in rows:
        if value is None or float(value) <= 0:
            continue
        daily[int(ts) // 86400] = float(value)     # 同一天只留最后一条
    pairs = sorted((d * 86400, v) for d, v in daily.items())
    if not pairs:
        return {}
    current = pairs[-1][1]
    values = sorted(v for _, v in pairs)
    n = len(values)
    below = sum(1 for v in values if v <= current)
    return {
        field: round(current, 2),
        f"{field}_position": round(below / n * 100.0, 1),
        f"{field}_samples": n,
        f"{field}_samples_ok": n >= _PE_MIN_SAMPLES,
        f"{field}_p25": round(values[int(n * 0.25)], 2),
        f"{field}_p75": round(values[min(n - 1, int(n * 0.75))], 2),
        f"{field}_source": "snapshot(按日去重)",
    }


def _pe_readout(ticker: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """自身 PE 的近一年分位（`_percentile_readout` 的薄包装，保留旧入口）。"""
    return _percentile_readout(ticker, "eq.pe", "pe", conn)


def _pb_readout(ticker: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """自身 PB 近一年分位（v406：给亏损/PE 缺失的票补第二把尺子）。"""
    return _percentile_readout(ticker, "eq.pb", "pb", conn)


def _consistency(valuation: Optional[float], revision: Optional[float],
                 evidence: List[str], detail_bits: List[str]) -> Tuple[Optional[float], List[str]]:
    """估值分位与盈利方向的**一致性校验**（防单边偏多）。

    问题：`(50−分位)/50` 这条映射把所有"估值低于自身一年中位数"的票都记成正分。
    但低 PE 分位有两种来源：① 股价跌了（真便宜）；② **盈利下修导致分母变小前的假摔**，
    即价值陷阱 —— 后者恰恰该看空。v405 没有这层校验，NVDA 直接顶到 +0.93。
    对称地：高估值分位若由强盈利增长支撑（成长溢价），也不该全额当利空。

    规则（只折半、不翻转，避免把证据用两遍）：
      · 估值偏正 且 盈利修正 ≤ −0.10 → 估值分量 ×0.5，标 value_trap
      · 估值偏负 且 盈利修正 ≥ +0.30 → 估值分量 ×0.5，标 growth_premium
    """
    if valuation is None or revision is None:
        return valuation, []
    flags: List[str] = []
    used = valuation
    if valuation > 0 and revision <= -0.10:
        used = round(valuation * 0.5, 4)
        flags.append("value_trap")
        evidence.append(f"低估值伴随盈利下修（修正分{revision:+.2f}）→ 估值分量折半，防价值陷阱")
    elif valuation < 0 and revision >= 0.30:
        used = round(valuation * 0.5, 4)
        flags.append("growth_premium")
        evidence.append(f"高估值由强盈利增长支撑（修正分{revision:+.2f}）→ 估值分量折半，非纯看空")
    return used, flags



def _surprise_pct(events: List[Dict[str, Any]]) -> Optional[float]:
    """最近一次「有 actual 也有 estimate」的季度预期差。"""
    reported = [e for e in events
                if e.get("eps_actual") is not None and e.get("eps_estimated") is not None]
    if not reported:
        return None
    last = max(reported, key=lambda e: str(e.get("event_date") or ""))
    est = float(last["eps_estimated"])
    act = float(last["eps_actual"])
    if est == 0:
        return None
    return round((act - est) / abs(est) * 100.0, 2)


# ── v448：盈利位置 / 周期分型（PE 陷阱修正）────────────────────────────────
# 背景（v445 实测）：PE = 收盘 ÷ TTM EPS。周期股在盈利峰值 EPS 最大 → PE 最小 →
# 历史分位 0% → 得分最高。MU 因此拿到 0.931，NVDA 0.939，而这两只恰恰是
# "盈利处在历史最高位"的两只。加 20 家公司的覆盖救不了这个错，只能改打分逻辑。

_TTM_POINTS_MAX = 8        # 只取最近 8 个 TTM 点：够判"位置"，也不让远古周期主导
# 8 个 TTM 点需要 11 个季度 → 约 1000 天。db_pack 默认窗口 800 天只有 6 个点
# （实测 MU 只拿到 6 点、profile=None，PE 陷阱修正整个失效），故这里单独放宽。
_EPS_LOOKBACK_DAYS = 1200
_CYC_STRONG = 1.5          # 周期度阈值：≥1.5 走强周期判型（MU 4.08 / CLSK 5.98）
_POS_HIGH = 0.80           # 盈利位置 ≥80% 视为峰值区
_POS_LOW = 0.20            # ≤20% 视为谷底区
_CASH_MIN_RATIO = 0.60     # 经营现金流/净利 < 0.6 → 盈利含金量差（应计占比高）
_CASH_DISCOUNT = 0.5       # 含金量差时盈利修正折半；只折一次，不与周期折半叠加超过两次
_CASH_RATIO_MAX = 3.0      # 比值 >3 多为一次性营运资本变动，不判
_EARNINGS_PRE_DAYS = 3     # 财报前 3 天：预期先被交易，读数随时重算
_EARNINGS_POST_DAYS = 3    # 财报后 3 天：actual 未回填完，TTM 可能还是旧值


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def ttm_eps_series(events: List[Dict[str, Any]]) -> List[Tuple[str, float]]:
    """财报事件 → TTM EPS 序列（滚动 4 季求和），按日期升序。

    为什么不直接用三表的 eps_diluted：那一列在库里 **8 季全 NULL**（源不提供），
    唯一可靠且带真实公布日的 EPS 序列是 `equity_earnings_events`。用它拼 TTM，
    同时拿到"公布日"这个时间锚，后续做防前视的 PE 倒推也用同一份数据。
    """
    rows: List[Tuple[str, float]] = []
    for ev in events:
        if ev.get("eps_actual") is None:
            continue
        date = str(ev.get("event_date") or "")[:10]
        if not date:
            continue
        try:
            rows.append((date, float(ev["eps_actual"])))
        except (TypeError, ValueError):
            continue
    # 同一天多条（历史上有重复公布）只留最后一条，避免把同一个季度加两遍
    by_date: Dict[str, float] = {}
    for date, eps in rows:
        by_date[date] = eps
    ordered = sorted(by_date.items())
    out: List[Tuple[str, float]] = []
    for i in range(len(ordered) - 3):
        window = ordered[i:i + 4]
        out.append((window[-1][0], round(sum(v for _, v in window), 4)))
    return out[-_TTM_POINTS_MAX:] if len(out) > _TTM_POINTS_MAX else out


def cycle_profile(series: List[Tuple[str, float]]) -> Optional[Dict[str, Any]]:
    """盈利位置 + 周期分型。样本不足 8 个 TTM 点就返回 None（不下结论）。

    位置 pos = (TTM_now − min) / (max − min)   —— 1 = 历史最高
    周期度 cyc = (第2大 − 第2小) / |中位数|     —— IQR 归一，抗单点异常

    读法：位置高 + 周期度低 = 成长股，位置高不构成风险（AAPL cyc 0.30）；
    位置高 + 周期度高 = 强周期见顶（MU cyc 4.08），此时"PE 便宜"是假便宜。
    """
    values = [float(v) for _, v in series]
    if len(values) < 8:
        return None
    now = values[-1]
    lo, hi = min(values), max(values)
    pos = 0.5 if hi <= lo else (now - lo) / (hi - lo)
    ordered = sorted(values)
    denom = abs(_median(values))
    cyc = (ordered[-2] - ordered[1]) / denom if denom else 0.0
    regime = "strong_cyclical" if cyc >= _CYC_STRONG else "steady"
    if pos >= _POS_HIGH:
        zone = "peak"
    elif pos <= _POS_LOW:
        zone = "trough"
    else:
        zone = "mid"
    return {
        "position": round(pos, 4),
        "cyclicity": round(cyc, 2),
        "regime": regime,
        "zone": zone,
        "now": round(now, 3),
        "min": round(lo, 3),
        "max": round(hi, 3),
        "points": len(values),
    }


def _cycle_fold(valuation: Optional[float], revision: Optional[float],
                profile: Optional[Dict[str, Any]],
                evidence: List[str]) -> Tuple[Optional[float], Optional[float], List[str]]:
    """强周期见顶/见底时，把估值与盈利修正各折半（不翻转）。

    为什么盈利修正也要折：周期峰值上的 EPS 同比本身就是周期放大出来的
    （MU +808%），把它按 ±40% 打满等于**再夸一次**同一个周期证据。
    折半不是"同一证据用两遍"，而是承认这个数字在峰值处不可信。

    平稳型（成长股）一律不动 —— 位置 100% 不等于危险（AAPL/NVDA cyc 0.30/0.75）。
    与 v406 的 value_trap / growth_premium 护栏并列，最多叠加两层折半（不叠到 1/8）。
    """
    if not profile or profile.get("regime") != "strong_cyclical":
        return valuation, revision, []
    zone = profile.get("zone")
    if zone not in ("peak", "trough"):
        return valuation, revision, []
    flags = ["peak_earnings" if zone == "peak" else "trough_earnings"]
    used_val = valuation
    used_rev = revision
    if valuation is not None:
        used_val = round(valuation * 0.5, 4)
    if revision is not None:
        used_rev = round(revision * 0.5, 4)
    pos_pct = float(profile.get("position") or 0.0) * 100.0
    evidence.append(
        f"盈利位置 {pos_pct:.0f}%（近{profile.get('points')}个TTM点区间 "
        f"{profile.get('min')}~{profile.get('max')}），周期度 {profile.get('cyclicity')}"
        + ("→ 强周期见顶：估值/盈利修正各折半，防把周期峰值当便宜"
           if zone == "peak" else
           "→ 强周期谷底：估值/盈利修正各折半，亏损底部的便宜同样不可信")
    )
    return used_val, used_rev, flags


# ── v448：亏损股专用口径 ────────────────────────────────────────────────────
# v445 实测：COIN/RIOT/CLSK/MARA 的 PE 无意义（TTM 亏损 → 跳过），PB 分位样本不足，
# 于是分数只由 EPS 同比驱动 → 四只票挤在 −0.44 / −1.0 两个值上，等于把
# "周期底部亏损"和"生意变差"读成同一件事。亏损股要看的是"生意还在不在长"，
# 而不是"亏了多少"。

_LOSS_WEIGHTS = {"rev_yoy": 0.40, "gm_trend": 0.20, "ocf_margin": 0.25, "ocf_trend": 0.15}


def _sum_field(rows: List[Dict[str, Any]], field: str) -> Optional[float]:
    vals = [r.get(field) for r in rows]
    if not vals or any(v is None for v in vals):
        return None
    try:
        return float(sum(vals))
    except (TypeError, ValueError):
        return None


def cash_quality(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """盈利含金量：TTM 经营现金流 / TTM 净利（只判"差"，不奖励"好"）。

    为什么需要这一把尺子：EPS 与净利都是会计口径，应计项（应收、存货、资本化）
    可以把利润做得很漂亮而现金没进来。TTM OCF/NI < 0.6 说明"赚的是账面利润"，
    这时盈利上修的读数不该给满权重 —— 但也不该反过来奖励 OCF/NI 高，
    否则同一份"盈利不错"的证据会被用了两遍。

    数据诚实：需要近四季 OCF 与净利齐全、且净利 > 0；净利占营收 <1% 时比值失真
    （分母趋零会让 0.1 亿净利配 10 亿现金流算出 100 倍），直接不判。
    """
    out: Dict[str, Any] = {"available": False, "ratio": None, "discount": 1.0,
                           "flags": [], "evidence": []}
    if len(rows) < 4:
        return out
    now4 = rows[:4]
    ni = _sum_field(now4, "net_income")
    ocf = _sum_field(now4, "operating_cash_flow")
    rev = _sum_field(now4, "revenue")
    if ni is None or ocf is None or ni <= 0:
        return out
    if rev and rev > 0 and ni < 0.01 * rev:
        return out
    ratio = round(ocf / ni, 3)
    if ratio > _CASH_RATIO_MAX:
        return out
    out.update({"available": True, "ratio": ratio})
    if ratio < _CASH_MIN_RATIO:
        out["discount"] = _CASH_DISCOUNT
        out["flags"] = ["low_cash_quality"]
        out["evidence"] = [
            f"TTM 经营现金流/净利 {ratio:.2f}（<{_CASH_MIN_RATIO}）→ 盈利含金量差，盈利修正折半"
        ]
    else:
        out["evidence"] = [f"TTM 经营现金流/净利 {ratio:.2f}（≥{_CASH_MIN_RATIO}，不额外加减分）"]
    return out


def stmt_growth(rows: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """季度同比（最新季 vs 去年同季）：三表只有 5–7 期时唯一不造假的口径。

    v452 为什么要它：yfinance 一家给 5–7 期，凑不出"近四季 vs 前四季"的 TTM 同比，
    于是 META/MSFT/TSLA/PLTR/SMCI 这些票明明有三表，盈利修正却是 None → 分数恒为 0，
    "覆盖 16/16"就变成了只看行数的假覆盖。用同季对比（Q3'26 vs Q3'25）是真实数据，
    只是降一档精度：EPS 可得就用 EPS，否则用净利并打 0.8 折。

    负基期不算同比（负负得正会把"亏损扩大"读成"增长"）。
    """
    if not rows:
        return None
    ordered = sorted(rows, key=lambda r: str(r.get("period_end") or ""), reverse=True)
    cur = ordered[0]
    cur_p = str(cur.get("period_end") or "")[:10]
    if len(cur_p) != 10:
        return None
    # v488：去年同季不再用"把年份减一"做字符串精确匹配。
    # 同一财季跨源的期末日会漂 1~4 天（2026-09-26 vs 2025-09-27），
    # 精确匹配会把整条同季同比断掉；改为"约一年前、最近的一条"。
    try:
        cur_day = time.mktime(time.strptime(cur_p, "%Y-%m-%d"))
    except (ValueError, TypeError):
        return None
    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for rec in ordered[1:]:
        p = str(rec.get("period_end") or "")[:10]
        try:
            gap = cur_day - time.mktime(time.strptime(p, "%Y-%m-%d"))
        except (ValueError, TypeError):
            continue
        if 330 <= gap / 86400.0 <= 400:
            candidates.append((abs(gap / 86400.0 - 365.0), rec))
    if not candidates:
        return None
    base = min(candidates, key=lambda x: x[0])[1]
    base_p = str(base.get("period_end") or "")[:10]
    out: Dict[str, Any] = {"cur_period": cur_p, "base_period": base_p}
    for field in ("revenue", "net_income", "eps_diluted"):
        c_val, b_val = cur.get(field), base.get(field)
        try:
            c_f, b_f = float(c_val), float(b_val)
        except (TypeError, ValueError):
            out[field] = None
            continue
        out[field] = round((c_f / b_f - 1.0) * 100.0, 2) if b_f > 0 else None
    return out if any(out.get(f) is not None for f in ("revenue", "net_income", "eps_diluted")) else None


def loss_maker_readout(rows: List[Dict[str, Any]],
                       eps_yoy: Optional[float]) -> Dict[str, Any]:
    """亏损股的四个读数：营收增速 / 毛利率趋势 / 经营现金流率 / 现金流趋势。

    rows 按 period_end 降序（最新在前），需要 8 季才能算"近四季 vs 前四季"。
    三表不够时退回 EPS 同比单因子，并标 `loss_eps_only` —— 宁可承认只有一个读数，
    也不要拿"亏损幅度"当唯一尺子。
    """
    components: Dict[str, Any] = {}
    evidence: List[str] = []
    flags: List[str] = []
    if len(rows) >= 8:
        now4, prev4 = rows[:4], rows[4:8]
        rev_now = _sum_field(now4, "revenue")
        rev_prev = _sum_field(prev4, "revenue")
        ocf_now = _sum_field(now4, "operating_cash_flow")
        ocf_prev = _sum_field(prev4, "operating_cash_flow")
        parts: Dict[str, float] = {}
        if rev_now and rev_prev:
            yoy = (rev_now / rev_prev - 1.0) * 100.0
            parts["rev_yoy"] = round(_clamp(yoy / 30.0), 4)      # ±30% 打满
            evidence.append(f"TTM 营收同比 {yoy:+.1f}%")
        gp_now = _sum_field(now4, "gross_profit")
        gp_prev = _sum_field(prev4, "gross_profit")
        if None not in (gp_now, gp_prev) and rev_now and rev_prev:
            gm_now = gp_now / rev_now * 100.0
            gm_prev = gp_prev / rev_prev * 100.0
            delta = gm_now - gm_prev
            parts["gm_trend"] = round(_clamp(delta / 5.0), 4)     # ±5pp 打满
            evidence.append(f"毛利率 {gm_prev:.1f}% → {gm_now:.1f}%（{delta:+.1f}pp）")
        if ocf_now is not None and rev_now:
            margin = ocf_now / rev_now * 100.0
            parts["ocf_margin"] = round(_clamp(margin / 25.0), 4)  # ±25% 打满
            evidence.append(f"TTM 经营现金流/营收 {margin:+.1f}%")
        if ocf_now is not None and ocf_prev is not None and rev_now:
            delta_pp = (ocf_now - ocf_prev) / rev_now * 100.0
            parts["ocf_trend"] = round(_clamp(delta_pp / 20.0), 4)
            evidence.append(f"经营现金流同比改善 {delta_pp:+.1f}pp（占营收）")
        usable = [(k, v) for k, v in parts.items() if k in _LOSS_WEIGHTS]
        if usable:
            wsum = sum(_LOSS_WEIGHTS[k] for k, _ in usable)
            score = round(sum(v * _LOSS_WEIGHTS[k] for k, v in usable) / wsum, 4)
            components.update(parts)
            components["loss_score"] = score
            return {"available": True, "score": score, "components": components,
                    "evidence": evidence, "flags": flags}

    # 三表缺 → 单因子兜底（ERP 上 MARA 就是这种情况：只有财报事件、没有三表）
    if eps_yoy is not None:
        # ±60% 打满、且**封顶 0.6**：只有 EPS 一个读数时不该让它顶到 ±1.0，
        # 否则 MARA 又回到"亏得越多分越低"的老毛病（v445 实测 −214% → −1.0）
        score = round(max(-0.6, min(0.6, float(eps_yoy) / 60.0)), 4)
        flags.append("loss_eps_only")
        components["eps_yoy"] = round(float(eps_yoy), 2)
        components["loss_score"] = score
        evidence.append(f"TTM EPS 同比 {float(eps_yoy):+.1f}%（三表不足，仅单因子口径）")
        return {"available": True, "score": score, "components": components,
                "evidence": evidence, "flags": flags}
    return {"available": False, "score": 0.0, "components": components,
            "evidence": evidence, "flags": flags}



# ── 「简称→代码」字典（cron 抓、决策链路只读）──────────────────────────────
# 静态别名表只有 74 个名字，实测 28 条占位决策只解出 4 条（14%）；缺口全是没收录的
# 中小盘简称（东山精密、中科飞测…）。库里 `cn_name_map` 有 5800 只 A股 + 2700 只港股
# 的简称，按需读一次、进程内缓存 30 分钟，决策链路依旧零联网。
_NAME_MAP_TTL = 1800
_name_map_cache: Dict[str, Tuple[float, Dict[str, Tuple[str, str]]]] = {}
_name_map_lock = threading.Lock()


def _name_map_for(asset: object) -> Dict[str, Tuple[str, str]]:
    """按占位标签挑市场，返回该市场的简称字典；读不到就返回空字典（不影响主流程）。"""
    ph = str(asset or "").strip().upper()
    market = "HK" if ph == "HK_STOCK" else ("A" if ph == "CN_STOCK" else None)
    if not market:
        return {}
    now = time.time()
    with _name_map_lock:
        hit = _name_map_cache.get(market)
        if hit and now - hit[0] < _NAME_MAP_TTL:
            return hit[1]
    try:
        data = cf.name_map(market=market)
    except Exception:
        data = {}
    with _name_map_lock:
        _name_map_cache[market] = (now, data)
    return data


_NULL_DISP = {"dispersion": None, "dispersion_level": None, "confidence": None,
              "bull_weight": None, "bear_weight": None}


def _dispersion(pairs: List[Tuple[Optional[float], float]]) -> Dict[str, Any]:
    """v474：公司侧分歧度 —— 加权标准差 + 可信度 + 多空权重。

    公司票也会自相矛盾："估值说便宜、盈利在砍"就是最经典的价值陷阱形态。
    以前这种组合只会被 `_consistency` 折半，但折半后没有任何标记说明"这里是吵架"，
    复盘看不出"分数被折"和"分数本来就低"的区别。

    返回五个字段，口径与 alt 侧（v472）完全一致，下游不用写两套：
        dispersion / dispersion_level / confidence / bull_weight / bear_weight
    """
    usable = [(float(v), float(w)) for v, w in pairs
              if v is not None and w is not None and w > 0]
    if len(usable) < 2:
        return {"dispersion": None, "dispersion_level": None, "confidence": None,
                "bull_weight": None, "bear_weight": None}
    wsum = sum(w for _, w in usable)
    mean = sum(v * w for v, w in usable) / wsum
    disp = (sum(w * (v - mean) ** 2 for v, w in usable) / wsum) ** 0.5
    level = "高" if disp >= 0.6 else ("中" if disp >= 0.35 else "低")
    return {"dispersion": round(disp, 3), "dispersion_level": level,
            "confidence": round(max(0.0, 1.0 - disp / 1.5), 3),
            "bull_weight": round(sum(w for v, w in usable if v > 0.05), 2),
            "bear_weight": round(sum(w for v, w in usable if v < -0.05), 2)}


def build(asset: Any, conn: Optional[sqlite3.Connection] = None,
          use_cache: bool = True, hint: Any = None) -> Dict[str, Any]:
    """算一只标的的基本面方向读数。任何异常都降级为 available=False。

    v406 起是**路由器**：按 asset_kinds 的五种类别分发，并把"为什么没有"说清楚
    （`reason` 字段）：non_equity 是真的不适用，placeholder 是我们还没解析出代码，
    no_data 是解析出来了但库里没财报。三者混成一句话，复盘就分不清缺口和边界。
    """
    kind, code = ak.classify(asset)
    label = str(asset or "").strip().upper()

    # v460: 非公司标的（黄金/原油/美元/外汇/加密）走在最前面 —— 这些代码形态
    # （XAUUSD / BTCUSDT / EURUSD）会被 classify 误判成"美股 ticker 但库里没财报"，
    # 于是以前一直记成 no_data（缺口），其实是有读数的，只是口径不同。
    try:
        import alt_fundamental as _alt
        _alt_label = _alt.canonical(asset)
    except Exception:
        _alt, _alt_label = None, None
    if _alt is not None and _alt_label:
        try:
            alt = _alt.build(asset, conn=conn, use_cache=use_cache)
        except Exception as exc:
            alt = {"available": False, "score": 0.0, "reason": "error",
                   "detail": f"非公司基本面模块不可用: {type(exc).__name__}: {str(exc)[:60]}",
                   "components": {}, "evidence": [], "gaps": []}
        alt.setdefault("kind", "alt")
        alt.setdefault("reason", "ok" if alt.get("available") else "insufficient")
        return alt

    # v406: URL 里常常只给「CN_STOCK」这种类别标签。新闻正文里点名了公司时，
    # 就按同一市场反解出代码 —— 否则 A股/港股通路永远触发率≈0。
    _hint_resolved = None
    if kind == "placeholder" and hint:
        _res = ak.resolve_from_text(hint, asset, _name_map_for(asset))
        if _res:
            _hint_resolved = _res
            kind, code = _res

    if kind == "non_equity":
        return {"available": False, "score": 0.0, "ticker": None,
                "kind": kind, "reason": "non_equity", "market": None,
                "detail": f"{label} 非公司标的，无公司级基本面（N/A，不是缺口）",
                "components": {}, "evidence": [], **_NULL_DISP}
    if kind == "placeholder":
        return {"available": False, "score": 0.0, "ticker": None,
                "kind": kind, "reason": "placeholder", "market": None,
                "detail": (f"{label or '空'} 是占位标签：新闻未解析出具体股票代码，"
                           f"基本面读不到（是**缺口**，不是不适用；勿据此推断其财报；"
                           f"新闻点名了公司名也请给代码）"),
                "components": {}, "evidence": [], **_NULL_DISP}
    if kind == "other_market":
        return {"available": False, "score": 0.0, "ticker": code, "kind": kind,
                "reason": "market_unsupported", "market": None,
                "detail": (f"{label}→{code}：该市场（韩/日/台等）基本面未接入，"
                           f"只有行情（是**缺口**，不是不适用）"),
                "components": {}, "evidence": [], **_NULL_DISP}
    if kind in ("a_share", "hk"):
        try:
            out = cf.build(code or "", "A" if kind == "a_share" else "HK",
                           conn=conn, use_cache=use_cache)
        except Exception as exc:
            return {"available": False, "score": 0.0, "ticker": code, "kind": kind,
                    "reason": "error", "market": "A" if kind == "a_share" else "HK",
                    "detail": f"基本面因子出错: {type(exc).__name__}: {str(exc)[:70]}",
                    "components": {}, "evidence": [], **_NULL_DISP}
        out.setdefault("kind", kind)
        out.setdefault("reason", "ok" if out.get("available") else "no_data")
        return out

    ticker = code or ""
    if not ticker:
        return {"available": False, "score": 0.0, "ticker": None, "kind": kind,
                "reason": "placeholder", "market": "US",
                "detail": f"{label or '空'} 未能归一成美股代码", "components": {}, "evidence": [], **_NULL_DISP}
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(ticker)
            if hit and hit[0] > time.time():
                return dict(hit[1])
    try:
        out = _build_uncached(ticker, conn, use_cache)
        out.setdefault("kind", kind)
        out.setdefault("market", "US")
        out.setdefault("reason", "ok" if out.get("available") else "no_data")
        return out
    except Exception as exc:
        return {"available": False, "score": 0.0, "ticker": ticker, "kind": kind,
                "reason": "error", "market": "US",
                "detail": f"基本面因子出错: {type(exc).__name__}: {str(exc)[:70]}",
                "components": {}, "evidence": [], **_NULL_DISP}



def _build_uncached(ticker: str, conn: Optional[sqlite3.Connection],
                    use_cache: bool) -> Dict[str, Any]:
    pack = ef.db_pack(conn, tickers=(ticker,), days=_EPS_LOOKBACK_DAYS)
    if ticker not in (pack.get("summary") or {}):
        return {"available": False, "score": 0.0, "ticker": ticker,
                "detail": f"{ticker} 库中无财报（需跑 cron_equity_fundamentals.py）",
                "components": {}, "evidence": [], **_NULL_DISP}

    summary = pack["summary"][ticker]
    ttm = summary.get("ttm") or {}
    yoy = summary.get("yoy") or {}
    events = [e for e in (pack.get("earnings") or []) if e.get("ticker") == ticker]
    components: Dict[str, Optional[float]] = {}
    evidence: List[str] = []
    detail_bits: List[str] = []

    # ── 0) 生意质量（v488）：毛利率/经营利润率趋势、FCF 率、杠杆率 ─────────
    # 只增加"能看见什么"，不改变方向分：本轮不给它权重，先让复盘与看板能读到。
    # 为什么不入分：这四项是同一份财报的第二次使用，未做 A/B 前加权重等于
    # 用"看起来合理"替代"验证过"（与 0.08 权重仍未被影子账本验证是同一问题）。
    depth: Dict[str, Any] = {}
    try:
        import company_depth as _cd
        depth = _cd.readout((pack.get("statements") or {}).get(ticker) or [], yoy)
        for key in ("gross_margin_ttm", "operating_margin_ttm", "fcf_margin_ttm",
                    "leverage", "equity_ratio", "gm_delta_pp", "opm_delta_pp",
                    "trend_basis"):
            components[f"depth_{key}"] = depth.get(key)
        if depth.get("flags"):
            components["depth_flags"] = list(depth["flags"])
    except Exception as exc:
        depth = {"available": False, "flags": [], "evidence": [],
                 "gaps": [f"生意质量读数不可用：{type(exc).__name__}"]}
        detail_bits.append("生意质量读数不可用")

    # ── 1) 估值分位（PE + PB 两把尺子）────────────────────────────────────
    # v406 为什么加 PB：COIN/RIOT 的 PE 是负的（亏损）、MSTR 历史 PE 从未取到 ——
    # 三只票的"估值"分量恒为 None，实际只剩盈利修正一个分量在打分。
    # PB 对亏损公司仍然有意义，有 PE 时两者取均值（等权，不预设哪个更准）。
    valuation = None
    pe_info = _pe_readout(ticker, conn)
    pb_info = _pb_readout(ticker, conn)
    pieces: List[float] = []
    pb_piece: Optional[float] = None
    if pe_info and pe_info.get("pe_samples_ok"):
        pos = float(pe_info["pe_position"])
        pieces.append(_clamp((50.0 - pos) / 50.0))          # 50% 分位 = 0，两端 ±1
        evidence.append(
            f"PE {pe_info['pe']:.2f}x，近{_PE_WINDOW_DAYS}天 {pos:.0f}% 分位"
            f"（区间 {pe_info['pe_p25']:.1f}~{pe_info['pe_p75']:.1f}x，n={pe_info['pe_samples']}）"
        )
    elif pe_info:
        detail_bits.append(f"PE分位样本仅{pe_info.get('pe_samples')}条<{_PE_MIN_SAMPLES}，按铁律不作结论")
    else:
        detail_bits.append("无正PE历史分位（快照未累积或TTM亏损，PE分位不适用）")
    if pb_info and pb_info.get("pb_samples_ok"):
        # v456：财库票（MSTR 等）的 PB 不参与估值判断。
        # 它的净资产就是 BTC，PB 低只说明"市值贴着币价"，不是"股票便宜" ——
        # 拿它做分位等于用币价的变化冒充估值变化，会把一只高溢价杠杆票打成"极便宜"。
        try:
            import peer_valuation as _pv_pb
            _pb_block_reason = _pv_pb.pb_excluded(ticker)
        except Exception:
            _pb_block_reason = None
        if _pb_block_reason:
            detail_bits.append(f"PB {pb_info['pb']:.2f}x 不参与估值：{_pb_block_reason}")
            components["pb_excluded"] = _pb_block_reason
        else:
            pos = float(pb_info["pb_position"])
            pb_piece = _clamp((50.0 - pos) / 50.0)
            pieces.append(pb_piece)
            evidence.append(
                f"PB {pb_info['pb']:.2f}x，近{_PE_WINDOW_DAYS}天 {pos:.0f}% 分位（n={pb_info['pb_samples']}）"
            )
    elif pb_info:
        detail_bits.append(f"PB分位样本仅{pb_info.get('pb_samples')}条<{_PE_MIN_SAMPLES}，不参与估值分")
    if pieces:
        valuation = round(sum(pieces) / len(pieces), 4)
    components["valuation_percentile"] = valuation
    components["valuation_basis"] = (["pe"] if pe_info.get("pe_samples_ok") else []) + \
                                    (["pb"] if (pb_info.get("pb_samples_ok")
                                                and not components.get("pb_excluded")) else [])

    # ── 1b) 同业可比（v454）：自身分位只答"跟自己比"，这里补"跟同行比" ──────
    # 只读库 + 本地缓存（allow_network=False）：决策链是热路径，不为一个同业查询卡住。
    # 网络补数在 cron 的 peer_valuation.warm() 里做。
    peer_score: Optional[float] = None
    try:
        import peer_valuation as _pv
        peer = _pv.build(ticker, conn, allow_network=False)
        if peer.get("available"):
            peer_score = float(peer["score"])
            components["peer_rel_score"] = round(peer_score, 4)
            components["peer_sector"] = peer.get("sector")
            components["peer_detail"] = peer.get("detail")
            evidence.extend(peer.get("evidence") or [])
            if valuation is None:
                valuation = round(peer_score * 0.85, 4)   # 只有横向没有纵向 → 打 0.85
                detail_bits.append("自身历史分位不可得，估值分量仅用同业可比（×0.85）")
            else:
                valuation = round((1.0 - _PEER_WEIGHT) * float(valuation)
                                  + _PEER_WEIGHT * peer_score, 4)
            components["valuation_percentile"] = valuation
        else:
            components["peer_detail"] = peer.get("detail") or ""
    except Exception as exc:
        components["peer_detail"] = f"同业可比不可用：{type(exc).__name__}"

    # ── 2) 财报窗口：分数随时会被重算，必须让复盘看见 ────────────────────
    surprise = _surprise_pct(events)
    if surprise is not None:
        evidence.append(f"最近一次 EPS 预期差 {surprise:+.1f}%")

    # v448-fix: 往回多看 3 天。财报窗口是 ±3 天对称的，只认"未来"会漏掉
    # 「昨天/今天公布」这种最该折半的情形（实测 NVDA 事件日期 2026-11-17，
    # 按 >=今天 过滤则整根线断掉，days=None，窗口护栏一次没触发过）。
    _win_from = time.strftime("%Y-%m-%d", time.localtime(time.time() - _EARNINGS_POST_DAYS * 86400))
    upcoming = [e for e in events if e.get("eps_estimated") is not None
                and e.get("eps_actual") is None and str(e.get("event_date") or "") >= _win_from]
    days = None
    nxt = None
    if upcoming:
        nxt = min(upcoming, key=lambda e: str(e.get("event_date")))
        try:
            days = (datetime.strptime(nxt["event_date"], "%Y-%m-%d").date()
                    - datetime.now().date()).days
        except Exception:
            days = None
        if days is None or days >= 0:
            evidence.append(f"下次财报 {nxt['event_date']}（{days}天后）")
        else:
            evidence.append(f"财报事件 {nxt['event_date']} 已过 {-days} 天（actual 未回填）")

    # ── 3) 盈利路径分流（v448）：盈利股看估值+修正，亏损股看生意本身 ────
    series = ttm_eps_series(events)
    profile = cycle_profile(series)
    # v452：事件表没有实际 EPS 时（新票常见），退回三表的同季同比
    q_growth = stmt_growth((pack.get("statements") or {}).get(ticker) or [])
    components["earnings_position"] = round(float(profile["position"]), 4) if profile else None
    # v490：盈利位置分区与样本期数也落进 components —— 影子账本要按 zone / 期数分组
    components["earnings_zone"] = str(profile.get("zone")) if profile else None
    components["depth_points"] = summary.get("n_quarters")
    components["cyclicity"] = float(profile["cyclicity"]) if profile else None
    components["profile"] = (f"{profile['regime']}/{profile['zone']}") if profile else None

    profit_ref = ttm.get("eps_diluted")
    if profit_ref is None:
        profit_ref = ttm.get("net_income")
    loss_maker = bool(float(profit_ref) <= 0) if profit_ref is not None else False
    components["path"] = "loss_maker" if loss_maker else "profitable"
    flags: List[str] = []
    # v488：生意质量护栏与证据并进主读数（只做标签与解释，不改方向分）
    flags.extend(depth.get("flags") or [])
    evidence.extend((depth.get("evidence") or [])[:2])

    # 盈利股：盈利修正方向（EPS 同比 0.65 + 预期差 0.25 + 一致预期抬升 0.10）
    growth = None
    eps_yoy = yoy.get("eps_diluted")
    if eps_yoy is not None:
        growth = round(_clamp(float(eps_yoy) / 40.0), 4)      # ±40% 打到饱和
        components["growth_basis"] = "ttm_eps"
        evidence.append(f"TTM EPS 同比 {float(eps_yoy):+.1f}%")
    elif q_growth and (q_growth.get("eps_diluted") is not None
                       or q_growth.get("net_income") is not None):
        g_eps = q_growth.get("eps_diluted")
        g_val = g_eps if g_eps is not None else q_growth.get("net_income")
        growth = round(_clamp(float(g_val) / 40.0) * (1.0 if g_eps is not None else 0.8), 4)
        components["growth_basis"] = ("quarter_yoy_eps" if g_eps is not None
                                      else "quarter_yoy_ni")
        evidence.append(
            f"同季同比 {'EPS' if g_eps is not None else '净利'} {float(g_val):+.1f}%"
            f"（{q_growth['cur_period']} vs {q_growth['base_period']}，TTM 期数不足降档）")
        if g_eps is None:
            detail_bits.append("EPS 缺失，用净利同季同比并打 0.8 折")
    elif yoy.get("revenue") is not None:
        growth = round(_clamp(float(yoy["revenue"]) / 30.0), 4)
        detail_bits.append("EPS 同比缺失，用营收同比代理")
        evidence.append(f"TTM 营收同比 {float(yoy['revenue']):+.1f}%（EPS同比缺失，代理）")

    consensus = None
    # 一致预期只在"还没公布"时才有意义；刚过去的财报用它算"抬升"是错位的
    if upcoming and surprise is not None and days is not None and days >= 0:
        est_next = float(nxt["eps_estimated"])
        last_actual = next((float(e["eps_actual"]) for e in sorted(
            [x for x in events if x.get("eps_actual") is not None],
            key=lambda x: str(x.get("event_date") or ""), reverse=True)), None)
        if last_actual not in (None, 0):
            delta = (est_next / last_actual - 1.0) * 100.0
            consensus = round(_clamp(delta / 15.0), 4)    # ±15% 预期抬升打到饱和
            evidence.append(f"下次一致预期 EPS {est_next:.2f}（较上次实际 {delta:+.1f}%）")

    parts = [(growth, 0.65), (round(_clamp(surprise / 10.0), 4) if surprise is not None else None, 0.25),
             (consensus, 0.10)]
    usable = [(v, w) for v, w in parts if v is not None]
    revision = None
    if usable:
        wsum = sum(w for _, w in usable)
        revision = round(sum(v * w for v, w in usable) / wsum, 4)
    components["earnings_revision"] = revision
    components["consensus_tilt"] = consensus

    score: Optional[float] = None
    if loss_maker:
        # 亏损股不走 PE / EPS 同比的尺子：COIN/RIOT/CLSK/MARA 会全挤在同一个分数上。
        rows = (pack.get("statements") or {}).get(ticker) or []
        loss = loss_maker_readout(rows, eps_yoy)
        for key in ("rev_yoy", "gm_trend", "ocf_margin", "ocf_trend", "eps_yoy"):
            if loss["components"].get(key) is not None:
                components[key] = loss["components"][key]
        components["loss_score"] = loss["components"].get("loss_score")
        flags.extend(loss["flags"])
        evidence.extend(loss["evidence"])       # 追在 EPS 同比之后，不覆盖
        if loss["available"]:
            score = float(loss["score"])
            if pb_piece is not None:
                # PB 只给 0.3：亏损公司的净资产受减值/增发影响，可信度低于现金流
                score = round(0.7 * score + 0.3 * pb_piece, 4)
                evidence.append(f"叠加 PB 分位 {pb_piece:+.2f}（权重 0.3）")
            components["valuation_used"] = pb_piece
        else:
            detail_bits.append("亏损且三表不足 8 季 → 无可用读数")
    else:
        # ── 4) 周期分型修正（v448）：强周期见顶/见底，把"便宜"和"高增长"各折半
        valuation_used, revision_used, cyc_flags = _cycle_fold(
            valuation, revision, profile, evidence)
        flags.extend(cyc_flags)
        # ── 4b) 盈利含金量（v454）：净利好看但现金流不跟 → 盈利修正折半
        cash = cash_quality((pack.get("statements") or {}).get(ticker) or [])
        if cash["available"]:
            components["ocf_ni_ratio"] = cash["ratio"]
            evidence.extend(cash["evidence"])
            if (cash["discount"] < 1.0 and revision_used is not None
                    and revision_used > 0):
                revision_used = round(revision_used * cash["discount"], 4)
                flags.extend(cash["flags"])
        # ── 5) 一致性校验（v406）：低估值 + 盈利下修 ≠ 看多，是价值陷阱 ──
        valuation_used, cons_flags = _consistency(valuation_used, revision_used,
                                                  evidence, detail_bits)
        flags.extend(cons_flags)
        components["valuation_used"] = valuation_used
        components["earnings_revision_used"] = revision_used
        comps = [(v, w) for v, w in ((valuation_used, 0.5), (revision_used, 0.5)) if v is not None]
        if comps:
            wsum = sum(w for _, w in comps)
            score = round(sum(v * w for v, w in comps) / wsum, 4)
            if valuation_used is None:
                # v452：只有盈利面、没有估值分位时，方向分打到 0.7 —— 半张画面不该按满音量喊
                score = round(score * 0.7, 4)
                flags.append("earnings_only")
                evidence.append("估值分位不可得（PE/PB 样本不足）：仅盈利面，方向分 ×0.7")

    # ── 5b) 宏观传导（v454）：把 33 组 FRED 从"prompt 背景板"变成一条可复现的倾斜 ──
    # 幅度上限 ±0.15，乘 0.08 权重后对最终方向分的影响 ≤1.2% —— 宏观是背景，不是主角。
    if score is not None:
        try:
            import macro_equity_regime as _mr
            mt = _mr.tilt_for(ticker, conn, regime=_mr.cached_build(conn))
            if mt.get("available") and mt.get("tilt") is not None:
                tilt = float(mt["tilt"])
                score = round(_clamp(float(score) + _MACRO_TILT_CAP * tilt), 4)
                components["macro_tilt"] = round(tilt, 4)
                components["macro_sector"] = mt.get("sector")
                bits = "；".join(mt.get("evidence") or [])
                evidence.append(f"宏观倾斜 {tilt:+.2f}（{mt.get('sector')}）"
                                + (f"：{bits}" if bits else ""))
            else:
                components["macro_tilt"] = None
        except Exception as exc:
            components["macro_tilt"] = None
            components["macro_detail"] = f"宏观不可用：{type(exc).__name__}"

    # ── 6) 财报窗口折半（v448）：前后 3 天数据随时被重算，不给满权重 ──────
    if score is not None and days is not None and -_EARNINGS_POST_DAYS <= days <= _EARNINGS_PRE_DAYS:
        score = round(score * 0.5, 4)
        flags.append("earnings_window")
        evidence.append(
            f"财报窗口（{days:+d} 天，阈值 ±{_EARNINGS_PRE_DAYS} 天）："
            f"营收/盈利随时重算，方向分折半"
        )

    available = score is not None
    score = round(float(score or 0.0), 4)

    # v474：公司侧分歧度 —— 盈利股看"估值 vs 盈利修正"，亏损股看生意四项本身
    if loss_maker:
        _pairs = [(components.get(k), w) for k, w in _LOSS_WEIGHTS.items()]
    else:
        _pairs = [(components.get("valuation_used"), 0.50),
                  (components.get("earnings_revision_used"), 0.50),
                  (components.get("macro_tilt"), 0.10)]   # 宏观是背景，只给 0.10
    _disp = _dispersion(_pairs)

    out = {
        "available": available,
        "score": score,
        "ticker": ticker,
        "components": components,
        "flags": flags,          # value_trap / growth_premium / peak_earnings / trough_earnings
                                 # / earnings_window / loss_eps_only（复盘要能按标签筛）
        "evidence": evidence[:8],
        # v488：生意质量四件套 + 仍然看不到的深度（分部/债务到期/指引/电话会/DCF）
        "depth": depth,
        "gaps": (depth.get("gaps") or []),
        "detail": "；".join(detail_bits) or (
            "亏损股口径（营收/毛利率/现金流）" if loss_maker else "估值分位 + 盈利修正均可得"),
        "asof": int(pack.get("asof") or time.time()),
        "earnings_in_days": days,
        "earnings_next": (nxt.get("event_date") if nxt else None),
        # v474：五个字段与 alt 侧同名同义，影子账本/闸门共用一套读取
        "dispersion": _disp["dispersion"], "dispersion_level": _disp["dispersion_level"],
        "confidence": _disp["confidence"], "bull_weight": _disp["bull_weight"],
        "bear_weight": _disp["bear_weight"],
    }
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[ticker] = (time.time() + _CACHE_TTL, dict(out))
    return out


def to_row_value(fund: Dict[str, Any]) -> str:
    """落库用：把读数压成 JSON 字符串（decision_context / factor_snapshots 可查）。"""
    try:
        return json.dumps(fund, ensure_ascii=False, default=str)
    except Exception:
        return "{}"
