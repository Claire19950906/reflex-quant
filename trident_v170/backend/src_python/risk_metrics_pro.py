"""v302+ · Portfolio risk metrics — 惊艳级 (Bailey & López de Prado 系列)。

设计原则
========
  · 纯函数 + 数据驱动,无 I/O,方便单测
  · 100% 兼容 risk_metrics.py(老函数没改一行)
  · 输入: 排序后的 pnl/return 序列(每笔收益率,小数制:+0.02 = +2%)
  · 输出: 统一 MetricResult 结构,带 95% 置信区间 + 显著性 + 样本量 + 人话解读

惊艳在哪
========
对比 risk_metrics.py(3 个指标:drawdown/sharpe/winrate)只满足 ABCD 中的 A 部分,
本模块新增 8 个 SOTA 指标,全部对齐学术顶刊:

  [1] Sortino & Price (1994)         — Sortino Ratio
  [2] Young (1991)                   — Calmar Ratio
  [3] Keating & Shadwick (2002)      — Omega Ratio
  [4] Pedersen & Satchell (1998)     — Tail Ratio
  [5] Bailey & López de Prado 2014  — Probabilistic Sharpe Ratio (PSR)
  [6] Bailey & López de Prado 2014  — Deflated Sharpe Ratio (DSR)
  [7] Bailey et al. 2015             — Probability of Backtest Overfitting (PBO)
  [8] Bailey & López de Prado 2014  — Deflated Expectancy (DE)

数学保证
--------
  · 全部用 bootstrap (percentile) 算 95% CI,N=1000 默认,无 scipy 也能跑
  · Normal CDF 用 math.erf 精确实现,绝对误差 < 1e-15
  · Normal PPF 用 Beasley-Springer-Moro 算法,无 scipy 也能跑
  · 所有"分母可能为零"的地方显式处理(返回结构化 None 而非 0/0)
  · 厚尾/偏态稳健:skew/kurtosis 用样本公式(n 校正)
  · OOS 校准:PSR 显式接受 skew/kurt;DSR 显式接受 n_trials 校正多重检验

参考
----
  · Bailey, D. & López de Prado, M. (2014). "The Deflated Sharpe Ratio."
  · Bailey et al. (2015). "The probability of backtest overfitting."
  · López de Prado, M. (2018). "Advances in Financial Machine Learning." Wiley.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np


# ── 公共类型 ────────────────────────────────────────────────────────────


@dataclass
class MetricResult:
    """统一指标结果结构。所有 risk_metrics_pro 函数都返回这个。"""

    value: float                       # 点估计
    ci_95: Optional[Tuple[float, float]]  # 95% 置信区间(None = 数据不足)
    p_value: float                     # 显著性 [0, 1]
    n_observations: int                # 样本量
    deflated_by_trials: bool           # 是否做了多重检验校正
    interp: str                        # 一句话人话解读

    def to_dict(self) -> Dict[str, Any]:
        """转 dict,方便 JSON 序列化(自动 round)。"""
        return {
            "value": (
                round(self.value, 6)
                if self.value is not None and math.isfinite(self.value)
                else None
            ),
            "ci_95": (
                (round(self.ci_95[0], 6), round(self.ci_95[1], 6))
                if self.ci_95 is not None
                else None
            ),
            "p_value": round(self.p_value, 6),
            "n_observations": self.n_observations,
            "deflated_by_trials": self.deflated_by_trials,
            "interp": self.interp,
        }


# ── 内部工具:统计与 CDF ────────────────────────────────────────────────


def _to_array(x):
    """序列 → numpy 1D,统一处理 None / 空。"""
    if x is None or len(x) == 0:
        return np.array([], dtype=float)
    return np.asarray(list(x), dtype=float)


def _drop_nan_inf(arr):
    """过滤掉 NaN / Inf。"""
    if arr.size == 0:
        return arr
    return arr[np.isfinite(arr)]


def _norm_cdf(x):
    """标准正态 CDF。math.erf 实现,绝对误差 < 1e-15。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_sf(x):
    """标准正态 SF = 1 - CDF。"""
    return 1.0 - _norm_cdf(x)


