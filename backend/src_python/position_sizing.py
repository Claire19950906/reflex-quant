"""v230+ P0.1: 仓位管理 (Confidence-Tier Sizing, v1).

基于 ai_decisions.evidence_confidence 把每笔信号建仓的名义金额乘以分层倍数，
让高信心信号多吃一口、低信心信号少吃一口。

v1 保持 action-agnostic（BUY/SELL 同倍率），只按 confidence 分层。
action-aware sizing（如"通过 SELL gate 的高强度 SELL 减半"）留待 v2 数据驱动。

设计原则：
- 默认开启；env POSITION_SIZING_ENABLED=0 → 整层透明旁路。
- 输出 (scaled_notional, metadata) 元组，便于上层日志/审计。
- clamp 区间 [MIN_MULT, MAX_MULT] 防"evidence_confidence=0.99 → 100x"的失控。
- 缺失 confidence → 返回 base * 1.0（P0.1 透明兜底，不阻断建仓）。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple


# 全局 kill-switch（默认开）。
_POSITION_SIZING_ENABLED_RAW = os.getenv("POSITION_SIZING_ENABLED", "1").strip().lower()
POSITION_SIZING_ENABLED = _POSITION_SIZING_ENABLED_RAW in ("1", "true", "yes", "on")


# 默认三层：low < 0.3 → 0.5x；medium 0.3-0.7 → 1.0x；high >= 0.7 → 1.5x。
# 通过 env POSITION_SIZING_TIERS_JSON 覆盖（JSON dict: threshold -> multiplier）。
# 阈值表示 "confidence ≥ 该值则用此倍率"，按升序排列。
# 必须含 0.0 阈值（最低 tier）；否则 c < 最低阈值时无 tier 匹配。
# 例子：POSITION_SIZING_TIERS_JSON='{"0.0": 0.5, "0.3": 1.0, "0.7": 1.5}'
_DEFAULT_TIERS: Dict[float, float] = {0.0: 0.5, 0.3: 1.0, 0.7: 1.5}
_TIERS_RAW = os.getenv("POSITION_SIZING_TIERS_JSON", "").strip()
try:
    _PARSED = json.loads(_TIERS_RAW) if _TIERS_RAW else {}
    POSITION_SIZING_TIERS: Dict[float, float] = {
        float(k): float(v) for k, v in _PARSED.items()
    } if _PARSED else _DEFAULT_TIERS
except (json.JSONDecodeError, ValueError, TypeError):
    POSITION_SIZING_TIERS = _DEFAULT_TIERS


# clamp 上下限（任何 signal 都不会超过这俩边界）。
_POSITION_SIZING_MIN_MULT_RAW = os.getenv("POSITION_SIZING_MIN_MULT", "0.25").strip()
POSITION_SIZING_MIN_MULT: float = (
    float(_POSITION_SIZING_MIN_MULT_RAW) if _POSITION_SIZING_MIN_MULT_RAW else 0.25
)
_POSITION_SIZING_MAX_MULT_RAW = os.getenv("POSITION_SIZING_MAX_MULT", "2.0").strip()
POSITION_SIZING_MAX_MULT: float = (
    float(_POSITION_SIZING_MAX_MULT_RAW) if _POSITION_SIZING_MAX_MULT_RAW else 2.0
)


def _select_multiplier(confidence: Optional[float]) -> float:
    """根据 confidence 找最大满足阈值（最高 tier）；缺失/越界 → 1.0。

    分层语义（按阈值升序排列）：
      - c < threshold[0]                → 最低 tier 倍率
      - threshold[i] ≤ c < threshold[i+1] → 第 i 个 tier 倍率
      - c ≥ threshold[-1]               → 最高 tier 倍率
    """
    if confidence is None:
        return 1.0
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return 1.0
    if c < 0 or c > 1:
        return 1.0
    sorted_thresholds = sorted(POSITION_SIZING_TIERS.items(), key=lambda kv: kv[0])
    if not sorted_thresholds:
        return 1.0
    # 默认 = 最低 tier 倍率（confidence 低于所有阈值时落到最低层）
    chosen = sorted_thresholds[0][1]
    for threshold, mult in sorted_thresholds:
        if c >= threshold:
            chosen = mult
        else:
            break
    return chosen


def compute_notional(
    signal: Dict[str, Any],
    base_notional: float,
    conn: Optional[Any] = None,
) -> Tuple[float, Dict[str, Any]]:
    """按 evidence_confidence 计算 scaled_notional 与元数据。

    Args:
        signal: ai_decisions 行 dict（需含 `evidence_confidence`，可缺）。
        base_notional: 基础名义金额（如 QUICK_SIM_NOTIONAL_USDT）。
        conn: 保留位（v2 数据驱动模式会用）。

    Returns:
        (scaled_notional, metadata dict)
        - scaled_notional: clamp 后的 USDT 值
        - metadata: {"enabled", "tier", "multiplier", "confidence", "reason"}

    安全：
      - POSITION_SIZING_ENABLED=0 → 直接 return (base, {reason: "disabled"})
      - evidence_confidence 缺失/非法 → (base * 1.0, {reason: "missing_confidence"})
    """
    if not POSITION_SIZING_ENABLED:
        return float(base_notional), {
            "enabled": False,
            "tier": "n/a",
            "multiplier": 1.0,
            "confidence": signal.get("evidence_confidence"),
            "reason": "disabled",
        }
    confidence = signal.get("evidence_confidence")
    raw_mult = _select_multiplier(confidence)
    clamped_mult = max(POSITION_SIZING_MIN_MULT, min(POSITION_SIZING_MAX_MULT, raw_mult))
    scaled = float(base_notional) * clamped_mult
    # 找分层名（low / medium / high / fallback）。
    if confidence is None:
        tier = "fallback"
        reason = "missing_confidence"
    elif clamped_mult != raw_mult:
        tier = "clamped"
        reason = "clamp_applied"
    else:
        # 找第一个匹配的阈值区间名。
        if clamped_mult == max(POSITION_SIZING_TIERS.values(), default=1.0):
            tier = "high"
        elif clamped_mult == min(POSITION_SIZING_TIERS.values(), default=1.0):
            tier = "low"
        else:
            tier = "medium"
        reason = "tier_match"
    return round(scaled, 4), {
        "enabled": True,
        "tier": tier,
        "multiplier": clamped_mult,
        "raw_multiplier": raw_mult,
        "confidence": confidence,
        "reason": reason,
    }
