"""市场验证层：市场确认、反向与抢跑（E007、E010）。E007 依赖事后行情（basis=outcome）。"""

from __future__ import annotations

from typing import Any, Dict

from ...core.context import CheckContext
from ...core.report import LayerResult
from ._base import LayerSpec, run_spec

SPEC = LayerSpec(name="market", functions=("check_market",), label="市场验证层")


def run(decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    return run_spec(SPEC, decision, ctx)
