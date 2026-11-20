#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v202 段 4 · 因果图谱改进候选（KG Improvements）

与「模拟盘参数调整」（paper-adjustments）严格区分：
本模块专管**分析层**的因果图谱/因果链库的「加边 / 删边 / 改权重 / 启停规则」审批流。

数据流：
  归因复核 / 决策错误码 / 反思系统 -> INSERT kg_improvements(status=pending)
  /review 段 4 页面 -> GET /api/analysis/kg-improvements?status=pending
  人工审批   -> POST /api/analysis/kg-improvements/{id}/approve | reject
  (Phase 2) -> 扫 status=approved 行写回 causal_graph / causal_chains

本文件只暴露 CRUD + stats，**不直接改图谱** —— 避免审批 API 反向影响
正在跑的推理 worker（causal_graph.run_reasoning 会重置边）。
审批只标 status=approved，由独立的 reconcile worker 异步应用。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

import db


# 与 db.py `_KG_PROPOSAL_KINDS` / `_KG_PROPOSAL_STATUSES` 保持同步
_VALID_KINDS = ("add_edge", "remove_edge", "reweight", "enable_rule", "disable_rule")
_VALID_STATUSES = ("pending", "approved", "rejected")


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row):
    d = dict(row)
    raw = d.get("payload") or "{}"
    try:
        d["payload"] = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        d["payload"] = {}
    return d


def list_proposals(status=None, limit=20, conn=None):
    """按状态过滤列出 proposal；默认按 last_seen_at DESC。"""
    own = conn is None
    conn = _connect() if conn is None else conn
    try:
        if conn.row_factory is not sqlite3.Row:
            conn.row_factory = sqlite3.Row
        if status and status in _VALID_STATUSES:
            rows = conn.execute(
                "SELECT * FROM kg_improvements WHERE status=? "
                "ORDER BY last_seen_at DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM kg_improvements ORDER BY last_seen_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def count_by_status(conn=None):
    """返回 {pending, approved, rejected} 三个计数。"""
    own = conn is None
    conn = _connect() if conn is None else conn
    try:
        if conn.row_factory is not sqlite3.Row:
            conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM kg_improvements GROUP BY status"
        ).fetchall()
        out = {s: 0 for s in _VALID_STATUSES}
        for r in rows:
            out[str(r["status"])] = int(r["cnt"])
        return out
    finally:
        if own:
            conn.close()


def get_kg_stats(conn=None):
    """返回前端 kg-stats 全图快照（KG 改进面板顶部 4 张卡片的数据）。"""
    own = conn is None
    conn = _connect() if conn is None else conn
    try:
        if conn.row_factory is not sqlite3.Row:
            conn.row_factory = sqlite3.Row
        # 因果图谱：节点 active=1 才计入；边全部计入（含失效的仍展示）
        graph_edges = int(
            conn.execute("SELECT COUNT(*) AS c FROM asset_driver_edges").fetchone()["c"]
        )
        graph_nodes = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM asset_driver_nodes WHERE active=1"
            ).fetchone()["c"]
        )
        chains_total = int(
            conn.execute("SELECT COUNT(*) AS c FROM causal_chains").fetchone()["c"]
        )
        chains_enabled = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM causal_chains WHERE enabled=1"
            ).fetchone()["c"]
        )
        chains_disabled = chains_total - chains_enabled
        # 最近更新时间：因果链 + 图谱节点 + 边 取 MAX
        row = conn.execute(
            "SELECT MAX(ts) AS ts FROM ("
            "  SELECT updated_at AS ts FROM causal_chains"
            "  UNION ALL SELECT updated_at AS ts FROM asset_driver_nodes"
            "  UNION ALL SELECT updated_at AS ts FROM asset_driver_edges"
            ")"
        ).fetchone()
        last_update_at = row["ts"] if row and row["ts"] else None
        cnt = count_by_status(conn)
        return {
            "graph_edges": graph_edges,
            "graph_nodes": graph_nodes,
            "chains_total": chains_total,
            "chains_enabled": chains_enabled,
            "chains_disabled": chains_disabled,
            "last_update_at": last_update_at,
            "pending_proposals": cnt.get("pending", 0),
            "approved_proposals": cnt.get("approved", 0),
        }
    finally:
        if own:
            conn.close()


