"""v459 宏观驱动系数回测校准 —— 只测"计算与判读"这一层，不测网络。

理由：回测的价值在于口径一致与判读正确，网络取数失败属于环境噪声，
用假序列测才能保证"同样的输入永远得到同样的 IC 与同样的结论"。
"""

import math
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))
sys.path.insert(0, str(BACKEND / "scripts"))

import macro_driver_calibration as mc


def _fred(series, values, start_days=0):
    """把一串数值摊成等间隔日线（2026-01-01 起）。"""
    return [(f"2026-{1 + (start_days + i) // 28:02d}-{1 + (start_days + i) % 28:02d}", v)
            for i, v in enumerate(values)]


def _flat(n, value, start=0):
    return [(f"2026-{1 + (start + i) // 28:02d}-{1 + (start + i) % 28:02d}", value)
            for i in range(n)]


def test_ranks_handles_ties():
    assert mc._ranks([3, 3, 1]) == [2.5, 2.5, 1.0]
    assert mc._ranks([1, 2, 3, 4]) == [1.0, 2.0, 3.0, 4.0]


def test_spearman_is_one_for_monotone():
    import pytest
    assert mc.spearman_ic([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert mc.spearman_ic([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_none_on_constant():
    assert mc.spearman_ic([1, 1, 1], [1, 2, 3]) is None


def test_quintile_spread_picks_top_minus_bottom():
    x = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    y = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]      # 最高组 1%，最低组 0%
    assert mc.quintile_spread(x, y) == 1.0


def test_forward_returns_uses_horizon():
    prices = [("2026-01-01", 100.0), ("2026-01-02", 101.0), ("2026-01-05", 102.0)]
    out = mc.forward_returns(prices, 2)
    assert abs(out["2026-01-01"] - 2.0) < 1e-9
    assert "2026-01-05" not in out          # 没有足够前瞻


def test_build_driver_frame_skips_until_warm():
    fred = {
        "dgs10": _flat(25, 4.0),
        "dgs2": _flat(25, 4.2),
        "t10y2y": _flat(25, -0.2),
        "vix": _flat(25, 18.0),
        "hy_oas": _flat(25, 3.0),
        "dxy": _flat(25, 100.0),
    }
    merged = mc.build_driver_frame(fred, {"x": (0.5, 0.3, 0.1, 0.1)})
    # 20 日窗口需要 21 条观测，第 21 条起才有驱动
    assert len(merged) == 5
    assert merged[0]["rate"] == 0.0
    assert merged[0]["risk"] == 0.0
    assert merged[0]["curve"] == 0.4        # -( -0.2 )/0.5 = 0.4
    assert abs(merged[0]["tilt_x"] - (-0.5 * 0.0 + 0.3 * 0.0 - 0.1 * 0.0 - 0.1 * 0.4)) < 1e-9


def test_build_driver_frame_rate_sign_and_clamp():
    # 10Y 20 日涨 60bp → 60/0.30 = 2.0 → 截到 +1
    d10 = _flat(20, 4.0) + [(f"2026-{1 + (20 + i) // 28:02d}-{1 + (20 + i) % 28:02d}", 4.6)
                             for i in range(5)]
    fred = {
        "dgs10": d10,
        "dgs2": _flat(25, 4.0),
        "t10y2y": _flat(25, 0.0),
        "vix": _flat(25, 18.0),
        "hy_oas": _flat(25, 3.0),
        "dxy": _flat(25, 100.0),
    }
    rows = mc.build_driver_frame(fred, {"x": (1.0, 0.0, 0.0, 0.0)})
    assert rows[-1]["rate"] == 1.0                     # 已截断
    assert rows[-1]["tilt_x"] == -1.0                  # 利率上行 → tilt 负


def test_evaluate_aligned_sign_flips_for_inverse_relation():
    days = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(60)]
    rows = [{"day": d, "rate": float(i)} for i, d in enumerate(days)]
    # 驱动越高，前瞻收益越低 → 与先验（负号）一致 → ic_aligned 为正
    fwd = {d: -float(i) for i, d in enumerate(days)}
    res = mc.evaluate(rows, fwd, "rate", 5, prior_sign=-1)
    assert res["ic"] < 0
    assert res["ic_aligned"] > 0
    assert res["signal"] is True
    assert res["sign_ok"] is True
    # 同一个 IC，sign 反着给 → 对齐后为负、无信号
    res2 = mc.evaluate(rows, fwd, "rate", 5, prior_sign=+1)
    assert res2["ic_aligned"] < 0
    assert res2["sign_ok"] is False      # 强度一样，但方向反了 → 这才是要抓的东西


def test_evaluate_returns_none_when_samples_too_few():
    rows = [{"day": "2026-01-01", "rate": 0.1}]
    res = mc.evaluate(rows, {"2026-01-01": 1.0}, "rate", 10, prior_sign=-1)
    assert res["ic"] is None
    assert res["samples_non_overlap"] == 1


def test_macro_tilt_cap_was_cut_to_005():
    """v459：回测显示复合 tilt 无预测力 → cap 由 0.15 减到 0.05（可 env 恢复）。"""
    import fundamental_factor as ff
    assert ff._MACRO_TILT_CAP == 0.05
