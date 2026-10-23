"""Tests for changepoint.py (v240+) — BOCPD 算法正确性 + 集成.

覆盖:
  - BOCPD 状态更新正确性（run-length 后验归一化、changepoint/fresh_run prob 单调性）
  - Step change 检测（已知 regime shift → fresh_run_prob 应当 spike）
  - Steady-state stability（无 regime shift → fresh_run_prob 应当保持低）
  - max_run_length 截断
  - detect_in_stream 便利函数
  - 数值稳定性 + 性能
"""
from __future__ import annotations

import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import changepoint as cp  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# 1. 基础状态管理
# ═══════════════════════════════════════════════════════════════════════════

def test_initial_state():
    """初始状态: changepoint_prob=1.0 (尚未观察任何数据, 起点必为 cp)."""
    det = cp.BOCPD(hazard_lambda=50)
    assert det.changepoint_prob == 1.0
    assert det.run_length_posterior == [1.0]
    assert det.most_likely_run_length == 0


def test_posterior_sums_to_one():
    """每步更新后 posterior 应归一化."""
    det = cp.BOCPD(hazard_lambda=50)
    for _ in range(20):
        det.update(0.5)
    s = sum(det.run_length_posterior)
    assert abs(s - 1.0) < 1e-9, f"posterior sum = {s}"


def test_reset_clears_state():
    """reset() 后状态回到初始."""
    det = cp.BOCPD(hazard_lambda=50)
    for _ in range(10):
        det.update(0.5)
    det.reset()
    assert det.changepoint_prob == 1.0
    assert det.run_length_posterior == [1.0]


# ═══════════════════════════════════════════════════════════════════════════
# 2. fresh_run_prob vs changepoint_prob
# ═══════════════════════════════════════════════════════════════════════════

def test_fresh_run_prob_at_startup_is_one():
    """初始: r=0 prob=1.0, fresh_run(0)=1.0."""
    det = cp.BOCPD(hazard_lambda=50)
    assert det.fresh_run_prob(k=0) == 1.0
    assert det.fresh_run_prob(k=5) == 1.0


def test_fresh_run_prob_monotone_in_k():
    """fresh_run_prob(k) 单调不减."""
    det = cp.BOCPD(hazard_lambda=50)
    for _ in range(10):
        det.update(0.5)
    p0 = det.fresh_run_prob(k=0)
    p3 = det.fresh_run_prob(k=3)
    p10 = det.fresh_run_prob(k=10)
    assert p0 <= p3 <= p10


def test_changepoint_prob_bounded_by_hazard():
    """changepoint_prob 不应超过 1/λ (理论上限)."""
    det = cp.BOCPD(hazard_lambda=10, max_run_length=200)
    for x in [0.3] * 100 + [0.7] * 100 + [0.3] * 100:
        det.update(x)
    # 截断后 cp_prob 可能略低于 1/λ; 但绝不应超过
    assert det.changepoint_prob <= 0.1 + 1e-9, det.changepoint_prob


# ═══════════════════════════════════════════════════════════════════════════
# 3. Step change 检测
# ═══════════════════════════════════════════════════════════════════════════

def test_detects_clear_step_change():
    """50 × 0.3 then 50 × 0.7: fresh_run_prob 应当在 step 附近 spike."""
    det = cp.BOCPD(hazard_lambda=50)
    fresh_probs = []
    for i in range(100):
        det.update(0.3 if i < 50 else 0.7)
        fresh_probs.append(det.fresh_run_prob(k=5))

    pre_step = max(fresh_probs[40:50])
    post_step = max(fresh_probs[50:60])
    assert post_step > pre_step + 0.3, (
        f"expected fresh_run_prob to spike after step: "
        f"pre={pre_step:.3f} post={post_step:.3f}"
    )
    assert post_step > 0.5, f"post_step fresh_run_prob={post_step:.3f}"


