#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kg_factor_ontology.py — v423 把长句驱动折到规范因子（默认 dry-run）

做什么：
  1. 建 `factor_ontology` 表 + `asset_driver_nodes.factor_key` 列（幂等）；
  2. 按别名表把每个活跃节点归到规范因子（归不到的留 NULL，不硬猜）；
  3. 为被用到的因子建 `MACRO` 规范节点（短名），并给每个实例建 `instance_of` 边，
     让"实际利率同时驱动 XAU / US10Y / DXY / BTC"在图上是**连通的**，而不是靠 18 条
     硬编码 correlates；
  4. 打印覆盖度与未命中样本（补别名的依据）。

只读预演：
    python3 scripts/kg_factor_ontology.py
落库：
    python3 scripts/kg_factor_ontology.py --apply
复活被误杀的慢变量（可选，单独确认）：
    python3 scripts/kg_factor_ontology.py --apply --revive-slow
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
AUDIT_DIR = BACKEND / "runtime" / "chain_cleanup"
sys.path.insert(0, str(BACKEND / "src_python"))

import factor_ontology

_SKIP_KINDS = ("asset", "evidence", "macro")


def _foreign_label(asset: str, label: str) -> bool:
    """品种专属别名出现在别的品种的核心里 → 不该复活（AAPL 里写"黄金定价"就是污染）。"""
    a = str(asset or "").upper()
    if a in ("", "ALL", "MACRO", "XAU", "XAG", "WTI", "BRENT", "COPPER"):
        return False
    from causal_graph import _foreign_asset_label  # 延迟 import，脚本可独立跑
    return _foreign_asset_label(asset, label)


def _connect(readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=30.0)
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn


def _inventory(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = [dict(r) for r in conn.execute(
        "SELECT id, asset, label, kind, is_core, confidence, active FROM asset_driver_nodes "
        "WHERE active=1"
    ).fetchall()]
    nodes = [r for r in rows if r["kind"] not in _SKIP_KINDS]
    mapped: List[Tuple[Dict[str, Any], str]] = []
    unmatched: List[Dict[str, Any]] = []
    for r in nodes:
        key = factor_ontology.map_label(r["label"])
        if key:
            mapped.append((r, key))
        else:
            unmatched.append(r)
    by_key: Dict[str, int] = {}
    for _r, k in mapped:
        by_key[k] = by_key.get(k, 0) + 1
    # 复活候选：被衰减误杀的慢变量（只读预估）。同 (asset, 因子) 只留最优的一条，
    # 否则 BTC 会一次复活 7 条"全球风险偏好"同义句 —— 覆盖率上去了，可读性反而更差。
    revive: List[Dict[str, Any]] = []
    _best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT id, asset, label, confidence, version, kind FROM asset_driver_nodes "
        "WHERE active=0 AND confidence>=0.15 AND kind='core'"
    ).fetchall():
        r = dict(r)
        key = factor_ontology.map_label(r["label"])
        if not key or not factor_ontology.is_slow(key):
            continue
        if not r["label"] or r["label"].startswith("["):
            continue
        if _foreign_label(r["asset"], r["label"]):
            continue
        cur = _best.get((r["asset"], key))
        if cur is None or (r["confidence"], r["version"], r["id"]) > (
                cur["confidence"], cur["version"], cur["id"]):
            _best[(r["asset"], key)] = {**r, "factor_key": key}
    revive = sorted(_best.values(), key=lambda x: (x["asset"], x["factor_key"]))
    return {"nodes": nodes, "mapped": mapped, "unmatched": unmatched, "by_key": by_key,
            "revive": revive}


def _print_inventory(inv: Dict[str, Any]) -> None:
    n = len(inv["nodes"])
    m = len(inv["mapped"])
    print("库: %s" % DB_PATH)
    print("待归因节点: %d（已排除 asset/evidence/macro）" % n)
    print("命中规范因子: %d（覆盖 %.1f%%），未命中 %d"
          % (m, 100.0 * m / max(n, 1), len(inv["unmatched"])))
    print("因子分布（前 12）: %s"
          % sorted(inv["by_key"].items(), key=lambda kv: -kv[1])[:12])
    print("未命中样本（前 12，补别名用）:")
    for r in inv["unmatched"][:12]:
        print("  [%s/%s] %s" % (r["asset"], r["kind"], str(r["label"])[:56]))
    print("慢变量复活候选（active=0、core、置信>=0.15、可归因）: %d" % len(inv["revive"]))
    for r in inv["revive"][:8]:
        print("  [%s] %s（%s, conf %.2f）"
              % (r["asset"], str(r["label"])[:40], r["factor_key"], r["confidence"]))


