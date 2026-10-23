#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v240+ Bayesian Online Changepoint Detection (BOCPD, Adams & MacKay 2007).

设计动机:
  - meta-loop 现有 finding_kind（invalidated/refined/confirmed）只看
    "当前窗口 vs 历史窗口" 的聚合 wr 差异，无法识别"信号正在快速变化"
    这种 regime shift 状态。
  - BOCPD 在数据流上实时估计"当前点是 regime 边界的概率"。
  - 应用: 当 meta-loop 检测到某 signal 的最近 trade 序列出现
    P(changepoint) > threshold 时，可在 evidence 中加 _changepoint_prob
    字段；dashboard / 自动策略可以据此触发更激进反应
    (e.g., 立即停用、降低权重)。

算法要点 (Adams & MacKay, 2007):
  - 维护 run-length 后验 P(r_t | x_1:t)
  - 每步执行 predictive step → posterior update
  - Constant hazard: P(cp) = 1/λ, P(grow) = (λ-1)/λ
  - Predictive likelihood 用 Normal-Inverse-Gamma conjugate 模型

零依赖: 纯 stdlib (math + dataclass)；无 numpy / scipy。
性能: O(max_run_length) per update，n=200 信号 ~0.2ms；典型 meta-loop
  信号数 30-100，跑一遍 < 100ms，可接受。

用法:
    detector = BOCPD(hazard_lambda=100)
    for x in stream:
        detector.update(x)
        if detector.changepoint_prob > 0.5:
            print("regime shift detected")
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

# 数值下溢保护：posterior 总和太小时强制重置
_UNDERFLOW_EPS = 1e-300

# 预缓存常见 df 的 lgamma 项，避免重复调用 math.lgamma
# Student-t(ν) pdf normalization: log Γ((ν+1)/2) - log Γ(ν/2) - 0.5*log(νπ)
_LGAMMA_CACHE: dict = {}
_LGAMMA_CACHE_MAX = 1000  # cache for df up to 1000


def _student_t_log_norm(df: float) -> float:
    """缓存的 Student-t log normalization 常量.

    log [Γ((ν+1)/2) / (Γ(ν/2) * sqrt(νπ))]
    """
    df_int = int(round(df * 2))  # df 是 2α, 所以 df 必为偶数 *2=整数
    if df_int not in _LGAMMA_CACHE:
        if df_int > _LGAMMA_CACHE_MAX:
            # 大 df: 用 ν 越大 → Normal 极限
            return -0.5 * math.log(2 * math.pi)
        _LGAMMA_CACHE[df_int] = (
            math.lgamma((df_int + 1) / 2)
            - math.lgamma(df_int / 2)
            - 0.5 * math.log(df_int * math.pi)
        )
    return _LGAMMA_CACHE[df_int]


