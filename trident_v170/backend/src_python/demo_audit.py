#!/usr/bin/env python3
"""
Trident v300 — 调用审计 + 水印
================================

记录每个 demo/licensed 模式的请求到 SQLite，便于：

  1. **事后追溯**：发现泄露/破解 → 查日志定位是哪批 key/IP 在刷
  2. **异常检测**：单 IP / 单 key 突增 → 提示运营
  3. **水印取证**：每个响应带 ``X-Trident-Watermark`` 头，包含 IP + key-id 哈希，
     被截屏/抓包后仍可识别来源

不依赖任何第三方，纯 SQLite + 内存缓存。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("trident.demo_audit")


AUDIT_DB_PATH = os.getenv(
    "TRIDENT_AUDIT_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "trident_audit.db"),
)


def _conn() -> sqlite3.Connection:
    """打开审计库（首次调用时建表）。"""
    conn = sqlite3.connect(AUDIT_DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            client_ip TEXT,
            api_key_id TEXT,
            method TEXT,
            path TEXT,
            status INTEGER,
            latency_ms REAL,
            watermark TEXT,
            mode TEXT,
            note TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ip ON audit_log(client_ip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_key ON audit_log(api_key_id)")
    conn.commit()
    return conn


def record(method: str, path: str, client_ip: str,
            api_key_id: Optional[str], status: int,
            latency_ms: float, watermark: str, mode: str,
            note: str = "") -> None:
    """记录一次请求（异常内部捕获，不影响业务）。"""
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO audit_log (ts, client_ip, api_key_id, method, path, "
                "status, latency_ms, watermark, mode, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), client_ip, api_key_id, method, path,
                 status, latency_ms, watermark, mode, note),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("audit record 失败: %s", e)


def query_recent(limit: int = 100, client_ip: Optional[str] = None,
                api_key_id: Optional[str] = None) -> list:
    """查最近 N 条审计记录（运维面板用）。"""
    try:
        conn = _conn()
        try:
            sql = "SELECT * FROM audit_log WHERE 1=1"
            params: list = []
            if client_ip:
                sql += " AND client_ip = ?"
                params.append(client_ip)
            if api_key_id:
                sql += " AND api_key_id = ?"
                params.append(api_key_id)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def detect_anomaly(window_sec: int = 3600, top_n: int = 10) -> Dict[str, Any]:
    """简单异常检测：1h 内请求量前 N 的 IP / API Key（突增预警）。"""
    try:
        conn = _conn()
        try:
            since = time.time() - window_sec
            rows = conn.execute("""
                SELECT client_ip, api_key_id, COUNT(*) as cnt
                FROM audit_log
                WHERE ts >= ?
                GROUP BY client_ip, api_key_id
                ORDER BY cnt DESC
                LIMIT ?
            """, (since, top_n)).fetchall()
            return {
                "window_sec": window_sec,
                "top": [dict(r) for r in rows],
            }
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


def purge_old_records(retention_days: int = 30) -> int:
    """P2: 清理超出保留期的 audit_log 记录，防止 SQLite DB 无界增长。

    设计要点：
    - ``retention_days <= 0`` → 跳过（disable cleanup，给运维 opt-out）。
    - 异常内部捕获 — DB 锁 / 磁盘满都不应让审计清理拖死业务请求。
    - 在每条 INSERT 之后再调用；高频调用者可后台跑每 N 小时一次。
    - 返回删除条数便于监控（写入 trident.audit logger info）。
    """
    if retention_days <= 0:
        return 0
    try:
        cutoff = time.time() - (retention_days * 86400)
        conn = _conn()
        try:
            cur = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            conn.commit()
            removed = cur.rowcount
            if removed:
                logger.info(
                    "audit purge: 删除 %d 条 > %d 天前的记录 (cutoff=%.0f)",
                    removed, retention_days, cutoff,
                )
            return removed
        finally:
            conn.close()
    except Exception as e:
        logger.warning("audit purge failed (non-fatal): %s: %s", type(e).__name__, e)
        return 0