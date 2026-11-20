"""审计层输入契约 decision.v1。

只描述"审计需要什么"，不描述"交易需要什么"。
结果回填（forward_pnl / mfe / mae / is_correct）全部 optional —— 这是把审计
层从交易链路里松绑的关键：审计在决策前就要能跑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

SCHEMA_PATH = Path(__file__).with_name("decision.v1.schema.json")


def load_schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(decision: Dict[str, Any]) -> List[str]:
    """轻量校验（不引入 jsonschema 依赖）。返回缺失/类型问题列表，空=通过。

    只校验"缺了就没法审"的字段；其余字段缺失由对应 layer 报 not_applicable。
    """
    problems: List[str] = []
    if not isinstance(decision, dict):
        return ["decision 必须是 dict"]

    did = decision.get("id", decision.get("decision_id"))
    if did in (None, ""):
        problems.append("缺 id/decision_id：无 trace_id 无法出报告")

    action = str(decision.get("suggested_action") or "").strip().upper()
    if not action:
        problems.append("缺 suggested_action：无法判定方向")

    asset = str(decision.get("target_asset") or "").strip()
    if not asset:
        problems.append("缺 target_asset：无法定位标的（可填 NONE，但必须显式）")

    if not (decision.get("reasoning") or decision.get("reasoning_path")):
        problems.append("缺 reasoning / reasoning_path：逻辑层无法审")

    comp = decision.get("completeness_score")
    if comp is not None:
        try:
            float(comp)
        except (TypeError, ValueError):
            problems.append("completeness_score 必须是数值")
    return problems