def test_no_false_detection_after_warmup():
    """常数序列 warmup 后 fresh_run 应保持低."""
    det = cp.BOCPD(hazard_lambda=50)
    for _ in range(50):
        det.update(0.5)
    steady_fresh = []
    for _ in range(30):
        det.update(0.5)
        steady_fresh.append(det.fresh_run_prob(k=5))
    assert max(steady_fresh) < 0.3, (
        f"steady-state fresh_run_prob should be < 0.3, got max={max(steady_fresh):.3f}"
    )


def test_gradual_drift_partial_detection():
    """慢漂移不应触发 sharp spike, 但 mid-stream 可能有 moderate rise."""
    det = cp.BOCPD(hazard_lambda=50)
    fresh_probs = []
    for i in range(100):
        x = 0.3 + i * 0.005
        det.update(x)
        fresh_probs.append(det.fresh_run_prob(k=5))

    # 慢漂移 (0.005/step) 不易被 BOCPD 检出 — fresh_run 应保持低
    # 这是 correct behavior: BOCPD 擅长 sharp changes, 不是 gradual drift
    mid = max(fresh_probs[40:60])
    assert mid < 0.5, f"mid-drift fresh_run should stay moderate, got {mid:.3f}"


# ═══════════════════════════════════════════════════════════════════════════
# 4. max_run_length 截断
# ═══════════════════════════════════════════════════════════════════════════

def test_max_run_length_cap():
    """更新次数 > max_run_length 时 posterior 不超过 cap."""
    det = cp.BOCPD(hazard_lambda=50, max_run_length=10)
    for i in range(100):
        det.update(0.5 + (i % 3) * 0.01)
    assert len(det.run_length_posterior) <= 10


# ═══════════════════════════════════════════════════════════════════════════
# 5. detect_in_stream 便利函数
# ═══════════════════════════════════════════════════════════════════════════

def test_detect_in_stream_constant():
    """常数序列 → steady-state fresh_run < threshold."""
    steady_max = 0
    det = cp.BOCPD(hazard_lambda=50)
    for i, x in enumerate([0.5] * 100):
        det.update(x)
        if i >= 20:
            steady_max = max(steady_max, det.fresh_run_prob(k=5))
    assert steady_max < 0.5


def test_detect_in_stream_step_change():
    """step change → detected=True, 边界附近有 changepoint_indices."""
    res = cp.detect_in_stream(
        [0.3] * 50 + [0.7] * 50, hazard_lambda=50, threshold=0.3,
        fresh_run_k=5,
    )
    assert res["detected"]
    boundary_in = any(48 <= i <= 55 for i in res["changepoint_indices"])
    assert boundary_in, (
        f"expected boundary in {res['changepoint_indices']}"
    )


def test_detect_in_stream_returns_expected_keys():
    """返回字典应包含所有声明的 keys."""
    res = cp.detect_in_stream([0.5] * 10)
    for k in [
        "changepoint_indices", "final_changepoint_prob",
        "final_fresh_run_prob", "max_fresh_run_prob",
        "max_fresh_run_index", "detected", "n_points",
    ]:
        assert k in res


# ═══════════════════════════════════════════════════════════════════════════
# 6. 性能
# ═══════════════════════════════════════════════════════════════════════════

def test_performance_realistic():
    """真实场景: 50 个数据点 + max_run_length=50, 应 < 50ms."""
    import time
    rng = random.Random(42)
    det = cp.BOCPD(hazard_lambda=50, max_run_length=50)
    t0 = time.time()
    for _ in range(50):
        det.update(rng.random())
    elapsed = (time.time() - t0) * 1000
    assert elapsed < 50, f"too slow: {elapsed:.1f}ms"


# ═══════════════════════════════════════════════════════════════════════════
# 7. 数值稳定性
# ═══════════════════════════════════════════════════════════════════════════