def decide_proposal(proposal_id, decision, note="", conn=None):
    """审批一条 proposal。decision: approve | reject。

    本版本**不直接写图谱**，仅标记 status + resolved_at + note。
    真正写回 causal_graph / causal_chains 由独立的 reconcile worker
    （v203+ 计划）扫 status=approved 行异步应用。
    """
    if decision not in ("approve", "reject"):
        return {"ok": False, "error": "decision 必须是 approve 或 reject"}
    target_status = "approved" if decision == "approve" else "rejected"
    own = conn is None
    conn = _connect() if conn is None else conn
    try:
        if conn.row_factory is not sqlite3.Row:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM kg_improvements WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"proposal {proposal_id} 不存在"}
        if row["status"] != "pending":
            return {
                "ok": False,
                "error": f"proposal {proposal_id} 已被处理 (status={row['status']})",
            }
        audit_note = (note or "").strip() or (
            f"approved by review panel - kind={row['kind']} target={row['target']}"
            if decision == "approve"
            else f"rejected by review panel - kind={row['kind']} target={row['target']}"
        )
        cur = conn.execute(
            "UPDATE kg_improvements SET status=?,"
            " resolved_at=datetime('now','localtime'), note=?"
            " WHERE id=? AND status='pending'",
            (target_status, audit_note[:500], int(proposal_id)),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "审批失败(可能已被并发处理)"}
        return {
            "ok": True,
            "id": int(proposal_id),
            "status": target_status,
            "kind": row["kind"],
            "target": row["target"],
        }
    finally:
        if own:
            conn.close()


def add_proposal(kind, target, *, reason="", confidence=0.5, sample_size=0,
                 impact="", payload=None, source="auto", conn=None):
    """新建一条改进候选(供归因复核 / 错误码分析器调用，非 API 入口)。"""
    if kind not in _VALID_KINDS:
        return {"ok": False, "error": f"kind 必须是 {_VALID_KINDS} 之一"}
    target = (target or "").strip()
    if not target:
        return {"ok": False, "error": "target 不能为空"}
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    payload_str = json.dumps(payload or {}, ensure_ascii=False)
    own = conn is None
    conn = _connect() if conn is None else conn
    try:
        cur = conn.execute(
            "INSERT INTO kg_improvements"
            " (kind, target, reason, confidence, sample_size, impact, payload, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, target[:200], (reason or "")[:1000], conf,
             int(sample_size), (impact or "")[:500], payload_str[:2000], source),
        )
        conn.commit()
        return {"ok": True, "id": int(cur.lastrowid)}
    finally:
        if own:
            conn.close()




def list_approved_unapplied(batch_max=100, conn=None):
    """v302+ reconcile worker: 列出 status=approved 但尚未应用（applied_at IS NULL）的行。

    自动跳过 apply_error 非空的行（之前已经尝试过一次, 失败原因非临时性,
    比如 legacy 'trade:N asset:X' 格式不可解析; 再次重试只会浪费 cron 时间）。
    """
    own = conn is None
    conn = _connect() if conn is None else conn
    try:
        if conn.row_factory is not sqlite3.Row:
            conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM kg_improvements WHERE status='approved' "
            "AND applied_at IS NULL AND (apply_error IS NULL OR apply_error='') "
            "ORDER BY last_seen_at ASC LIMIT ?",
            (int(batch_max),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def mark_applied(proposal_id: int, applied_target: str, conn=None) -> Dict[str, Any]:
    """v302+ reconcile worker: 标记 proposal 已应用，写入 applied_at + 应用位置备注。

    Args:
        proposal_id: kg_improvements.id
        applied_target: 实际写入的位置描述（去重用：避免重边/重规则）。如：
          - 'asset_driver_edges#42#58'
          - 'causal_chains#123'
          - 'reflection_checkers:E020'

    Returns: {ok, id, applied_at, applied_target}
    """
    own = conn is None
    conn = _connect() if conn is None else conn
    try:
        if conn.row_factory is not sqlite3.Row:
            conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "UPDATE kg_improvements SET applied_at=datetime('now','localtime'), "
            "apply_error='', note = note || char(10) || '[applied] ' || ? "
            "WHERE id=? AND status='approved'",
            (str(applied_target)[:200], int(proposal_id)),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": f"proposal {proposal_id} not in approved state"}
        return {"ok": True, "id": int(proposal_id), "applied_target": str(applied_target)}
    finally:
        if own:
            conn.close()


def mark_apply_failed(proposal_id: int, error_msg: str, conn=None) -> Dict[str, Any]:
    """v302+ reconcile worker: 标记 proposal 应用失败，记录错误信息。

    不改 status，仍为 'approved'（人工下次可重试）。写入 apply_error 列。
    """
    own = conn is None
    conn = _connect() if conn is None else conn
    try:
        if conn.row_factory is not sqlite3.Row:
            conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "UPDATE kg_improvements SET apply_error=? "
            "WHERE id=? AND status='approved'",
            (str(error_msg)[:500], int(proposal_id)),
        )
        conn.commit()
        return {"ok": True, "id": int(proposal_id), "error": str(error_msg)[:500]}
    finally:
        if own:
            conn.close()


