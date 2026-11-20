"""修复建议与判定依据。

两件事必须分清，否则"pre-trade 门禁"就是假的：

1. remediation —— 每条 finding 必须有下一步动作。内核 `_ERROR_CODE_GUIDANCE`
   覆盖了绝大多数码，但 E019/E020 等缺口在审计层补齐（补的是内核语义，不是编造）。
2. basis —— 该码是靠"决策当时的信息"判的，还是必须等行情走完才能判。
   只有 basis="decision" 的 finding 才能在进单前拦截。
"""

from __future__ import annotations

from typing import Dict

# 依赖事后行情/结算的码：进单前无法判定，只能做结算后复盘
OUTCOME_DEPENDENT_CODES: frozenset[str] = frozenset({
    "E007",  # 市场反向（用 forward_pnl）
    "E016",  # 跨资产方向错（用 forward_pnl 或 cross 实测）
    "E017",  # 跨资产无据传导（部分依赖实测）
    "E018",  # 历史反例（依赖历史成交结果）
})

# 内核表缺失的码，在审计层补一条可执行动作（标注 origin=audit）
OVERRIDES: Dict[str, str] = {
    "E019": (
        "完整性 < 0.7：先补齐 reasoning / asset_reasoning / invalidation_condition "
        "三类字段再重跑；此类决策不得采信，也不得计入任何胜率统计"
    ),
    "E020": (
        "该样本不满足 5 条硬规则（见 evidence.reasons）：先修 gate/结算链，"
        "再统计胜率；用事故样本算出的 alpha 必须作废"
    ),
    "E006": "样本量不足：标注「样本不足」，不得据此下方向性结论；等 n 达标再评估",
}


def for_code(code: str, fallback: str = "") -> str:
    from . import legacy  # 延迟导入，避免循环

    text = legacy.remediation_for(code)
    if text:
        return text
    if code in OVERRIDES:
        return OVERRIDES[code]
    return fallback


def basis_for(code: str) -> str:
    return "outcome" if code in OUTCOME_DEPENDENT_CODES else "decision"
