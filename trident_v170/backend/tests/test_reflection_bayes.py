"""Tests for reflection_bayes.py (v240+) and meta-loop Bayesian integration.

覆盖:
  - reflection_bayes.beta_posterior: mean / var / CI 数学正确性
  - reflection_bayes.bayes_compare: P(worse) / P(better) 单调性 + 对称性
  - reflection_bayes.bayes_classify_finding: 8 个 finding_kind 决策路径
  - meta_reflection_loop._classify_finding: 配置开关 + 集成
  - 全部 100% 离线，无 LLM / DB 依赖

v300+ P2 新增 Wilson score 区间测试:
  - _wilson_score_ci 数学正确性
  - beta_posterior(ci_method="auto") / "wald" / "wilson" 切换
  - _inverse_normal_cdf tail 分支 (v300+ P2 修复)
  - ci_method_used 字段透传
"""
from __future__ import annotations

import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import reflection_bayes as rb  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# 1. beta_posterior 数学正确性
# ═══════════════════════════════════════════════════════════════════════════

def test_beta_posterior_mean_simple():
    """50/100 → mean=0.50."""
    p = rb.beta_posterior(win=50, n=100)
    assert abs(p["mean"] - 0.50) < 1e-9


def test_beta_posterior_ci_width_decreases_with_n():
    """n 越大 CI 越窄。"""
    p_small = rb.beta_posterior(win=5, n=10)
    p_large = rb.beta_posterior(win=50, n=100)
    width_small = p_small["ci_high"] - p_small["ci_low"]
    width_large = p_large["ci_high"] - p_large["ci_low"]
    assert width_small > width_large


def test_beta_posterior_uniform_prior_handling():
    """n=0: 退化为先验 (uniform → mean=0.5)。"""
    p = rb.beta_posterior(win=0, n=0)
    assert abs(p["mean"] - 0.5) < 1e-9


def test_beta_posterior_invalid_n_raises():
    """n < 0 应抛错。"""
    with pytest.raises(ValueError):
        rb.beta_posterior(win=0, n=-1)


def test_beta_posterior_win_out_of_range_raises():
    """win > n 应抛错。"""
    with pytest.raises(ValueError):
        rb.beta_posterior(win=10, n=5)


