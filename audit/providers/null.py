"""空数据源：审计层在完全没有外部数据时的默认上下文。"""

from __future__ import annotations

from .base import DriversData, HistoryData, KlineData, RulesData


class NullBundle:
    label = "null"

    def rules(self) -> RulesData:
        return RulesData()

    def drivers(self) -> DriversData:
        return DriversData()

    def history(self) -> HistoryData:
        return HistoryData()

    def kline(self) -> KlineData:
        return KlineData()