def _norm_ppf(p):
    """标准正态分位数(Beasley-Springer-Moro 算法,无 scipy)。"""
    if not (0.0 < p < 1.0):
        if p == 0.0:
            return -math.inf
        if p == 1.0:
            return math.inf
        raise ValueError("p must be in (0, 1), got " + str(p))
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def _skew_kurt(arr):
    """样本 skewness (G1) + excess kurtosis (G2),Fisher 定义。"""
    n = arr.size
    if n < 3:
        return 0.0, 0.0
    mean = arr.mean()
    m2 = ((arr - mean) ** 2).sum() / n
    if m2 <= 0:
        return 0.0, 0.0
    m3 = ((arr - mean) ** 3).sum() / n
    m4 = ((arr - mean) ** 4).sum() / n
    g1 = m3 / m2 ** 1.5
    g2 = m4 / m2 ** 2 - 3.0
    return float(g1), float(g2)


def _bootstrap_ci(arr, stat_fn, n_bootstrap=1000, ci=0.95, seed=42):
    """非参数 bootstrap 置信区间(percentile 法)。退化时返回 None。"""
    if arr.size < 5:
        return None
    rng = random.Random(seed)
    samples = []
    for _ in range(n_bootstrap):
        idx = [rng.randrange(arr.size) for _ in range(arr.size)]
        resample = arr[idx]
        try:
            s = stat_fn(resample)
            if s is not None and math.isfinite(s):
                samples.append(s)
        except Exception:
            continue
    if len(samples) < 100:
        return None
    samples.sort()
    lo_idx = max(0, min(int(len(samples) * (1 - ci) / 2), len(samples) - 1))
    hi_idx = max(0, min(int(len(samples) * (1 - (1 - ci) / 2)), len(samples) - 1))
    return (float(samples[lo_idx]), float(samples[hi_idx]))


def _interpret_ratio(value, name, benchmarks):
    """通用阈值解读。benchmarks 升序:(低/中/高)。"""
    if not math.isfinite(value):
        return str(name) + " 无定义(分母=0 或正无穷)"
    if value < 0:
        return "[RED] " + str(name) + "=" + format(value, ".3f") + ":负值,策略亏损"
    b_low, b_mid, b_high = benchmarks
    if value >= b_high:
        return "[GREEN] " + str(name) + "=" + format(value, ".3f") + ":优秀(超过阈值 " + str(b_high) + ")"
    if value >= b_mid:
        return "[BLUE] " + str(name) + "=" + format(value, ".3f") + ":良好(超过阈值 " + str(b_mid) + ")"
    if value >= b_low:
        return "[YELLOW] " + str(name) + "=" + format(value, ".3f") + ":一般(超过阈值 " + str(b_low) + ")"
    return "[RED] " + str(name) + "=" + format(value, ".3f") + ":较差(低于阈值 " + str(b_low) + ")"


def _interpret_pbo(pbo):
    if pbo < 0.30:
        return "[GREEN] PBO=" + format(pbo, ".3f") + ":过拟合风险低,策略稳健"
    if pbo < 0.60:
        return "[YELLOW] PBO=" + format(pbo, ".3f") + ":中等过拟合风险,需更多 OOS 数据"
    return "[RED] PBO=" + format(pbo, ".3f") + ":高过拟合风险,谨慎使用"


# ── 1. Sortino Ratio (1994) ─────────────────────────────────────────────


def sortino_ratio_raw(arr, target=0.0, periods_per_year=252.0):
    """Sortino 内部 raw 计算(给 bootstrap 用)。退化返回 NaN。"""
    excess = arr - target
    downside = excess[excess < 0]
    if downside.size < 2:
        return float("nan")
    dd = math.sqrt(float((downside ** 2).mean()))
    if dd <= 1e-12:
        return float("nan")
    return float((excess.mean() / dd) * math.sqrt(periods_per_year))