def test_beta_posterior_ci_in_unit_interval():
    """CI 应被裁剪到 [0, 1]。"""
    p = rb.beta_posterior(win=0, n=2)
    assert 0.0 <= p["ci_low"] <= 1.0
    assert 0.0 <= p["ci_high"] <= 1.0
    assert p["ci_low"] <= p["ci_high"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. bayes_compare 单调性 + 对称性 + 不确定性
# ═══════════════════════════════════════════════════════════════════════════

def test_compare_clear_worse_high_prob():
    """20/100 vs 70/100: P(curr<base) 应接近 1.0。"""
    _, _, p_worse, p_better, _ = rb.bayes_compare(
        curr_win=20, curr_n=100,
        base_win=70, base_n=100,
    )
    assert p_worse > 0.99
    assert p_better < 0.01


def test_compare_clear_better_high_prob():
    """70/100 vs 20/100: P(curr>base) 应接近 1.0（对称性）。"""
    _, _, p_worse, p_better, _ = rb.bayes_compare(
        curr_win=70, curr_n=100,
        base_win=20, base_n=100,
    )
    assert p_better > 0.99
    assert p_worse < 0.01


def test_compare_identical_distributions():
    """两边 wr 相同 → P(worse) ≈ P(better) ≈ 0.5。"""
    _, _, p_worse, p_better, ci_ov = rb.bayes_compare(
        curr_win=50, curr_n=100,
        base_win=50, base_n=100,
    )
    # Monte Carlo SE ≈ 0.005 → 容忍 ±0.02
    assert abs(p_worse - 0.5) < 0.02
    assert abs(p_better - 0.5) < 0.02
    assert ci_ov > 0.5  # CI 完全重叠


def test_compare_small_samples_high_uncertainty():
    """2/5 vs 3/5 → 两边都不应极端置信。"""
    _, _, p_worse, _, _ = rb.bayes_compare(
        curr_win=2, curr_n=5,
        base_win=3, base_n=5,
    )
    assert 0.2 < p_worse < 0.8


def test_compare_large_n_low_uncertainty():
    """n=1000, 差异 5%: 应有显著置信度。"""
    _, _, p_worse, _, _ = rb.bayes_compare(
        curr_win=450, curr_n=1000,
        base_win=500, base_n=1000,
    )
    assert p_worse > 0.85


def test_compare_deterministic_with_seed():
    """相同 seed 应输出相同结果（pytest 友好）。"""
    args = dict(curr_win=20, curr_n=100, base_win=70, base_n=100, seed=42)
    r1 = rb.bayes_compare(**args)
    r2 = rb.bayes_compare(**args)
    assert r1[2] == r2[2]
    assert r1[3] == r2[3]


# ═══════════════════════════════════════════════════════════════════════════
# 3. bayes_classify_finding 决策路径
# ═══════════════════════════════════════════════════════════════════════════

def _cur(wr, n=50, luck=0.0):
    return {"n": n, "win_rate": wr, "avg_luck": luck,
            "layer": "L", "sub_layer": "S", "signal_name": "sig"}


def _base(wr, n=50):
    return {"n": n, "win_rate": wr}


def test_classify_no_baseline():
    kind, _, ev = rb.bayes_classify_finding(
        current=_cur(0.6), baseline=None,
    )
    assert kind == "no_baseline_new"
    assert ev["_method"] == "bayes"


def test_classify_clear_invalidated():
    """0.20/100 vs 0.70/100 → invalidated, P(worse) 极高。"""
    kind, _, ev = rb.bayes_classify_finding(
        current=_cur(0.20, n=100),
        baseline=_base(0.70, n=100),
    )
    assert kind == "invalidated"
    assert ev["_method"] == "bayes"
    assert ev["_bayes_prob_worse"] > 0.99


def test_classify_clear_refined():
    """0.70/100 vs 0.20/100 → refined, P(better) 极高。"""
    kind, _, ev = rb.bayes_classify_finding(
        current=_cur(0.70, n=100),
        baseline=_base(0.20, n=100),
    )
    assert kind == "refined"
    assert ev["_method"] == "bayes"
    assert ev["_bayes_prob_better"] > 0.99


def test_classify_confirmed_close_wr():
    """0.50/50 vs 0.55/50 → confirmed (差异小)。"""
    kind, _, ev = rb.bayes_classify_finding(
        current=_cur(0.50, n=50),
        baseline=_base(0.55, n=50),
    )
    assert kind == "confirmed"
    assert ev["_method"] == "bayes"


def test_classify_small_sample_delta_fallback():
    """n=3 < min_samples=5 → 走 delta 逻辑, _method 标记。"""
    kind, _, ev = rb.bayes_classify_finding(
        current=_cur(0.0, n=3),
        baseline=_base(1.0, n=3),
    )
    assert kind == "invalidated"
    assert ev["_method"] == "delta_fallback"
    assert "_fallback_reason" in ev


def test_classify_stable_skill():
    """高样本 + 高 luck + wr >= 0.55 → stable_skill。"""
    kind, _, ev = rb.bayes_classify_finding(
        current=_cur(0.65, n=30, luck=0.5),
        baseline=_base(0.50, n=30),
    )
    assert kind == "stable_skill"


def test_classify_unstable_luck():
    """高样本 + 低 luck + wr >= 0.55 → unstable_luck。"""
    kind, _, ev = rb.bayes_classify_finding(
        current=_cur(0.65, n=30, luck=-0.5),
        baseline=_base(0.50, n=30),
    )
    assert kind == "unstable_luck"


def test_classify_evidence_has_bayes_keys():
    """正常路径下 evidence 应包含完整贝叶斯审计字段。"""
    _, _, ev = rb.bayes_classify_finding(
        current=_cur(0.50, n=100),
        baseline=_base(0.50, n=100),
    )
    for k in [
        "_method", "_bayes_post_curr", "_bayes_post_base",
        "_bayes_prob_worse", "_bayes_prob_better",
        "_bayes_ci_overlap", "_bayes_prob_threshold",
        "_bayes_post_mean_diff",
    ]:
        assert k in ev, f"missing key {k} in evidence"


# ═══════════════════════════════════════════════════════════════════════════
# 4. meta_reflection_loop 集成 — 配置开关
# ═══════════════════════════════════════════════════════════════════════════

def test_meta_loop_uses_bayes_when_enabled(monkeypatch):
    """META_USE_BAYES=True (默认) + 大样本 → evidence 应包含 _bayes_* 字段。"""
    monkeypatch.setenv("META_USE_BAYES", "1")
    monkeypatch.setenv("META_BAYES_MC_SAMPLES", "500")  # 加速
    import importlib
    import config as cfg
    importlib.reload(cfg)
    import meta_reflection_loop as mr
    importlib.reload(mr)

    kind, _, ev = mr._classify_finding(
        current={"n": 100, "win_rate": 0.20, "avg_luck": 0.0,
                 "layer": "L", "sub_layer": "S", "signal_name": "sig"},
        baseline={"n": 100, "win_rate": 0.70},
    )
    assert kind == "invalidated"
    assert ev.get("_method") == "bayes"
    assert "_bayes_prob_worse" in ev


def test_meta_loop_falls_back_to_delta_when_disabled(monkeypatch):
    """META_USE_BAYES=0 → 走原 delta 逻辑, evidence 无 _method=delta。"""
    monkeypatch.setenv("META_USE_BAYES", "0")
    import importlib
    import config as cfg
    importlib.reload(cfg)
    import meta_reflection_loop as mr
    importlib.reload(mr)

    kind, _, ev = mr._classify_finding(
        current={"n": 100, "win_rate": 0.20, "avg_luck": 0.0,
                 "layer": "L", "sub_layer": "S", "signal_name": "sig"},
        baseline={"n": 100, "win_rate": 0.70},
    )
    assert kind == "invalidated"
    assert "_method" not in ev
    assert "wr_delta" in ev


def test_meta_loop_bayes_small_sample_falls_back(monkeypatch):
    """小样本 → 贝叶斯分支不接管, 走 delta fallback。"""
    monkeypatch.setenv("META_USE_BAYES", "1")
    monkeypatch.setenv("META_BAYES_MC_SAMPLES", "500")
    monkeypatch.setenv("META_BAYES_MIN_SAMPLES", "5")
    import importlib
    import config as cfg
    importlib.reload(cfg)
    import meta_reflection_loop as mr
    importlib.reload(mr)

    kind, _, ev = mr._classify_finding(
        current={"n": 3, "win_rate": 0.0, "avg_luck": 0.0,
                 "layer": "L", "sub_layer": "S", "signal_name": "sig"},
        baseline={"n": 3, "win_rate": 1.0},
    )
    assert kind == "invalidated"
    assert ev.get("_method") == "delta_fallback"


# ═══════════════════════════════════════════════════════════════════════════
# 5. v300+ P2 — Wilson score 区间 / ci_method 切换 / _inverse_normal_cdf 修复
# ═══════════════════════════════════════════════════════════════════════════


def test_inverse_normal_cdf_tail_branch():
    """v300+ P2【回归】tail 分支 (|p-0.5| >= 0.42) 原缺 return → None。
    修复后 ppf(0.975)≈1.96, ppf(0.005)≈-2.576。
    """
    z_975 = rb._inverse_normal_cdf(0.975)
    z_005 = rb._inverse_normal_cdf(0.005)
    assert z_975 is not None, "tail 分支不能返回 None"
    assert z_005 is not None, "tail 分支不能返回 None"
    assert abs(z_975 - 1.96) < 0.01
    assert abs(z_005 - (-2.576)) < 0.01
    # 对称: ppf(p) ≈ -ppf(1-p)
    assert abs(z_975 + z_005) < 0.01


def test_inverse_normal_cdf_mid_branch():
    """中段 (|p-0.5| < 0.42) 也应正常返回。ppf(0.70)≈0.5244。"""
    z = rb._inverse_normal_cdf(0.70)
    assert z is not None
    assert abs(z - 0.5244) < 0.05


def test_wilson_score_ci_bounded_in_unit_interval():
    """Wilson score 始终 ∈ [0, 1]，包括 win=0 和 win=n 边界。"""
    for w, n in [(0, 5), (5, 5), (0, 2), (2, 2), (2, 3), (1, 1)]:
        lo, hi = rb._wilson_score_ci(w, n)
        assert 0.0 <= lo <= 1.0, f"win={w} n={n} lo={lo}"
        assert 0.0 <= hi <= 1.0, f"win={w} n={n} hi={hi}"
        assert lo <= hi


def test_wilson_score_ci_known_values():
    """Wilson score 参考值（GraphPad / R binom.confint(method='wilson'), z=1.96）。
       0/10  → [0,     0.308]
      10/10  → [0.692, 1.0]
       5/10  → ≈ [0.236, 0.764]
    """
    lo, hi = rb._wilson_score_ci(0, 10)
    assert abs(lo) < 1e-6
    assert abs(hi - 0.308) < 0.01, hi

    lo, hi = rb._wilson_score_ci(10, 10)
    assert abs(lo - 0.692) < 0.01
    assert abs(hi - 1.0) < 1e-6

    lo, hi = rb._wilson_score_ci(5, 10)
    assert 0.22 < lo < 0.25
    assert 0.75 < hi < 0.78


def test_wilson_score_ci_wider_than_wald_for_extreme_proportion():
    """win=0/n=2 时 Wald ci_low 被 max(0, ..) 裁掉; Wilson ci_low > 0
    且 ci_width 通常 ≥ Wald（语义更准，不被裁切丢失信息）。"""
    _, wilson_hi = rb._wilson_score_ci(0, 2)
    # 直接比 Wald: mean=0.5, std=0.25, ci_high=0.5+1.96*0.25=0.99 → clip to 1
    # Wilson 应给 ≤ 1（避免 Wald 把 0.99 clip 后看着像"几乎贴近 100%"的错觉）
    assert wilson_hi < 1.0
    assert wilson_hi > 0.5  # 但中心仍在 0.5


def test_wilson_score_ci_prior_influences():
    """Beta 先验 α/β 应当影响 Wilson CI（共轱性）。"""
    lo_weak, hi_weak = rb._wilson_score_ci(0, 0, prior_alpha=1.0, prior_beta=1.0)
    lo_strong, hi_strong = rb._wilson_score_ci(0, 0, prior_alpha=100.0, prior_beta=100.0)
    assert lo_weak < lo_strong
    assert hi_weak > hi_strong


def test_beta_posterior_ci_method_auto_small_uses_wilson():
    """auto + 小样本 (a+b < 30) → Wilson。"""
    p = rb.beta_posterior(0, 2)  # a+b=4
    assert p["ci_method_used"] == "wilson"
    assert p["ci_low"] >= 0.0


def test_beta_posterior_ci_method_auto_large_uses_wald():
    """auto + 大样本 (a+b >= 30) → Wald。"""
    p = rb.beta_posterior(50, 100)  # a+b=152
    assert p["ci_method_used"] == "wald"


def test_beta_posterior_ci_method_forced_wilson():
    """ci_method='wilson' 强制走 Wilson，不论 n。"""
    p = rb.beta_posterior(50, 100, ci_method="wilson")
    assert p["ci_method_used"] == "wilson"


def test_beta_posterior_ci_method_forced_wald():
    """ci_method='wald' 强制走 Wald，行为与 v240 完全一致。"""
    p = rb.beta_posterior(2, 5, ci_method="wald")
    assert p["ci_method_used"] == "wald"
    # 与 v240 实现 num 相同: mean=3/7, var=3*5/(49*8)=...
    assert abs(p["mean"] - 3/7) < 1e-9


def test_beta_posterior_ci_method_invalid_raises():
    """未知 ci_method 应 fail fast。"""
    with pytest.raises(ValueError):
        rb.beta_posterior(5, 10, ci_method="jeffreys")


def test_beta_posterior_ci_method_used_in_output():
    """返回 dict 包含 ci_method_used 字段（audit / 复盘用）。"""
    p = rb.beta_posterior(50, 100)
    assert "ci_method_used" in p
    assert p["ci_method_used"] in ("wald", "wilson")


def test_beta_posterior_wilson_wider_than_wald_for_extreme_proportion():
    """win=2/n=3: Wilson 应给出 ≥ Wald 的 CI 宽度（更保守）。"""
    p_wald = rb.beta_posterior(2, 3, ci_method="wald")
    p_wilson = rb.beta_posterior(2, 3, ci_method="wilson")
    wald_w = p_wald["ci_high"] - p_wald["ci_low"]
    wilson_w = p_wilson["ci_high"] - p_wilson["ci_low"]
    assert wilson_w >= wald_w - 1e-6, (
        f"Wilson ({wilson_w:.4f}) 应不窄于 Wald ({wald_w:.4f})"
    )


def test_bayes_compare_forwards_ci_method():
    """bayes_compare 应透传 ci_method 到两个 posterior。"""
    pc, pb, _, _, _ = rb.bayes_compare(
        curr_win=2, curr_n=3, base_win=10, base_n=20,
        ci_method="wilson",
    )
    assert pc["ci_method_used"] == "wilson"
    assert pb["ci_method_used"] == "wilson"


def test_bayes_compare_auto_per_side_mixed():
    """auto 模式允许两侧各取所需：c 小样本→wilson, b 大样本→wald。"""
    pc, pb, _, _, _ = rb.bayes_compare(
        curr_win=2, curr_n=3,    # a+b=6   → wilson
        base_win=50, base_n=100, # a+b=152 → wald
    )
    assert pc["ci_method_used"] == "wilson"
    assert pb["ci_method_used"] == "wald"


def test_bayes_compare_ci_method_orthogonal_to_mc():
    """ci_method 不应影响 Monte Carlo P(worse)/P(better)
    （它们只依赖后验 α/β，不依赖 ci_low/ci_high）。"""
    _, _, pw_wald, pb_wald, _ = rb.bayes_compare(
        curr_win=5, curr_n=10, base_win=15, base_n=30, ci_method="wald",
    )
    _, _, pw_w, pb_w, _ = rb.bayes_compare(
        curr_win=5, curr_n=10, base_win=15, base_n=30, ci_method="wilson",
    )
    assert abs(pw_wald - pw_w) < 1e-4
    assert abs(pb_wald - pb_w) < 1e-4


def test_bayes_compare_wilson_more_conservative_overlap():
    """Wilson CI 更宽 → ci_overlap 通常更大（更保守）。"""
    _, _, _, _, overlap_wald = rb.bayes_compare(
        curr_win=3, curr_n=5, base_win=7, base_n=10,
        ci_method="wald",
    )
    _, _, _, _, overlap_wilson = rb.bayes_compare(
        curr_win=3, curr_n=5, base_win=7, base_n=10,
        ci_method="wilson",
    )
    assert overlap_wilson >= overlap_wald - 1e-6, (
        f"Wilson overlap ({overlap_wilson:.4f}) 应不小于 Wald ({overlap_wald:.4f})"
    )



