# -*- coding: utf-8 -*-
"""
signal_impact.py — 信号的**预期市场影响**估计 (v314 P0, 验收 #2)

为什么需要这个模块
------------------
验收 #2 的判据是: **决策时点的权重**与事后 |实际涨跌| 之间的 Spearman 相关
必须 ≥ 0.2 (n ≥ 200)。也就是"权重"必须能当"对市场的影响"的度量。

原来的 6 维权重 (base/consensus/source/reaction/regime/time) 全部是**信号自身
的属性**: LLM 有多确信、来源多可信、市场有没有反应、什么市况、时间衰减。
它们回答的是"这个信号可不可信", 不是"这个信号能推动多少价格"。

实测 (1385 条决策时点权重 × 真实涨跌配对):
    rho(原 6 维权重, |实际涨跌|) = +0.045
    rho(仅 conviction, |实际涨跌|) = +0.015
分量都没有区分度 —— 因为 |实际涨跌| 的方差**主要不来自信号**, 而来自
"这个品种在这个持有期上, 平时就能走多少" (波动率聚集效应):
    XAU 1-3d 平均 |涨跌| 0.55%  vs  ETH 1-3d 平均 2.87%

所以权重必须补上这一维: **该品种在该持有期上的预期波动幅度**。

口径 (严格 point-in-time, 零未来信息)
------------------------------------
用**决策时点之前**的日线收盘价算对数收益标准差 σ, 再按持有期放大:
    E|move_H| ≈ σ_daily × √H_days × √(2/π)
    (H 天收益近似正态时, |N(0,σ√H)| 的期望 = σ√H·√(2/π))

关键约束:
  * 只用 ts <= 决策时点 的 bar —— 决策那一刻能看到的只有过去;
  * σ 的样本窗口固定为决策时点往前 20 个交易日 (滚动窗口);
  * 真值 forward_move_pct 由 decision_truth 用 kline_bars 计算, 所以这里的
    价格源**必须也是 kline_bars** —— 混用另一套价格序列 (如 ohlcv_daily 的
    yfinance 口径与 kline_bars 1d 同日可差 6%) 会把两个不同量纲拼在一起,
    相关性直接被噪声吃掉。

验证 (1385 条配对, 严格 point-in-time):
    rho(影响先验, |实际涨跌|)            = +0.547
    时间 holdout (前 60% 设计 / 后 40% 检验): train +0.435 / test +0.656
    组内 (资产×持有期): XAU 1-3d +0.467 / BTC 1-3d +0.652 / ETH 1-3d +0.655
"""
from __future__ import annotations

import bisect
import math
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

# 持有期 (秒) —— 与 decision_truth.HORIZON_SECONDS 保持一致
HORIZON_SECONDS: Dict[str, int] = {
    "intraday": 3600,
    "1-3d": 3 * 86400,
    "1w+": 7 * 86400,
}
DEFAULT_HORIZON_SECONDS = 3 * 86400

# σ 的滚动窗口 (交易日)
SIGMA_WINDOW = 20
# 至少需要多少个收益率样本
SIGMA_MIN_SAMPLES = 8
# 影响基准: 1% 的绝对变动 = 1.0 倍影响
IMPACT_REFERENCE_PCT = 1.0
# 影响乘子的取值区间 (与其它 5 维同量纲)
IMPACT_MULT_MIN = 0.3
# v315 P0: 上限从 1.5 放宽到 2.5。旧的硬截断 min(1.5, ratio) 让绝大多数
# 加密品种的预期|涨跌|/基准 (实测 2~4) 全被压成同一个值 1.5 —— 实测 insert
# 配对行里 **55.7% 的 impact_mult 恰好等于 1.5**, 顶端区分度被彻底抹平。
# 这与 v314 修掉的 "min(1.0, 乘积)" 是同一类病: 硬截断 → 并列 → 排序信息丢失。
IMPACT_MULT_MAX = 2.5
# 软压缩拐点: ratio 越大越接近 MAX, 但永远严格单调 (不产生并列)
IMPACT_SOFT_K = 2.0


def horizon_seconds(expected_horizon: Optional[str]) -> int:
    """expected_horizon → 持有期秒数 (取区间上界, 与 decision_truth 同口径)"""
    return HORIZON_SECONDS.get(str(expected_horizon or "").strip(),
                               DEFAULT_HORIZON_SECONDS)


