#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kg_p0_graph_slim.py — v422 P0：因果图谱瘦身（可逆，默认 dry-run）

清理两类**合成节点**（不是业务节点，删了可从上一步的 JSONL 还原）：

A. `asset='all'` 的 seed 桶
   `seed_from_causal_chains` 旧实现把每条链的 `cause` 原文当 `all` 下的 core，
   每次 cron 重跑再 version+1。实测 160 个活跃节点（92 core 全是新闻标题）、
   229 条出边，把 ALL 视图变成标题墙。代码已改为默认不写；本脚本清存量。

B. 一次性 evidence 节点
   `persist_evidence_edges` 旧实现 label 带时间戳 `[SUPPORT] ev_7d_<now>_<nid>`，
   每跑一次多 1–2 个节点。实测 462 个、每个只有 1 条边；同一假设的票数从来没
   累积成一条边。代码已改为"每资产一个稳定锚点 + 边权=本次票数"。

处置顺序（**先折叠再删**，不丢信息）：
  1. 备份 A/B 的 nodes + 关联 edges 全量 JSONL；
  2. 把每个 (假设节点, kind) 最近一次的票数，折叠进 `[证据] <asset>` 稳定锚点边；
  3. 删除 A/B 节点及其边（按 id 显式删，不做全表 DELETE）；
  4. 打印前后计数。

用法
----
    python3 scripts/kg_p0_graph_slim.py            # dry-run
    python3 scripts/kg_p0_graph_slim.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
AUDIT_DIR = BACKEND / "runtime" / "chain_cleanup"
sys.path.insert(0, str(BACKEND / "src_python"))

# 旧 evidence 节点 label 归一化后形如 "[support]ev_7d_1791..._16"
_LEGACY_EV_RE = re.compile(r"^(?:\[support\]|\[contradict\])ev_7d_(\d+)_(\d+)$", re.I)
_TS_RE = re.compile(r"ev_7d_(\d+)_")


def _connect(readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=30.0)
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn


def _inventory(conn: sqlite3.Connection) -> Dict[str, Any]:
    nodes = [dict(r) for r in conn.execute(
        "SELECT * FROM asset_driver_nodes WHERE asset='all' AND kind!='asset'"
    ).fetchall()]
    a_ids = [int(n["id"]) for n in nodes]

    a_edges: List[Dict[str, Any]] = []
    if a_ids:
        ph = ",".join("?" * len(a_ids))
        a_edges = [dict(r) for r in conn.execute(
            "SELECT * FROM asset_driver_edges WHERE from_id IN (%s) OR to_id IN (%s)" % (ph, ph),
            a_ids + a_ids,
        ).fetchall()]

    ev_nodes = [dict(r) for r in conn.execute(
        "SELECT * FROM asset_driver_nodes WHERE kind='evidence'"
    ).fetchall()]
    legacy_ev = [n for n in ev_nodes if _LEGACY_EV_RE.match(str(n["label"] or "").strip().lower())]
    b_ids = [int(n["id"]) for n in legacy_ev]

    b_edges: List[Dict[str, Any]] = []
    if b_ids:
        ph = ",".join("?" * len(b_ids))
        b_edges = [dict(r) for r in conn.execute(
            "SELECT * FROM asset_driver_edges WHERE from_id IN (%s) OR to_id IN (%s)" % (ph, ph),
            b_ids + b_ids,
        ).fetchall()]

    # 折叠计划：每个 (假设 node_id, kind) 取**最近一次**核验的票数
    # （旧节点 label 里带 epoch，越大越新；同一轮支持/反驳是两个节点，各自取各自最新）
    folds: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for e in b_edges:
        fid, tid, kind = int(e["from_id"]), int(e["to_id"]), str(e["kind"])
        if fid in b_ids and kind in ("supports", "contradicts"):
            legacy = next((n for n in legacy_ev if int(n["id"]) == fid), None)
            if not legacy:
                continue
            m = _TS_RE.search(str(legacy["label"] or ""))
            ts = int(m.group(1)) if m else 0
            key = (tid, kind)
            cur = folds.get(key)
            if cur is None or ts >= cur["ts"]:
                folds[key] = {
                    "node_id": tid, "kind": kind, "ts": ts,
                    "weight": float(e["weight"] or 0),
                    "legacy_evidence_id": fid,
                    "legacy_label": legacy["label"],
                }

    hyp_asset: Dict[int, str] = {}
    for nid in {k[0] for k in folds}:
        row = conn.execute(
            "SELECT asset FROM asset_driver_nodes WHERE id=?", (nid,)
        ).fetchone()
        hyp_asset[nid] = (row["asset"] if row else "all") or "all"

    return {
        "all_nodes": nodes,
        "all_edges": a_edges,
        "legacy_ev_nodes": legacy_ev,
        "legacy_ev_edges": b_edges,
        "folds": folds,
        "hyp_asset": hyp_asset,
    }


