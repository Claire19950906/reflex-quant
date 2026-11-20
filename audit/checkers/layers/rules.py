"""规则铁律层：必须引用至少一条 active 铁律（E005）。依赖 rules provider。"""

from __future__ import annotations

from typing import Any, Dict

from ...core.context import CheckContext
from ...core.report import LayerResult
from ._base import LayerSpec, run_spec

SPEC = LayerSpec(name="rules", functions=("check_rules",), label="规则铁律层", requires="rules")


def run(decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    return run_spec(SPEC, decision, ctx)
