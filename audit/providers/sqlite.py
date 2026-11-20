"""SQLite 数据源：从现有库读最小列集，可用于"导出 provider 文件"或直连比对。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .base import DriversData, HistoryData, KlineData, RulesData

QUERIES = {
    "rules": "SELECT id, rule, active, kind FROM review_insights",
    "drivers": "SELECT asset, label, is_core, active FROM asset_driver_nodes",
    "samples": "SELECT decision_id, asset, is_correct, forward_pnl, ts FROM signal_performance",
    "trades": "SELECT id, decision_id, asset, action, pnl_pct, settled FROM quick_sim_trades",
    "decisions": (
        "SELECT id, target_asset, event_type_code, trade_gate_reason, "
        "evidence_action, evidence_confidence FROM ai_decisions"
    ),
}


class SqliteBundle:
    def __init__(self, db_path: str | Path, *, read_only: bool = True) -> None:
        self.db_path = Path(db_path)
        self.errors: List[str] = []
        self.label = f"sqlite:{self.db_path}"
        uri = f"file:{self.db_path}?mode=ro" if read_only else str(self.db_path)
        self._conn = sqlite3.connect(uri, uri=read_only, timeout=15.0)
        self._conn.row_factory = sqlite3.Row

    def _query(self, key: str) -> List[Dict[str, Any]]:
        try:
            return [dict(r) for r in self._conn.execute(QUERIES[key]).fetchall()]
        except sqlite3.Error as exc:
            self.errors.append(f"{key}: {type(exc).__name__}: {exc}")
            return []

    def rules(self) -> RulesData:
        return RulesData(rules=self._query("rules"))

    def drivers(self) -> DriversData:
        return DriversData(nodes=self._query("drivers"))

    def history(self) -> HistoryData:
        return HistoryData(
            samples=self._query("samples"),
            trades=self._query("trades"),
            decisions=self._query("decisions"),
        )

    def kline(self) -> KlineData:
        return KlineData()  # 内核当前不用 K 线

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