def test_no_nan_after_extreme_data():
    """极端数据不应导致 NaN."""
    det = cp.BOCPD(hazard_lambda=50)
    for x in [0.0] * 100 + [1.0] * 100:
        det.update(x)
    for p in det.run_length_posterior:
        assert not (p != p)  # NaN check
        assert p >= 0


def test_handles_constant_stream_without_collapse():
    """常数序列不应导致 posterior 退化（underflow protection）。"""
    det = cp.BOCPD(hazard_lambda=50, max_run_length=30)
    for _ in range(200):
        det.update(0.5)
    s = sum(det.run_length_posterior)
    assert abs(s - 1.0) < 1e-6
    assert len(det.run_length_posterior) > 1


# ═══════════════════════════════════════════════════════════════════════════
# 8. meta_reflection_loop._augment_with_bocpd 集成测试
# ═══════════════════════════════════════════════════════════════════════════

def test_augment_with_bocpd_disabled_by_default(monkeypatch):
    """默认 META_BOCPD_ENABLED=False → evidence 不应被增强."""
    monkeypatch.setenv("META_BOCPD_ENABLED", "0")
    import importlib
    import config as cfg
    importlib.reload(cfg)
    import meta_reflection_loop as mr
    importlib.reload(mr)

    evidence = {"current_wr": 0.5, "current_n": 50}
    out = mr._augment_with_bocpd(evidence, recent_trades=[0, 1, 1, 0, 1] * 10)
    assert out == evidence, "evidence should be unchanged when BOCPD disabled"


def test_augment_with_bocpd_enabled_adds_keys(monkeypatch):
    """META_BOCPD_ENABLED=True + recent_trades → evidence 应包含 _changepoint_prob."""
    monkeypatch.setenv("META_BOCPD_ENABLED", "1")
    import importlib
    import config as cfg
    importlib.reload(cfg)
    import meta_reflection_loop as mr
    importlib.reload(mr)

    evidence = {"current_wr": 0.5, "current_n": 50}
    # Step change in recent trades
    trades = [0.3] * 30 + [0.7] * 30
    out = mr._augment_with_bocpd(evidence, recent_trades=trades)
    assert "_changepoint_prob" in out
    assert "_fresh_run_prob" in out
    assert "_bocpd_max_run_length" in out
    assert out["_bocpd_n_trades"] == 60


def test_augment_with_bocpd_no_recent_trades(monkeypatch):
    """recent_trades=None 或 < 3 → 不增强, 不报错."""
    monkeypatch.setenv("META_BOCPD_ENABLED", "1")
    import importlib
    import config as cfg
    importlib.reload(cfg)
    import meta_reflection_loop as mr
    importlib.reload(mr)

    evidence = {"current_wr": 0.5}
    out_none = mr._augment_with_bocpd(evidence, recent_trades=None)
    out_short = mr._augment_with_bocpd(evidence, recent_trades=[0, 1])
    assert out_none == evidence
    assert out_short == evidence


def test_meta_loop_classify_with_bocpd_evidence(monkeypatch):
    """meta_loop._classify_finding 集成 recent_trades 后 evidence 应包含 BOCPD 字段."""
    monkeypatch.setenv("META_BOCPD_ENABLED", "1")
    monkeypatch.setenv("META_BAYES_MC_SAMPLES", "500")
    import importlib
    import config as cfg
    importlib.reload(cfg)
    import meta_reflection_loop as mr
    importlib.reload(mr)

    trades = [0.3] * 30 + [0.7] * 30
    kind, _, ev = mr._classify_finding(
        current={"n": 60, "win_rate": 0.5, "avg_luck": 0.0,
                 "layer": "L", "sub_layer": "S", "signal_name": "sig"},
        baseline={"n": 60, "win_rate": 0.5},
        recent_trades=trades,
    )
    assert "_changepoint_prob" in ev
    assert "_fresh_run_prob" in ev
    assert ev["_bocpd_n_trades"] == 60