def auto_approve_pending(min_confidence=0.85, batch_max=100, dry_run=False):
    """v202 R2-fix: 批量自动审批 kg_improvements 中高置信度的 pending 候选。

    决策规则（双重门）：
      ① confidence >= min_confidence
      ② 同 reason_code 在 attribution_reviews 里出现 >= 1 次（含本提案佐证）

    本函数只更新 status='approved'，不直接写 asset_driver_edges/causal_chains
    —— 这些由后续 reconcile worker (v203) 扫 status=approved 异步应用。
    """
    conn = _connect()
    try:
        if conn.row_factory is not sqlite3.Row:
            conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, kind, target, reason, confidence, payload "
            "FROM kg_improvements WHERE status='pending' "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (int(batch_max),),
        ).fetchall()
        approved_ids = []
        skipped = 0
        for r in rows:
            try:
                conf = float(r["confidence"] or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < min_confidence:
                skipped += 1
                continue
            try:
                payload = json.loads(r["payload"] or "{}") if isinstance(r["payload"], str) else (r["payload"] or {})
            except (ValueError, TypeError):
                payload = {}
            rc = str(payload.get("reason_code") or "")
            trade_id = None
            target = r["target"]
            if target.startswith("trade:"):
                parts = target.split()
                if parts and ":" in parts[0]:
                    trade_id = parts[0].split(":", 1)[1]
            try:
                # v202 R2-fix-ext: 放宽频次校验。kg_improvements.payload.reason_code
                # 与 attribution_reviews.reason_code 经常不一致（review_agent
                # 在不同 sub-layer 用不同 code），强匹配会把有效提案误跳过。
                # 改为只校 trade_id 是否在归因表出现 + 是否 LOSS 标记。
                if trade_id:
                    row = conn.execute(
                        "SELECT COUNT(*) AS c FROM attribution_reviews "
                        "WHERE trade_id=? AND verdict='LOSS'",
                        (trade_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) AS c FROM attribution_reviews "
                        "WHERE verdict='LOSS'",
                    ).fetchone()
                support = int(row["c"]) if row else 0
            except Exception:
                support = 0
            if support < 1:
                skipped += 1
                continue
            note = f"auto-approved R2-fix conf={conf:.2f} rc={rc} support={support}"
            if not dry_run:
                cur = conn.execute(
                    "UPDATE kg_improvements SET status='approved',"
                    " resolved_at=datetime('now','localtime'), note=?"
                    " WHERE id=? AND status='pending'",
                    (note[:500], int(r["id"])),
                )
                if cur.rowcount > 0:
                    approved_ids.append(int(r["id"]))
                else:
                    skipped += 1
            else:
                approved_ids.append(int(r["id"]))
        if not dry_run:
            conn.commit()
        return {
            "scanned": len(rows),
            "approved": approved_ids,
            "skipped": skipped,
            "dry_run": bool(dry_run),
            "threshold": min_confidence,
        }
    finally:
        conn.close()
