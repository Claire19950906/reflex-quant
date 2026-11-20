"""impact_score_ladder.py —— impact_score 的取数阶梯（纯函数、零依赖）。

为什么单列（v397）：
  `impact_score` 有 84.9%（线上 399/470）停在默认 5.0。5.0 既是"没填"的兜底，
  又正好落在"4-6 间接传导"档的中间 —— 落库后与"真打了 5 分"完全无法区分，
  直接导致 `base_mult` 近乎恒定、权重分辨率塌陷（S14）、影响力维名存实亡（S15）。

  阶梯原则（可解释、不造数）：
    1. 模型给了非 5.0 的分 → 尊重模型
    2. 门卫 0-100 分 → /10
    3. 门卫 strength 1-10 → 直接用（同一量纲，不换算）
    4. 都没有 → 5.0，但**来源必须标 default**（由调用方写进 field_sources，
       复盘时"5.0 是判断还是兜底"必须能分辨）

  顺序不可颠倒：门卫分是外部证据，优先于"模型恰好写了 5.0"这个不可信信号。
"""
from __future__ import annotations

DEFAULT_IMPACT = 5.0


def resolve_impact_score(model_impact, gate_impact_1to100, *, gate_strength=0,
                         default: float = DEFAULT_IMPACT) -> tuple:
    """返回 (impact_score, 来源标签)。

    来源标签: "model" / "gate" / "gate_strength" / "default"
    """
    try:
        mi = float(model_impact) if model_impact is not None else default
    except (TypeError, ValueError):
        mi = default
    try:
        g = int(gate_impact_1to100 or 0)
    except (TypeError, ValueError):
        g = 0
    try:
        gs = int(gate_strength or 0)
    except (TypeError, ValueError):
        gs = 0
    if abs(mi - default) > 1e-9:
        return max(0.0, min(10.0, mi)), "model"
    if g > 0:
        return max(0.0, min(10.0, round(g / 10.0, 1))), "gate"
    if 0 < gs <= 10:
        return max(0.0, min(10.0, float(gs))), "gate_strength"
    # v403：既没有模型判断、也没有门卫证据 → 这个分**没算出来**，不是"影响力中等"。
    # 返回 None，由落库写出 NULL（5.0 只保留为调用方显式传 default 时的兼容值）。
    return None, "unavailable"


SOURCE_FIELD_LABEL = {
    "model": "model",
    "gate": "gate_impact_1to100",
    "gate_strength": "gate_strength",
    "default": "default_unknown",
    "unavailable": "unavailable",
}


def source_label(src: str) -> str:
    """把来源标签转成写进 field_sources 的取值（未知来源原样返回）。"""
    return SOURCE_FIELD_LABEL.get(str(src or ""), str(src or "default_unknown"))