def _print_inventory(inv: Dict[str, Any]) -> None:
    print("库: %s" % DB_PATH)
    print("A · asset='all' 节点: %d（core %d / condition %d），关联边 %d"
          % (len(inv["all_nodes"]),
             sum(1 for n in inv["all_nodes"] if n["kind"] == "core"),
             sum(1 for n in inv["all_nodes"] if n["kind"] == "condition"),
             len(inv["all_edges"])))
    print("  样本:")
    for n in inv["all_nodes"][:4]:
        print("    [%s] %s" % (n["kind"], str(n["label"])[:52]))
    print("B · 一次性 evidence 节点: %d，关联边 %d"
          % (len(inv["legacy_ev_nodes"]), len(inv["legacy_ev_edges"])))
    for n in inv["legacy_ev_nodes"][:4]:
        print("    %s" % str(n["label"])[:60])
    folds = inv["folds"]
    by_asset: Dict[str, int] = {}
    for (nid, _kind) in folds:
        a = inv["hyp_asset"].get(nid, "all")
        by_asset[a] = by_asset.get(a, 0) + 1
    print("折叠计划: %d 条边（%d 个假设节点，覆盖 %d 个品种）"
          % (len(folds), len({k[0] for k in folds}), len(by_asset)))
    print("  按品种: %s" % dict(sorted(by_asset.items(), key=lambda kv: -kv[1])[:8]))


def _backup(inv: Dict[str, Any], stamp: str) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / ("p0-graph-slim-%s.jsonl" % stamp)
    with path.open("w", encoding="utf-8") as fh:
        for bucket, rows in (
            ("all_node", inv["all_nodes"]),
            ("all_edge", inv["all_edges"]),
            ("legacy_evidence_node", inv["legacy_ev_nodes"]),
            ("legacy_evidence_edge", inv["legacy_ev_edges"]),
        ):
            for r in rows:
                fh.write(json.dumps({"_bucket": bucket, **r}, ensure_ascii=False) + "\n")
    return path