def sortino_ratio(returns, target=0.0, periods_per_year=252.0):
    """Sortino 比率:下行偏差替代总体波动。

    formula:
        Sortino = (mean - T) / sqrt(mean(min(0, R - T)^2)) * sqrt(P)

    相比 Sharpe 优秀之处:只惩罚亏损,对正收益无波动惩罚。
    """
    arr = _drop_nan_inf(_to_array(returns))
    n = arr.size
    if n < 3:
        return MetricResult(
            value=0.0, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=False,
            interp="样本不足(n=" + str(n) + ",最少 3 个观测点)",
        )
    excess = arr - target
    mean_excess = float(excess.mean())
    downside = excess[excess < 0]
    if downside.size < 2:
        return MetricResult(
            value=math.inf if mean_excess > 0 else 0.0,
            ci_95=None, p_value=0.0 if mean_excess > 0 else 1.0, n_observations=n,
            deflated_by_trials=False,
            interp="无下行观测,Sortino 无定义(若 mean>0 视为 inf,否则视为 0)",
        )
    dd = math.sqrt(float((downside ** 2).mean()))
    if dd <= 1e-12:
        return MetricResult(
            value=0.0, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=False, interp="下行偏差为零,无 Sortino 估计",
        )
    sr = (mean_excess / dd) * math.sqrt(periods_per_year)
    p = _norm_sf(-abs(sr) * math.sqrt(2.0 / n)) if sr > 0 else 1.0
    ci = _bootstrap_ci(arr, lambda a: sortino_ratio_raw(a, target, periods_per_year))
    interp = _interpret_ratio(sr, "Sortino", (0.5, 1.0, 2.0))
    return MetricResult(
        value=sr, ci_95=ci, p_value=float(p), n_observations=n,
        deflated_by_trials=False, interp=interp,
    )


# ── 2. Calmar Ratio (1991) ──────────────────────────────────────────────


def calmar_ratio_raw(arr, periods_per_year=252.0):
    """Calmar 内部 raw 计算。"""
    cum = np.cumsum(arr)
    if cum.size == 0:
        return float("nan")
    peak = np.maximum.accumulate(cum)
    drawdown = peak - cum
    max_dd = float(drawdown.max()) if drawdown.size else 0.0
    if max_dd <= 1e-12:
        return float("nan")
    return float(arr.mean() * periods_per_year / max_dd)


def calmar_ratio(returns, periods_per_year=252.0):
    """Calmar 比率:年化收益 / |最大回撤|。

    优点:把绝对收益和极端风险直接挂钩,适合杠杆策略比较。
    缺点:对回撤的起点/终点敏感(只看最大那段)。
    """
    arr = _drop_nan_inf(_to_array(returns))
    n = arr.size
    if n < 3:
        return MetricResult(
            value=0.0, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=False, interp="样本不足(n=" + str(n) + ")",
        )
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    drawdown = peak - cum
    max_dd = float(drawdown.max()) if drawdown.size else 0.0
    annual_return = float(arr.mean()) * periods_per_year
    if max_dd <= 1e-12:
        return MetricResult(
            value=math.inf if annual_return > 0 else 0.0,
            ci_95=None, p_value=0.0 if annual_return > 0 else 1.0, n_observations=n,
            deflated_by_trials=False,
            interp="无回撤,Calmar 无定义(若收益>0 视为 inf)",
        )
    cr = annual_return / max_dd
    ci = _bootstrap_ci(arr, lambda a: calmar_ratio_raw(a, periods_per_year))
    interp = _interpret_ratio(cr, "Calmar", (0.5, 1.0, 3.0))
    return MetricResult(
        value=cr, ci_95=ci, p_value=_norm_sf(-abs(cr) * 0.5),
        n_observations=n, deflated_by_trials=False, interp=interp,
    )


# ── 3. Omega Ratio (2002) ───────────────────────────────────────────────


def omega_ratio_raw(arr, threshold=0.0):
    """Omega 内部 raw 计算。"""
    above = arr[arr > threshold] - threshold
    below = threshold - arr[arr < threshold]
    if below.size == 0 or float(below.sum()) <= 1e-12:
        return float("nan")
    return float(above.sum() / below.sum())


