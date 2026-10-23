#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v302+ reconcile worker — 把 status=approved 的 kg_improvements 真正写回图谱/规则库。

设计动机:
  v202 实现了 kg_improvements 表 + auto_approve_pending() + decide_proposal(),
  但仅翻转 status='approved', 没有任何代码真正把改动写回
  asset_driver_edges / causal_chains / 规则库。
  本模块补上这段缺失的 apply 步骤。

设计要点:
  - 按 kind + target 分发到对应 handler:
      add_rule / target='reflection_checkers:*' -> advisory 写入 worker_heartbeat
      add_edge / target='asset_driver_edges#<from_id>#<to_id>' -> 插入边
      remove_edge / 同 target -> 删除边
      reweight / 同 target -> 改 weight
      enable_rule / target='causal_chains#<id>' -> set_enabled(1)
      disable_rule / 同 target -> set_enabled(0)
  - 包在 causal_graph_guard.causal_guard 里, drift>20% 自动回滚
  - 失败不抛错: 写 apply_error, 留 status='approved' 让下次重试
  - dry_run=True 可做预览
"""

from __future__ import annotations

import sqlite3
import traceback
from typing import Any, Dict, List, Optional

import causal_graph_guard
import causal_chains
import db
import kg_improvements


# 每次 reconcile 处理上限
DEFAULT_BATCH_MAX = 50


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _target_ids(target):
    """解析 'asset_driver_edges#<from_id>#<to_id>' 或 'causal_chains#<id>'。"""
    if not target:
        return None
    parts = target.split("#")
    if len(parts) == 3 and parts[0] == "asset_driver_edges":
        try:
            return {"from_id": int(parts[1]), "to_id": int(parts[2]), "kind": "edge"}
        except ValueError:
            return None
    if len(parts) == 2 and parts[0] == "causal_chains":
        try:
            return {"chain_id": int(parts[1]), "kind": "chain"}
        except ValueError:
            return None
    return None


def _apply_add_rule(proposal, conn):
    """add_rule: target='reflection_checkers:<error_code>' 是当前 kg_feedback 唯一来源。

    实际代码修改需人工 (在 reflection_checkers.py 加 E020 防御);
    本 worker 只做「policy 标注 + 记录 apply_target」, 不改代码本身。
    """
    target = proposal.get("target", "")
    if not target.startswith("reflection_checkers"):
        return {"ok": False, "error": "add_rule 不支持的 target: " + str(target)}
    payload = proposal.get("payload", {}) or {}
    rule_code = payload.get("rule_code") or target.split(":", 1)[-1]
    try:
        conn.execute(
            "INSERT INTO worker_heartbeat (worker_name, last_tick_at, last_status, last_error) "
            "VALUES (?, datetime('now','localtime'), 'ok', ?)",
            ("kg_reconcile_add_rule", "advisory rule_code=" + str(rule_code) + " proposal_id=" + str(proposal.get("id"))),
        )
        conn.commit()
    except Exception:
        pass
    return {
        "ok": True,
        "applied_target": "reflection_checkers:" + str(rule_code),
        "note": "advisory 写入 worker_heartbeat; 实际代码加固需人工",
    }


def _apply_add_edge(proposal, conn):
    """add_edge: asset_driver_edges#<from_id>#<to_id> — 在因果图谱插入一条边。"""
    ids = _target_ids(proposal.get("target", ""))
    if not ids or ids["kind"] != "edge":
        return {"ok": False, "error": "add_edge target 解析失败: " + str(proposal.get("target"))}
    payload = proposal.get("payload", {}) or {}
    kind = payload.get("kind") or "causes"
    weight = float(payload.get("weight", 1.0))
    evidence = payload.get("evidence", "kg_reconcile_v302 proposal=" + str(proposal.get("id")))
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO asset_driver_edges "
            "(from_id, to_id, kind, weight, evidence) VALUES (?, ?, ?, ?, ?)",
            (ids["from_id"], ids["to_id"], kind, weight, str(evidence)[:1000]),
        )
        conn.commit()
        return {
            "ok": True,
            "applied_target": "asset_driver_edges#" + str(ids["from_id"]) + "#" + str(ids["to_id"]),
            "rowid": cur.lastrowid,
        }
    except sqlite3.IntegrityError as e:
        return {"ok": False, "error": "边已存在或 FK 失败: " + str(e)}