def _apply(inv: Dict[str, Any]) -> Dict[str, Any]:
    import causal_graph
    from db_write_guard import write_txn

    timeout_s = 30.0
    with write_txn(purpose="kg-p0-graph-slim", timeout_s=timeout_s) as conn:
        conn.row_factory = sqlite3.Row
        # 1) 折叠：稳定锚点 = [证据] <asset>
        anchors: Dict[str, Optional[int]] = {}
        folded = 0
        for (nid, kind), f in inv["folds"].items():
            a = inv["hyp_asset"].get(nid, "all")
            a = str(a or "all").strip().upper() or "ALL"
            if a not in anchors:
                anchors[a] = causal_graph._upsert_node(
                    conn, a, "%s %s" % (causal_graph._EVIDENCE_ANCHOR_PREFIX, a),
                    "evidence", False, 0.5, "")
            out_id = anchors[a]
            if not out_id:
                continue
            conn.execute(
                "INSERT INTO asset_driver_edges "
                "(from_id,to_id,kind,weight,evidence,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(from_id,to_id,kind) DO UPDATE SET "
                "  weight=MAX(weight, excluded.weight), evidence=excluded.evidence, "
                "  updated_at=excluded.updated_at",
                (out_id, int(nid), kind, float(min(f["weight"], 10)),
                 "folded from %s" % str(f["legacy_label"])[:40],
                 time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            folded += 1

        # 2) 删 A 桶的边 + 节点（按 id）
        a_ids = [int(n["id"]) for n in inv["all_nodes"]]
        for nid in a_ids:
            conn.execute("DELETE FROM asset_driver_edges WHERE from_id=? OR to_id=?", (nid, nid))
        for nid in a_ids:
            conn.execute("DELETE FROM asset_driver_nodes WHERE id=?", (nid,))

        # 3) 删 B 桶的边 + 节点（按 id）
        b_ids = [int(n["id"]) for n in inv["legacy_ev_nodes"]]
        for nid in b_ids:
            conn.execute("DELETE FROM asset_driver_edges WHERE from_id=? OR to_id=?", (nid, nid))
        for nid in b_ids:
            conn.execute("DELETE FROM asset_driver_nodes WHERE id=?", (nid,))

        return {"deleted_all_nodes": len(a_ids), "deleted_all_edges": len(inv["all_edges"]),
                "deleted_evidence_nodes": len(b_ids),
                "deleted_evidence_edges": len(inv["legacy_ev_edges"]),
                "folded_edges": folded, "anchors": len(anchors)}


def _load_backup(path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            bucket = rec.pop("_bucket", "")
            if bucket == "legacy_evidence_node":
                nodes.append(rec)
            elif bucket == "legacy_evidence_edge":
                edges.append(rec)
    return nodes, edges


def _refold_from_backup(path: Path) -> Dict[str, Any]:
    """把备份里的旧证据票数重新折叠到稳定锚点（幂等，MAX 语义）。

    用途：首次 --apply 时若某个锚点因校验被拒（如 CN_STOCK 的 label 带下划线），
    对应假设的票数会跟着旧节点一起被删。本模式按备份重放，只补票、不复活旧节点。
    """
    import causal_graph
    from db_write_guard import write_txn

    nodes, edges = _load_backup(path)
    by_id = {int(n["id"]): n for n in nodes}
    latest: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for e in edges:
        kind = str(e.get("kind") or "")
        if kind not in ("supports", "contradicts"):
            continue
        fid = int(e["from_id"])
        legacy = by_id.get(fid)
        if not legacy:
            continue
        m = _TS_RE.search(str(legacy.get("label") or ""))
        ts = int(m.group(1)) if m else 0
        key = (int(e["to_id"]), kind)
        if key not in latest or ts >= latest[key]["ts"]:
            latest[key] = {"ts": ts, "weight": float(e.get("weight") or 0),
                           "label": legacy.get("label")}

    folded = 0
    anchors: Dict[str, int] = {}
    with write_txn(purpose="kg-p0-refold", timeout_s=30.0) as conn:
        conn.row_factory = sqlite3.Row
        for (nid, kind), f in latest.items():
            row = conn.execute("SELECT asset FROM asset_driver_nodes WHERE id=?", (nid,)).fetchone()
            asset = (row["asset"] if row else "") or "all"
            asset = str(asset).strip().upper() or "ALL"
            if asset not in anchors:
                aid = causal_graph._evidence_anchor_node(conn, asset)
                if not aid:
                    print("  [skip] 锚点创建失败: %s" % asset)
                    continue
                anchors[asset] = int(aid)
            conn.execute(
                "INSERT INTO asset_driver_edges "
                "(from_id,to_id,kind,weight,evidence,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(from_id,to_id,kind) DO UPDATE SET "
                "  weight=MAX(weight, excluded.weight), evidence=excluded.evidence, "
                "  updated_at=excluded.updated_at",
                (anchors[asset], int(nid), kind, float(min(f["weight"], 10)),
                 "refold from %s" % str(f["label"])[:40],
                 time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            folded += 1
    return {"refolded_edges": folded, "anchors": len(anchors)}


def main() -> int:
    ap = argparse.ArgumentParser(description="因果图谱 P0 瘦身（v422）")
    ap.add_argument("--apply", action="store_true", help="落库（默认 dry-run）")
    ap.add_argument("--refold-from", default="",
                    help="从 P0 备份 JSONL 重放旧证据票数到稳定锚点（幂等）")
    args = ap.parse_args()

    if args.refold_from:
        path = Path(args.refold_from)
        if not path.exists():
            print("备份文件不存在: %s" % path)
            return 2
        print("重放: %s" % path)
        print(json.dumps(_refold_from_backup(path), ensure_ascii=False))
        return 0

    ro = _connect(readonly=True)
    try:
        inv = _inventory(ro)
    finally:
        ro.close()

    _print_inventory(inv)

    if not args.apply:
        print("\n（dry-run：未写库。加 --apply 落库；会先备份 JSONL）")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = _backup(inv, stamp)
    print("\n备份: %s（%d 行）" % (backup, len(inv["all_nodes"]) + len(inv["all_edges"])
                                  + len(inv["legacy_ev_nodes"]) + len(inv["legacy_ev_edges"])))

    result = _apply(inv)
    print("已落库: %s" % json.dumps(result, ensure_ascii=False))

    ro = _connect(readonly=True)
    try:
        left_all = ro.execute(
            "SELECT COUNT(*) FROM asset_driver_nodes WHERE asset='all' AND kind!='asset'"
        ).fetchone()[0]
        left_ev = ro.execute(
            "SELECT COUNT(*) FROM asset_driver_nodes WHERE kind='evidence'"
        ).fetchone()[0]
        total_nodes = ro.execute("SELECT COUNT(*) FROM asset_driver_nodes").fetchone()[0]
        total_edges = ro.execute("SELECT COUNT(*) FROM asset_driver_edges").fetchone()[0]
        print("复核: all 桶 %d · evidence 节点 %d · 总节点 %d · 总边 %d"
              % (left_all, left_ev, total_nodes, total_edges))
    finally:
        ro.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
