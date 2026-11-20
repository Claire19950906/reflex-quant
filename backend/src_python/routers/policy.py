# -*- coding: utf-8 -*-
"""策略补丁队列 / 反思→policy 闭环路由（结构切片，2026-11-17）。

从 api_server.py 搬来 5 条端点 + 4 个同步助手 + 2 个请求模型：
  /api/policy/patches（列表）、/{patch_id}/review（人工审 approve/reject）、
  /run（手动跑一轮 reflection_policy_updater）、/api/policy/sibling-rules（兄弟规则差异）、
  /api/admin/insight/promote-now（把洞察立刻推成补丁）。
审阅只允许 pending 态，非 pending 返回 409；逐字保留。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

import license_auth
from pydantic import BaseModel

import db

# v453 安全：这些路由全是「写」操作（重置/归档/备份/策略/凭证），
# full 模式默认要求 API Key；仅回环地址在 TRIDENT_LOCAL_TRUST=1 时免鉴权。
router = APIRouter(dependencies=[Depends(license_auth.require_api_key)])


class _PolicyPatchReviewBody(BaseModel):
    note: str = ""


def _policy_patches_pending_sync(status: Optional[str] = None,
                                 target_module: Optional[str] = None,
                                 limit: int = 100) -> List[Dict[str, Any]]:
    """读取 policy_patch_queue 中 pending/approved/rejected 的补丁 + payload 解析。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        where = ["1=1"]
        params: List[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if target_module:
            where.append("target_module = ?")
            params.append(target_module)
        sql = f"""
            SELECT id, patch_type, target_module, patch_payload, patch_payload_hash,
                   source_kind, source_reflection_ids, source_error_codes,
                   sample_size, cumulative_pnl_impact, confidence_score,
                   rationale, status, created_at, reviewed_at, reviewer_note,
                   applied_at, expires_at, applied_count, last_apply_at
              FROM policy_patch_queue
             WHERE {' AND '.join(where)}
          ORDER BY (CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1
                                WHEN 'applied' THEN 2 WHEN 'rejected' THEN 3
                                WHEN 'expired' THEN 4 ELSE 5 END),
                   created_at DESC
             LIMIT ?
        """
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r["patch_payload"]) if r["patch_payload"] else {}
            except Exception:
                payload = {"_raw": r["patch_payload"][:200]}
            out.append({
                "id": int(r["id"]),
                "patch_type": r["patch_type"],
                "target_module": r["target_module"],
                "payload": payload,
                "payload_hash": r["patch_payload_hash"],
                "source_kind": r["source_kind"],
                "source_reflection_ids": json.loads(r["source_reflection_ids"] or "[]"),
                "source_error_codes": json.loads(r["source_error_codes"] or "[]"),
                "sample_size": int(r["sample_size"] or 0),
                "cumulative_pnl_impact": float(r["cumulative_pnl_impact"] or 0.0),
                "confidence_score": float(r["confidence_score"] or 0.0),
                "rationale": r["rationale"] or "",
                "status": r["status"],
                "created_at": int(r["created_at"] or 0),
                "reviewed_at": int(r["reviewed_at"] or 0),
                "reviewer_note": r["reviewer_note"] or "",
                "applied_at": int(r["applied_at"] or 0),
                "expires_at": int(r["expires_at"] or 0),
                "applied_count": int(r["applied_count"] or 0),
                "last_apply_at": int(r["last_apply_at"] or 0),
            })
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/api/policy/patches")
async def get_policy_patches(status: Optional[str] = None,
                             target_module: Optional[str] = None,
                             limit: int = 100):
    """列出反思生成的策略补丁候选。"""
    return await asyncio.to_thread(
        _policy_patches_pending_sync, status, target_module, limit,
    )


def _policy_patch_review_sync(patch_id: int, action: str,
                              note: str = "") -> Dict[str, Any]:
    """人工审: action in ('approve','reject')。approve 后 status='approved'。"""
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail=f"action must be approve/reject, got {action!r}")
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM policy_patch_queue WHERE id = ?",
            (int(patch_id),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"patch {patch_id} not found")
        if row[0] not in ("pending",):
            raise HTTPException(
                status_code=409,
                detail=f"patch {patch_id} status={row[0]}, can only review pending patches",
            )
        now = int(time.time())
        new_status = "approved" if action == "approve" else "rejected"
        conn.execute(
            """
            UPDATE policy_patch_queue
               SET status = ?, reviewed_at = ?, reviewer_note = ?
             WHERE id = ? AND status = 'pending'
            """,
            (new_status, now, str(note)[:500], int(patch_id)),
        )
        conn.commit()
        return {"id": int(patch_id), "status": new_status, "reviewed_at": now}
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.post("/api/policy/patches/{patch_id}/review")
async def review_policy_patch(patch_id: int, body: _PolicyPatchReviewBody,
                              action: str = "approve"):
    """人工审补丁: ?action=approve|reject。approve 进入 approved 状态。"""
    return await asyncio.to_thread(
        _policy_patch_review_sync, int(patch_id), action, body.note or "",
    )


