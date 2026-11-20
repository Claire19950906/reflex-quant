"""因果方向层：驱动因子节点命中与因果链完整性（E004、E015）。依赖 history provider。"""

from __future__ import annotations

from typing import Any, Dict

from ...core.context import CheckContext
from ...core.report import LayerResult
from ._base import LayerSpec, run_spec

SPEC = LayerSpec(name="causal", functions=("check_causal",), label="因果方向层", requires="drivers")


def run(decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    return run_spec(SPEC, decision, ctx)