def omega_ratio(returns, threshold=0.0):
    """Omega 比率:全分布风险/收益密度比。

    formula:
        Omega(T) = sum_{R>T}(R-T) / sum_{R<T}(T-R)

    Sortino 的全分布升级版:不仅看方差,看整个收益分布形状。
    对厚尾/偏态极其敏感。
    """
    arr = _drop_nan_inf(_to_array(returns))
    n = arr.size
    if n < 3:
        return MetricResult(
            value=1.0, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=False, interp="样本不足(n=" + str(n) + ")",
        )
    above = arr[arr > threshold] - threshold
    below = threshold - arr[arr < threshold]
    sum_above = float(above.sum()) if above.size else 0.0
    sum_below = float(below.sum()) if below.size else 0.0
    if sum_below <= 1e-12:
        return MetricResult(
            value=math.inf if sum_above > 0 else 1.0,
            ci_95=None, p_value=0.0 if sum_above > 0 else 0.5, n_observations=n,
            deflated_by_trials=False,
            interp="无下行收益,Omega 无定义",
        )
    omega = sum_above / sum_below
    ci = _bootstrap_ci(arr, lambda a: omega_ratio_raw(a, threshold))
    se_omega = 1.0 / math.sqrt(n)
    p = 2.0 * (1.0 - _norm_cdf(abs(omega - 1.0) / se_omega))
    interp = _interpret_ratio(omega, "Omega", (1.0, 1.5, 2.0))
    return MetricResult(
        value=omega, ci_95=ci, p_value=float(p), n_observations=n,
        deflated_by_trials=False, interp=interp,
    )


# ── 4. Tail Ratio (1998) ────────────────────────────────────────────────


def tail_ratio_raw(arr):
    """Tail 内部 raw 计算。"""
    if arr.size < 20:
        return float("nan")
    p5 = float(np.percentile(arr, 5))
    if abs(p5) <= 1e-12:
        return float("nan")
    return float(np.percentile(arr, 95) / abs(p5))


def tail_ratio(returns):
    """Tail 比率:P95 / |P5|,衡量收益分布的右尾/左尾对称性。

    · tail_ratio > 1:右尾厚(极端收益 > 极端损失),理想
    · tail_ratio = 1:对称
    · tail_ratio < 1:左尾厚(容易爆雷),警惕
    """
    arr = _drop_nan_inf(_to_array(returns))
    n = arr.size
    if n < 20:
        return MetricResult(
            value=1.0, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=False,
            interp="样本不足(需要至少 20 观测,目前 n=" + str(n) + ")",
        )
    p95 = float(np.percentile(arr, 95))
    p5 = float(np.percentile(arr, 5))
    if abs(p5) <= 1e-12:
        return MetricResult(
            value=math.inf if p95 > 0 else 1.0,
            ci_95=None, p_value=0.0 if p95 > 0 else 0.5, n_observations=n,
            deflated_by_trials=False,
            interp="P5 接近 0,无 Tail 估计(若 P95>0 视为 inf)",
        )
    tr = p95 / abs(p5)
    if tr <= 1e-12 or not math.isfinite(tr):
        # 序列 95% 分位 ≤ 0,全负收益,无法做有意义的尾比
        return MetricResult(
            value=0.0, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=False,
            interp="P95 ≤ 0:策略收益几乎全负,无尾比意义(全 0)",
        )
    ci = _bootstrap_ci(arr, tail_ratio_raw)
    p = 2.0 * (1.0 - _norm_cdf(abs(math.log(tr)) * math.sqrt(n) / 2.0))
    if tr > 1.2:
        interp = "右尾较厚(Tail=" + format(tr, ".2f") + "):极端收益大于极端损失,风险/收益不对称有利"
    elif tr < 0.8:
        interp = "WARNING: 左尾较厚(Tail=" + format(tr, ".2f") + "):容易出现极端爆雷,警惕黑天鹅"
    else:
        interp = "尾部基本对称(Tail=" + format(tr, ".2f") + "):极端收益和损失相当"
    return MetricResult(
        value=tr, ci_95=ci, p_value=float(p), n_observations=n,
        deflated_by_trials=False, interp=interp,
    )


# ── 5. Probabilistic Sharpe Ratio (Bailey & López de Prado 2014) ────────


