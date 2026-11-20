"""自洽逻辑层：因子方向与结论是否矛盾（E003、E013）。"""

from __future__ import annotations

from typing import Any, Dict

from ...core.context import CheckContext
from ...core.report import LayerResult
from ._base import LayerSpec, run_spec

SPEC = LayerSpec(name="logic", functions=("check_logic",), label="自洽逻辑层")


def run(decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    return run_spec(SPEC, decision, ctx)