def _policy_patches_run_sync(window_days: int = 7,
                             do_expire: bool = True) -> Dict[str, Any]:
    """手动触发一轮 policy_updater。"""
    from review import reflection_policy_updater as _rpu
    return _rpu.run_policy_update_cycle(window_days=window_days, do_expire=do_expire)


@router.post("/api/policy/patches/run")
async def run_policy_update_cycle(window_days: int = 7, do_expire: bool = True):
    """手动触发一轮反思 -> 候选生成。"""
    return await asyncio.to_thread(
        _policy_patches_run_sync, int(window_days), bool(do_expire),
    )


def _sibling_diff_rules_sync(asset: Optional[str] = None,
                             min_confidence: float = 0.0,
                             limit: int = 50) -> List[Dict[str, Any]]:
    """读取 sibling_diff_rules。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        where = ["1=1"]
        params: List[Any] = []
        if asset:
            where.append("asset = ?")
            params.append(asset)
        if min_confidence > 0:
            where.append("confidence_score >= ?")
            params.append(float(min_confidence))
        sql = f"""
            SELECT id, rule_signature, fingerprint, asset,
                   shared_features, winner_only_features, loser_only_features,
                   winner_decision_ids, loser_decision_ids,
                   n_pairs, consistency_score, confidence_score, pnl_gap_avg,
                   discovered_at, last_seen_at, status, promoted_to
              FROM sibling_diff_rules
             WHERE {' AND '.join(where)}
          ORDER BY confidence_score DESC, pnl_gap_avg DESC, discovered_at DESC
             LIMIT ?
        """
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                shared = json.loads(r["shared_features"] or "{}")
                wo = json.loads(r["winner_only_features"] or "{}")
                lo = json.loads(r["loser_only_features"] or "{}")
            except Exception:
                shared, wo, lo = {}, {}, {}
            out.append({
                "id": int(r["id"]),
                "rule_signature": r["rule_signature"],
                "fingerprint": r["fingerprint"],
                "asset": r["asset"],
                "shared_features": shared,
                "winner_only_features": wo,
                "loser_only_features": lo,
                "winner_decision_ids": json.loads(r["winner_decision_ids"] or "[]"),
                "loser_decision_ids": json.loads(r["loser_decision_ids"] or "[]"),
                "n_pairs": int(r["n_pairs"] or 0),
                "consistency_score": float(r["consistency_score"] or 0.0),
                "confidence_score": float(r["confidence_score"] or 0.0),
                "pnl_gap_avg": float(r["pnl_gap_avg"] or 0.0),
                "discovered_at": int(r["discovered_at"] or 0),
                "last_seen_at": int(r["last_seen_at"] or 0),
                "status": r["status"],
                "promoted_to": r["promoted_to"] or "",
            })
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/api/policy/sibling-rules")
async def get_sibling_diff_rules(asset: Optional[str] = None,
                                 min_confidence: float = 0.0,
                                 limit: int = 50):
    """列出兄弟决策差分提取的因果规则。"""
    return await asyncio.to_thread(
        _sibling_diff_rules_sync, asset, min_confidence, limit,
    )


class _PromoteNowBody(BaseModel):
    """\xe6\x89\x8b\xe5\x8a\xa8\xe8\xa7\xa6\xe5\x8f\x91 weekly apply\xe3\x80\x82"""
    min_samples: int = 10
    dry_run: bool = False


@router.post("/api/admin/insight/promote-now")
def admin_promote_insight_now(body: _PromoteNowBody):
    """\xe7\xab\x8b\xe5\x8d\x88a\xe8\xb7\x91\xe4\xb8\x80\xe6\xac\xa1 weekly_insight_apply.apply_once\xe3\x80\x82"""
    import weekly_insight_apply as _wia
    conn = db.get_connection()
    try:
        result = _wia.apply_once(
            conn,
            min_samples=max(0, int(body.min_samples)),
            dry_run=bool(body.dry_run),
        )
        return {
            "scanned": int(result.get("scanned", 0)),
            "inserted": [x["adj_id"] for x in result.get("inserted", [])],
            "merged": [x["adj_id"] for x in result.get("merged", [])],
            "skipped_kind0": len(result.get("skipped_kind0", [])),
            "skipped_insufficient_n": len(result.get("skipped_insufficient_n", [])),
            "skipped_no_param": len(result.get("skipped_no_param", [])),
            "skipped_no_direction": len(result.get("skipped_no_direction", [])),
            "dry_run": bool(body.dry_run),
            "min_samples": int(body.min_samples),
        }
    finally:
        conn.close()