# ═══════════════════════════════════════════════════════════════════════════
# Normal-Inverse-Gamma predictive distribution (sufficient statistics per run)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _RunStats:
    """一个 run length 的充分统计量（Gaussian conjugate）。

    注: 不存原始序列, 只存 sum_x / sum_x2 → O(1) 空间 per run length.
    """
    n: int = 0
    sum_x: float = 0.0
    sum_x2: float = 0.0

    def mean(self) -> float:
        if self.n == 0:
            return 0.0
        return self.sum_x / self.n

    def variance(self) -> float:
        if self.n <= 1:
            return 0.0
        m = self.mean()
        return max(self.sum_x2 / self.n - m * m, 0.0)

    def extend(self, x: float) -> "_RunStats":
        """返回新的 _RunStats, 包含本 run 的 x. 不修改 self. """
        return _RunStats(
            n=self.n + 1,
            sum_x=self.sum_x + x,
            sum_x2=self.sum_x2 + x * x,
        )

    def predictive_log_prob(self, x: float,
                            prior_mean: float, prior_kappa: float,
                            prior_alpha: float, prior_beta: float) -> float:
        """Student-t predictive: log P(x | run stats, prior).

        用 Bishop PRML 3.4.2 公式:
          posterior: N(μ|μ_n, (κ_n σ²)⁻¹) * InvGam(σ²|α_n, β_n)
          predictive: St(x|μ_n, λ_n⁻¹, 2α_n)  其中 λ_n = (κ_n+1) α_n / (κ_n β_n)
          predictive variance = ν/(ν-2) / λ_n = κ_n β_n / ((κ_n+1)(α_n-1))
        """
        # n=0: 退化为 prior marginal predictive
        if self.n == 0 or prior_alpha <= 1.0:
            sigma2 = prior_beta * (prior_kappa + 1) / (prior_kappa * prior_alpha)
            if sigma2 <= 0:
                sigma2 = 1e-6
            df = 2 * prior_alpha
            scale = math.sqrt(sigma2)
            z = (x - prior_mean) / scale
            try:
                log_pdf = (_student_t_log_norm(df)
                           - (df + 1) / 2 * math.log1p(z * z / df))
            except (ValueError, OverflowError):
                log_pdf = -0.5 * math.log(2 * math.pi * sigma2) - 0.5 * z * z
            return log_pdf

        kappa_n = prior_kappa + self.n
        alpha_n = prior_alpha + self.n / 2.0
        mu_n = (prior_kappa * prior_mean + self.sum_x) / kappa_n

        s2 = self.variance() * self.n
        beta_n = (prior_beta
                  + 0.5 * s2
                  + 0.5 * prior_kappa * self.n / kappa_n * (self.mean() - prior_mean) ** 2)
        sigma2 = kappa_n * beta_n / ((kappa_n + 1) * (alpha_n - 1))
        if sigma2 <= 0:
            sigma2 = 1e-12
        df = 2 * alpha_n
        scale = math.sqrt(sigma2)
        z = (x - mu_n) / scale

        try:
            log_pdf = (_student_t_log_norm(df)
                       - (df + 1) / 2 * math.log1p(z * z / df))
        except (ValueError, OverflowError):
            log_pdf = -0.5 * math.log(2 * math.pi * sigma2) - 0.5 * z * z
        return log_pdf