class DailySeriesCache:
    """kline_bars 1d 的日线序列缓存 (按资产)。

    只保留 '1d' 且按日历日去重 —— 与 causal_chain_evaluator._daily_series
    同一口径 (同资产同日只留一行), 避免两处对"哪天"的理解不一致。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._series: Dict[str, Tuple[List[int], List[float]]] = {}
        self._loaded = False

    def load(self, conn: sqlite3.Connection) -> None:
        if self._loaded:
            return
        best: Dict[str, Dict[str, Tuple[int, float]]] = {}
        try:
            rows = conn.execute(
                "SELECT symbol, ts, close FROM kline_bars "
                "WHERE timeframe='1d' ORDER BY symbol, ts"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            sym = str(r[0] or "").upper()
            if not sym:
                continue
            try:
                ts = int(r[1]) // 1000 if int(r[1]) > 100_000_000_000 else int(r[1])
                close = float(r[2])
            except (TypeError, ValueError):
                continue
            if close <= 0:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            prev = best.setdefault(sym, {}).get(day)
            if prev is None or ts >= prev[0]:
                best[sym][day] = (ts, close)
        for sym, days in best.items():
            ordered = sorted(days.items())
            self._series[sym] = (
                [d[1][0] for d in ordered],
                [d[1][1] for d in ordered],
            )
        self._loaded = True

    def get(self, symbol: str) -> Optional[Tuple[List[int], List[float]]]:
        return self._series.get(str(symbol or "").upper())


def expected_abs_move_pct(
    cache: DailySeriesCache,
    asset: str,
    expected_horizon: Optional[str],
    asof_ts: int,
    window: int = SIGMA_WINDOW,
) -> Optional[float]:
    """该品种在该持有期上的**预期绝对变动 (%)**, 严格只用 asof 之前的数据。

    返回 None 表示数据不足 (调用方应保持中性, 不影响权重)。
    """
    series = cache.get(asset)
    if not series:
        return None
    times, closes = series
    if len(times) < SIGMA_MIN_SAMPLES + 2:
        return None
    asof = int(asof_ts or 0)
    idx = bisect.bisect_right(times, asof) - 1
    if idx < window:
        return None
    rets: List[float] = []
    for i in range(idx - window + 1, idx + 1):
        c0, c1 = closes[i - 1], closes[i]
        if c0 > 0 and c1 > 0:
            rets.append(math.log(c1 / c0))
    if len(rets) < SIGMA_MIN_SAMPLES:
        return None
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    h_days = max(1.0, horizon_seconds(expected_horizon) / 86400.0)
    # |N(0, σ√H)| 的期望 = σ√H·√(2/π)
    return sd * math.sqrt(h_days) * math.sqrt(2.0 / math.pi) * 100.0


def soft_clamp(ratio: float) -> float:
    """严格单调的软压缩: ratio → [IMPACT_MULT_MIN, IMPACT_MULT_MAX]。

    v315 P0: 取代旧的硬截断 `min(MAX, ratio)`。
    硬截断的问题不是"上限太大", 而是**并列**: 落在上限之上的所有值被压成
    同一个数, 而 Spearman 只看排序 —— 并列越多, 排序信息丢得越多。实测旧公式
    下 55.7% 的配对行 impact_mult 全部等于 1.5, 顶端完全没有区分度。
    软压缩 ratio/(ratio+K) 严格单调, 上限只负责兜住极端值, 不再制造并列。
    """
    lo = IMPACT_MULT_MIN
    hi = IMPACT_MULT_MAX
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return 1.0
    if r <= 0:
        return lo
    return lo + (hi - lo) * (r / (r + IMPACT_SOFT_K))


def impact_mult(
    cache: DailySeriesCache,
    asset: str,
    expected_horizon: Optional[str],
    asof_ts: int,
) -> float:
    """影响乘子 (0.3 ~ 2.5): 该品种该持有期的预期波动幅度相对基准的倍数。

    数据不足 → 1.0 (中性, 不改变原有权重语义)。
    """
    exp_move = expected_abs_move_pct(cache, asset, expected_horizon, asof_ts)
    if exp_move is None or exp_move <= 0:
        return 1.0
    ratio = exp_move / IMPACT_REFERENCE_PCT
    return soft_clamp(ratio)


__all__ = [
    "HORIZON_SECONDS", "DEFAULT_HORIZON_SECONDS", "SIGMA_WINDOW",
    "IMPACT_REFERENCE_PCT", "IMPACT_MULT_MIN", "IMPACT_MULT_MAX", "IMPACT_SOFT_K",
    "DailySeriesCache", "horizon_seconds", "expected_abs_move_pct",
    "soft_clamp", "impact_mult",
]
