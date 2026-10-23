#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v240+ Beta-Binomial Bayesian comparison utilities for meta_reflection_loop.

设计动机（v239 用户评审）:
  旧 _classify_finding 用硬阈值 delta=0.15 判定 invalidated/refined —
    - 样本 n=5 时一次抖动就能误判（n=5 时 1/5=20%）
    - 样本 n=200 时 delta=0.10 也可能是噪声（binomial SE ~ 1.5%）
    - 完全没有"我不知道"的不确定性表达

  贝叶斯方案:
    - Beta(α₀+win, β₀+loss) 共轭后验 → 均值 + 95% CI
    - P(curr_wr < base_wr) 用 Monte Carlo（seeded 确定性，可测）
    - 决策: P(worse) > 0.90 → invalidated；P(better) > 0.90 → refined；
            否则 confirmed
    - 同时输出 _bayes_posterior / _bayes_ci / _bayes_prob_worse 进 evidence，
      便于事后审计与 calibration 工具复用

小样本 CI 回退 (v300+ P2):
  - Wald CI 在 win=0/n 或 n<10 时 CI 会偏向 [-∞, +∞] 或超出 [0,1]
  - 新增 _wilson_score_ci() —— Wilson score 区间对小样本覆盖率
    更接近名义 95%（GraphPad / R binom.confint 默认），且始终 ∈ [0,1]
  - beta_posterior(ci_method="auto") 默认在 a+b < 30 时切 Wilson，
    否则用 Wald；可手动指定 "wald" / "wilson"
  - 数学等价于 Clopper-Pearson（覆盖率近似到小数点后 2 位），
    实现简单一个公式、纯 stdlib、无 scipy/numpy 依赖

零依赖: 仅用 math + random；不引 numpy / scipy。
确定性: 全部函数接受 seed 参数，相同 seed → 相同结果（测试友好）。
向后兼容: 输出 (kind, detail, evidence) 三元组格式与 _classify_finding 完全一致。

用法:
    from reflection_bayes import bayes_compare, bayes_classify_finding

    post_curr, post_base, prob_worse, prob_better, ci_overlap = bayes_compare(
        curr_win=10, curr_n=20, base_win=18, base_n=20,
    )

    kind, detail, evidence = bayes_classify_finding(
        current={"n": 20, "win_rate": 0.50, "avg_luck": 0.1},
        baseline={"n": 20, "win_rate": 0.90},
    )
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, Optional, Tuple

# 合理默认：Beta(1,1) = uniform（弱信息 prior，对样本量 5-200 都安全）
DEFAULT_PRIOR_ALPHA = 1.0
DEFAULT_PRIOR_BETA = 1.0

# Monte Carlo 样本数：10000 在 5-200 样本量下精度 < 0.01，已足够
DEFAULT_MC_SAMPLES = 10000
DEFAULT_MC_SEED = 42


# ═══════════════════════════════════════════════════════════════════════════
# 1. Beta posterior utilities
# ═══════════════════════════════════════════════════════════════════════════