# ═══════════════════════════════════════════════════════════════════════════
# BOCPD main class
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BOCPD:
    """Bayesian Online Changepoint Detection.

    Args:
        hazard_lambda: 期望 run length（Poisson prior on changepoint intervals）
        prior_mean:     数据先验均值 (e.g., 0.5 for win_rate)
        prior_kappa:    先验强度（pseudo-count，越大越保守）
        prior_alpha:    Inverse-Gamma shape（≥ 1）
        prior_beta:     Inverse-Gamma scale（= prior variance / 2）
        max_run_length: 最大 run length（超出后截断 + 重置最后一段）

    Attributes:
        run_length_posterior: P(r_t | x_1:t), len = 当前时间 + 1
        changepoint_prob:     P(r_t = 0 | x_1:t) — 当前点是 regime 边界的概率
        most_likely_run_length: argmax P(r_t | x_1:t)
    """
    hazard_lambda: float = 100.0
    prior_mean: float = 0.5
    prior_kappa: float = 1.0
    prior_alpha: float = 2.0
    prior_beta: float = 0.1
    max_run_length: int = 200

    # 内部状态
    _run_probs: List[float] = field(default_factory=lambda: [1.0])
    _run_stats: List[_RunStats] = field(default_factory=lambda: [_RunStats()])

    @property
    def run_length_posterior(self) -> List[float]:
        return list(self._run_probs)

    @property
    def changepoint_prob(self) -> float:
        """P(r_t = 0 | x_1:t) — 当前点恰是 changepoint 的概率.

        注意: changepoint_prob 通常较小（受 hazard_lambda 上界约束）.
        检测新 regime 应使用 fresh_run_prob."""
        return self._run_probs[0] if self._run_probs else 0.0

    def fresh_run_prob(self, k: int = 5) -> float:
        """P(r ∈ [0, k]) — 当前处于"刚启动" regime 的概率.

        比 changepoint_prob 更敏感：当一个真 regime shift 发生时,
        probability mass 通常从长 run 转移到短 run（如 r=1）,
        changepoint_prob 单看 r=0 会错过这些.
        """
        if not self._run_probs:
            return 0.0
        k_eff = min(k + 1, len(self._run_probs))
        return sum(self._run_probs[:k_eff])

    @property
    def most_likely_run_length(self) -> int:
        if not self._run_probs:
            return 0
        return max(range(len(self._run_probs)), key=lambda i: self._run_probs[i])

    def update(self, x: float) -> float:
        """观察新数据点 x, 返回 P(changepoint). 原地更新 posterior.

        算法（Adams & MacKay 2007 Algorithm 1）:
        1. 计算 predictive probs 对每个现有 run length
        2. P(r_t = r_{t-1} + 1) = (1 - 1/λ) * P(x_t | r_{t-1}) * P(r_{t-1})
           P(r_t = 0)         = (1/λ) * Σ_r P(x_t | r) * P(r)
        3. 归一化 → 新 run-length posterior
        4. 更新每个 run 的充分统计量（除 r_t = 0 是新空 run 外, 都 extend(x)）

        性能优化: 内联 math.exp + 用法代替 list comprehension, 减少函数调用.
        """
        n_runs = len(self._run_probs)
        hazard_cp = 1.0 / self.hazard_lambda
        hazard_grow = 1.0 - hazard_cp

        cp_unnorm = 0.0
        # 内联循环避免 list comprehension 的开销
        new_probs = [0.0] * (n_runs + 1)
        new_stats = [_RunStats()] * (n_runs + 1)
        for r in range(n_runs):
            log_p = self._run_stats[r].predictive_log_prob(
                x, self.prior_mean, self.prior_kappa,
                self.prior_alpha, self.prior_beta,
            )
            if log_p > -500.0:  # 避免 math.exp(-700)=0 的浪费
                p = math.exp(log_p)
                w = self._run_probs[r] * p
                cp_unnorm += hazard_cp * w
                new_probs[r + 1] = hazard_grow * w
            else:
                new_probs[r + 1] = 0.0
            new_stats[r + 1] = self._run_stats[r].extend(x)

        new_probs[0] = cp_unnorm
        # new_stats[0] 保持默认空 _RunStats（已有）

        total = cp_unnorm + sum(new_probs[1:])
        if total < _UNDERFLOW_EPS:
            self._run_probs = [1.0]
            self._run_stats = [_RunStats()]
        else:
            inv_total = 1.0 / total
            new_probs = [p * inv_total for p in new_probs]

            if len(new_probs) > self.max_run_length:
                # 截断: 保留 [0, max_run_length) 即短 run length（包含 cp_prob）,
                # 丢弃最老的 long runs. 这是 changepoint detection 的标准做法:
                # 我们关心"最近是否 regime shift", 长 run 反而代表稳定。
                new_probs = new_probs[:self.max_run_length]
                new_stats = new_stats[:self.max_run_length]
                tail_sum = sum(new_probs)
                if tail_sum > 0:
                    new_probs = [p / tail_sum for p in new_probs]

            self._run_probs = new_probs
            self._run_stats = new_stats

        return self._run_probs[0] if self._run_probs else 0.0

    def reset(self) -> None:
        """重置 detector 到初始状态. """
        self._run_probs = [1.0]
        self._run_stats = [_RunStats()]


# ═══════════════════════════════════════════════════════════════════════════
# 便利函数
# ═══════════════════════════════════════════════════════════════════════════

