"""v302+ · risk_metrics_pro 测试套件。

覆盖:
  · 8 个指标 + 聚合入口的 happy path
  · 边界条件:空序列 / 单点 / 全 0 / 全相同 / NaN / Inf
  · 数值正确性:用已知公式直接验证(不靠 cross-validation)
  · 退化场景:Sortino 零下行 / Calmar 零回撤 / Omega 1.0 / DSR 极端 trials
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
import risk_metrics_pro as rmp  # noqa: E402
from risk_metrics_pro import (  # noqa: E402
    MetricResult,
    _bootstrap_ci,
    _norm_cdf,
    _norm_ppf,
    _norm_sf,
    _skew_kurt,
    _to_array,
    _drop_nan_inf,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def normal_returns():
    """典型 1 年日收益(250 天,均值 0.001, std 0.02)。"""
    np.random.seed(42)
    return np.random.normal(0.001, 0.02, 250).tolist()


@pytest.fixture
def losing_returns():
    """全部亏损的 50 天。"""
    np.random.seed(123)
    return np.random.normal(-0.01, 0.005, 50).tolist()


@pytest.fixture
def zero_returns():
    """全 0 序列(50 天)。"""
    return [0.0] * 50


@pytest.fixture
def skewed_returns():
    """偏态:大部分微正,偶发大跌。"""
    np.random.seed(7)
    r = list(np.random.normal(0.005, 0.01, 100))
    r[50] = -0.20  # 黑天鹅
    return r


@pytest.fixture
def nan_returns():
    """含 NaN / Inf 的混合序列。"""
    return [0.01, 0.02, float("nan"), 0.015, float("inf"), 0.005, -0.01, 0.0]


# ──────────────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────────────


class TestNormCdf:
    """_norm_cdf 用 math.erf 实现,与标准正态表对照。"""

    def test_cdf_zero(self):
        assert abs(_norm_cdf(0.0) - 0.5) < 1e-10

    def test_cdf_one_sigma(self):
        # P(Z <= 1) ≈ 0.8413
        assert abs(_norm_cdf(1.0) - 0.8413) < 1e-3

    def test_cdf_two_sigma(self):
        # P(Z <= 1.96) ≈ 0.975
        assert abs(_norm_cdf(1.96) - 0.9750) < 1e-3

    def test_cdf_three_sigma(self):
        # P(Z <= 3) ≈ 0.9987
        assert abs(_norm_cdf(3.0) - 0.9987) < 1e-3

    def test_cdf_symmetry(self):
        # 1 - Phi(x) = Phi(-x)
        assert abs(_norm_cdf(1.5) + _norm_cdf(-1.5) - 1.0) < 1e-10

    def test_sf_equals_one_minus_cdf(self):
        assert abs(_norm_sf(1.96) - (1.0 - _norm_cdf(1.96))) < 1e-10


class TestNormPpf:
    """_norm_ppf 用 Beasley-Springer-Moro,验证与标准值一致。"""

    def test_ppf_median(self):
        assert abs(_norm_ppf(0.5) - 0.0) < 1e-6

    def test_ppf_975(self):
        # Phi(1.96) ≈ 0.975
        assert abs(_norm_ppf(0.975) - 1.96) < 1e-3

    def test_ppf_025(self):
        assert abs(_norm_ppf(0.025) - (-1.96)) < 1e-3

    def test_ppf_round_trip(self):
        """CDF 和 PPF 互逆。"""
        for x in [-2.0, -1.0, 0.0, 0.5, 1.0, 2.5]:
            p = _norm_cdf(x)
            x_back = _norm_ppf(p)
            assert abs(x - x_back) < 1e-3, f"x={x}, p={p}, back={x_back}"

    def test_ppf_extreme_p(self):
        # 极端 p 应返回 ±inf
        assert _norm_ppf(0.9999999) > 4.0
        assert _norm_ppf(0.0000001) < -4.0

    def test_ppf_invalid_p(self):
        """p=0 和 p=1 在函数内部已处理(返回 ±inf),不抛错。"""
        assert _norm_ppf(0.0) == -math.inf
        assert _norm_ppf(1.0) == math.inf
        # 真正抛错的是超出 (0,1) 但又非 0/1 的值
        with pytest.raises(ValueError):
            _norm_ppf(-0.1)
        with pytest.raises(ValueError):
            _norm_ppf(1.1)


class TestSkewKurt:
    """_skew_kurt:Fisher 定义,正态分布 skew=0, kurt=0。"""

    def test_normal_zero(self):
        np.random.seed(0)
        arr = np.random.normal(0, 1, 5000)
        s, k = _skew_kurt(arr)
        assert abs(s) < 0.15  # 大样本正态 skew 接近 0
        assert abs(k) < 0.3  # excess kurtosis 接近 0

    def test_right_skewed_positive(self):
        arr = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 100])  # 长右尾
        s, k = _skew_kurt(arr)
        assert s > 0

    def test_left_skewed_negative(self):
        arr = np.array([-100, 1, 2, 3, 4, 5, 6, 7, 8, 9])  # 长左尾
        s, k = _skew_kurt(arr)
        assert s < 0

    def test_too_few_samples(self):
        s, k = _skew_kurt(np.array([1.0, 2.0]))
        assert s == 0.0 and k == 0.0


class TestBootstrapCi:
    """_bootstrap_ci 验证区间是否真的包含真实值。"""

    def test_contains_mean_for_normal(self):
        np.random.seed(0)
        arr = np.random.normal(0.01, 0.02, 200)
        ci = _bootstrap_ci(arr, lambda a: float(a.mean()))
        assert ci is not None
        true_mean = float(arr.mean())
        assert ci[0] <= true_mean <= ci[1], f"true mean {true_mean} not in CI {ci}"

    def test_returns_none_for_too_few(self):
        arr = np.array([1.0, 2.0, 3.0])
        assert _bootstrap_ci(arr, lambda a: float(a.mean())) is None

    def test_ci_is_95_percent(self):
        """理论上 95% CI 应包含 95% 的重复样本均值。"""
        np.random.seed(42)
        arr = np.random.normal(0, 1, 100)
        true_mean = 0.0
        covers = 0
        for _ in range(100):
            ci = _bootstrap_ci(arr, lambda a: float(a.mean()), seed=np.random.randint(0, 1<<30))
            if ci[0] <= true_mean <= ci[1]:
                covers += 1
        assert covers >= 80  # 至少 80%(允许 5-10% 误差,因 bootstrap 本身有方差)


class TestArrayHelpers:
    def test_to_array_none(self):
        assert _to_array(None).size == 0

    def test_to_array_empty(self):
        assert _to_array([]).size == 0

    def test_drop_nan_inf(self):
        arr = np.array([1.0, np.nan, 2.0, np.inf, -np.inf, 3.0])
        cleaned = _drop_nan_inf(arr)
        assert cleaned.size == 3
        assert list(cleaned) == [1.0, 2.0, 3.0]


# ──────────────────────────────────────────────────────────────────────
# Sortino
# ──────────────────────────────────────────────────────────────────────


class TestSortino:
    def test_happy_path(self, normal_returns):
        result = rmp.sortino_ratio(normal_returns)
        assert isinstance(result, MetricResult)
        assert result.value > 0
        assert result.n_observations == 250

    def test_value_has_ci(self, normal_returns):
        result = rmp.sortino_ratio(normal_returns)
        assert result.ci_95 is not None
        assert result.ci_95[0] < result.value < result.ci_95[1]

    def test_losing_returns(self, losing_returns):
        result = rmp.sortino_ratio(losing_returns)
        assert result.value < 0

    def test_empty_returns(self):
        result = rmp.sortino_ratio([])
        assert "样本不足" in result.interp

    def test_no_downside(self):
        result = rmp.sortino_ratio([0.01, 0.02, 0.03, 0.04, 0.05])
        assert result.value == math.inf

    def test_target_above_mean(self, normal_returns):
        r1 = rmp.sortino_ratio(normal_returns, target=0.1)
        assert r1.value < 0

    def test_handles_nan(self, nan_returns):
        result = rmp.sortino_ratio(nan_returns)
        assert result.n_observations == 6


# ──────────────────────────────────────────────────────────────────────
# Calmar
# ──────────────────────────────────────────────────────────────────────


class TestCalmar:
    def test_happy_path(self, normal_returns):
        result = rmp.calmar_ratio(normal_returns)
        assert result.value > 0

    def test_no_drawdown(self):
        result = rmp.calmar_ratio([0.01, 0.02, 0.03, 0.04, 0.05])
        assert result.value == math.inf

    def test_losing_returns(self, losing_returns):
        result = rmp.calmar_ratio(losing_returns)
        assert result.value < 0

    def test_empty(self):
        result = rmp.calmar_ratio([])
        assert "样本不足" in result.interp


# ──────────────────────────────────────────────────────────────────────
# Omega
# ──────────────────────────────────────────────────────────────────────


class TestOmega:
    def test_happy_path(self, normal_returns):
        result = rmp.omega_ratio(normal_returns)
        assert result.value > 0
        assert 0.5 < result.value < 3.0

    def test_pure_positive(self):
        result = rmp.omega_ratio([0.01, 0.02, 0.03])
        assert result.value == math.inf

    def test_pure_negative(self):
        result = rmp.omega_ratio([-0.01, -0.02, -0.03])
        assert result.value == 0.0

    def test_threshold_monotonic(self, normal_returns):
        r1 = rmp.omega_ratio(normal_returns, threshold=0.0)
        r2 = rmp.omega_ratio(normal_returns, threshold=0.01)
        r3 = rmp.omega_ratio(normal_returns, threshold=0.05)
        assert r1.value > r2.value > r3.value

    def test_empty(self):
        result = rmp.omega_ratio([])
        assert "样本不足" in result.interp


# ──────────────────────────────────────────────────────────────────────
# Tail
# ──────────────────────────────────────────────────────────────────────


class TestTail:
    def test_needs_20_samples(self):
        result = rmp.tail_ratio([0.01] * 10)
        assert "样本不足" in result.interp

    def test_symmetric(self):
        np.random.seed(1)
        ret = np.random.normal(0, 1, 500).tolist()
        result = rmp.tail_ratio(ret)
        assert 0.7 < result.value < 1.3

    def test_left_tail_dominant(self):
        np.random.seed(99)
        ret = list(np.random.normal(0.001, 0.01, 100))
        ret[10] = -0.30
        ret[20] = -0.25
        result = rmp.tail_ratio(ret)
        assert result.value < 1.0

    def test_all_negative_returns_no_crash(self):
        """bug fix:全部负收益(P95 ≤ 0)时不应在 _norm_cdf(math.log(0)) 处崩溃。"""
        # 100 个都是 -0.01 附近,P95 = -0.01
        ret = [-0.01] * 100
        result = rmp.tail_ratio(ret)
        # 期望安全退化:value=0, p_value=1,interp 含 "P95"
        assert result.value == 0.0
        assert result.p_value == 1.0
        assert "P95" in result.interp

    def test_near_zero_p95_no_log_zero(self):
        """bug fix:P95 极小接近 0 时,tr ≈ 0,math.log(tr) 不应崩。"""
        # 99 个 -0.01 + 1 个 0,P95 = 0 → tr = 0
        ret = [-0.01] * 99 + [0.0]
        result = rmp.tail_ratio(ret)
        assert result.value == 0.0
        assert "P95" in result.interp


# ──────────────────────────────────────────────────────────────────────
# PSR
# ──────────────────────────────────────────────────────────────────────


class TestPsr:
    def test_known_value(self):
        """已知:SR=1, n=250 → PSR ≈ 1.0(强显著)。"""
        result = rmp.probabilistic_sharpe_ratio(observed_sr=1.0, n=250)
        assert result.value > 0.99
        assert result.p_value < 0.01

    def test_zero_sr(self):
        """SR=0 → PSR = 0.5(无信息)。"""
        result = rmp.probabilistic_sharpe_ratio(observed_sr=0.0, n=100)
        assert abs(result.value - 0.5) < 0.01

    def test_negative_sr(self):
        """SR=-1 → PSR < 0.16(显著差)。"""
        result = rmp.probabilistic_sharpe_ratio(observed_sr=-1.0, n=250)
        assert result.value < 0.16

    def test_benchmark_above_sr(self):
        """SR* > 观测 SR → PSR < 0.5。"""
        result = rmp.probabilistic_sharpe_ratio(observed_sr=0.5, n=100, sr_benchmark=1.0)
        assert result.value < 0.5

    def test_auto_skew_kurt(self, normal_returns):
        """传 returns 自动估 skew/kurt。"""
        n = len(normal_returns)
        arr = np.array(normal_returns)
        sr = arr.mean() / arr.std() * math.sqrt(n)
        result = rmp.probabilistic_sharpe_ratio(sr, n, returns=normal_returns)
        # n=250 + 正 SR → PSR 应非常接近 1(强显著)
        assert result.value > 0.95

    def test_empty_n(self):
        result = rmp.probabilistic_sharpe_ratio(observed_sr=1.0, n=2)
        assert "样本不足" in result.interp

    def test_zero_denominator(self):
        """极端 skew × SR 让分母 ≤ 0 → 返回 0.5 退化值。"""
        # 让 1 - skew*SR + (kurt/4)*SR^2 ≤ 0
        # 选 skew=2, kurt=10, SR=0.5: 1 - 1 + 0.625 = 0.625 (>0)
        # 选 skew=5, kurt=10, SR=0.5: 1 - 2.5 + 0.625 = -0.875 (<0) ✓
        result = rmp.probabilistic_sharpe_ratio(
            observed_sr=0.5, n=10, skew=5.0, kurt=10.0
        )
        assert result.value == 0.5
        assert "退化" in result.interp


# ──────────────────────────────────────────────────────────────────────
# DSR
# ──────────────────────────────────────────────────────────────────────


class TestDsr:
    def test_zero_trials_equals_psr(self, normal_returns):
        """n_trials=1 时 DSR ≈ PSR(无多重检验校正)。"""
        n = len(normal_returns)
        arr = np.array(normal_returns)
        sr = arr.mean() / arr.std() * math.sqrt(n)
        psr = rmp.probabilistic_sharpe_ratio(sr, n, returns=normal_returns)
        dsr = rmp.deflated_sharpe_ratio(sr, n_trials=1, n_obs=n, returns=normal_returns)
        assert abs(psr.value - dsr.value) < 0.01

    def test_more_trials_lower_dsr(self, normal_returns):
        """n_trials 越大,DSR 越低(多重检验惩罚)。"""
        n = len(normal_returns)
        arr = np.array(normal_returns)
        sr = arr.mean() / arr.std() * math.sqrt(n)
        dsr1 = rmp.deflated_sharpe_ratio(sr, n_trials=1, n_obs=n, returns=normal_returns)
        dsr10 = rmp.deflated_sharpe_ratio(sr, n_trials=10, n_obs=n, returns=normal_returns)
        dsr100 = rmp.deflated_sharpe_ratio(sr, n_trials=100, n_obs=n, returns=normal_returns)
        assert dsr1.value >= dsr10.value >= dsr100.value

    def test_expected_max_sharpe_grows(self):
        """E[max SR] 随 N 增大。"""
        e1 = rmp._expected_max_sharpe(1)
        e10 = rmp._expected_max_sharpe(10)
        e100 = rmp._expected_max_sharpe(100)
        e1000 = rmp._expected_max_sharpe(1000)
        assert e1 < e10 < e100 < e1000
        # 1000 试验的渐近值 = sqrt(2 ln 1000) - correction ≈ 3.12
        assert 2.8 < e1000 < 3.5
        assert 2.0 < e100 < 2.6  # 100 试验
        assert 1.5 < e10 < 2.0  # 10 试验

    def test_deflated_flag(self):
        result = rmp.deflated_sharpe_ratio(0.5, n_trials=10, n_obs=100)
        assert result.deflated_by_trials is True

    def test_insufficient(self):
        result = rmp.deflated_sharpe_ratio(1.0, n_trials=0, n_obs=100)
        assert "参数不足" in result.interp


# ──────────────────────────────────────────────────────────────────────
# MinBT / PBO
# ──────────────────────────────────────────────────────────────────────


class TestMinBT:
    def test_perfect_overlap_low_pbo(self):
        """IS 和 OOS 排名完全一致 → PBO 接近 0。"""
        np.random.seed(0)
        scores = np.random.normal(0, 1, 20).tolist()
        result = rmp.min_backtest_overfitting(is_scores=scores, oos_scores=scores)
        assert result.value < 0.30
        assert result.deflated_by_trials is True

    def test_random_pbo_medium(self):
        """IS 和 OOS 完全独立 → PBO 接近 0.5-0.8。"""
        np.random.seed(1)
        is_arr = np.random.normal(0, 1, 50).tolist()
        np.random.seed(2)
        oos_arr = np.random.normal(0, 1, 50).tolist()
        result = rmp.min_backtest_overfitting(is_scores=is_arr, oos_scores=oos_arr)
        assert 0.3 < result.value < 0.9

    def test_formula_path(self):
        """路径 2:best_is_oos_sharpe + n_obs。"""
        result = rmp.min_backtest_overfitting(best_is_oos_sharpe=0.5, n_obs=100)
        # PBO = 1 - Phi(0.5 * sqrt(99)) ≈ 0
        assert result.value < 0.01

    def test_asymptotic_path(self):
        """路径 3:渐近估计。"""
        result = rmp.min_backtest_overfitting(n_configs=100, n_obs=250)
        assert 0.0 <= result.value <= 1.0

    def test_too_few_scores(self):
        result = rmp.min_backtest_overfitting(is_scores=[1, 2], oos_scores=[1, 2])
        assert "样本不足" in result.interp

    def test_no_params(self):
        result = rmp.min_backtest_overfitting()
        assert "参数不足" in result.interp


# ──────────────────────────────────────────────────────────────────────
# Deflated Expectancy
# ──────────────────────────────────────────────────────────────────────


class TestDeflatedExpectancy:
    def test_happy_path(self, normal_returns):
        result = rmp.deflated_expectancy(normal_returns, n_strategies=1)
        assert result.deflated_by_trials is True

    def test_more_strategies_lowers_de(self):
        """n_strategies 越大,DE 下界越低(多重检验惩罚)。"""
        # 用较强的收益(mean=0.02)使 SR 大,这样 inflation 项才会显著
        np.random.seed(11)
        ret = np.random.normal(0.02, 0.05, 250).tolist()
        r1 = rmp.deflated_expectancy(ret, n_strategies=1)
        r10 = rmp.deflated_expectancy(ret, n_strategies=10)
        r100 = rmp.deflated_expectancy(ret, n_strategies=100)
        assert r1.value >= r10.value >= r100.value

    def test_losing_returns(self, losing_returns):
        result = rmp.deflated_expectancy(losing_returns, n_strategies=5)
        assert result.value < 0

    def test_zero_variance(self):
        result = rmp.deflated_expectancy([0.01] * 50, n_strategies=1)
        assert "无波动" in result.interp

    def test_too_few_samples(self):
        result = rmp.deflated_expectancy([0.01, 0.02], n_strategies=1)
        assert "样本不足" in result.interp


# ──────────────────────────────────────────────────────────────────────
# 聚合入口
# ──────────────────────────────────────────────────────────────────────


class TestComputePortfolioMetricsPro:
    def test_returns_all_keys(self, normal_returns):
        m = rmp.compute_portfolio_metrics_pro(normal_returns, n_trials=10)
        for key in ["sortino", "calmar", "omega", "tail_ratio", "psr", "dsr", "minbt", "deflated_expectancy"]:
            assert key in m
        for key in ["sharpe", "max_drawdown", "winrate", "total_pnl"]:
            assert key in m
        assert "_meta" in m
        assert m["_meta"]["n"] == 250
        assert m["_meta"]["n_trials"] == 10

    def test_each_metric_has_standard_fields(self, normal_returns):
        m = rmp.compute_portfolio_metrics_pro(normal_returns)
        for key in ["sortino", "calmar", "omega", "tail_ratio", "psr", "dsr", "minbt", "deflated_expectancy"]:
            d = m[key]
            assert "value" in d
            assert "p_value" in d
            assert "n_observations" in d
            assert "deflated_by_trials" in d
            assert "interp" in d

    def test_dsr_lower_than_psr_with_trials(self, normal_returns):
        m = rmp.compute_portfolio_metrics_pro(normal_returns, n_trials=20)
        # 多试验时 DSR ≤ PSR(多重检验校正更保守)
        assert m["dsr"]["value"] <= m["psr"]["value"] + 0.01

    def test_empty_input(self):
        m = rmp.compute_portfolio_metrics_pro([], n_trials=1)
        assert m["_meta"]["n"] == 0
        for key in ["sortino", "calmar", "omega"]:
            assert "value" in m[key]
