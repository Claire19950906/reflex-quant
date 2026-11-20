"""推理质量层：假设/反事实/校准/历史/机理/置信度（E021–E027）。"""

from __future__ import annotations

from typing import Any, Dict

from ...core.context import CheckContext
from ...core.report import LayerResult
from ._base import LayerSpec, run_spec

SPEC = LayerSpec(name="reasoning", functions=("check_reasoning_logic",), label="推理质量层")


def run(decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    return run_spec(SPEC, decision, ctx)
