"""数据/证据层：硬事实、时间锚点、因子方向、事件链关联（E001a–E001f、E002、E009）。"""

from __future__ import annotations

from typing import Any, Dict

from ...core.context import CheckContext
from ...core.report import LayerResult
from ._base import LayerSpec, run_spec

SPEC = LayerSpec(
    name="data",
    functions=("check_data",),
    label="数据/证据层",
)


def run(decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    return run_spec(SPEC, decision, ctx)
