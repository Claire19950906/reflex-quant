#!/usr/bin/env python3
"""strength_weights.py — 决策链趋势/共振加减分权重标定与读取。

背景
----
`tech_signal_bridge.decide_direction()` 对候选方向做趋势同向加分、逆势减分、
多周期共振加减分。这些系数最初是经验值，本模块把它们集中成可回测标定的权重表，
脚本/常驻服务都从这里读；后续跑历史信号前向收益回归后，直接改这里的默认值即可。

权重口径
--------
· 趋势同向（强趋势与候选同向）：TREND_SAME
· 趋势逆势（强趋势里逆势抄底/摸顶）：TREND_AGAINST
· 多周期共振同向：RESONANCE_SAME
· 多周期共振逆势：RESONANCE_AGAINST
strength 夹在 [0, 1]。

回测标定时，用历史信号触发日的前向收益，比较"有趋势/共振证据"与"无证据"两组的
命中差和超额差，把同/逆势系数按边际差缩放；当前先给与台账一致的保守默认值。
"""
from __future__ import annotations

from typing import Dict, Optional

#: 默认加减分权重（强度 0~1 区间内的增量）
# 由 scripts/calibrate_strength_weights.py 在 15805 条历史信号上标定（5 日前向口径）：
#   trend_same     边际 -0.073% → -0.007（趋势同向基本无加分，回落近中性）
#   trend_against  边际 -1.554% → -0.155（逆势抄底/摸顶明显更差，保留较重减分）
#   resonance_same 边际 +0.478% → +0.048（多周期同向有正向边际）
#   resonance_against 边际 -0.254% → -0.025（逆势共振小幅减分）
DEFAULT_WEIGHTS: Dict[str, float] = {
    "trend_same": -0.007,       # 强趋势同向加分（标定后近中性）
    "trend_against": -0.155,    # 强趋势逆势减分（标定值，逆势显著更差）
    "resonance_same": 0.048,    # 多周期共振同向加分（标定值）
    "resonance_against": -0.025,  # 多周期共振逆势减分（标定值）
}

#: 标定后的权重（回测校准后写这里；None 表示回落到默认）
_CALIBRATED: Dict[str, Optional[float]] = {k: None for k in DEFAULT_WEIGHTS}


def weights() -> Dict[str, float]:
    """取当前生效权重：标定值优先，未标定回落默认。"""
    return {k: (_CALIBRATED[k] if _CALIBRATED[k] is not None else v)
            for k, v in DEFAULT_WEIGHTS.items()}


def calibrate(new: Dict[str, Optional[float]]) -> None:
    """写入标定权重（进程内）；None 项回落到默认。"""
    for k, v in new.items():
        if k in _CALIBRATED:
            _CALIBRATED[k] = v


def clamp(v: float) -> float:
    return max(0.0, min(1.0, v))
