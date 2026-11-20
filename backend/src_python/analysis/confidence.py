# -*- coding: utf-8 -*-
"""置信度与档位：把「一个乘积」换成「四档分层」。

为什么要换（2026-11-16 实测）
--------------------------
旧公式 `confidence = |score|*100 × 显著性 × 覆盖 × 风险 × 佐证 × (1-矛盾比例)`：

  · 方向分为 0 时全式为 0 → 445/482（92.3%）的置信度恒为 0，闸门在做「有没有算过」
    而不是「值不值得做」；
  · 乘子链让 0.1-0.3 的弱信号被压到 15 分以下，而实测这批弱信号事后方向正确率
    60%（30 分钟）/70%（60 分钟），反而通过闸门的 3 笔全亏。

结论：评分不该由一个标量决定「过不过」，而应由**证据档位**决定「走哪条通道」：

  A  tradable   证据齐、结构完整、方向明确   → 允许真实建仓
  B  shadow     证据基本齐、方向明确但结构一般 → 只进影子账本（不下单）
  C  watch      有方向但输出被压缩/证据薄     → 只记账、只观察（不丢弃方向）
  D  hold       没方向或分析降级              → HOLD，必须带原因

本模块是纯函数：不读库、不读 env（阈值由调用方传入，默认值见 DEFAULTS）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 档位阈值（可被 config/.env 覆盖；此处只给默认，便于单测与回放）
DEFAULTS = {
    "A_min_score": 0.35,       # |方向分| ≥ 0.35 才算强信号
    "B_min_score": 0.20,
    "A_min_coverage": 0.70,    # 硬数据覆盖：有快照/有证据的因子占比
    "B_min_coverage": 0.50,
    "C_min_coverage": 0.25,
    "min_structure": 0.60,     # 结构完整度（八段是否真展开）
}

TIER_A = "A_tradable"
TIER_B = "B_shadow"
TIER_C = "C_watch"
TIER_D = "D_hold"

TIER_LABELS = {
    TIER_A: "可建仓",
    TIER_B: "影子单",
    TIER_C: "只观察",
    TIER_D: "观望",
}


@dataclass(frozen=True)
class Verdict:
    """一个决策的质量判定。confidence 仅用于展示/排序，不用于过不过。"""

    tier: str
    confidence: float          # 0-100，展示用
    direction: float           # 带符号的方向分（= 模型 score，未做乘子）
    coverage: float
    structure: float
    consistency: float
    compressed: bool           # 输出被压缩（只有摘要/骨架），方向仍可能有效
    reason: str

    @property
    def tradable(self) -> bool:
        return self.tier == TIER_A

    @property
    def shadowable(self) -> bool:
        return self.tier in (TIER_A, TIER_B)

    @property
    def has_direction(self) -> bool:
        return self.tier in (TIER_A, TIER_B, TIER_C)


def _clamp(x, lo=0.0, hi=1.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def rank(
    *,
    score: float,
    coverage: float = 1.0,
    structure: float = 1.0,
    consistency: float = 1.0,
    risk_penalty: float = 0.0,
    contradictions: int = 0,
    compressed: bool = False,
    degraded: bool = False,
    thresholds: Optional[dict] = None,
) -> Verdict:
    """按证据档位给结论定性。

    score         模型给出的方向分（-1..1），0 表示没有方向
    coverage      硬数据覆盖度 0..1（由 readiness/hard_data_status 折算）
    structure     八段结构完整度 0..1（骨架兜底 = 低）
    consistency   信号自洽度 0..1（方向与分数、与盘面是否一致）
    risk_penalty  波动风险扣分 0..0.3（ATR 极高时下调，不做乘法）
    contradictions 矛盾条数（每条扣 0.08，封顶 0.4）
    compressed    输出被压缩（只有摘要/骨架）：不判死，降一档
    degraded      分析降级（没跑出东西）：直接 D
    """
    th = dict(DEFAULTS)
    if thresholds:
        th.update(thresholds)

    sc = float(score or 0.0)
    abs_sc = min(abs(sc), 1.0)
    cov = _clamp(coverage)
    st = _clamp(structure)
    cons = _clamp(consistency)
    pen = _clamp(risk_penalty, 0.0, 0.3) + min(0.4, 0.08 * max(0, int(contradictions)))
    quality = max(0.0, 1.0 - pen)

    # 展示用置信度：加权而非纯乘 —— 让「方向弱但证据齐」也能拿到中间分，
    # 不再出现「0.1 的方向分被乘成 3 分」这种无法排序的结果。
    confidence = round(
        100.0 * (0.55 * abs_sc + 0.25 * cov + 0.12 * st + 0.08 * cons) * quality, 2
    )

    if degraded or abs_sc <= 0.0:
        return Verdict(TIER_D, confidence, sc, cov, st, cons, compressed,
                       "分析降级或无方向")

    if (not compressed and abs_sc >= th["A_min_score"] and cov >= th["A_min_coverage"]
            and st >= th["min_structure"] and cons >= 0.8):
        return Verdict(TIER_A, confidence, sc, cov, st, cons, compressed, "强信号且证据齐")

    if (not compressed and abs_sc >= th["B_min_score"] and cov >= th["B_min_coverage"]
            and st >= th["min_structure"]):
        return Verdict(TIER_B, confidence, sc, cov, st, cons, compressed, "方向明确但证据一般")

    if abs_sc >= 0.10 and cov >= th["C_min_coverage"]:
        # 关键改动：压缩输出（只有摘要/骨架）不再一律 HOLD，而是进「只观察」通道，
        # 方向保留、记账保留，绝不下单 —— 实测这批事后正确率 60-70%。
        return Verdict(TIER_C, confidence, sc, cov, st, cons, compressed,
                       "压缩输出或证据偏薄：只观察")

    return Verdict(TIER_D, confidence, sc, cov, st, cons, compressed, "方向太弱/证据不足")


def coverage_from_hard_status(hard_status: str, market_confirmation: str = "",
                              confirmation_proxy: Optional[float] = None) -> float:
    """把 hard_data_status 折算成 0-1 的覆盖度（供 rank 使用）。

    ok 且盘面有确认 → 1.0；ok 但盘面 unknown → 0.6（有价无结构）；
    缺一份源 → 0.4；无标的/无源 → 0。

    `confirmation_proxy` 是**本地可算**的盘面确认（`decision_guard` 用 24h 动量代理，
    因为 `market_confirmation` 是 LLM 三值枚举、实测 511/516 是 unknown、positive 从没出现过）。
    传入且足够强时把 0.6 抬到 0.75 —— 这不是放水，是把「模型没说的话」用价格补上；
    默认 None，不传就和旧行为完全一致，避免在样本不足时静默放宽 A 档。
    """
    hs = str(hard_status or "").strip()
    mc = str(market_confirmation or "").strip().lower()
    if hs == "ok":
        if mc not in ("", "unknown"):
            return 1.0
        try:
            if confirmation_proxy is not None and abs(float(confirmation_proxy)) >= 0.2:
                return 0.75
        except (TypeError, ValueError):
            pass
        return 0.6
    if hs.startswith("missing:feed_stale"):
        return 0.55 if mc not in ("", "unknown") else 0.35
    if hs.startswith("missing:none_target"):
        return 0.0
    if hs.startswith("missing:no_local_feed") or hs.startswith("missing:unknown_symbol"):
        return 0.0
    return 0.4
