#!/usr/bin/env python3
"""修复被写坏的节点证据（2026-11-18 事故）。

事故：scripts/causal_node_source_backfill.py 首次实现把 evidence 字典当作
`chain` 参数传进 `causal_graph._serialize_evidence()`，结果写入的 chain 变成了
键名列表（["chain","_status",…]），6 个节点的机制文本被覆盖。

修复：这 6 个节点的原文都来自链库（文本匹配分数 1.0），按链库重建 chain 层；
唯一非精确匹配的 #583 不猜出处，明确标注"待重建"。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))
import causal_graph as cg

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")

# node_id -> chain_id（精确同名匹配）；None 表示"出处不明，只标待重建"
TARGETS = {197: 127, 485: 74, 565: 107, 569: 22, 872: 127, 583: None}


def layers_from_chain(conn, cid):
    r = conn.execute("SELECT name, cause, effect, mechanism FROM causal_chains WHERE id=?",
                     (cid,)).fetchone()
    if not r:
        return None
    out = [str(r[0] or "").strip()]
    for k in (1, 2, 3):
        v = str(r[k] or "").strip()
        if v and v.upper() != "NONE" and v not in out:
            out.append(v)
    return [x for x in out if x] or None


def main():
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DEFAULT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    for nid, cid in TARGETS.items():
        row = conn.execute("SELECT id, asset, label, kind, evidence FROM asset_driver_nodes "
                           "WHERE id=?", (nid,)).fetchone()
        if not row:
            print("  #%s 不存在，跳过" % nid)
            continue
        ev = cg._parse_evidence(row["evidence"] or "")
        if cid:
            layers = layers_from_chain(conn, cid)
            if not layers:
                print("  #%s 链%s 无文本，跳过" % (nid, cid))
                continue
            src = "chain_lib:%d(src=rebuild)" % cid
            status = cg._STATUS_UNCONFIRMED
            position = ev.get("_position")
        else:
            layers = [str(row["label"] or ""),
                      "机制文本于 2026-11-18 被回填脚本覆盖，待重建（出处未确证，不猜链）"]
            src = None
            status = cg._STATUS_UNCONFIRMED
            position = None
        payload = cg._serialize_evidence(
            layers, status=status, position=position,
            fact_ids=ev.get("_fact_ids") or [], expect=ev.get("_expect"),
            expect_src=ev.get("_expect_src"), source=src,
        )
        print("  #%s %s → %s" % (nid, row["label"][:26], (src or "待重建")))
        if apply:
            conn.execute("UPDATE asset_driver_nodes SET evidence=?, updated_at=? WHERE id=?",
                         (payload, cg._now(), nid))
    if apply:
        conn.commit()
        print("\n已修复 %d 个节点" % len(TARGETS))
        for nid in TARGETS:
            ev = cg._parse_evidence(conn.execute(
                "SELECT evidence FROM asset_driver_nodes WHERE id=?", (nid,)
            ).fetchone()[0])
            print("  #%s chain条数=%d 首条=%s src=%s"
                  % (nid, len(ev.get("chain") or []),
                     str((ev.get("chain") or [""])[0])[:30], ev.get("_source")))
    else:
        print("\n（dry-run，加 --apply 执行）")
    conn.close()


if __name__ == "__main__":
    main()
