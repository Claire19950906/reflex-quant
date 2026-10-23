"""v230p1 — 市场 regime 时序识别(Sticky HMM + BOCPD)。

本模块是 market_regime.py(单时点资金/价格背离)之上的**时序层**:
  - 用历史收益序列识别"现在到底在哪种 regime"
  - 3 状态(默认): bull / bear / range
  - sticky HMM + BOCPD,自下而上不依赖 hmmlearn/scipy
  - 输出: 当前 regime + 概率分布 + 距上次切换天数 + 给 decision_guard 的因子权重建议

设计原则:
  1. 纯 numpy + math(与 risk_metrics_pro 一致,不污染环境)
  2. 输入容错: 序列太短 / 含 nan / 全零方差 都安全退化
  3. 与现有 market_regime.detect_price_fund_divergence 解耦,可单独使用
  4. 输出结构化 RegimeState dataclass,便于 JSON 化 / 喂 LLM

参考文献:
  - Baum & Petrie 1966 HMM
  - Fox et al. 2011 sticky HDP-HMM
  - Adams & MacKay 2007 BOCPD
  - Hamilton 1989 regime switching
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


# ─────────────────────────────────────────────────────────────────────
# 1. 工具函数
# ─────────────────────────────────────────────────────────────────────


def _to_array(returns):
    arr = np.asarray(list(returns), dtype=np.float64) if not isinstance(returns, np.ndarray) else returns.astype(np.float64)
    return arr


def _drop_nan_inf(arr):
    return arr[np.isfinite(arr)]


def _gaussian_logpdf(x, mu, sigma):
    if sigma <= 1e-12:
        return np.where(np.isclose(x, mu), 0.0, -np.inf)
    z = (x - mu) / sigma
    return -0.5 * z * z - math.log(sigma) - 0.5 * math.log(2 * math.pi)


def _logsumexp(a, axis=None):
    """log-sum-exp。对小数组(K=3)用纯 Python 更快。"""
    a = np.asarray(a)
    if a.ndim == 1 and a.size <= 8:
        # fast path for small 1D
        m = float(a.max())
        if not math.isfinite(m):
            return float('-inf')
        s = 0.0
        for v in a:
            s += math.exp(float(v) - m)
        return m + math.log(s)
    m = np.max(a, axis=axis, keepdims=True)
    m = np.where(np.isfinite(m), m, 0.0)
    # 防止 exp 溢出:clip 到 [-50, 50](e^-50 ~ 2e-22,足够小)
    d = np.clip(a - m, -50.0, 50.0)
    out = m + np.log(np.sum(np.exp(d), axis=axis, keepdims=True))
    if axis is None:
        return float(np.squeeze(out))
    return np.squeeze(out, axis=axis)


# ─────────────────────────────────────────────────────────────────────
# 2. Sticky HMM(高斯发射)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class StickyHMMParams:
    n_states: int = 3
    pi = None
    A = None
    mu = None
    sigma = None
    sticky_alpha: float = 5.0
    state_names: Tuple[str, ...] = ()


class StickyHMM:
    """3 状态 sticky 高斯 HMM。"""

    def __init__(self, n_states=3, sticky_alpha=5.0, seed=0):
        self.K = int(n_states)
        if self.K < 2:
            raise ValueError("n_states must be >= 2")
        self.sticky_alpha = float(sticky_alpha)
        self.rng = np.random.default_rng(seed)
        self.params = StickyHMMParams(n_states=self.K, sticky_alpha=sticky_alpha)

    def _init_params(self, x):
        K = self.K
        qs = np.quantile(x, np.linspace(0.1, 0.9, K))
        order = np.argsort(qs)
        self.params.mu = qs[order]
        self.params.sigma = np.full(K, max(np.std(x) * 0.5, 1e-4))
        off = 1.0
        diag = 1.0 + self.sticky_alpha
        A = np.full((K, K), off / (K - 1))
        np.fill_diagonal(A, diag)
        A /= A.sum(axis=1, keepdims=True)
        self.params.A = A
        self.params.pi = np.full(K, 1.0 / K)

    def _compute_gamma_alpha(self, x):
        """E-step forward。返回 (log_alpha, log_B, log_likelihood)。"""
        K, T = self.K, x.size
        log_pi = np.log(self.params.pi + 1e-300)
        log_A = np.log(self.params.A + 1e-300)
        log_B = np.stack([_gaussian_logpdf(x, self.params.mu[k], self.params.sigma[k]) for k in range(K)], axis=0)
        log_alpha = np.empty((K, T))
        # fast init t=0
        log_alpha[:, 0] = log_pi + log_B[:, 0]
        # K=3 标量特化: 展开 3x3
        if K == 3:
            la0 = log_alpha[0, 0]
            la1 = log_alpha[1, 0]
            la2 = log_alpha[2, 0]
            lb0 = log_B[0]
            lb1 = log_B[1]
            lb2 = log_B[2]
            laA00 = log_A[0, 0]; laA01 = log_A[0, 1]; laA02 = log_A[0, 2]
            laA10 = log_A[1, 0]; laA11 = log_A[1, 1]; laA12 = log_A[1, 2]
            laA20 = log_A[2, 0]; laA21 = log_A[2, 1]; laA22 = log_A[2, 2]
            for t in range(1, T):
                # 0,1,2 三个新 log_alpha
                a0 = (la0 + laA00, la1 + laA10, la2 + laA20)
                m0 = max(a0)
                a0s = math.exp(a0[0] - m0) + math.exp(a0[1] - m0) + math.exp(a0[2] - m0)
                new0 = m0 + math.log(a0s) + lb0[t]
                a1 = (la0 + laA01, la1 + laA11, la2 + laA21)
                m1 = max(a1)
                a1s = math.exp(a1[0] - m1) + math.exp(a1[1] - m1) + math.exp(a1[2] - m1)
                new1 = m1 + math.log(a1s) + lb1[t]
                a2 = (la0 + laA02, la1 + laA12, la2 + laA22)
                m2 = max(a2)
                a2s = math.exp(a2[0] - m2) + math.exp(a2[1] - m2) + math.exp(a2[2] - m2)
                new2 = m2 + math.log(a2s) + lb2[t]
                la0, la1, la2 = new0, new1, new2
                log_alpha[0, t] = la0
                log_alpha[1, t] = la1
                log_alpha[2, t] = la2
            ll_vec = (la0, la1, la2)
            m = max(ll_vec)
            ll = m + math.log(math.exp(la0 - m) + math.exp(la1 - m) + math.exp(la2 - m))
            return log_alpha, log_B, ll
        # fallback for K != 3
        for t in range(1, T):
            for k in range(K):
                log_alpha[k, t] = _logsumexp(log_alpha[:, t - 1] + log_A[:, k]) + log_B[k, t]
        ll = _logsumexp(log_alpha[:, -1])
        return log_alpha, log_B, ll

    def _compute_xi_gamma(self, x, log_alpha, log_B):
        """E-step:计算 xi (转移后验) 和 gamma (状态后验)。"""
        K, T = self.K, x.size
        log_A = np.log(self.params.A + 1e-300)
        log_beta = np.zeros((K, T))
        if K == 3:
            laA00 = log_A[0, 0]; laA01 = log_A[0, 1]; laA02 = log_A[0, 2]
            laA10 = log_A[1, 0]; laA11 = log_A[1, 1]; laA12 = log_A[1, 2]
            laA20 = log_A[2, 0]; laA21 = log_A[2, 1]; laA22 = log_A[2, 2]
            lb0 = log_B[0]; lb1 = log_B[1]; lb2 = log_B[2]
            # backward (vectorized per t)
            lb0_t = 0.0; lb1_t = 0.0; lb2_t = 0.0
            for t in range(T - 2, -1, -1):
                # beta_k = logsumexp(A[k,:] + B[:,t+1] + beta[:])
                v0 = (laA00 + lb1 + lb0_t, laA00 + lb2 + lb0_t)  # wait wrong
                # 正确: beta[k, t] = logsumexp( A[k,j] + B[j, t+1] + beta[j, t+1] for j in 0..K-1 )
                a0 = (laA00 + lb1[t + 1] + lb0_t, laA00 + lb2[t + 1] + lb0_t)  # placeholder
                # K=3: 重写
                v00 = laA00 + lb0[t + 1] + lb0_t
                v01 = laA01 + lb1[t + 1] + lb1_t
                v02 = laA02 + lb2[t + 1] + lb2_t
                m0 = max(v00, v01, v02)
                new_lb0 = m0 + math.log(math.exp(v00 - m0) + math.exp(v01 - m0) + math.exp(v02 - m0))
                v10 = laA10 + lb0[t + 1] + lb0_t
                v11 = laA11 + lb1[t + 1] + lb1_t
                v12 = laA12 + lb2[t + 1] + lb2_t
                m1 = max(v10, v11, v12)
                new_lb1 = m1 + math.log(math.exp(v10 - m1) + math.exp(v11 - m1) + math.exp(v12 - m1))
                v20 = laA20 + lb0[t + 1] + lb0_t
                v21 = laA21 + lb1[t + 1] + lb1_t
                v22 = laA22 + lb2[t + 1] + lb2_t
                m2 = max(v20, v21, v22)
                new_lb2 = m2 + math.log(math.exp(v20 - m2) + math.exp(v21 - m2) + math.exp(v22 - m2))
                lb0_t, lb1_t, lb2_t = new_lb0, new_lb1, new_lb2
                log_beta[0, t] = lb0_t
                log_beta[1, t] = lb1_t
                log_beta[2, t] = lb2_t
            log_beta[0, T - 1] = 0.0
            log_beta[1, T - 1] = 0.0
            log_beta[2, T - 1] = 0.0
        else:
            for t in range(T - 2, -1, -1):
                for k in range(K):
                    log_beta[k, t] = _logsumexp(log_A[k, :] + log_B[:, t + 1] + log_beta[:, t + 1])
        log_gamma = log_alpha + log_beta
        log_gamma = log_gamma - _logsumexp(log_gamma, axis=0)
        gamma = np.exp(log_gamma)
        # xi: 用 log-space 一次算 (K=3) or numpy (other)
        if K == 3:
            xi = np.zeros((3, 3, T - 1))
            for t in range(T - 1):
                ll_t = _logsumexp(log_alpha[:, -1])
                for i in range(3):
                    for j in range(3):
                        val = log_alpha[i, t] + log_A[i, j] + log_B[j, t + 1] + log_beta[j, t + 1] - ll_t
                        xi[i, j, t] = math.exp(val)
            return xi, gamma
        xi = np.zeros((K, K, T - 1))
        for t in range(T - 1):
            for i in range(K):
                for j in range(K):
                    xi[i, j, t] = math.exp(
                        log_alpha[i, t] + log_A[i, j] + log_B[j, t + 1] + log_beta[j, t + 1] - _logsumexp(log_alpha[:, -1])
                    )
        return xi, gamma

    def _m_step(self, x, xi, gamma):
        """M-step:更新 A, mu, sigma, pi。"""
        K = self.K
        A_new = xi.sum(axis=2)
        A_new /= A_new.sum(axis=1, keepdims=True) + 1e-300
        self.params.A = A_new
        weights = gamma + 1e-12
        mu_new = (weights * x).sum(axis=1) / weights.sum(axis=1)
        sigma_new = np.sqrt((weights * (x - mu_new[:, None]) ** 2).sum(axis=1) / weights.sum(axis=1))
        sigma_new = np.maximum(sigma_new, 1e-6)
        self.params.mu = mu_new
        self.params.sigma = sigma_new
        self.params.pi = gamma[:, 0] / gamma[:, 0].sum()

    def fit(self, returns, n_iter=20, tol=1e-4):
        """Baum-Welch 训练。"""
        x = _drop_nan_inf(_to_array(returns))
        if x.size < 5:
            return self.params
        if self.params.mu is None:
            self._init_params(x)
        prev_ll = -np.inf
        for it in range(n_iter):
            log_alpha, log_B, ll = self._compute_gamma_alpha(x)
            if not np.isfinite(ll):
                break
            xi, gamma = self._compute_xi_gamma(x, log_alpha, log_B)
            self._m_step(x, xi, gamma)
            if abs(ll - prev_ll) < tol:
                break
            prev_ll = ll
        order = np.argsort(self.params.mu)
        self._reorder_states(order)
        return self.params

    def _reorder_states(self, order):
        p = self.params
        p.mu = p.mu[order]
        p.sigma = p.sigma[order]
        p.A = p.A[np.ix_(order, order)]
        p.pi = p.pi[order]
        default_names = ["bear", "range", "bull"]
        if self.K >= 3:
            p.state_names = tuple(default_names[: self.K])
        else:
            p.state_names = tuple(default_names[1:self.K + 1])

    def viterbi(self, returns):
        """最可能状态序列。"""
        x = _drop_nan_inf(_to_array(returns))
        if x.size == 0 or self.params.mu is None:
            return np.zeros(max(x.size, 0), dtype=int)
        K, T = self.K, x.size
        log_pi = np.log(self.params.pi + 1e-300)
        log_A = np.log(self.params.A + 1e-300)
        log_B = np.stack([_gaussian_logpdf(x, self.params.mu[k], self.params.sigma[k]) for k in range(K)], axis=0)
        V = np.full((K, T), -np.inf)
        ptr = np.zeros((K, T), dtype=int)
        V[:, 0] = log_pi + log_B[:, 0]
        for t in range(1, T):
            for k in range(K):
                scores = V[:, t - 1] + log_A[:, k]
                ptr[k, t] = int(np.argmax(scores))
                V[k, t] = scores[ptr[k, t]] + log_B[k, t]
        path = np.zeros(T, dtype=int)
        path[-1] = int(np.argmax(V[:, -1]))
        for t in range(T - 2, -1, -1):
            path[t] = ptr[path[t + 1], t + 1]
        return path

    def predict_proba(self, returns):
        """后验 P(state_t | x_1:T),返回 (T, K) 数组。"""
        x = _drop_nan_inf(_to_array(returns))
        T = x.size
        if T == 0 or self.params.mu is None:
            return np.zeros((0, self.K))
        log_alpha, log_B, _ = self._compute_gamma_alpha(x)
        K = self.K
        log_beta = np.zeros((K, T))
        log_A = np.log(self.params.A + 1e-300)
        for t in range(T - 2, -1, -1):
            for k in range(K):
                log_beta[k, t] = _logsumexp(log_A[k, :] + log_B[:, t + 1] + log_beta[:, t + 1])
        log_gamma = log_alpha + log_beta
        log_gamma = log_gamma - _logsumexp(log_gamma, axis=0)
        return np.exp(log_gamma.T)


# ─────────────────────────────────────────────────────────────────────
# 3. BOCPD(Bayesian Online Changepoint Detection)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class BOCPDState:
    """BOCPD 状态。"""
    run_length_probs: Any = None
    last_change_t: int = 0
    t: int = 0
    change_score: float = 0.0


def bocpd_update(
    x_t,
    state,
    *,
    hazard=1.0 / 50.0,
    mu0=0.0,
    kappa0=1.0,
    alpha0=1.0,
    beta0=1.0,
    max_run_length=500,
):
    """BOCPD 单步更新(NIG 先验,正态近似预测)。"""
    t = state.t
    prev = state.run_length_probs if state.run_length_probs is not None else np.array([1.0])
    if not hasattr(state, 'sum_x_buf') or state.sum_x_buf is None:
        state.sum_x_buf = np.zeros(max_run_length + 1)
        state.sum_x2_buf = np.zeros(max_run_length + 1)
        state.n_buf = np.zeros(max_run_length + 1, dtype=int)
    sum_x = state.sum_x_buf[: prev.size + 1]
    sum_x2 = state.sum_x2_buf[: prev.size + 1]
    n_buf = state.n_buf[: prev.size + 1]
    if prev.size > max_run_length:
        prev = prev[:max_run_length]
        prev = prev / prev.sum()
    K = prev.size
    # K=3 fast path (Python scalars)
    if K == 3:
        lp0 = math.log(max(float(prev[0]), 1e-300))
        lp1 = math.log(max(float(prev[1]), 1e-300))
        lp2 = math.log(max(float(prev[2]), 1e-300))
        log_pred = [0.0, 0.0, 0.0, 0.0]  # K+1 = 4
        for k in range(4):
            n = int(n_buf[k])
            if n == 0:
                mu_n = mu0
                var = (beta0 / alpha0) * (1.0 + 1.0 / kappa0)
            else:
                mu_n = float(sum_x[k]) / n
                sse = float(sum_x2[k]) - n * mu_n * mu_n
                if sse < 0:
                    sse = 0.0
                a_n = alpha0 + n / 2.0
                b_n = beta0 + sse / 2.0 + (kappa0 * n * (mu_n - mu0) ** 2) / (2.0 * (kappa0 + n))
                k_n = kappa0 + n
                var = b_n * (k_n + 1) / (a_n * k_n)
                if var < 1e-6:
                    var = 1e-6
            if var <= 1e-12:
                log_pred[k] = 0.0 if abs(x_t - mu_n) < 1e-9 else -1e30
            else:
                z = (x_t - mu_n) / math.sqrt(var)
                log_pred[k] = -0.5 * z * z - 0.5 * math.log(2 * math.pi * var)
        # 归一 (K=3)
        v = (
            lp0 + log_pred[0],
            lp1 + log_pred[1],
            lp2 + log_pred[2],
        )
        m = max(v[0], v[1], v[2])
        s = math.exp(v[0] - m) + math.exp(v[1] - m) + math.exp(v[2] - m)
        lnz = m + math.log(s)
        post0 = v[0] - lnz
        post1 = v[1] - lnz
        post2 = v[2] - lnz
        # growth + change
        log_h = math.log(max(1 - hazard, 1e-12))
        log_hc = math.log(max(hazard, 1e-12))
        # change: logsumexp(post_i + log_hc)
        m_c = max(post0 + log_hc, post1 + log_hc, post2 + log_hc)
        s_c = (math.exp(post0 + log_hc - m_c) +
               math.exp(post1 + log_hc - m_c) +
               math.exp(post2 + log_hc - m_c))
        log_change = m_c + math.log(s_c)
        g0 = post0 + log_h
        g1 = post1 + log_h
        g2 = post2 + log_h
        all_g = (log_change, g0, g1, g2)
        m_g = max(all_g)
        s_g = sum(math.exp(x - m_g) for x in all_g)
        new_p0 = math.exp(log_change - m_g) / s_g
        new_p1 = math.exp(g0 - m_g) / s_g
        new_p2 = math.exp(g1 - m_g) / s_g
        new_p3 = math.exp(g2 - m_g) / s_g
        new_probs = np.array([new_p0, new_p1, new_p2, new_p3])
        change_score = float(new_p0)
        # 更新累计 sum_x, sum_x2
        new_sum_x = np.zeros(5)
        new_sum_x2 = np.zeros(5)
        new_n = np.zeros(5, dtype=int)
        new_sum_x[1] = sum_x[0] + x_t
        new_sum_x2[1] = sum_x2[0] + x_t * x_t
        new_n[1] = int(n_buf[0]) + 1
        new_sum_x[2] = sum_x[1] + x_t
        new_sum_x2[2] = sum_x2[1] + x_t * x_t
        new_n[2] = int(n_buf[1]) + 1
        new_sum_x[3] = sum_x[2] + x_t
        new_sum_x2[3] = sum_x2[2] + x_t * x_t
        new_n[3] = int(n_buf[2]) + 1
        new_sum_x[0] = x_t
        new_sum_x2[0] = x_t * x_t
        new_n[0] = 1
        new_state = BOCPDState(run_length_probs=new_probs, t=t + 1, change_score=change_score)
        new_state.sum_x_buf = new_sum_x
        new_state.sum_x2_buf = new_sum_x2
        new_state.n_buf = new_n
        return new_state
    # fallback for K != 3
    log_prev = np.log(prev + 1e-300)
    log_pred = np.empty(K + 1)
    for k in range(K + 1):
        n = n_buf[k]
        if n == 0:
            mu_n = mu0
            var = (beta0 / alpha0) * (1 + 1.0 / kappa0)
        else:
            mu_n = sum_x[k] / n
            sse = sum_x2[k] - n * mu_n * mu_n
            sse = max(sse, 0.0)
            a_n = alpha0 + n / 2.0
            b_n = beta0 + sse / 2.0 + (kappa0 * n * (mu_n - mu0) ** 2) / (2.0 * (kappa0 + n))
            k_n = kappa0 + n
            var = b_n * (k_n + 1) / (a_n * k_n)
            var = max(var, 1e-6)
        if var <= 1e-12:
            log_pred[k] = 0.0 if abs(x_t - mu_n) < 1e-9 else -np.inf
        else:
            z = (x_t - mu_n) / math.sqrt(var)
            log_pred[k] = -0.5 * z * z - 0.5 * math.log(2 * math.pi * var)
    log_joint = log_prev + log_pred[:K]
    log_norm = _logsumexp(log_joint)
    # log_norm=-inf 时 log_post 会产生 NaN,用 -inf 兜底
    if not math.isfinite(log_norm):
        log_post = np.full(K, -math.inf)
    else:
        log_post = log_joint - log_norm
    log_growth = np.empty(K + 1)
    log_growth[:K] = log_post + math.log(max(1 - hazard, 1e-12))
    log_change = _logsumexp(log_post + math.log(hazard))
    log_growth[0] = log_change
    new_probs = np.exp(log_growth - _logsumexp(log_growth))
    change_score = float(new_probs[0])
    new_sum_x = np.zeros(K + 2)
    new_sum_x2 = np.zeros(K + 2)
    new_n = np.zeros(K + 2, dtype=int)
    new_sum_x[1:K + 1] = sum_x[:K] + x_t
    new_sum_x2[1:K + 1] = sum_x2[:K] + x_t * x_t
    new_n[1:K + 1] = n_buf[:K] + 1
    new_sum_x[0] = x_t
    new_sum_x2[0] = x_t * x_t
    new_n[0] = 1
    new_state = BOCPDState(
        run_length_probs=new_probs,
        t=t + 1,
        change_score=change_score,
    )
    new_state.sum_x_buf = new_sum_x
    new_state.sum_x2_buf = new_sum_x2
    new_state.n_buf = new_n
    return new_state


# ─────────────────────────────────────────────────────────────────────
# 4. 整合:regime 分类 + 因子权重
# ─────────────────────────────────────────────────────────────────────


REGIME_FACTOR_WEIGHTS = {
    "bull":   {"trend": 1.30, "momentum": 1.20, "mean_reversion": 0.70, "volatility": 0.90, "size": 1.00},
    "range":  {"trend": 0.85, "momentum": 0.90, "mean_reversion": 1.20, "volatility": 1.00, "size": 1.00},
    "bear":   {"trend": 0.60, "momentum": 0.70, "mean_reversion": 1.30, "volatility": 1.20, "size": 0.85},
}


@dataclass
class RegimeState:
    regime: str
    confidence: float
    state_probs: Dict[str, float]
    days_in_regime: int
    change_score: float
    factor_weights: Dict[str, float]
    viterbi_path: List[str]
    interp: str
    hmm_log_likelihood: float
    n_obs: int
    hmm_state_names: List[str]


def classify_regime_hmm(
    returns,
    *,
    n_states=3,
    n_iter=20,
    hazard=1.0 / 50.0,
    seed=0,
    tail=10,
):
    """主入口:用 sticky HMM + BOCPD 识别当前 regime。

    参数:
      returns    日收益序列
      n_states   HMM 状态数
      n_iter     Baum-Welch 迭代数
      hazard     BOCPD hazard rate(1/期望 run length)
      seed       随机种子
      tail       返回最近 N 天的 Viterbi 路径

    返回: RegimeState dataclass
    """
    x = _drop_nan_inf(_to_array(returns))
    T = int(x.size)
    if T < 5:
        return RegimeState(
            regime="range", confidence=0.0,
            state_probs={"bull": 0.33, "range": 0.34, "bear": 0.33},
            days_in_regime=0, change_score=0.0,
            factor_weights=dict(REGIME_FACTOR_WEIGHTS["range"]),
            viterbi_path=["range"] * max(tail, 0),
            interp="样本不足,返回 neutral",
            hmm_log_likelihood=0.0, n_obs=T,
            hmm_state_names=["bear", "range", "bull"][:n_states],
        )
    hmm = StickyHMM(n_states=n_states, seed=seed)
    hmm.fit(x, n_iter=n_iter)
    post = hmm.predict_proba(x)
    path = hmm.viterbi(x)
    state_names = list(hmm.params.state_names)
    cur_probs = post[-1]
    cur_state = int(np.argmax(cur_probs))
    cur_name = state_names[cur_state] if cur_state < len(state_names) else "range"
    confidence = float(cur_probs[cur_state])
    # BOCPD: 用数据自适应先验(首 30 个点的均值/方差)
    init_n = min(30, T)
    mu_init = float(np.mean(x[:init_n]))
    var_init = float(np.var(x[:init_n])) + 1e-6
    # 把先验强度设为:预测方差 ≈ 4 * var_init (允许 2σ 跳变)
    kappa0 = 1.0
    alpha0 = 1.0
    beta0 = alpha0 * var_init * 4.0
    bocpd = BOCPDState(run_length_probs=np.array([1.0]), t=0)
    last_change_t = 0
    last_change_score = 0.0
    for t_idx, val in enumerate(x):
        bocpd = bocpd_update(
            float(val), bocpd, hazard=hazard,
            mu0=mu_init, kappa0=kappa0, alpha0=alpha0, beta0=beta0,
        )
        if bocpd.change_score > last_change_score:
            last_change_score = bocpd.change_score
            last_change_t = t_idx
    days_in_regime = int(T - 1 - last_change_t)
    weights = dict(REGIME_FACTOR_WEIGHTS.get(cur_name, REGIME_FACTOR_WEIGHTS["range"]))
    tail_path = [state_names[p] if p < len(state_names) else "range" for p in path[-tail:]]
    while len(state_names) < n_states:
        state_names.append("state_" + str(len(state_names)))
    state_names = state_names[:n_states]
    interp = (
        "HMM 状态=" + cur_name + "(置信=" + format(confidence, ".2f") + "),"
        "BOCPD days_in_regime=" + str(days_in_regime) + ","
        "change_score=" + format(last_change_score, ".3f") + ","
        "因子权重:trend=" + format(weights['trend'], ".2f") +
        " mean_reversion=" + format(weights['mean_reversion'], ".2f")
    )
    # 估计 log-likelihood
    log_A = np.log(hmm.params.A + 1e-300)
    seg = min(T, 50)
    log_B = np.stack([_gaussian_logpdf(x[-seg:], hmm.params.mu[k], hmm.params.sigma[k]) for k in range(hmm.K)], axis=0)
    log_alpha = np.empty((hmm.K, seg))
    log_alpha[:, 0] = np.log(hmm.params.pi + 1e-300) + log_B[:, 0]
    for t in range(1, seg):
        for k in range(hmm.K):
            log_alpha[k, t] = _logsumexp(log_alpha[:, t - 1] + log_A[:, k]) + log_B[k, t]
    hmm_ll = _logsumexp(log_alpha[:, -1])
    return RegimeState(
        regime=cur_name,
        confidence=round(confidence, 4),
        state_probs={state_names[k]: round(float(cur_probs[k]), 4) for k in range(n_states)},
        days_in_regime=days_in_regime,
        change_score=round(float(last_change_score), 4),
        factor_weights={k: round(float(v), 3) for k, v in weights.items()},
        viterbi_path=tail_path,
        interp=interp,
        hmm_log_likelihood=round(float(hmm_ll), 3),
        n_obs=T,
        hmm_state_names=state_names,
    )


def regime_state_to_dict(rs):
    return asdict(rs)


if __name__ == "__main__":
    np.random.seed(0)
    r = np.concatenate([
        np.random.normal(0.002, 0.010, 100),
        np.random.normal(0.000, 0.012, 50),
        np.random.normal(-0.003, 0.015, 100),
    ])
    state = classify_regime_hmm(r)
    print(regime_state_to_dict(state))