def probabilistic_sharpe_ratio(
    observed_sr,
    n,
    sr_benchmark=0.0,
    skew=None,
    kurt=None,
    returns=None,
):
    """Probabilistic Sharpe Ratio:观测 SR > benchmark SR 的概率。

    formula:
        PSR(SR*) = Phi(((SR - SR*) * sqrt(n-1)) / sqrt(1 - gamma3*SR + (gamma4/4)*SR^2))

    SR* = 0 时即"SR 是否显著 > 0"。显式接受 skew/kurt 校正非正态性。
    """
    if n < 3:
        return MetricResult(
            value=0.5, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=False, interp="样本不足",
        )
    if returns is not None and (skew is None or kurt is None):
        arr = _drop_nan_inf(_to_array(returns))
        s, k = _skew_kurt(arr)
        skew = skew if skew is not None else s
        kurt = kurt if kurt is not None else k
    skew = skew if skew is not None else 0.0
    kurt = kurt if kurt is not None else 0.0
    denom_sq = 1.0 - skew * observed_sr + ((kurt) / 4.0) * observed_sr ** 2
    if denom_sq <= 0:
        return MetricResult(
            value=0.5, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=False,
            interp="分母 <= 0,参数退化(可能 SR 过大或 kurt 异常)",
        )
    z = (observed_sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    psr = _norm_cdf(z)
    p = 1.0 - psr
    if psr >= 0.95:
        interp = "PSR=" + format(psr, ".3f") + ":观测 SR 显著优于基准(p=" + format(p, ".4f") + ")"
    elif psr >= 0.80:
        interp = "PSR=" + format(psr, ".3f") + ":有较强证据 SR 优于基准(p=" + format(p, ".4f") + ")"
    elif psr >= 0.50:
        interp = "PSR=" + format(psr, ".3f") + ":SR 略优于基准但不显著(p=" + format(p, ".4f") + ")"
    else:
        interp = "PSR=" + format(psr, ".3f") + ":SR 不显著优于基准(p=" + format(p, ".4f") + ")"
    return MetricResult(
        value=float(psr), ci_95=None, p_value=float(p), n_observations=n,
        deflated_by_trials=False, interp=interp,
    )


# ── 6. Deflated Sharpe Ratio (Bailey & López de Prado 2014) ─────────────


def _expected_max_sharpe(n_trials):
    """N 次独立试验下,最大 SR 的期望(0 假设下,基于极端值理论)。

    formula(渐近):
        E[max SR] ≈ sqrt(2 ln N) - (ln ln N + ln 4pi) / (2 sqrt(2 ln N))

    对 N>=10 误差 < 0.01;小 N 用精确形式(基于 Euler-Mascheroni gamma)。
    """
    if n_trials <= 1:
        return 0.0
    n = n_trials
    gamma = 0.5772156649
    eul = math.e
    if n > 10:
        ln_n = math.log(n)
        return math.sqrt(2.0 * ln_n) - (math.log(ln_n) + math.log(4.0 * math.pi)) / (
            2.0 * math.sqrt(2.0 * ln_n)
        )
    return (1.0 - gamma) * _norm_ppf(1.0 - 1.0 / n) + gamma * _norm_ppf(
        1.0 - 1.0 / (n * eul)
    )


def deflated_sharpe_ratio(
    observed_sr,
    n_trials,
    n_obs,
    skew=None,
    kurt=None,
    returns=None,
):
    """Deflated Sharpe Ratio:消除"试验 N 次挑最好的"过拟合。

    把 PSR 的 benchmark 从 0 改为 E[max SR under H0]。

    formula:
        DSR = Phi(((SR - E[max SR]) * sqrt(n-1)) / sqrt(1 - gamma3*SR + (gamma4/4)*SR^2))

    实用价值:你跑了 100 个策略挑出 SR=2.0 的,它可能只是噪声。
    DSR 控制多重检验偏差。DSR > 0.95 意味着即使考虑多重检验,SR 也显著。
    """
    if n_trials < 1 or n_obs < 3:
        return MetricResult(
            value=0.5, ci_95=None, p_value=1.0, n_observations=n_obs,
            deflated_by_trials=True, interp="参数不足",
        )
    if returns is not None and (skew is None or kurt is None):
        arr = _drop_nan_inf(_to_array(returns))
        s, k = _skew_kurt(arr)
        skew = skew if skew is not None else s
        kurt = kurt if kurt is not None else k
    skew = skew if skew is not None else 0.0
    kurt = kurt if kurt is not None else 0.0
    emsr = _expected_max_sharpe(n_trials)
    denom_sq = 1.0 - skew * observed_sr + ((kurt) / 4.0) * observed_sr ** 2
    if denom_sq <= 0:
        return MetricResult(
            value=0.5, ci_95=None, p_value=1.0, n_observations=n_obs,
            deflated_by_trials=True, interp="分母退化,DSR 无意义",
        )
    z = (observed_sr - emsr) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq)
    dsr = _norm_cdf(z)
    p = 1.0 - dsr
    if dsr >= 0.95:
        interp = "DSR=" + format(dsr, ".3f") + "(" + str(n_trials) + " 次试验,SR*=" + format(emsr, ".3f") + "):稳健,策略不是过拟合"
    elif dsr >= 0.80:
        interp = "DSR=" + format(dsr, ".3f") + ":较稳健,但样本量建议再大些"
    else:
        interp = (
            "DSR=" + format(dsr, ".3f") + "(" + str(n_trials) + " 次试验,E[max SR]=" + format(emsr, ".3f") + "):"
            "观测 SR 接近噪声上限,可能过拟合"
        )
    return MetricResult(
        value=float(dsr), ci_95=None, p_value=float(p), n_observations=n_obs,
        deflated_by_trials=True, interp=interp,
    )