def _apply(inv: Dict[str, Any], *, revive_slow: bool) -> Dict[str, Any]:
    import causal_graph
    from db_write_guard import write_txn

    stamp = time.strftime("%Y%m%d-%H%M%S")
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    def _audit(kind: str, **kw: Any) -> None:
        with (AUDIT_DIR / ("factor-ontology-%s.jsonl" % stamp)).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, **kw}, ensure_ascii=False) + "\n")

    stats = {"seeded": 0, "backfilled": 0, "macro_nodes": 0, "instance_edges": 0,
             "revived": 0, "skipped_macro": 0}
    with write_txn(purpose="kg-factor-ontology", timeout_s=30.0) as conn:
        conn.row_factory = sqlite3.Row
        factor_ontology.ensure_tables(conn)
        stats["seeded"] = factor_ontology.seed_ontology(conn)

        # 1) 回填 factor_key（只写命中项；未命中的保持原值，避免覆盖人工标注）
        rows = conn.execute(
            "SELECT id, asset, label, kind FROM asset_driver_nodes WHERE active=1"
        ).fetchall()
        for r in rows:
            if r["kind"] in _SKIP_KINDS:
                continue
            key = factor_ontology.map_label(r["label"])
            if not key:
                continue
            cur = conn.execute(
                "UPDATE asset_driver_nodes SET factor_key=? WHERE id=? AND "
                "(factor_key IS NULL OR factor_key='')",
                (key, int(r["id"])),
            )
            if cur.rowcount:
                stats["backfilled"] += 1

        # 2) MACRO 规范节点 + instance_of 边
        keys = [r["factor_key"] for r in conn.execute(
            "SELECT DISTINCT factor_key FROM asset_driver_nodes "
            "WHERE active=1 AND factor_key IS NOT NULL AND factor_key!=''"
        ).fetchall()]
        macro_ids: Dict[str, int] = {}
        for key in sorted(k for k in keys if k):
            label = factor_ontology.canonical(key)
            if not factor_ontology.canonical_label_safe(key):
                stats["skipped_macro"] += 1
                continue
            row = conn.execute(
                "SELECT id FROM asset_driver_nodes WHERE asset='MACRO' AND label=?",
                (causal_graph._norm_label(label),),
            ).fetchone()
            if row:
                mid = int(row["id"])
                conn.execute(
                    "UPDATE asset_driver_nodes SET active=1, updated_at=? WHERE id=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), mid),
                )
            else:
                mid = causal_graph._upsert_node(
                    conn, "MACRO", label, "macro", False, 0.5,
                    json.dumps({"factor_key": key}, ensure_ascii=False))
                if not mid:
                    stats["skipped_macro"] += 1
                    continue
                stats["macro_nodes"] += 1
            macro_ids[key] = mid
            _audit("macro_node", key=key, node_id=mid, label=label)

        inst = conn.execute(
            "SELECT id, asset, label, factor_key FROM asset_driver_nodes "
            "WHERE active=1 AND factor_key IS NOT NULL AND factor_key!='' AND asset!='MACRO'"
        ).fetchall()
        for r in inst:
            mid = macro_ids.get(r["factor_key"])
            if not mid:
                continue
            cur = conn.execute(
                "INSERT INTO asset_driver_edges "
                "(from_id, to_id, kind, weight, evidence, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(from_id,to_id,kind) DO NOTHING",
                (int(r["id"]), mid, "instance_of", 1.0,
                 "onto:%s" % r["factor_key"], time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            if cur.rowcount:
                stats["instance_edges"] += 1

        # 3) 可选：复活被"本轮没再出现"误杀的慢变量（只碰慢因子、置信 >=0.2）
        if revive_slow:
            cand = conn.execute(
                "SELECT id, asset, label, confidence, version FROM asset_driver_nodes "
                "WHERE active=0 AND confidence>=0.15 AND kind='core'"
            ).fetchall()
            _pick: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for r in cand:
                key = factor_ontology.map_label(r["label"])
                if not key or not factor_ontology.is_slow(key):
                    continue
                if causal_graph._is_junk_factor_name(r["label"]):
                    continue
                if causal_graph._looks_like_event(r["label"]):
                    continue
                if _foreign_label(r["asset"], r["label"]):
                    continue
                k2 = (str(r["asset"]), key)
                cur = _pick.get(k2)
                if cur is None or (r["confidence"], r["version"], r["id"]) > (
                        cur["confidence"], cur["version"], cur["id"]):
                    _pick[k2] = {"id": int(r["id"]), "label": r["label"],
                                 "confidence": r["confidence"], "version": r["version"],
                                 "key": key, "asset": r["asset"]}
            for p in _pick.values():
                conn.execute(
                    "UPDATE asset_driver_nodes SET active=1, factor_key=?, updated_at=? WHERE id=?",
                    (p["key"], time.strftime("%Y-%m-%d %H:%M:%S"), p["id"]),
                )
                stats["revived"] += 1
                _audit("revive_slow", node_id=p["id"], asset=p["asset"], label=p["label"],
                       factor_key=p["key"], confidence=p["confidence"])
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="v423 规范因子本体迁移")
    ap.add_argument("--apply", action="store_true", help="落库（默认 dry-run）")
    ap.add_argument("--revive-slow", action="store_true",
                    help="同时复活被衰减误杀的慢变量节点（需配合 --apply）")
    args = ap.parse_args()

    ro = _connect(readonly=True)
    try:
        inv = _inventory(ro)
    finally:
        ro.close()
    _print_inventory(inv)
    if not args.apply:
        print("\n（dry-run：未写库）")
        return 0

    stats = _apply(inv, revive_slow=args.revive_slow)
    print("\n已落库: %s" % json.dumps(stats, ensure_ascii=False))

    ro = _connect(readonly=True)
    try:
        total = ro.execute("SELECT COUNT(*) FROM asset_driver_nodes WHERE active=1").fetchone()[0]
        mapped = ro.execute(
            "SELECT COUNT(*) FROM asset_driver_nodes WHERE active=1 AND factor_key IS NOT NULL "
            "AND factor_key!='' AND kind NOT IN ('asset','evidence','macro')"
        ).fetchone()[0]
        macro = ro.execute(
            "SELECT COUNT(*) FROM asset_driver_nodes WHERE kind='macro' AND active=1"
        ).fetchone()[0]
        inst = ro.execute(
            "SELECT COUNT(*) FROM asset_driver_edges WHERE kind='instance_of'"
        ).fetchone()[0]
        print("复核: 活跃 %d · 已归因 %d · MACRO %d · instance_of 边 %d"
              % (total, mapped, macro, inst))
    finally:
        ro.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