def _apply_remove_edge(proposal, conn):
    ids = _target_ids(proposal.get("target", ""))
    if not ids or ids["kind"] != "edge":
        return {"ok": False, "error": "remove_edge target 解析失败: " + str(proposal.get("target"))}
    try:
        cur = conn.execute(
            "DELETE FROM asset_driver_edges WHERE from_id=? AND to_id=?",
            (ids["from_id"], ids["to_id"]),
        )
        conn.commit()
        return {
            "ok": True,
            "applied_target": "asset_driver_edges#" + str(ids["from_id"]) + "#" + str(ids["to_id"]),
            "deleted_rows": cur.rowcount,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _apply_reweight(proposal, conn):
    ids = _target_ids(proposal.get("target", ""))
    if not ids or ids["kind"] != "edge":
        return {"ok": False, "error": "reweight target 解析失败: " + str(proposal.get("target"))}
    payload = proposal.get("payload", {}) or {}
    new_weight = float(payload.get("weight", 1.0))
    try:
        cur = conn.execute(
            "UPDATE asset_driver_edges SET weight=?, updated_at=datetime('now','localtime') "
            "WHERE from_id=? AND to_id=?",
            (new_weight, ids["from_id"], ids["to_id"]),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "未找到对应边"}
        return {
            "ok": True,
            "applied_target": "asset_driver_edges#" + str(ids["from_id"]) + "#" + str(ids["to_id"]),
            "new_weight": new_weight,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _apply_enable_or_disable(proposal):
    """enable_rule / disable_rule: 切 causal_chains.enabled。"""
    ids = _target_ids(proposal.get("target", ""))
    if not ids or ids["kind"] != "chain":
        return {"ok": False, "error": "enable/disable_rule target 解析失败: " + str(proposal.get("target"))}
    desired = 1 if proposal.get("kind") == "enable_rule" else 0
    try:
        res = causal_chains.set_enabled(ids["chain_id"], enabled=desired)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error", "set_enabled failed")}
        return {
            "ok": True,
            "applied_target": "causal_chains#" + str(ids["chain_id"]),
            "enabled": desired,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
        try:
            return {"chain_id": int(parts[1]), "kind": "chain"}
        except ValueError:
            return None
    return None
def _apply_one(proposal):
    """单条 proposal 的 apply 入口, 带 guard 包裹的图谱写操作。

    Returns: {ok, applied_target?, error?, kind, proposal_id}
    """
    pid = proposal.get("id")
    kind = proposal.get("kind")
    target = proposal.get("target", "")
    if pid is None or kind is None:
        return {"ok": False, "error": "proposal 缺 id/kind", "proposal_id": pid}

    conn = _connect()
    try:
        if kind in ("add_edge", "remove_edge", "reweight"):
            # 涉及 asset_driver_edges -> 套 guard (drift>20% 自动回滚)
            with causal_graph_guard.causal_guard(
                conn, label="kg_reconcile_" + str(kind),
                tables=["asset_driver_edges"],
            ):
                if kind == "add_edge":
                    res = _apply_add_edge(proposal, conn)
                elif kind == "remove_edge":
                    res = _apply_remove_edge(proposal, conn)
                else:
                    res = _apply_reweight(proposal, conn)
        elif kind == "add_rule":
            res = _apply_add_rule(proposal, conn)
        elif kind in ("enable_rule", "disable_rule"):
            res = _apply_enable_or_disable(proposal)
        else:
            return {"ok": False, "error": "未知 kind: " + str(kind), "proposal_id": pid}

        res["proposal_id"] = pid
        res["kind"] = kind
        res["target"] = target
        return res
    except Exception as e:
        return {
            "ok": False,
            "error": type(e).__name__ + ": " + str(e)[:200],
            "trace": traceback.format_exc(limit=2),
            "proposal_id": pid,
            "kind": kind,
            "target": target,
        }
    finally:
        conn.close()


def reconcile_approved(batch_max=DEFAULT_BATCH_MAX, dry_run=False):
    """主入口: 扫 status=approved 且 applied_at IS NULL 的行, 应用并写回状态。

    Returns: {
        scanned, applied_ids, applied_targets, failed, dry_run
    }
    """
    proposals = kg_improvements.list_approved_unapplied(batch_max=batch_max)
    if dry_run:
        return {
            "scanned": len(proposals),
            "dry_run": True,
            "would_apply": [
                {"id": p.get("id"), "kind": p.get("kind"), "target": p.get("target")}
                for p in proposals
            ],
        }

    applied_ids = []
    applied_targets = {}
    failed = []

    for p in proposals:
        pid = p.get("id")
        if pid is None:
            continue
        res = _apply_one(p)
        if res.get("ok"):
            kg_improvements.mark_applied(pid, res["applied_target"])
            applied_ids.append(int(pid))
            applied_targets[int(pid)] = res["applied_target"]
        else:
            err = res.get("error", "unknown")
            kg_improvements.mark_apply_failed(pid, err)
            failed.append({"id": int(pid), "error": err})

    return {
        "scanned": len(proposals),
        "applied_ids": applied_ids,
        "applied_targets": applied_targets,
        "failed": failed,
        "dry_run": False,
    }