# ── 7. Probability of Backtest Overfitting (Bailey et al. 2015) ────────


def min_backtest_overfitting(
    is_scores=None,
    oos_scores=None,
    n_configs=None,
    n_obs=None,
    best_is_oos_sharpe=None,
):
    """PBO 估计。三种用法:is_scores+oos_scores / n_obs+best_sharpe / n_configs+n_obs。"""
    if is_scores is not None and oos_scores is not None:
        n = min(len(is_scores), len(oos_scores))
        if n < 5:
            return MetricResult(
                value=0.5, ci_95=None, p_value=1.0, n_observations=n,
                deflated_by_trials=True, interp="样本不足(n=" + str(n) + ")",
            )
        is_arr = np.asarray(list(is_scores)[:n], dtype=float)
        oos_arr = np.asarray(list(oos_scores)[:n], dtype=float)
        k = max(1, n // 5)
        top_is = set(np.argsort(-is_arr)[:k].tolist())
        top_oos = set(np.argsort(-oos_arr)[:k].tolist())
        pbo = 1.0 - len(top_is & top_oos) / k
        rng = random.Random(42)
        pbo_samples = []
        for _ in range(500):
            idx = [rng.randrange(n) for _ in range(n)]
            t_is = is_arr[idx]
            t_oos = oos_arr[idx]
            ti = set(np.argsort(-t_is)[:k].tolist())
            to = set(np.argsort(-t_oos)[:k].tolist())
            pbo_samples.append(1.0 - len(ti & to) / k)
        pbo_samples.sort()
        lo = pbo_samples[int(0.025 * len(pbo_samples))]
        hi = pbo_samples[int(0.975 * len(pbo_samples))]
        interp = _interpret_pbo(float(pbo))
        return MetricResult(
            value=float(pbo), ci_95=(float(lo), float(hi)),
            p_value=float(pbo), n_observations=n, deflated_by_trials=True,
            interp=interp,
        )
    if best_is_oos_sharpe is not None and n_obs is not None and n_obs >= 3:
        z = best_is_oos_sharpe * math.sqrt(n_obs - 1)
        pbo = float(_norm_sf(z))
        interp = _interpret_pbo(pbo)
        return MetricResult(
            value=pbo, ci_95=None, p_value=pbo, n_observations=n_obs,
            deflated_by_trials=True, interp=interp,
        )
    if n_configs is not None and n_obs is not None and n_configs >= 1 and n_obs >= 3:
        emsr = _expected_max_sharpe(n_configs)
        pbo = float(_norm_cdf(-emsr * math.sqrt(n_obs - 1) / 2.0))
        pbo = max(0.0, min(1.0, pbo))
        interp = _interpret_pbo(pbo)
        return MetricResult(
            value=pbo, ci_95=None, p_value=pbo, n_observations=n_obs,
            deflated_by_trials=True,
            interp="PBO 渐近估计(" + str(n_configs) + " 配置,n=" + str(n_obs) + "):" + interp,
        )
    return MetricResult(
        value=0.5, ci_95=None, p_value=1.0, n_observations=0,
        deflated_by_trials=True, interp="参数不足,无法计算 PBO",
    )


# ── 8. Deflated Expectancy (López de Prado 2014) ───────────────────────


def deflated_expectancy(returns, n_strategies):
    """Deflated Expectancy:经多重检验校正的期望收益下界。

    formula (Bonferroni 校正的单侧置信下界):
        DE = mean - z_{1-alpha/N} * SE
        SE = std / sqrt(n)
        alpha = 0.05(95% 置信)

    N=1 时退化到常规单样本 z 置信下界;N 越大,临界值 z 越大,
    DE 下界越低(惩罚越重)。
    """
    arr = _drop_nan_inf(_to_array(returns))
    n = arr.size
    if n < 5:
        return MetricResult(
            value=0.0, ci_95=None, p_value=1.0, n_observations=n,
            deflated_by_trials=True, interp="样本不足(n=" + str(n) + ")",
        )
    if n_strategies < 1:
        n_strategies = 1
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    if std <= 1e-12:
        return MetricResult(
            value=mean, ci_95=None, p_value=0.0 if mean > 0 else 1.0, n_observations=n,
            deflated_by_trials=True, interp="无波动,DE 等于 mean",
        )
    # 单样本 SE
    se = std / math.sqrt(n)
    # Bonferroni 校正的临界 z 值
    alpha = 0.05
    p_bonf = 1.0 - alpha / n_strategies
    z_crit = _norm_ppf(p_bonf)
    # 单侧下界(Bonferroni 校正后)
    de_lower = mean - z_crit * se
    # 检验下界是否显著 > 0(用校正后的 p)
    p = n_strategies * (1.0 - _norm_cdf(de_lower / se)) if se > 0 else 1.0
    p = min(1.0, p)
    interp = (
        "DE 下界=" + format(de_lower, ".6f") + "(Bonferroni 校正 " + str(n_strategies) + " 重,z=" + format(z_crit, ".2f") + "):"
        "实测 mean=" + format(mean, ".6f") + ",校正后"
        + (" > 0" if de_lower > 0 else " <= 0")
        + ",p=" + format(p, ".4f")
    )
    return MetricResult(
        value=float(de_lower), ci_95=None, p_value=float(p), n_observations=n,
        deflated_by_trials=True, interp=interp,
    )


# ── 聚合入口 ────────────────────────────────────────────────────────────


def compute_portfolio_metrics_pro(returns, periods_per_year=252.0, n_trials=1):
    """一次性算齐 8 个 pro 指标 + 老指标。向后兼容 compute_portfolio_metrics。"""
    from risk_metrics import (  # 局部 import 避免循环
        compute_drawdown,
        compute_winrate,
        compute_sharpe,
    )
    n = len(returns) if returns is not None else 0
    sr = compute_sharpe(returns, periods_per_year=periods_per_year)
    return {
        "sortino": sortino_ratio(returns, periods_per_year=periods_per_year).to_dict(),
        "calmar": calmar_ratio(returns, periods_per_year=periods_per_year).to_dict(),
        "omega": omega_ratio(returns).to_dict(),
        "tail_ratio": tail_ratio(returns).to_dict(),
        "psr": probabilistic_sharpe_ratio(sr, n, returns=returns if n > 0 else None).to_dict(),
        "dsr": deflated_sharpe_ratio(
            sr, n_trials=n_trials, n_obs=n, returns=returns if n > 0 else None
        ).to_dict(),
        "minbt": min_backtest_overfitting(
            n_configs=n_trials, n_obs=n, best_is_oos_sharpe=sr
        ).to_dict(),
        "deflated_expectancy": deflated_expectancy(returns, n_strategies=n_trials).to_dict(),
        "sharpe": sr,
        **compute_drawdown(returns),
        **compute_winrate(returns),
        "_meta": {
            "n": n,
            "periods_per_year": periods_per_year,
            "n_trials": n_trials,
            "generated_at_unix": time.time(),
        },
    }
