"""v170-P0+: trade_uuid 全链路 + 三字段对账 + 应急隔离（quarantine + alerts）。

设计要点：

1. trade_uuid 是 ai_decisions 主键（UNIQUE 索引）；其它链路表
   trade_reflections_v2 / structured_reflections / decision_errors /
   quick_sim_trades 都借决策 trade_uuid 形成逻辑外键。

2. backfill UUID：每行 ai_decisions 生成 uuid4().hex[:16]，其它表通过
   decision_id 同步；缺决策引用的孤儿 reflection 立即 quarantine + 告警。

3. 三字段对账（成交时间 + 合约 + 价格）：
   - 时间容差 ±60s
   - 合约严格一致（target_asset == asset）
   - 价格容差 ±0.5%
   任一不一致即 mismatch：写 alert + 临时从 review 库移除（quarantine）。

4. 应急流程：
   - auto_quarantine_on_mismatch=True 时检测到即隔离（默认开）
   - quarantine = UPDATE quarantined_at = now, quarantine_reason = ...
   - alerts 表记录全过程，可追溯
   - 人工修正后须双重校验（resolve 前自动重跑对账，PASS 才解锁）

5. API 过滤：所有 review/replay 端点 WHERE quarantined_at IS NULL；
   quarantined 行不进 /api/review/snapshot 汇总。

6. 启动期 api_server._migrate_schema() 调 ensure_schema() 增量加列；
   独立调用 python id_reconcile.py {migrate|backfill|check|alerts|resolve}。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# 让脚本既能被 import（被 api_server 调用）也能独立 CLI 运行。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import db as _db  # type: ignore

# 三字段对账容差（PRD 给定）。
TIME_TOLERANCE_SEC = 60          # entry_time 与 settled_at 时差容差
PRICE_TOLERANCE_PCT = 0.005      # 0.5% 价格容差
AUTO_QUARANTINE_ON_MISMATCH = True

# ─────────────────────────────────────────────────────────────────────────────
# Schema: alerts 表 + 5 表 ALTER
# ─────────────────────────────────────────────────────────────────────────────

_CREATE_ID_RECONCILE_ALERTS = """
CREATE TABLE IF NOT EXISTS id_reconcile_alerts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id            TEXT UNIQUE NOT NULL,
    alert_type          TEXT NOT NULL,
    severity            TEXT NOT NULL DEFAULT 'warn',
    trade_uuid          TEXT,
    reflection_id       INTEGER,
    decision_id         INTEGER,
    source_table        TEXT,
    payload             TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    acknowledged_at     TEXT,
    acknowledged_by     TEXT,
    resolved_at         TEXT,
    resolution_note     TEXT
);
"""

_CREATE_IDX_RECONCILE_ALERTS_TS = (
    "CREATE INDEX IF NOT EXISTS idx_id_reconcile_alerts_created_at "
    "ON id_reconcile_alerts(created_at)"
)
_CREATE_IDX_RECONCILE_ALERTS_UUID = (
    "CREATE INDEX IF NOT EXISTS idx_id_reconcile_alerts_trade_uuid "
    "ON id_reconcile_alerts(trade_uuid)"
)

# ALTER TABLE 列定义（兼容 SQLite：单条 ALTER；多次调用幂等）。
# ai_decisions：trade_uuid UNIQUE + quarantine 三件套
_AI_DECISIONS_ID_COLUMNS: List[Tuple[str, str]] = [
    ("trade_uuid",        "TEXT DEFAULT NULL"),
    ("quarantined_at",    "TEXT DEFAULT NULL"),
    ("quarantine_reason", "TEXT DEFAULT ''"),
]
# trade_reflections_v2：trade_uuid + quarantine + mismatch_detail
_TRADE_REFLECTIONS_V2_ID_COLUMNS: List[Tuple[str, str]] = [
    ("trade_uuid",        "TEXT DEFAULT NULL"),
    ("quarantined_at",    "TEXT DEFAULT NULL"),
    ("quarantine_reason", "TEXT DEFAULT ''"),
    ("mismatch_detail",   "TEXT DEFAULT ''"),
]
# structured_reflections / decision_errors / quick_sim_trades 仅 trade_uuid
_OTHER_ID_COLUMNS: List[Tuple[str, str]] = [
    ("trade_uuid", "TEXT DEFAULT NULL"),
]


def _now_iso() -> str:
    """本地时区 ISO8601 秒精度字符串；统一时间戳来源。"""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _gen_trade_uuid() -> str:
    """16 位 hex UUID，全库唯一。uuid4().hex[:16] 仍能保证 64bit 熵。"""
    return uuid.uuid4().hex[:16]


def _table_has(conn: sqlite3.Connection, table: str) -> bool:
    """表是否存在。"""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _add_columns_idempotent(conn: sqlite3.Connection, table: str,
                            columns: List[Tuple[str, str]]) -> List[str]:
    """幂等 ADD COLUMN，返回本轮新增的列名。"""
    if not _table_has(conn, table):
        return []
    existing = _table_columns(conn, table)
    added: List[str] = []
    for col, typedef in columns:
        if col in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
        added.append(col)
    return added


def _create_unique_index(conn: sqlite3.Connection, table: str, col: str) -> str:
    idx_name = f"uniq_{table}_{col}"
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON {table}({col})"
    )
    return idx_name


def _create_index(conn: sqlite3.Connection, table: str, col: str) -> str:
    idx_name = f"idx_{table}_{col}"
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({col})"
    )
    return idx_name


def ensure_schema(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """幂等地建 alerts 表 + 给 5 链路表加 trade_uuid / quarantine 列。

    Returns:
        {"added_columns": [...], "created_tables": [...], "created_indexes": [...]}
    """
    own_conn = conn is None
    if own_conn:
        conn = _db.get_connection()
    try:
        created_tables: List[str] = []
        if not _table_has(conn, "id_reconcile_alerts"):
            conn.execute(_CREATE_ID_RECONCILE_ALERTS)
            created_tables.append("id_reconcile_alerts")
        conn.execute(_CREATE_IDX_RECONCILE_ALERTS_TS)
        conn.execute(_CREATE_IDX_RECONCILE_ALERTS_UUID)

        added: List[str] = []
        added += [f"ai_decisions.{c}" for c in _add_columns_idempotent(
            conn, "ai_decisions", _AI_DECISIONS_ID_COLUMNS)]
        added += [f"trade_reflections_v2.{c}" for c in _add_columns_idempotent(
            conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_ID_COLUMNS)]
        for t in ("structured_reflections", "decision_errors", "quick_sim_trades"):
            added += [f"{t}.{c}" for c in _add_columns_idempotent(
                conn, t, _OTHER_ID_COLUMNS)]

        created_indexes: List[str] = []
        if _table_has(conn, "ai_decisions") and "trade_uuid" in _table_columns(conn, "ai_decisions"):
            created_indexes.append(_create_unique_index(conn, "ai_decisions", "trade_uuid"))
        for t in ("trade_reflections_v2", "structured_reflections",
                  "decision_errors", "quick_sim_trades"):
            if _table_has(conn, t) and "trade_uuid" in _table_columns(conn, t):
                created_indexes.append(_create_index(conn, t, "trade_uuid"))

        conn.commit()
        return {
            "added_columns": added,
            "created_tables": created_tables,
            "created_indexes": created_indexes,
        }
    finally:
        if own_conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers: alerts / quarantine / time diff
# ─────────────────────────────────────────────────────────────────────────────

def _write_alert(conn: sqlite3.Connection, *, alert_type: str, severity: str,
                 trade_uuid: Optional[str], reflection_id: Optional[int],
                 decision_id: Optional[int], source_table: str,
                 payload: Dict[str, Any]) -> str:
    """写 alerts 行，返回新 alert_id。"""
    alert_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO id_reconcile_alerts "
        "(alert_id, alert_type, severity, trade_uuid, reflection_id, decision_id, "
        " source_table, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            alert_id, alert_type, severity, trade_uuid, reflection_id, decision_id,
            source_table, json.dumps(payload, ensure_ascii=False), _now_iso(),
        ),
    )
    return alert_id


def _quarantine_reflection(conn: sqlite3.Connection, reflection_id: int,
                            decision_id: int, reason: str) -> None:
    """将一条 trade_reflections_v2 记录标记 quarantine（物理保留数据，逻辑过滤）。"""
    conn.execute(
        "UPDATE trade_reflections_v2 SET quarantined_at = ?, quarantine_reason = ? "
        "WHERE id = ?",
        (_now_iso(), reason[:500], reflection_id),
    )


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    raw = s[:19] if "." not in s[:19] else s.split(".")[0]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, f)
        except ValueError:
            continue
    return None


def _time_diff_seconds(a: Optional[str], b: Optional[str]) -> Optional[float]:
    pa, pb = _parse_iso(a), _parse_iso(b)
    if not pa or not pb:
        return None
    return abs((pa - pb).total_seconds())


# ─────────────────────────────────────────────────────────────────────────────
# Backfill UUID（双保险 #1：内嵌对账）
# ─────────────────────────────────────────────────────────────────────────────

def backfill_uuids(conn: Optional[sqlite3.Connection] = None,
                    *, run_orphan_check: bool = True) -> Dict[str, Any]:
    """为 5 链路表生成 trade_uuid；orphan reflection 立刻 quarantine + alert。

    双保险 #1: backfill 后立即跑三字段对账（run_orphan_check=True）。
    """
    own_conn = conn is None
    if own_conn:
        conn = _db.get_connection()
    try:
        result: Dict[str, Any] = {
            "ai_decisions_added": 0, "reflections_added": 0,
            "structured_added": 0, "errors_added": 0, "quick_sim_added": 0,
            "orphan_quarantined": 0, "alerts_written": 0, "mismatches_found": 0,
        }
        # ai_decisions 主键
        rows = conn.execute(
            "SELECT id FROM ai_decisions "
            "WHERE trade_uuid IS NULL OR trade_uuid = '' ORDER BY id ASC"
        ).fetchall()
        for (did,) in rows:
            nu = _gen_trade_uuid()
            try:
                conn.execute(
                    "UPDATE ai_decisions SET trade_uuid=? WHERE id=? "
                    "AND (trade_uuid IS NULL OR trade_uuid='')",
                    (nu, did))
                if conn.execute("SELECT changes()").fetchone()[0]:
                    result["ai_decisions_added"] += 1
            except sqlite3.IntegrityError:
                nu = _gen_trade_uuid()
                conn.execute(
                    "UPDATE ai_decisions SET trade_uuid=? WHERE id=? "
                    "AND (trade_uuid IS NULL OR trade_uuid='')",
                    (nu, did))
                result["ai_decisions_added"] += 1
        return _bf_phase2(conn, result, run_orphan_check)
    finally:
        if own_conn:
            conn.close()


def _bf_phase2(conn, result, run_orphan_check):
    """trade_reflections_v2 + 其它三表 + 双保险对账。"""
    decision_uuid_map: Dict[int, str] = dict(conn.execute(
        "SELECT id, trade_uuid FROM ai_decisions WHERE trade_uuid IS NOT NULL"
    ).fetchall())
    # trade_reflections_v2：通过 decision_id 同步；orphan 立即 quarantine
    refs = conn.execute(
        "SELECT id, decision_id, quarantined_at FROM trade_reflections_v2"
    ).fetchall()
    for (ref_id, did, already_q) in refs:
        if not did:
            continue
        tu = decision_uuid_map.get(did)
        if not tu:
            if already_q:
                continue
            _quarantine_reflection(conn, ref_id, did,
                reason=f"orphan_reflection:decision_id={did} 不在 ai_decisions")
            _write_alert(conn, alert_type="orphan_reflection", severity="critical",
                trade_uuid=None, reflection_id=ref_id, decision_id=did,
                source_table="trade_reflections_v2",
                payload={"reason": "decision_id_missing_in_ai_decisions"})
            result["orphan_quarantined"] += 1
            result["alerts_written"] += 1
            continue
        cur = conn.execute(
            "SELECT trade_uuid FROM trade_reflections_v2 WHERE id=?",
            (ref_id,)).fetchone()
        if cur and not cur[0]:
            conn.execute(
                "UPDATE trade_reflections_v2 SET trade_uuid=? WHERE id=?",
                (tu, ref_id))
            result["reflections_added"] += 1
    # structured / errors / quick_sim：通过 decision_col 同步
    for tbl, key in (
        ("structured_reflections", "structured_added"),
        ("decision_errors",        "errors_added"),
        ("quick_sim_trades",       "quick_sim_added"),
    ):
        if not _table_has(conn, tbl):
            continue
        cols = _table_columns(conn, tbl)
        dcol = next((c for c in ("decision_id","ref_id","ai_decision_id")
                     if c in cols), None)
        if not dcol:
            continue
        rows = conn.execute(
            f"SELECT id, {dcol}, trade_uuid FROM {tbl}"
        ).fetchall()
        for (rid, did, cur_uuid) in rows:
            if not did:
                if not cur_uuid:
                    _write_alert(conn, alert_type="missing_uuid", severity="info",
                        trade_uuid=None, reflection_id=None, decision_id=None,
                        source_table=tbl, payload={"row_id": rid,
                                                   "reason": "no decision_id link"})
                    result["alerts_written"] += 1
                continue
            tu = decision_uuid_map.get(did)
            if tu and not cur_uuid:
                conn.execute(f"UPDATE {tbl} SET trade_uuid=? WHERE id=?", (tu, rid))
                result[key] += 1
            elif not tu and not cur_uuid:
                _write_alert(conn, alert_type="missing_uuid", severity="warn",
                    trade_uuid=None, reflection_id=None, decision_id=did,
                    source_table=tbl, payload={"row_id": rid,
                                               "reason": "decision_id_orphan"})
                result["alerts_written"] += 1
    conn.commit()
    # 双保险 #1: backfill 完立即跑三字段对账
    if run_orphan_check:
        mism = check_mismatches(conn, auto_quarantine=AUTO_QUARANTINE_ON_MISMATCH)
        result["mismatches_found"] = mism.get("mismatches_found", 0)
        result["alerts_written"] += mism.get("alerts_written", 0)
        result["orphan_quarantined"] += mism.get("quarantined", 0)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 三字段对账 + 应急隔离
# ─────────────────────────────────────────────────────────────────────────────

def check_mismatches(conn: Optional[sqlite3.Connection] = None,
                      *, auto_quarantine: bool = True) -> Dict[str, Any]:
    """三字段对账（成交时间 + 合约 + 价格）。

    JOIN: trade_reflections_v2 JOIN ai_decisions ON trade_uuid
    维度：
    - 时间差 > 60s        → mismatch
    - target_asset != asset → mismatch
    - |entry_price - ai.entry_price| / ai.entry_price > 0.5% → mismatch

    任一不一致即 mismatch：写 alert + 临时从 review 库移除（quarantine）。
    """
    own_conn = conn is None
    if own_conn:
        conn = _db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.decision_id, r.trade_uuid, r.asset, r.entry_price,
                   r.settled_at, r.quarantined_at,
                   a.target_asset, a.entry_price AS a_entry_price, a.entry_time,
                   a.forward_pnl
            FROM trade_reflections_v2 r
            INNER JOIN ai_decisions a ON a.trade_uuid = r.trade_uuid
            WHERE r.trade_uuid IS NOT NULL
              AND (r.quarantined_at IS NULL OR r.quarantined_at = '')
            """
        ).fetchall()

        details: List[Dict[str, Any]] = []
        mismatches = 0
        quarantined = 0
        alerts_written = 0

        for row in rows:
            (ref_id, did, tu, asset, entry_price, settled_at, _already,
             target_asset, a_entry_price, a_entry_time, _pnl) = row
            issues: List[str] = []
            # 1. 合约
            if (asset or "").strip() != (target_asset or "").strip():
                issues.append(f"asset:r={asset!r} vs a={target_asset!r}")
            # 2. 价格
            if entry_price and a_entry_price and a_entry_price > 0:
                diff_pct = abs(entry_price - a_entry_price) / a_entry_price
                if diff_pct > PRICE_TOLERANCE_PCT:
                    issues.append(
                        f"price:r={entry_price} vs a={a_entry_price} diff={diff_pct:.4%}")
            # 3. 成交时间（r.settled_at vs a.entry_time）
            td = _time_diff_seconds(settled_at, a_entry_time)
            if td is not None and td > TIME_TOLERANCE_SEC:
                issues.append(f"time:r={settled_at} vs a={a_entry_time} dt={td:.0f}s")

            if not issues:
                continue
            mismatches += 1
            details.append({
                "reflection_id": ref_id,
                "decision_id": did,
                "trade_uuid": tu,
                "issues": issues,
            })
            # 写 mismatch_detail
            conn.execute(
                "UPDATE trade_reflections_v2 SET mismatch_detail=? WHERE id=?",
                (json.dumps(issues, ensure_ascii=False), ref_id))
            if auto_quarantine:
                reason = f"mismatch:{' | '.join(issues)[:480]}"
                _quarantine_reflection(conn, ref_id, did, reason)
                quarantined += 1
            _write_alert(conn, alert_type="mismatch", severity="critical",
                trade_uuid=tu, reflection_id=ref_id, decision_id=did,
                source_table="trade_reflections_v2",
                payload={"issues": issues, "auto_quarantine": auto_quarantine})
            alerts_written += 1

        conn.commit()
        return {
            "checked": len(rows),
            "mismatches_found": mismatches,
            "quarantined": quarantined,
            "alerts_written": alerts_written,
            "details": details[:20],
        }
    finally:
        if own_conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 双重校验解锁