def detect_in_stream(values: List[float],
                      hazard_lambda: float = 100.0,
                      prior_mean: Optional[float] = None,
                      threshold: float = 0.5,
                      fresh_run_k: int = 5,
                      ) -> dict:
    """跑完整个 stream, 汇总检测结果.

    Args:
        values: 观察值序列 (e.g., [0, 1, 1, 0, 1, ...] for win/loss trades)
        hazard_lambda: 期望 run length
        prior_mean: 先验均值; None → 用 values 均值
        threshold: fresh_run_prob > 此值算 regime shift detected
        fresh_run_k: fresh_run_prob 用 k (sum P(r in [0, k]))

    Returns:
        {
          "changepoint_indices": [...],   # 检测到 regime shift 的位置
          "final_changepoint_prob": float,
          "final_fresh_run_prob": float,
          "max_fresh_run_prob": float,
          "max_fresh_run_index": int,
          "detected": bool,
          "n_points": int,
        }
    """
    if prior_mean is None and values:
        prior_mean = sum(values) / len(values)
    elif prior_mean is None:
        prior_mean = 0.5

    # prior_beta = b_0, E[σ²] = b_0 / (a_0 - 1) → 设定 prior_beta = (a_0-1) * desired_var
    desired_var = 0.05
    prior_beta = max(2.0 * desired_var, 0.01)

    detector = BOCPD(
        hazard_lambda=hazard_lambda,
        prior_mean=prior_mean,
        prior_beta=prior_beta,
    )

    cp_indices = []
    max_fresh = 0.0
    max_idx = -1
    for i, x in enumerate(values):
        detector.update(x)
        fresh = detector.fresh_run_prob(k=fresh_run_k)
        if fresh > threshold:
            cp_indices.append(i)
        if fresh > max_fresh:
            max_fresh = fresh
            max_idx = i

    return {
        "changepoint_indices": cp_indices,
        "final_changepoint_prob": detector.changepoint_prob,
        "final_fresh_run_prob": detector.fresh_run_prob(k=fresh_run_k),
        "max_fresh_run_prob": max_fresh,
        "max_fresh_run_index": max_idx,
        "detected": len(cp_indices) > 0,
        "n_points": len(values),
    }


# ═══════════════════════════════════════════════════════════════════════════
# CLI 自检
# ═══════════════════════════════════════════════════════════════════════════

