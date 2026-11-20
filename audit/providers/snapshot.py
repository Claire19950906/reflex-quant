"""快照：把 provider 数据装进一个内存 SQLite，供旧内核零改动运行。

这是 W2 的核心手法——不重写 1,601 行校验器，而是让它继续用 SQL，
只是 SQL 跑在"由 provider 灌出来"的内存库上。表结构按内核真实查询的列建，
多余列一律不建（缺列时内核会拿到 OperationalError 并降级，与线上行为一致）。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from .base import DriversData, HistoryData, KlineData, RulesData

_SCHEMA = (
    """CREATE TABLE review_insights (id INTEGER, rule TEXT, active INTEGER, kind INTEGER)""",
    """CREATE TABLE asset_driver_nodes (asset TEXT, label TEXT, is_core INTEGER, active INTEGER)""",
    """CREATE TABLE signal_performance (decision_id INTEGER, asset TEXT, is_correct TEXT,
                                        forward_pnl REAL, ts INTEGER)""",
    """CREATE TABLE quick_sim_trades (id INTEGER, decision_id INTEGER, asset TEXT, action TEXT,
                                      pnl_pct REAL, settled INTEGER)""",
    """CREATE TABLE ai_decisions (id INTEGER, target_asset TEXT, asset TEXT,
                                  event_type_code TEXT, trade_gate_reason TEXT,
                                  evidence_action TEXT, evidence_confidence REAL)""",
)

_INSERTS = (
    ("review_insights", ("id", "rule", "active", "kind")),
    ("asset_driver_nodes", ("asset", "label", "is_core", "active")),
    ("signal_performance", ("decision_id", "asset", "is_correct", "forward_pnl", "ts")),
    ("quick_sim_trades", ("id", "decision_id", "asset", "action", "pnl_pct", "settled")),
    ("ai_decisions", ("id", "target_asset", "asset", "event_type_code",
                      "trade_gate_reason", "evidence_action", "evidence_confidence")),
)

_HISTORY_MAP = {
    "review_insights": None,
    "asset_driver_nodes": None,
    "signal_performance": "samples",
    "quick_sim_trades": "trades",
    "ai_decisions": "decisions",
}


def _defaults(row: Dict[str, Any]) -> Dict[str, Any]:
    """补默认值：只补 schema 里"结构上应当存在"的列，不改写观测到的值。"""
    out = dict(row)
    if "asset" in out or "target_asset" in out:
        out.setdefault("asset", out.get("target_asset"))
        out.setdefault("label", out.get("label"))
    if out.get("asset") is None and out.get("target_asset") is not None:
        out["asset"] = out["target_asset"]
    return out


def build(
    rules: RulesData,
    drivers: DriversData,
    history: HistoryData,
    kline: KlineData | None = None,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for ddl in _SCHEMA:
        conn.execute(ddl)

    payloads = {
        "review_insights": rules.rules,
        "asset_driver_nodes": drivers.nodes,
        "signal_performance": history.samples,
        "quick_sim_trades": history.trades,
        "ai_decisions": history.decisions,
    }

    for table, cols in _INSERTS:
        rows = payloads.get(table) or []
        if not rows:
            continue
        values = [tuple(_defaults(r).get(c) for c in cols) for r in rows]
        conn.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            values,
        )
    conn.commit()
    return conn
