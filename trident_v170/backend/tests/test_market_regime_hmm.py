"""market_regime_hmm 单元测试。"""
import math

import numpy as np
import pytest

import market_regime_hmm as hmm


def test_logsumexp_1d():
    a = np.array([0.0, 0.0, 0.0])
    val = hmm._logsumexp(a)
    assert abs(val - math.log(3.0)) < 1e-6


def test_logsumexp_with_inf():
    a = np.array([-1e30, -1e30, 0.0])
    val = hmm._logsumexp(a)
    assert abs(val) < 1e-6


def test_gaussian_logpdf():
    x = np.array([0.0, 1.0, -1.0])
    lp = hmm._gaussian_logpdf(x, mu=0.0, sigma=1.0)
    expected = -0.5 * math.log(2 * math.pi) - 0.5 * np.array([0.0, 1.0, 1.0])
    assert np.allclose(lp, expected, atol=1e-6)


def test_to_array():
    arr = hmm._to_array([0.1, 0.2, 0.3])
    assert arr.dtype == np.float64
    assert arr.shape == (3,)


def test_drop_nan_inf():
    arr = np.array([0.1, np.nan, 0.2, np.inf, -np.inf, 0.3])
    clean = hmm._drop_nan_inf(arr)
    assert clean.size == 3


# ─────────────────────────────────────────────
# StickyHMM
# ─────────────────────────────────────────────


def test_hmm_init_and_fit_synthetic():
    """长 + 持久 regime 数据应能学到 sticky 转移。"""
    np.random.seed(0)
    # 每个 regime 持续 500 天,共 1500 天
    r = np.concatenate([
        np.random.normal(0.005, 0.005, 500),
        np.random.normal(0.000, 0.010, 500),
        np.random.normal(-0.005, 0.015, 500),
    ])
    m = hmm.StickyHMM(n_states=3, seed=0, sticky_alpha=20.0)
    p = m.fit(r, n_iter=20)
    assert p.mu is not None
    assert p.mu[0] < p.mu[1] < p.mu[2]
    assert np.all(p.sigma > 0)
    assert np.allclose(p.A.sum(axis=1), 1.0, atol=1e-6)
    # sticky 性质:边界状态(bear/bull)对角线应 > 最大非对角(中间 range 可能是过渡态)
    for k in (0, 2):
        diag = p.A[k, k]
        max_off = max(p.A[k, j] for j in range(3) if j != k)
        assert diag > max_off, f"state {k}: diag={diag} not > max_off={max_off}"


def test_hmm_viterbi_path_length():
    np.random.seed(1)
    r = np.random.normal(0, 0.01, 100)
    m = hmm.StickyHMM(n_states=3, seed=0)
    m.fit(r, n_iter=5)
    path = m.viterbi(r)
    assert path.shape == (100,)


def test_hmm_predict_proba_sums_to_one():
    np.random.seed(2)
    r = np.random.normal(0, 0.01, 50)
    m = hmm.StickyHMM(n_states=3, seed=0)
    m.fit(r, n_iter=5)
    post = m.predict_proba(r)
    assert post.shape == (50, 3)
    assert np.allclose(post.sum(axis=1), 1.0, atol=1e-6)


def test_hmm_short_input_safe():
    m = hmm.StickyHMM(n_states=3)
    r = np.array([0.01, 0.02, 0.015])
    p = m.fit(r)
    path = m.viterbi(np.array([0.01, 0.02]))
    assert path.size == 2


def test_hmm_k_must_be_at_least_2():
    with pytest.raises(ValueError):
        hmm.StickyHMM(n_states=1)


# ─────────────────────────────────────────────
# BOCPD
# ─────────────────────────────────────────────


def test_bocpd_first_step():
    state = hmm.BOCPDState(run_length_probs=np.array([1.0]), t=0)
    new = hmm.bocpd_update(0.01, state, hazard=0.05)
    assert new.t == 1
    assert new.run_length_probs.shape == (2,)
    assert abs(new.run_length_probs.sum() - 1.0) < 1e-6


