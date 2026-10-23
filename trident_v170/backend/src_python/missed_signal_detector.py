#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v188 missed_signal 自动检测器 —— 给 ai_decisions.missed_signal 字段打标。

旧设计: ai_decisions.missed_signal 字段已存在(10 行人工预填=1),但全 backend 没有任何
写入路径。本模块提供自动检测逻辑,接入 paper_reflection_worker + gate_reflection_worker
在结算时调用。

missed_signal = 1 表示:
  "行情有显著异动(>= 2.5% 或 vol > 1.5x),但 ai_decisions.suggested_action = HOLD
   (即:本应有 BUY/SELL 决策,但 AI 错过了)"

检测窗口:
  - 异动点 ts 后 [-300s, +300s] 的 ai_decisions (asset 匹配)
  - 该决策 suggested_action=HOLD 且 abs(forward_pnl) > 0.005 (异动兑现了)
  - 标记 missed_signal=1

数据流:
  1. paper_reflection_worker.daemon_loop 跑完一轮后调用 detect_for_recent()
  2. gate_reflection_worker.run_once(asset) 同步调用一遍
  3. 手动 /api/v188/missed-signal/detect 触发
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

import db


WINDOW_S = 300
MIN_PNL = 0.005
ABN_PCT_THRESHOLD = 0.025
ABN_VOL_THRESHOLD = 1.5


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _detect_recent_abnormals(asset: str) -> List[Dict[str, Any]]:
    """读 abnormal_signals 取最近异动点(复用 api_server._detect_5m_abnormals)。"""
    try:
        from api_server import _detect_5m_abnormals
        return _detect_5m_abnormals(asset) or []
    except Exception as exc:
        print(f"[MISSED-SIG] 拿 abnormals 失败 asset={asset}: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return []


def detect_for_decision(decision_id: int) -> Optional[int]:
    """对单条决策检测 missed_signal。返回 1/0 或 None(数据不足)。"""
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT id, target_asset, suggested_action, settled, forward_pnl,
                   no_trade_reason, created_at, ts
              FROM ai_decisions WHERE id = ?
        """, (decision_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        # v188 R1-fix: 漏检定义 = AI HOLD + 同时段异常事件，跟"已结算"无关。
        # 旧逻辑 `not d.get("settled")` 会把全部 HOLD 决策拒掉（quick_sim.settle_trades
        # 只结算 BUY/SELL，HOLD 永远 settled=0），导致扫描器 SELECT 永远空集、scan_state
        # last_marked 永远 0、反思系统"漏检发现"模块完全哑火。
        if d.get("suggested_action") != "HOLD":
            return 0
        # v188 R1-fix-ext: 漏检判定不需要 forward_pnl。
        # HOLD 决策不进场，quick_sim 永远不写 forward_pnl（89 条决策里 74 条 HOLD
        # 全部 pnl=NULL），旧检查 `abs(pnl) < MIN_PNL` 会把全部 HOLD 拒掉。
        # 异常事件本身就是"错过机会"的证据，不再要求事后兑现。
        ts = d.get("ts") or 0
        if not ts:
            try:
                ts = int(time.mktime(time.strptime(
                    (d.get("created_at") or "")[:19],
                    "%Y-%m-%dT%H:%M:%S")))
            except Exception:
                return 0
        asset = (d.get("target_asset") or "").upper()
        if not asset:
            return 0
        abnormals = _detect_recent_abnormals(asset)
        for a in abnormals:
            ats = int(a.get("ts") or 0)
            if abs(ats - ts) <= WINDOW_S:
                pct = abs(a.get("pct_change") or 0)
                ratio = abs(a.get("vol_ratio") or 0)
                if pct >= ABN_PCT_THRESHOLD or ratio >= ABN_VOL_THRESHOLD:
                    return 1
        return 0
    except Exception as exc:
        print(f"[MISSED-SIG] detect_for_decision {decision_id} 失败: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        conn.close()


def detect_for_recent(asset: Optional[str] = None,
                      window_hours: int = 24, limit: int = 200,
                      incremental: bool = True) -> Dict[str, Any]:
    """批量检测: 扫最近 window_hours 内某品种(或全品种)已 settle 的 HOLD 决策。

    v188 P1-4: 增量扫描 - 把 last_decision_id 存到 missed_signal_scan_state
    (按 asset 主键), 后续只扫 id > since_id 的新行。
    把 11s/50 -> <1s/50 (大多数时候 0s)。
    """
    asset_filter = (asset or "").upper()
    conn = _connect()
    stats = {"scanned": 0, "marked": 0, "details": [], "incremental": incremental}
    try:
        # 确保状态表存在(开发期自动建表)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS missed_signal_scan_state (
                asset TEXT PRIMARY KEY,
                last_decision_id INTEGER NOT NULL DEFAULT 0,
                last_scan_ts INTEGER NOT NULL DEFAULT 0,
                last_scan_count INTEGER NOT NULL DEFAULT 0,
                last_marked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        since_id = 0
        if incremental and asset_filter:
            row = conn.execute(
                "SELECT last_decision_id FROM missed_signal_scan_state WHERE asset=?",
                (asset_filter,),
            ).fetchone()
            if row:
                since_id = int(row[0] or 0)
        if incremental and since_id > 0:
            sql = """
                SELECT id, target_asset, suggested_action, forward_pnl,
                       no_trade_reason, settled, created_at, ts
                  FROM ai_decisions
                 WHERE id > ?
                   AND suggested_action = 'HOLD'
                   AND (missed_signal = 0 OR missed_signal IS NULL)
            """
            params: list = [since_id]
            if asset_filter:
                sql += " AND target_asset = ?"
                params.append(asset_filter)
            sql += " ORDER BY id ASC LIMIT ?"
            params.append(max(1, min(limit, 1000)))
        else:
            cutoff_ts = int(time.time()) - window_hours * 3600
            sql = """
                SELECT id, target_asset, suggested_action, forward_pnl,
                       no_trade_reason, settled, created_at, ts
                  FROM ai_decisions
                 WHERE suggested_action = 'HOLD'
                   AND (missed_signal = 0 OR missed_signal IS NULL)
                   AND (ts IS NULL OR ts >= ?)
            """
            params = [cutoff_ts]
            if asset_filter:
                sql += " AND target_asset = ?"
                params.append(asset_filter)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, min(limit, 1000)))
        rows = conn.execute(sql, params).fetchall()
        max_id_seen = since_id
        for r in rows:
            stats["scanned"] += 1
            max_id_seen = max(max_id_seen, int(r["id"]))
            missed = detect_for_decision(int(r["id"]))
            if missed:
                stats["marked"] += 1
                stats["details"].append({
                    "decision_id": int(r["id"]),
                    "asset": r["target_asset"],
                    "forward_pnl": r["forward_pnl"],
                    "no_trade_reason": r["no_trade_reason"],
                })
                conn.execute(
                    "UPDATE ai_decisions SET missed_signal=1 WHERE id=?",
                    (int(r["id"]),),
                )
        if asset_filter and max_id_seen > 0:
            try:
                conn.execute("""
                    INSERT INTO missed_signal_scan_state
                        (asset, last_decision_id, last_scan_ts, last_scan_count, last_marked, updated_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(asset) DO UPDATE SET
                        last_decision_id = excluded.last_decision_id,
                        last_scan_ts = excluded.last_scan_ts,
                        last_scan_count = excluded.last_scan_count,
                        last_marked = excluded.last_marked,
                        updated_at = datetime('now')
                """, (asset_filter, max_id_seen, int(time.time()),
                      stats["scanned"], stats["marked"]))
            except Exception as _e:
                print(f"[MISSED-SIG] update scan_state failed: {_e}", flush=True)
        elif asset_filter:
            # v176.7：无任何 settled HOLD 时也写一条"已扫过"基线记录，
            # 否则扫描状态表在数据稀疏时长期空、面板误以为未跑。
            try:
                conn.execute("""
                    INSERT INTO missed_signal_scan_state
                        (asset, last_decision_id, last_scan_ts, last_scan_count, last_marked, updated_at)
                    VALUES (?, 0, ?, 0, 0, datetime('now'))
                    ON CONFLICT(asset) DO UPDATE SET
                        last_scan_ts = excluded.last_scan_ts,
                        updated_at = datetime('now')
                """, (asset_filter, int(time.time())))
            except Exception as _e:
                print(f"[MISSED-SIG] update scan_state (empty) failed: {_e}", flush=True)
        conn.commit()
    except Exception as exc:
        print(f"[MISSED-SIG] detect_for_recent 失败: {type(exc).__name__}: {exc}", flush=True)
    finally:
        conn.close()
    return stats


def reset_scan_state(asset: Optional[str] = None) -> Dict[str, Any]:
    """v188 P1-4: 重置扫描进度(强制下次全扫)。首次部署或数据回填用。"""
    conn = _connect()
    out = {"deleted": 0}
    try:
        if asset:
            asset = asset.upper()
            cur = conn.execute(
                "DELETE FROM missed_signal_scan_state WHERE asset=?",
                (asset,),
            )
            out["deleted"] = cur.rowcount
        else:
            cur = conn.execute("DELETE FROM missed_signal_scan_state")
            out["deleted"] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return out


def stats_for_asset(asset: str, days: int = 7) -> Dict[str, Any]:
    """某品种最近 days 天 missed_signal 统计 + 决策对照。"""
    asset = (asset or "").upper()
    cutoff_ts = int(time.time()) - max(1, days) * 86400
    conn = _connect()
    out: Dict[str, Any] = {
        "asset": asset, "days": days,
        "n_total": 0,
        "n_total_settled": 0,
        "n_hold": 0, "n_missed": 0,
        "missed_rate": None, "missed_decisions": [],
    }
    try:
        # v300-audit: 原查询硬性要求 settled = 1，而全库 1971 条 ai_decisions 里
        # 只有 1 条 settled → /api/v188/missed-signal/{asset} 永远返回全 0。
        # 改为统计窗口内全部决策，settled 只作为子集指标单列。
        rows = conn.execute("""
            SELECT id, forward_pnl, no_trade_reason, missed_signal, settled, created_at
              FROM ai_decisions
             WHERE target_asset = ?
               AND (ts IS NULL OR ts >= ?)
        """, (asset, cutoff_ts)).fetchall()
        out["n_total"] = len(rows)
        out["n_total_settled"] = sum(1 for r in rows if int(r["settled"] or 0) == 1)
        for r in rows:
            out["n_hold"] += 1
            if int(r["missed_signal"] or 0) == 1:
                out["n_missed"] += 1
                out["missed_decisions"].append({
                    "decision_id": int(r["id"]),
                    "forward_pnl": r["forward_pnl"],
                    "no_trade_reason": r["no_trade_reason"],
                    "settled": int(r["settled"] or 0),
                    "created_at": r["created_at"],
                })
        if out["n_hold"] > 0:
            out["missed_rate"] = round(out["n_missed"] / out["n_hold"] * 100, 1)
    finally:
        conn.close()
    return out


__all__ = ["detect_for_decision", "detect_for_recent", "reset_scan_state", "stats_for_asset"]