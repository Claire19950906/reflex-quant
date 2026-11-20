"""Provider 数据契约。

审计内核只依赖 5 张表（见 README），把它们抽成 4 组数据：

    rules     review_insights     铁律（id/rule/active/kind）
    drivers   asset_driver_nodes  驱动因子节点（asset/label/is_core/active）
    history   signal_performance  历史样本（decision_id/is_correct/forward_pnl/ts/asset）
              quick_sim_trades    成交样本（id/decision_id/asset/action/pnl_pct/settled）
              ai_decisions        最小列（id/target_asset/asset/event_type_code/
                                  trade_gate_reason/evidence_action/evidence_confidence）
    kline     kline_bars          预留（当前内核不用，接口先留）

file / http / sqlite 三种实现返回同一形状，审计结果应逐条一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, runtime_checkable

VERSION = "providers.v1"


@dataclass
class RulesData:
    rules: List[Dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.rules


@dataclass
class DriversData:
    nodes: List[Dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.nodes


@dataclass
class HistoryData:
    samples: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.samples or self.trades)


@dataclass
class KlineData:
    bars: List[Dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.bars


@runtime_checkable
class Bundle(Protocol):
    """一组可选的数据源。任何一种实现都必须提供这四个方法且不抛异常。"""

    label: str

    def rules(self) -> RulesData: ...
    def drivers(self) -> DriversData: ...
    def history(self) -> HistoryData: ...
    def kline(self) -> KlineData: ...