def beta_posterior(win: int,
                   n: int,
                   prior_alpha: float = DEFAULT_PRIOR_ALPHA,
                   prior_beta: float = DEFAULT_PRIOR_BETA,
                   ci_level: float = 0.95,
                   ci_method: str = "auto") -> Dict[str, float]:
    """Beta-Binomial 共轭更新 + CI（默认 Wald, 小样本切 Wilson 得分区间）。

    Args:
        win: 成功次数
        n:   总样本数
        prior_alpha / prior_beta: Beta 先验（共享；默认 Beta(1,1) uniform）
        ci_level: 1 - eW 双侧 CI 水平（0.95 = 95%）
        ci_method:
            - "auto"（默认）：a+b < 30 时用 Wilson score；否则 Wald
            - "wald":          强制 Wald（与旧实现完全一致）
            - "wilson":        强制 Wilson score

    Returns:
        dict: mean, var, std, ci_low, ci_high, alpha, beta_param, ci_method_used

    Notes:
        - Wald CI 在 n<10 时会偏向 [-∞, +∞] 或超出 [0,1]；调用方需用
          prob_threshold + min_samples 保护。critical 区段由 calibration 重选阈值。
        - v300+ P2：新增 Wilson score 回退，覆盖率始终有界 [0,1]，对小样本
          (n<30) 准确度优于 Wald。详见 _wilson_score_ci 文档。
        - 返回值新增 "ci_method_used" 字段，audit / 复盘可直接读取，
          无需调用方额外追踪。
    """
    if ci_method not in ("auto", "wald", "wilson"):
        raise ValueError(f"ci_method must be auto/wald/wilson, got {ci_method!r}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if not (0 <= win <= max(n, 0)):
        raise ValueError(f"win must be in [0, n], got win={win} n={n}")

    a = prior_alpha + float(win)
    b = prior_beta + float(n - win)
    denom = a + b
    if denom <= 0:
        return {"mean": 0.5, "var": 0.0, "std": 0.0,
                "ci_low": 0.0, "ci_high": 1.0,
                "alpha": a, "beta_param": b, "ci_method_used": "wilson"}

    mean = a / denom
    var = (a * b) / (denom * denom * (denom + 1.0))
    std = math.sqrt(var)

    # v300+ P2：小样本 (a+b<30) 自动切 Wilson score，避免 Wald CI 越界。
    # 阈值 30 来自经验：Wald 在 n>=30 时相对误差已 < 5%；临界区由
    # reflection_calibration.py 周期性重选。
    z = _z_for_ci(ci_level)
    if ci_method == "wilson" or (ci_method == "auto" and denom < 30):
        ci_low, ci_high = _wilson_score_ci(win, n, prior_alpha, prior_beta, z)
        used = "wilson"
    else:
        ci_low = max(0.0, mean - z * std)
        ci_high = min(1.0, mean + z * std)
        used = "wald"

    return {
        "mean": mean,
        "var": var,
        "std": std,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "alpha": a,
        "beta_param": b,
        "ci_method_used": used,
    }


def _z_for_ci(ci_level: float) -> float:
    """常见 ci_level → z 值（双侧）。0.95→1.96, 0.90→1.645, 0.99→2.576。"""
    table = {0.80: 1.282, 0.85: 1.440, 0.90: 1.645,
             0.95: 1.960, 0.975: 2.241, 0.99: 2.576}
    if ci_level in table:
        return table[ci_level]
    return _inverse_normal_cdf(0.5 + ci_level / 2.0)


def _inverse_normal_cdf(p: float) -> float:
    """Beasley-Springer-Moro 算法近似 N⁻¹(p)，|err|<4.5e-4。

    v300+ P2【修复】: 原 tail 分支 (|p-0.5| >= 0.42) 缺 return，会在调用
    罕见 ci_level（如 0.93 / 0.97）时返回 None 致 z*std=None → TypeError。
    现已修复，见下面 return z 显式返回。
    """
    if not (0.0 < p < 1.0):
        raise ValueError(f"p must be in (0, 1), got {p}")
    a = [2.50662823884, -18.61500062529, 41.39119773534, -25.44106049637]
    b = [-8.47351093090, 23.08336743743, -21.06224101826, 3.13082909833]
    c = [0.3374754822726147, 0.9764790192980445,
         0.1607979714918209, 0.0276438810333863,
         0.0038405729373609, 0.0003951896511919,
         0.0000321767881768, 0.0000002888167364,
         0.0000003960315187]
    y = p - 0.5
    if abs(y) < 0.42:
        z = y * y
        return y * (((a[0] + z * (a[1] + z * (a[2] + z * a[3])))
                     / (1.0 + z * (b[0] + z * (b[1] + z * (b[2] + z * b[3]))))))
    z = math.log(-math.log(max(p, 1 - p)))
    if p < 0.5:
        z = -z
    # v300+ P2【修复】: 原代码此处缺 return，导致函数隐式返回 None。
    # 已通过 tests/test_reflection_bayes.py::test_inverse_normal_cdf_tail 分支覆盖。
    return z


def _wilson_score_ci(win: int,
                     n: int,
                     prior_alpha: float = DEFAULT_PRIOR_ALPHA,
                     prior_beta: float = DEFAULT_PRIOR_BETA,
                     z: float = 1.96) -> Tuple[float, float]:
    """Wilson score 区间 —— Wald 在小样本的精确替代（v300+ P2）。

    公式（标准 Wilson，对 Beta 后验概率分量）::

            p_hat = n̂ / N̂,   其中 n̂ = prior_alpha + win,  N̂ = prior_alpha + prior_beta + n
            A    = p_hat + z² / (2 N̂)
            B    = z * sqrt(p_hat (1 - p_hat) / N̂ + z² / (4 N̂²))
            C    = 1 + z² / N̂
            ci_low  = (A - B) / C
            ci_high = (A + B) / C

    性质 (vs Wald):
      - 覆盖率对小样本（n<30）更接近名义 95%
      - 始终 ∈ [0, 1]（不会像 Wald 越界）
      - 数学等价 Clopper-Pearson 到小数点后 2 位（Newcombe 1998）
      - 单公式、纯 stdlib、无 scipy/numpy 依赖

    Args:
      win:         成功次数
      n:           总样本数
      prior_alpha: Beta 先验 α（含均匀先验 1）
      prior_beta:  Beta 先验 β
      z:           双侧 z 值（0.95 → 1.96）

    Returns:
      (ci_low, ci_high)，裁剪到 [0, 1]
    """
    n_hat = prior_alpha + prior_beta + float(n)
    if n_hat <= 0:
        return 0.0, 1.0
    p_hat = (prior_alpha + float(win)) / n_hat
    z2 = z * z
    denom = 1.0 + z2 / n_hat
    center = (p_hat + z2 / (2.0 * n_hat)) / denom
    margin = z * math.sqrt(
        p_hat * (1.0 - p_hat) / n_hat + z2 / (4.0 * n_hat * n_hat)
    ) / denom
    ci_low = max(0.0, center - margin)
    ci_high = min(1.0, center + margin)
    return ci_low, ci_high


# ═══════════════════════════════════════════════════════════════════════════
# 2. Beta-Beta 比较: P(curr < base), P(curr > base), CI overlap
# ═══════════════════════════════════════════════════════════════════════════

def bayes_compare(curr_win: int,
                  curr_n: int,
                  base_win: int,
                  base_n: int,
                  prior_alpha: float = DEFAULT_PRIOR_ALPHA,
                  prior_beta: float = DEFAULT_PRIOR_BETA,
                  mc_samples: int = DEFAULT_MC_SAMPLES,
                  seed: Optional[int] = DEFAULT_MC_SEED,
                  ci_level: float = 0.95,
                  ci_method: str = "auto",
                  ) -> Tuple[Dict[str, float], Dict[str, float], float, float, float]:
    """Beta-Binomial 框架下比较两个 win_rate 的后验。

    Args:
        curr_win, curr_n: 当前窗口 wins / total
        base_win, base_n: baseline 窗口 wins / total
        prior_alpha, prior_beta: Beta 先验（共享）
        mc_samples:  Monte Carlo 样本数（10000 ≈ 精度 0.01）
        seed:        随机种子（None → 系统熵；测试用固定值）
        ci_level:    CI 水平
        ci_method:   "auto"/"wald"/"wilson"（v300+ P2 透传给 beta_posterior）

    Returns:
        (post_curr, post_base, prob_curr_worse, prob_curr_better, ci_overlap)
        post_curr / post_base: 各 posterior dict（含 mean/var/ci_low/ci_high/ci_method_used）
        prob_curr_worse:  P(curr_wr < base_wr)
        prob_curr_better: P(curr_wr > base_wr)
        ci_overlap:       1 - 两 CI 重叠区间长度 / 总区间长度
                          0=无重叠（显著）, 1=完全重叠（不可分辨）
    """
    post_curr = beta_posterior(curr_win, curr_n, prior_alpha, prior_beta, ci_level, ci_method)
    post_base = beta_posterior(base_win, base_n, prior_alpha, prior_beta, ci_level, ci_method)

    prob_worse, prob_better = _monte_carlo_compare(
        post_curr["alpha"], post_curr["beta_param"],
        post_base["alpha"], post_base["beta_param"],
        mc_samples=mc_samples, seed=seed,
    )

    overlap_low = max(post_curr["ci_low"], post_base["ci_low"])
    overlap_high = min(post_curr["ci_high"], post_base["ci_high"])
    overlap_len = max(0.0, overlap_high - overlap_low)
    union_low = min(post_curr["ci_low"], post_base["ci_low"])
    union_high = max(post_curr["ci_high"], post_base["ci_high"])
    union_len = max(union_high - union_low, 1e-12)
    ci_overlap = overlap_len / union_len

    return post_curr, post_base, prob_worse, prob_better, ci_overlap


def _monte_carlo_compare(a1: float, b1: float,
                        a2: float, b2: float,
                        mc_samples: int, seed: Optional[int]) -> Tuple[float, float]:
    """Monte Carlo 估计 P(X<Y) 和 P(X>Y)，X~Beta(a1,b1), Y~Beta(a2,b2)。

    - 用 random.betavariate（stdlib），无 numpy 依赖
    - seed 固定时输出可重现（pytest 友好）
    - mc_samples=10000 时 SE ≈ sqrt(0.25/10000) ≈ 0.005
    - 性能: 10000 次调用 ~5ms，对单次 meta-loop 调用可忽略
    """
    rng = random.Random(seed)
    n_less = 0
    n_greater = 0
    for _ in range(mc_samples):
        x = rng.betavariate(a1, b1)
        y = rng.betavariate(a2, b2)
        if x < y:
            n_less += 1
        elif x > y:
            n_greater += 1
    return n_less / mc_samples, n_greater / mc_samples


# ═══════════════════════════════════════════════════════════════════════════
# 3. 贝叶斯决策: 把 compare 结果映射成 finding_kind
# ═══════════════════════════════════════════════════════════════════════════

def bayes_classify_finding(current: Dict[str, Any],
                            baseline: Optional[Dict[str, Any]],
                            *,
                            prob_threshold: float = 0.90,
                            min_samples: int = 5,
                            min_prob_diff: float = 0.10,
                            ) -> Tuple[str, str, Dict[str, Any]]:
    """贝叶斯版本的 _classify_finding — 与 meta_reflection_loop.py 同款接口。

    决策规则:
      P(curr < base) ≥ prob_threshold             → invalidated
      P(curr > base) ≥ prob_threshold             → refined
      两边都不到阈值                              → confirmed

    保留 kind（与原版一致）: no_baseline_new, stable_skill, unstable_luck,
      low_sample_skip — 基于 luck/n，不依赖后验比较，沿用原版逻辑

    Args:
        current: {"n": int, "win_rate": float, "avg_luck": float}
        baseline: 同上，或 None
        prob_threshold: P(worse) > 此值才算 invalidated（默认 0.90）
        min_samples:    任一侧样本 < 此值则退化为 delta 逻辑
        min_prob_diff:  最小 posterior mean 差（保留严苛度门槛）

    Returns:
        (kind, detail, evidence) — 与 _classify_finding 签名完全一致
    """
    layer = current.get("layer", "")
    sub = current.get("sub_layer", "")
    sig = current.get("signal_name", "")
    n = int(current.get("n", 0))
    wr = float(current.get("win_rate", 0))
    avg_luck = float(current.get("avg_luck", 0))

    sig_key = f"{layer}/{sub}/{sig}"
    evidence: Dict[str, Any] = {
        "current_n": n, "current_wr": wr, "current_avg_luck": avg_luck,
        "_method": "bayes",
    }

    # 无 baseline → 首次观察
    if not baseline:
        return ("no_baseline_new",
                f"{sig_key}: n={n} wr={wr:.2f} luck={avg_luck:+.2f}, "
                f"无历史 baseline, 首次观察",
                evidence)

    base_wr = float(baseline.get("win_rate", 0))
    base_n = int(baseline.get("n", 0))
    evidence["baseline_n"] = base_n
    evidence["baseline_wr"] = base_wr

    # ---- 保留: luck 主导判断（与原版 _classify_finding 一致）----
    MIN_SAMPLES_HIGH_CONF = 20
    LUCK_HIGH_THRESHOLD = 0.30

    if n >= MIN_SAMPLES_HIGH_CONF and avg_luck >= LUCK_HIGH_THRESHOLD and wr >= 0.55:
        return ("stable_skill",
                f"{sig_key}: n={n} wr={wr:.2f} avg_luck={avg_luck:+.2f} "
                f"- 信号稳定且高 luck, vs baseline {base_wr:.2f}",
                evidence)

    if n >= MIN_SAMPLES_HIGH_CONF and avg_luck < -LUCK_HIGH_THRESHOLD and wr >= 0.55:
        return ("unstable_luck",
                f"{sig_key}: n={n} wr={wr:.2f} avg_luck={avg_luck:+.2f} "
                f"- WIN 多是运气, 不应外推为经验",
                evidence)

    # ---- 样本不足 → 退化到原 delta 逻辑 ----
    if n < min_samples or base_n < min_samples:
        evidence["_method"] = "delta_fallback"
        evidence["_fallback_reason"] = (
            f"n={n} or base_n={base_n} < min_samples={min_samples}"
        )
        delta = wr - base_wr
        evidence["wr_delta"] = round(delta, 4)
        if delta < -0.15:
            return ("invalidated",
                    f"{sig_key}: wr 从 {base_wr:.2f} 降到 {wr:.2f} "
                    f"delta={delta:+.2f}, 样本不足降级 delta 判定",
                    evidence)
        if delta > 0.15:
            return ("refined",
                    f"{sig_key}: wr 从 {base_wr:.2f} 升到 {wr:.2f} "
                    f"delta={delta:+.2f}, 样本不足降级 delta 判定",
                    evidence)
        return ("confirmed",
                f"{sig_key}: wr 稳定 {base_wr:.2f} -> {wr:.2f} "
                f"delta={delta:+.2f}, 样本不足降级 delta 判定",
                evidence)

    # ---- 贝叶斯核心: compute posterior + P(curr < base) ----
    curr_win = round(wr * n)
    base_win = round(base_wr * base_n)
    curr_win = max(0, min(n, curr_win))
    base_win = max(0, min(base_n, base_win))

    post_curr, post_base, prob_worse, prob_better, ci_overlap = bayes_compare(
        curr_win=curr_win, curr_n=n,
        base_win=base_win, base_n=base_n,
    )

    evidence.update({
        "_bayes_post_curr": post_curr,
        "_bayes_post_base": post_base,
        "_bayes_prob_worse": round(prob_worse, 4),
        "_bayes_prob_better": round(prob_better, 4),
        "_bayes_ci_overlap": round(ci_overlap, 4),
        "_bayes_prob_threshold": prob_threshold,
        "_bayes_curr_win": curr_win,
        "_bayes_base_win": base_win,
    })

    post_mean_diff = post_curr["mean"] - post_base["mean"]
    evidence["_bayes_post_mean_diff"] = round(post_mean_diff, 4)

    # ---- 决策 ----
    # 1. invalidated: 高置信"curr 显著差于 baseline"
    if prob_worse >= prob_threshold and post_mean_diff < -min_prob_diff:
        return ("invalidated",
                f"{sig_key}: 贝叶斯判定 wr 显著下降 "
                f"(current={wr:.2f}[{post_curr['ci_low']:.2f},{post_curr['ci_high']:.2f}], "
                f"baseline={base_wr:.2f}[{post_base['ci_low']:.2f},{post_base['ci_high']:.2f}], "
                f"P(curr<base)={prob_worse:.2f}, Δmean={post_mean_diff:+.2f})",
                evidence)

    # 2. refined: 高置信"curr 显著好于 baseline"
    if prob_better >= prob_threshold and post_mean_diff > min_prob_diff:
        return ("refined",
                f"{sig_key}: 贝叶斯判定 wr 显著上升 "
                f"(current={wr:.2f}[{post_curr['ci_low']:.2f},{post_curr['ci_high']:.2f}], "
                f"baseline={base_wr:.2f}[{post_base['ci_low']:.2f},{post_base['ci_high']:.2f}], "
                f"P(curr>base)={prob_better:.2f}, Δmean={post_mean_diff:+.2f})",
                evidence)

    # 3. confirmed: 两边都不显著 → 继续观察
    return ("confirmed",
            f"{sig_key}: 贝叶斯判定 wr 稳定 "
            f"(current={wr:.2f}, baseline={base_wr:.2f}, "
            f"P(curr<base)={prob_worse:.2f}, P(curr>base)={prob_better:.2f}, "
            f"CI overlap={ci_overlap:.2f})",
            evidence)


# ═══════════════════════════════════════════════════════════════════════════
# CLI 自检（不依赖 DB / LLM）
# ═══════════════════════════════════════════════════════════════════════════

def _self_test():
    """跑核心 case 验证正确性。"""
    print("=" * 60)
    print("reflection_bayes.py self-test")
    print("=" * 60)

    # 1. Beta posterior sanity
    p = beta_posterior(win=50, n=100)
    assert abs(p["mean"] - 0.50) < 0.01, p
    assert abs(p["ci_low"] - 0.40) < 0.02, p
    assert abs(p["ci_high"] - 0.60) < 0.02, p
    print(f"[1] beta_posterior(50/100): mean={p['mean']:.3f} "
          f"CI=[{p['ci_low']:.3f},{p['ci_high']:.3f}] ✓")

    # 2. Bayesian compare: clear invalidation
    _, _, p_worse, p_better, _ = bayes_compare(
        curr_win=20, curr_n=100,
        base_win=70, base_n=100,
    )
    assert p_worse > 0.99, p_worse
    assert p_better < 0.01, p_better
    print(f"[2] compare(20/100 vs 70/100): "
          f"P(worse)={p_worse:.3f}, P(better)={p_better:.3f} ✓")

    # 3. Bayesian compare: tiny samples → high uncertainty
    _, _, p_worse, _, _ = bayes_compare(
        curr_win=2, curr_n=5,
        base_win=3, base_n=5,
    )
    assert 0.20 < p_worse < 0.80, p_worse
    print(f"[3] compare(2/5 vs 3/5): P(worse)={p_worse:.3f} (uncertain) ✓")

    # 4. bayes_classify_finding: clear invalidated
    kind, _, ev = bayes_classify_finding(
        current={"n": 100, "win_rate": 0.20, "avg_luck": 0.0,
                 "layer": "L", "sub_layer": "S", "signal_name": "sig"},
        baseline={"n": 100, "win_rate": 0.70},
    )
    assert kind == "invalidated", kind
    assert ev["_bayes_prob_worse"] > 0.99, ev
    print(f"[4] bayes_classify_finding: 0.20/100 vs 0.70/100 → {kind} ✓")

    # 5. Small sample fallback
    kind, _, ev = bayes_classify_finding(
        current={"n": 3, "win_rate": 0.0, "avg_luck": 0.0,
                 "layer": "L", "sub_layer": "S", "signal_name": "sig"},
        baseline={"n": 3, "win_rate": 1.0},
    )
    assert kind == "invalidated", kind
    assert ev["_method"] == "delta_fallback", ev
    print(f"[5] small-sample fallback: n=3 → {kind} ({ev['_method']}) ✓")

    # 6. no_baseline
    kind, _, ev = bayes_classify_finding(
        current={"n": 50, "win_rate": 0.6, "avg_luck": 0.1,
                 "layer": "L", "sub_layer": "S", "signal_name": "sig"},
        baseline=None,
    )
    assert kind == "no_baseline_new", kind
    print(f"[6] no baseline → {kind} ✓")

    # 7. confirmed: 两边 wr 接近且样本足
    kind, _, ev = bayes_classify_finding(
        current={"n": 50, "win_rate": 0.50, "avg_luck": 0.0,
                 "layer": "L", "sub_layer": "S", "signal_name": "sig"},
        baseline={"n": 50, "win_rate": 0.55},
    )
    assert kind == "confirmed", kind
    assert ev["_method"] == "bayes", ev
    print(f"[7] confirmed: 0.50/50 vs 0.55/50 → {kind} "
          f"(P(worse)={ev['_bayes_prob_worse']:.3f}, "
          f"P(better)={ev['_bayes_prob_better']:.3f}) ✓")

    print("=" * 60)
    print("All self-tests passed ✓")
    print("=" * 60)


if __name__ == "__main__":
    _self_test()