def test_bocpd_change_point_detected():
    np.random.seed(0)
    pre = np.random.normal(0.01, 0.005, 30)
    post = np.random.normal(-0.01, 0.005, 30)
    series = np.concatenate([pre, post])
    state = hmm.BOCPDState(run_length_probs=np.array([1.0]), t=0)
    scores = []
    for v in series:
        state = hmm.bocpd_update(float(v), state, hazard=1.0 / 30.0,
                                  mu0=0.0, kappa0=1.0, alpha0=1.0, beta0=0.0001)
        scores.append(state.change_score)
    # BOCPD 第一步是初始化高点,跳过
    max_idx = int(np.argmax(scores[3:])) + 3
    assert 25 <= max_idx <= 40, f"max change at t={max_idx}, expected ~30"


def test_bocpd_no_change_in_stable_series():
    np.random.seed(0)
    r = np.random.normal(0, 0.01, 100)
    state = hmm.BOCPDState(run_length_probs=np.array([1.0]), t=0)
    scores = []
    for v in r:
        state = hmm.bocpd_update(float(v), state, hazard=1.0 / 50.0,
                                  mu0=0.0, kappa0=1.0, alpha0=1.0, beta0=0.0001)
        scores.append(state.change_score)
    assert max(scores) < 0.5


# ─────────────────────────────────────────────
# 主入口 classify_regime_hmm
# ─────────────────────────────────────────────


def test_classify_regime_hmm_basic():
    np.random.seed(0)
    r = np.concatenate([
        np.random.normal(0.003, 0.010, 100),
        np.random.normal(0.000, 0.012, 50),
        np.random.normal(-0.004, 0.015, 100),
    ])
    s = hmm.classify_regime_hmm(r, n_iter=10)
    assert s.regime in ("bull", "range", "bear")
    assert 0.0 <= s.confidence <= 1.0
    assert s.n_obs == 250
    assert "bull" in s.state_probs
    assert set(s.factor_weights.keys()) == {"trend", "momentum", "mean_reversion", "volatility", "size"}


def test_classify_regime_short_returns_neutral():
    s = hmm.classify_regime_hmm(np.array([0.01, 0.02]))
    assert s.regime == "range"
    assert s.confidence == 0.0
    assert s.days_in_regime == 0
    assert s.n_obs == 2


def test_classify_regime_with_nan():
    r = np.array([0.01, 0.02, np.nan, 0.015, -0.01] * 30)
    s = hmm.classify_regime_hmm(r, n_iter=5)
    # 150 - 30 个 nan = 120 有效
    assert s.n_obs == 120
    assert s.regime in ("bull", "range", "bear")


def test_classify_regime_viterbi_tail():
    np.random.seed(7)
    r = np.random.normal(0.001, 0.01, 80)
    s = hmm.classify_regime_hmm(r, n_iter=8, tail=5)
    assert len(s.viterbi_path) == 5
    for name in s.viterbi_path:
        assert name in ("bull", "range", "bear")


def test_classify_regime_factor_weights_by_regime():
    np.random.seed(0)
    r_bull = np.random.normal(0.005, 0.005, 200)
    r_bear = np.random.normal(-0.005, 0.015, 200)
    s_bull = hmm.classify_regime_hmm(r_bull, n_iter=10)
    s_bear = hmm.classify_regime_hmm(r_bear, n_iter=10)
    if s_bull.regime == "bull" and s_bear.regime == "bear":
        assert s_bull.factor_weights["trend"] > s_bear.factor_weights["trend"]
        assert s_bear.factor_weights["mean_reversion"] > s_bull.factor_weights["mean_reversion"]


def test_classify_regime_to_dict():
    np.random.seed(0)
    r = np.random.normal(0, 0.01, 60)
    s = hmm.classify_regime_hmm(r, n_iter=5)
    d = hmm.regime_state_to_dict(s)
    assert isinstance(d, dict)
    assert "regime" in d
    assert "state_probs" in d
    assert "factor_weights" in d


def test_classify_regime_handles_constant_returns():
    s = hmm.classify_regime_hmm(np.zeros(50), n_iter=5)
    assert s.regime in ("bull", "range", "bear")
    assert s.confidence <= 0.6
