"""confidence_split — 把「一个 confidence」拆成三个独立量（v342）

背景（v342 判断链颗粒度）：
    现在的 confidence 是 0-100 的单一数字，来源混杂：
      · 方向分（加权因子）
      · 历史胜率（同品种样本，样本少时几乎无信息）
      · 波动折扣 / 佐证加成
    一个数字同时承载「方向对不对」「赔率划不划算」「历史可不可靠」，
    事后复盘无法判断到底哪一环错了。

本模块只做**加法**：不改 unified_confidence 的结果，另给三量。
    1) direction   —— 方向可信度：可得因子权重占全部权重的比例（覆盖度）
    2) reliability —— 历史可靠性：胜率 Wilson 95% 下界（样本不足 → None）
    3) odds        —— 预期赔率：预期波动 / 当前 ATR（>1 = 赔率大于寻常波动）
"""
from math import sqrt
from typing import Any, Dict, Optional

_Z_95 = 1.96


def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def wilson_lower(wins: int, n: int, z: float = _Z_95) -> Optional[float]:
    """二项比例的 Wilson 95% 下界。n=0 返回 None（样本不足，不是 0）。"""
    if n <= 0:
        return None
    p = max(0.0, min(1.0, wins / n))
    denom = 1.0 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, min(1.0, (center - margin) / denom))


def compute(*,
            coverage: Optional[float],
            wins: int = 0,
            settled: int = 0,
            expected_move_pct: Optional[float] = None,
            atr_pct: Optional[float] = None,
            contradiction_count: int = 0,
            min_sample: int = 10) -> Dict[str, Any]:
    """三量拆分。任何一项缺输入 → 该量为 None 并在 notes 里说明，不填 0 冒充。"""
    notes = []

    direction = _f(coverage)
    if direction is None:
        notes.append("direction: 无覆盖度输入")
    else:
        direction = max(0.0, min(1.0, direction))
        if contradiction_count:
            penalty = min(0.30, 0.10 * contradiction_count)
            direction = max(0.0, direction - penalty)
            notes.append(f"direction: 矛盾 -{penalty:.2f}")

    reliability = None
    if settled < min_sample:
        notes.append(f"reliability: 样本 {settled}<{min_sample}，只报区间不下结论")
    else:
        reliability = wilson_lower(wins, settled)

    odds = None
    move = _f(expected_move_pct)
    atr = _f(atr_pct)
    if move is None or atr is None or atr <= 0:
        notes.append("odds: 缺预期波动或 ATR")
    else:
        odds = abs(move) / atr

    return {
        "direction": round(direction, 4) if direction is not None else None,
        "reliability": round(reliability, 4) if reliability is not None else None,
        "odds": round(odds, 4) if odds is not None else None,
        "sample": {"wins": int(wins), "settled": int(settled), "min_sample": int(min_sample)},
        "notes": notes,
    }
