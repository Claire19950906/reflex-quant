"""事件因果层：事件链、历史反例、样本完整性（E014–E018、E020）。

三个子检查器读三张不同的表，依赖各自判定：

    event_logic                asset_driver_nodes   → drivers
    historical_counterfactual  signal_performance   → samples
    sample_integrity           quick_sim_trades     → trades

没有成交样本的客户只会跳 sample_integrity，event_logic 照跑。
"""

from __future__ import annotations

from typing import Any, Dict

from ...core.context import CheckContext
from ...core.report import LayerResult
from ._base import LayerSpec, PartSpec, run_spec

PARTS = (
    PartSpec("event_logic", ("check_event_logic",), requires="drivers"),
    PartSpec("historical_counterfactual", ("check_historical_counterfactual",), requires="samples"),
    PartSpec("sample_integrity", ("check_sample_integrity",), requires="trades"),
)

SPEC = LayerSpec(
    name="event_causality",
    functions=(),
    label="事件因果 + 历史反例 + 样本完整性",
    requires=None,
    parts=PARTS,
)


def run(decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    return run_spec(SPEC, decision, ctx)