# ─────────────────────────────────────────────────────────────────────────────

def resolve(trade_uuid: str, *, note: str = "", resolver: str = "manual",
            auto_recheck: bool = True) -> Dict[str, Any]:
    """人工解锁一条 quarantine 记录。

    1. 重新跑对账（auto_recheck=True 时强制要求 PASS）
    2. PASS → 清除 quarantined_at；FAIL → 拒绝解锁并返回原因
    3. 写 resolved 类型 alert
    """
    conn = _db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.decision_id, r.asset, r.entry_price, r.settled_at,
                   a.target_asset, a.entry_price AS a_entry_price, a.entry_time
            FROM trade_reflections_v2 r
            INNER JOIN ai_decisions a ON a.trade_uuid = r.trade_uuid
            WHERE r.trade_uuid = ?
            """,
            (trade_uuid,),
        ).fetchall()
        issues_remaining: List[str] = []
        for row in rows:
            (ref_id, _did, asset, entry_price, settled_at,
             target_asset, a_entry_price, a_entry_time) = row
            if (asset or "").strip() != (target_asset or "").strip():
                issues_remaining.append(f"ref_id={ref_id} asset mismatch")
            if entry_price and a_entry_price and a_entry_price > 0:
                if abs(entry_price - a_entry_price) / a_entry_price > PRICE_TOLERANCE_PCT:
                    issues_remaining.append(f"ref_id={ref_id} price mismatch")
            td = _time_diff_seconds(settled_at, a_entry_time)
            if td is not None and td > TIME_TOLERANCE_SEC:
                issues_remaining.append(f"ref_id={ref_id} time mismatch ({td:.0f}s)")

        recheck_passed = (len(issues_remaining) == 0)

        if not auto_recheck or recheck_passed:
            conn.execute(
                "UPDATE trade_reflections_v2 SET quarantined_at=NULL, "
                "quarantine_reason='', mismatch_detail='' WHERE trade_uuid=?",
                (trade_uuid,))
            unlocked_count = conn.execute("SELECT changes()").fetchone()[0]
            alert_id = _write_alert(
                conn, alert_type="resolved", severity="info",
                trade_uuid=trade_uuid, reflection_id=None, decision_id=None,
                source_table="trade_reflections_v2",
                payload={"recheck_passed": recheck_passed,
                         "issues_remaining": issues_remaining,
                         "note": note, "resolver": resolver,
                         "unlocked_count": unlocked_count})
            conn.execute(
                "UPDATE id_reconcile_alerts SET acknowledged_at=?, "
                "acknowledged_by=?, resolved_at=?, resolution_note=? "
                "WHERE alert_id=?",
                (_now_iso(), resolver, _now_iso(), note[:500], alert_id))
            conn.commit()
            return {"ok": True, "trade_uuid": trade_uuid,
                    "recheck_passed": recheck_passed, "issues_remaining": [],
                    "alert_id": alert_id, "unlocked_count": unlocked_count}
        else:
            conn.commit()
            return {"ok": False, "trade_uuid": trade_uuid,
                    "recheck_passed": False, "issues_remaining": issues_remaining,
                    "alert_id": None, "unlocked_count": 0}
    finally:
        conn.close()


def list_alerts(*, limit: int = 100, alert_type: Optional[str] = None,
               unresolved_only: bool = False) -> List[Dict[str, Any]]:
    """查询 alerts；默认最近 100 条。"""
    conn = _db.get_connection()
    try:
        sql = "SELECT * FROM id_reconcile_alerts"
        params: List[Any] = []
        where: List[str] = []
        if alert_type:
            where.append("alert_type = ?")
            params.append(alert_type)
        if unresolved_only:
            where.append("resolved_at IS NULL AND alert_type != 'resolved'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        # PRAGMA table_info 列顺序: cid(0), name(1), type(2), notnull(3),
        # dflt_value(4), pk(5) — 必须取 d[1] 才是列名。
        cols = [d[1] for d in conn.execute(
            "PRAGMA table_info(id_reconcile_alerts)").fetchall()]
        out: List[Dict[str, Any]] = []
        for row in rows:
            entry = dict(zip(cols, row))
            try:
                entry["payload"] = json.loads(entry.get("payload") or "{}")
            except Exception:
                pass
            out.append(entry)
        return out
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="trade_uuid 全链路对账 + 应急隔离（v170-P0+）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mig = sub.add_parser("migrate", help="建 alerts 表 + 加 trade_uuid / quarantine 列")
    p_mig.set_defaults(fn=lambda _a: print(json.dumps(
        ensure_schema(), ensure_ascii=False, indent=2)))

    p_bf = sub.add_parser("backfill", help="backfill trade_uuid（内嵌对账）")
    p_bf.add_argument("--no-orphan-check", action="store_true",
                      help="跳过内嵌对账（不推荐）")
    p_bf.set_defaults(fn=lambda a: print(json.dumps(
        backfill_uuids(run_orphan_check=not a.no_orphan_check),
        ensure_ascii=False, indent=2)))

    p_ck = sub.add_parser("check", help="三字段对账 + 应急 quarantine")
    p_ck.add_argument("--no-auto-quarantine", action="store_true",
                      help="只检查不 quarantine")
    p_ck.set_defaults(fn=lambda a: print(json.dumps(
        check_mismatches(auto_quarantine=not a.no_auto_quarantine),
        ensure_ascii=False, indent=2)))

    p_rv = sub.add_parser("resolve", help="人工解锁 + 双重校验")
    p_rv.add_argument("trade_uuid", help="目标 trade_uuid")
    p_rv.add_argument("--note", default="", help="修正说明")
    p_rv.add_argument("--resolver", default="manual", help="操作人")
    p_rv.add_argument("--skip-recheck", action="store_true",
                      help="跳过二次校验（不推荐）")
    p_rv.set_defaults(fn=lambda a: print(json.dumps(
        resolve(a.trade_uuid, note=a.note, resolver=a.resolver,
                auto_recheck=not a.skip_recheck),
        ensure_ascii=False, indent=2)))

    p_al = sub.add_parser("alerts", help="列告警")
    p_al.add_argument("--limit", type=int, default=50)
    p_al.add_argument("--type", default=None,
                      choices=["orphan_reflection", "mismatch",
                               "missing_uuid", "resolved"])
    p_al.add_argument("--unresolved-only", action="store_true")
    p_al.set_defaults(fn=lambda a: print(json.dumps(
        list_alerts(limit=a.limit, alert_type=a.type,
                    unresolved_only=a.unresolved_only),
        ensure_ascii=False, indent=2)))

    args = parser.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(cli())