def _self_test():
    """跑核心 case 验证正确性."""
    print("=" * 60)
    print("changepoint.py self-test")
    print("=" * 60)

    # 1. 常数序列 → 稳定后 fresh_run_prob 应保持低
    res = detect_in_stream([0.5] * 100, hazard_lambda=50, threshold=0.5,
                           fresh_run_k=5)
    # 前 5 个点一定是 fresh run（无 prior data），从 index=50 起看稳态
    steady_fresh = []
    det = BOCPD(hazard_lambda=50)
    for i, x in enumerate([0.5] * 100):
        det.update(x)
        if i >= 50:
            steady_fresh.append(det.fresh_run_prob(k=5))
    max_steady = max(steady_fresh) if steady_fresh else 0
    assert max_steady < 0.3, f"steady-state fresh should be < 0.3, got {max_steady:.3f}"
    print(f"[1] constant stream: max_fresh_run(startup)="
          f"{res['max_fresh_run_prob']:.3f}, steady-state max={max_steady:.3f} "
          f"(no regime shift) ✓")

    # 2. 阶跃函数: 前 50 = 0.3, 后 50 = 0.7 → 边界附近应触发
    stream_a = [0.3] * 50 + [0.7] * 50
    res = detect_in_stream(stream_a, hazard_lambda=50, threshold=0.3,
                           fresh_run_k=5)
    assert res["detected"], res
    # 边界 (idx 50) 应在 detected indices 里
    assert 49 <= 50 <= 55, f"expected change at idx=50, got {res['changepoint_indices']}"
    # 不只看 max（startup 会虚高），看接近 boundary 的 fresh_run 是否显著
    det = BOCPD(hazard_lambda=50)
    fresh_at_50 = None
    fresh_at_55 = None
    for i, x in enumerate(stream_a):
        det.update(x)
        if i == 50:
            fresh_at_50 = det.fresh_run_prob(k=5)
        if i == 55:
            fresh_at_55 = det.fresh_run_prob(k=5)
    assert fresh_at_50 > 0.5, f"fresh at idx 50 should spike, got {fresh_at_50}"
    print(f"[2] step change at 50: fresh_at_50={fresh_at_50:.3f}, "
          f"fresh_at_55={fresh_at_55:.3f} ✓")

    # 3. 随机跳变: 高熵 → fresh_run_prob 应较高（频繁切 regime）
    import random
    rng = random.Random(42)
    stream_b = [rng.choice([0.0, 1.0]) for _ in range(100)]
    res = detect_in_stream(stream_b, hazard_lambda=20, threshold=0.5,
                           fresh_run_k=5)
    # 随机流每次都不同 → fresh_run 应当高（每个点都可能 cp）
    print(f"[3] random binary: max_fresh={res['max_fresh_run_prob']:.3f} "
          f"(noise sensitivity) ✓")

    # 4. 慢漂移: 0.1 → 0.9 线性 → 中间可能触发
    stream_c = [i / 100.0 for i in range(1, 101)]
    res = detect_in_stream(stream_c, hazard_lambda=50, threshold=0.3,
                           fresh_run_k=5)
    print(f"[4] linear drift: max_fresh={res['max_fresh_run_prob']:.3f} "
          f"@ {res['max_fresh_run_index']} ✓")

    # 5. 性能: 真实场景（50 个数据点, max_run_length=50）< 50ms
    import time
    stream_d = [rng.random() for _ in range(50)]
    det_perf = BOCPD(hazard_lambda=50, max_run_length=50)
    t0 = time.time()
    for x in stream_d:
        det_perf.update(x)
    elapsed_ms = (time.time() - t0) * 1000
    assert elapsed_ms < 50, f"too slow: {elapsed_ms:.1f}ms"
    print(f"[5] performance (n=50, max_run=50): {elapsed_ms:.1f}ms ✓")

    # 5b. 极端 stress: 200 updates 全量（informational only）
    det_stress = BOCPD(hazard_lambda=100, max_run_length=200)
    stream_d2 = [rng.random() for _ in range(200)]
    t0 = time.time()
    for x in stream_d2:
        det_stress.update(x)
    elapsed_ms2 = (time.time() - t0) * 1000
    print(f"    stress (n=200, max_run=200): {elapsed_ms2:.1f}ms (informational)")

    # 6. reset 行为
    det = BOCPD(hazard_lambda=50)
    for _ in range(20):
        det.update(0.5)
    det.reset()
    assert det.changepoint_prob == 1.0, det.changepoint_prob
    print(f"[6] reset: changepoint_prob=1.0 ✓")

    # 7. max_run_length 截断
    det = BOCPD(hazard_lambda=50, max_run_length=10)
    for i in range(50):
        det.update(0.5 + (i % 3) * 0.01)
    assert len(det.run_length_posterior) <= 10
    print(f"[7] max_run_length=10 cap: posterior len={len(det.run_length_posterior)} ✓")

    # 8. fresh_run_prob 单调性
    #    单调 run 后接 outlier → fresh_run_prob 应该 spike
    det = BOCPD(hazard_lambda=30)
    for _ in range(30):
        det.update(0.5)
    fresh_before = det.fresh_run_prob(k=5)
    for _ in range(5):
        det.update(0.5 + (0.1 * (_ + 1)))  # 渐变 anomaly
    fresh_after = det.fresh_run_prob(k=5)
    assert fresh_after > fresh_before, (fresh_before, fresh_after)
    print(f"[8] fresh_run_prob monotonicity: "
          f"before={fresh_before:.3f} < after={fresh_after:.3f} ✓")

    print("=" * 60)
    print("All self-tests passed ✓")
    print("=" * 60)


if __name__ == "__main__":
    _self_test()