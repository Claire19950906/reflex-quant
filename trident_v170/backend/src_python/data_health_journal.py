# -*- coding: utf-8 -*-
"""data_health_journal.py - P2 多源数据 fallback 健康日志

每一次按 feed_redund_policy 走多源尝试都会被记入 feed_health 表：
  asset / feed_kind / source / status / latency_ms / error / value / ts

API:
  record_attempt(...)  ← 在 ai_worker / ingest / cross_asset_snapshot 调用 fallback 时记录
  summary(asset=None, days=7)  ← 仪表盘聚合 — 健康率/最常用源/最近失败
"""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Dict, List, Optional

# status 取值
STATUS_SUCCESS = "success"   # 数据正常返回 + 在合理区间
STATUS_DEGRADED = "degraded"  # 返回但跨源差异 > EXOG_MAX_PRICE_DRIFT_PCT 或无 cross-check
STATUS_FAILED = "failed"     # 失败/超时/异常
STATUS_SKIPPED = "skipped"   # fallback 跳过 (前序源已成功)


def ensure_table(conn: sqlite3.Connection) -> None:
    """确保 feed_health 表存在 — 幂等。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feed_health (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            feed_kind TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            latency_ms INTEGER DEFAULT 0,
            value REAL,
            error TEXT,
            ts TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feed_health_asset_ts
            ON feed_health(asset, ts)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feed_health_kind
            ON feed_health(feed_kind)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feed_health_status
            ON feed_health(status, ts)
    """)
    conn.commit()


def record_attempt(asset: str, feed_kind: str, source: str,
                   status: str, latency_ms: int = 0,
                   value: Any = None, error: Optional[str] = None,
                   conn: sqlite3.Connection = None) -> int:
    if conn is None:
        return -1
    ensure_table(conn)
    if status not in (STATUS_SUCCESS, STATUS_DEGRADED,
                      STATUS_FAILED, STATUS_SKIPPED):
        status = STATUS_FAILED
    try:
        value_f = float(value) if value is not None and not isinstance(value, str) else None
    except (TypeError, ValueError):
        value_f = None
    cur = conn.execute(
        """INSERT INTO feed_health
              (asset, feed_kind, source, status, latency_ms, value, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (str(asset or "?").upper(), str(feed_kind or "?").upper(),
         str(source or "?").lower(), status, int(latency_ms or 0),
         value_f, (str(error)[:500] if error else None))
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def summary(asset=None, days=7, conn=None) -> Dict[str, Any]:
    """健康度汇总 - 按 (asset, feed_kind) 分组。"""
    if conn is None:
        return {"days": days, "asset": asset, "by_kind": [], "totals": {}}
    ensure_table(conn)
    conn.row_factory = sqlite3.Row
    days = min(max(int(days or 7), 1), 30)
    where = ["ts >= datetime('now', ?)"]
    params: List[Any] = ["-{} days".format(days)]
    if asset:
        where.append("asset = ?")
        params.append(str(asset).upper())

    rows = conn.execute(
        "SELECT * FROM feed_health WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT 5000",
        params,
    ).fetchall()

    by_key: Dict[str, Dict[str, Any]] = {}
    totals = {"success": 0, "degraded": 0, "failed": 0, "skipped": 0}
    last_seen: Dict[str, str] = {}
    for r in rows:
        d = dict(r)
        key = "{}/{}".format(d["asset"], d["feed_kind"])
        bucket = by_key.setdefault(key, {
            "asset": d["asset"], "feed_kind": d["feed_kind"],
            "attempts": 0, "success": 0, "degraded": 0, "failed": 0,
            "primary_source": None,
            "primary_success_rate": 0.0,
            "last_attempt_ts": d["ts"], "last_status": d["status"],
            "last_error": d.get("error"),
            "primary_attempts": 0, "primary_success": 0,
        })
        bucket["attempts"] += 1
        bucket[d["status"]] = bucket.get(d["status"], 0) + 1
        totals[d["status"]] = totals.get(d["status"], 0) + 1
        # 第一源 = 每个 fallback 链的第 1 个 source
        if d["source"] and (bucket["primary_source"] is None
                            or d["id"] > bucket.get("_last_first_id", 0)):
            bucket["primary_source"] = d["source"]
            bucket["primary_attempts"] += 1
            if d["status"] == STATUS_SUCCESS:
                bucket["primary_success"] += 1
            bucket["_last_first_id"] = d["id"]
        last_seen[key] = d["ts"]

    by_kind = []
    for k, b in by_key.items():
        b.pop("_last_first_id", None)
        if b["primary_attempts"]:
            b["primary_success_rate"] = round(
                b["primary_success"] / b["primary_attempts"] * 100, 1)
        b.pop("primary_attempts", None)
        b.pop("primary_success", None)
        by_kind.append(b)

    # 健康率 = success / (success + degraded + failed)
    denom = totals["success"] + totals["degraded"] + totals["failed"]
    health_rate = round(totals["success"] / denom * 100, 1) if denom else None
    return {"days": days, "asset": (asset or "").upper() or None,
            "by_kind": sorted(by_kind, key=lambda x: -x["attempts"]),
            "totals": totals, "health_rate_pct": health_rate,
            "last_seen": last_seen}