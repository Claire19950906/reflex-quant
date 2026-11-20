#!/usr/bin/env python3
"""v399：置信度的「有 / 无」判定（纯函数、零依赖）。

为什么要单独一个模块
--------------------
`evidence.unified_confidence` 的公式是

    confidence = |final_score| × 100 × 显著性 × 覆盖度因子 × 风险 × 佐证

降级路径（target_none / llm_empty / summary_only / server_fallback）里
`final_score = 0` 且 `coverage = 0`，公式输出 **0.0**。
但 0.0 有两种完全不同的含义：

  - 「算过，结论就是没信心」 → 应该保留 0.0
  - 「没有因子可用，根本没算」 → 应该是 **NULL**，不是 0

历史实现把两者都写成 0.0，于是 447/494 条决策被统计成"低置信度决策"，
而其中绝大多数根本没进入过置信度计算。这违反 `AGENTS.md` 铁律 4
「0 不得作为有效值参与统计」。

判定口径（只有一条）：**coverage > 0 才算算过**。
coverage 是 `decision_guard` 里"可得因子权重 / 总权重"，是唯一能证明
"确实有证据进了公式"的量。拿不到 coverage（字段缺失 / guard 异常兜底）
一律视为"没算"，因为此时无法证明有证据。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def coverage_of(guard: Any) -> float:
    """从 guard 结果里取 evidence coverage；取不到返回 0.0（= 没算）。"""
    if not isinstance(guard, dict):
        return 0.0
    detail = guard.get("confidence_detail")
    if not isinstance(detail, dict):
        return 0.0
    try:
        return float(detail.get("coverage") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_unavailable(guard: Any) -> bool:
    """True = 没有证据可算（落库应写 NULL，闸门原因应写"无可证据"）。"""
    return coverage_of(guard) <= 0.0


def db_value(guard: Any, computed: Any) -> Optional[float]:
    """落库值：没算过 → None；算过 → 保留数字（含合法的 0.0）。

    注意这不是"把 0 洗成 NULL"——`computed` 只有在 coverage>0 时才会被保留，
    而 coverage>0 的 0.0 是真结论（例如 AI 主动观望且因子互相抵消）。
    """
    if is_unavailable(guard):
        return None
    try:
        return float(computed)
    except (TypeError, ValueError):
        return None


def db_value_for_decision(guard: Any, computed: Any, degraded: bool = False) -> Optional[float]:
    """落库值（带降级标记）：降级决策一律 NULL。

    v452：`analysis_degraded=1` 的决策（llm_empty / target_none / summary_only /
    server_fallback）按定义**不构成判断**，它的置信度不是"算出来很低"，而是
    "根本没有可评的证据"。此前只按 coverage 判定，覆盖率>0 的降级行仍会写 0.0，
    被 `fake_data.py` F1 判成假数据（实测 11-19 早 125 条、且每小时新增）。
    这里把"降级"并入唯一判据：降级 → NULL，不再有 0.0 占位。
    """
    if degraded:
        return None
    return db_value(guard, computed)


def gate_reason(unavailable: bool, fallback: str = "") -> str:
    """闸门原因文案：把"没算"和"算出来低"说成两件事。

    `evidence.evaluate` 产出的 verdict 文案是「统一置信度 X 低于闸门 Y」——
    在"根本没算过"的场景里这句话是错的（那个 X 是占位的 0），
    所以要丢掉，换成降级原因本身。
    """
    if not unavailable:
        return fallback
    fb = str(fallback or "").strip()
    if "统一置信度" in fb:
        fb = ""
    return f"无可证据（coverage=0，置信度不适用）：{fb or '未建仓'}"


if __name__ == "__main__":  # pragma: no cover — 手查用
    print(db_value({"confidence_detail": {"coverage": 0.0}}, 0.0))   # None
    print(db_value({"confidence_detail": {"coverage": 0.35}}, 0.0))  # 0.0
    print(gate_reason(True, "统一置信度 0 低于闸门 15"))
