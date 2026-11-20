#!/usr/bin/env python3
"""节点 → 链库显式 ID 关联回填（v429）。

问题：节点 evidence 里只有"机制文本"，没有"这条机制来自链库哪一条链"。
结果是：链库既然能算出 13/30 的净增益，节点这边却无法说明证据出处；
清空壳时补进去的 5 条写了 `_source`，存量 105 条一条都没有。

做法（只补，不改机制文本、不改核验结论）：
  1) 对 active=1 且 kind in (chain, condition) 的节点，取出机制文本；
  2) 与该品种（含 MACRO）的**全部链**做字符二元组相似度匹配（阈值 0.5）；
  3) 匹配上且 evidence 无 `_source` 的，写入 `chain_lib:<id>(hit=…,src=backfill)`；
     匹配不上的留空并在 CSV 里标 unmatched —— 不猜，不伪造出处。

默认 dry-run；`--apply` 才写库，输出审计 CSV。
"""
import argparse
import csv
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))

import causal_graph as cg

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "node_source")
MIN_SCORE = 0.5


def _chain_index(conn):
    idx = []
    rows = conn.execute(
        "SELECT id, asset, name, cause, effect, mechanism FROM causal_chains"
    ).fetchall()
    for r in rows:
        full = " ".join(str(r[k] or "") for k in ("name", "cause", "effect", "mechanism"))
        idx.append({
            "id": int(r["id"]),
            "asset": (r["asset"] or "").upper(),
            "grams_name": cg._bigrams(str(r["name"] or "")),
            "grams_full": cg._bigrams(full),
        })
    return idx


def _node_text(ev, label):
    parts = [str(label or "")]
    for item in (ev.get("chain") or []):
        parts.append(str(item))
    return " ".join(p for p in parts if p)


def _best(idx, asset, text):
    asset_u = str(asset or "").upper()
    grams = cg._bigrams(text)
    if not grams:
        return None
    best = None
    for c in idx:
        if not c["asset"]:
            continue
        if c["asset"] not in (asset_u, "MACRO", "GLOBAL"):
            continue
        if not c["grams_name"] and not c["grams_full"]:
            continue
        score = max(len(c["grams_name"] & grams) / max(1, len(c["grams_name"])),
                    len(c["grams_full"] & grams) / max(1, len(grams)))
        if score < MIN_SCORE:
            continue
        if best is None or score > best["score"]:
            best = {"id": c["id"], "score": round(score, 3)}
    return best


def main():
    global MIN_SCORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    args = ap.parse_args()

    MIN_SCORE = args.min_score

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    idx = _chain_index(conn)

    rows = conn.execute(
        "SELECT id, asset, label, kind, evidence FROM asset_driver_nodes "
        "WHERE active=1 AND kind IN ('chain','condition') ORDER BY id"
    ).fetchall()

    plan = []
    for r in rows:
        ev = cg._parse_evidence(r["evidence"] or "")
        if ev.get("_source"):
            continue
        text = _node_text(ev, r["label"])
        if not (ev.get("chain") or []):
            continue
        m = _best(idx, r["asset"], text)
        plan.append((r, ev, m))

    matched = [p for p in plan if p[2]]
    unmatched = [p for p in plan if not p[2]]
    print("[node-source] 待回填 %d 条：可匹配 %d / 匹配不上 %d"
          % (len(plan), len(matched), len(unmatched)))
    for r, ev, m in matched[:5]:
        print("  #%s %s %s score=%.2f → chain_lib:%d"
              % (r["id"], r["asset"], str(r["label"] or "")[:22], m["score"], m["id"]))

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "node_source_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["node_id", "asset", "kind", "label", "chain_id", "score", "status"])
        for r, ev, m in plan:
            w.writerow([r["id"], r["asset"], r["kind"], (r["label"] or "")[:120],
                        m["id"] if m else "", m["score"] if m else "",
                        "matched" if m else "unmatched"])
    print("[node-source] 明细: %s" % path)

    if not args.apply:
        print("[node-source] dry-run，未写库（加 --apply 执行）")
        return

    done = 0
    for r, ev, m in matched:
        # 注意：_serialize_evidence 收具名参数，不能喂 evidence 字典
        # （2026-11-18 事故就是踩这个坑，写坏了 6 个节点的机制文本）
        payload = cg._serialize_evidence(
            ev.get("chain") or [],
            status=ev.get("_status") or cg._STATUS_UNCONFIRMED,
            fail=ev.get("_fail"), position=ev.get("_position"),
            fact_ids=ev.get("_fact_ids") or [], verified_at=ev.get("_verified_at"),
            expect=ev.get("_expect"), expect_src=ev.get("_expect_src"),
            cond={"n": ev.get("_cond_n"), "hits": ev.get("_cond_hits"),
                  "ratio": ev.get("_cond_ratio"), "chain_ids": ev.get("_cond_chains"),
                  "base_rate": ev.get("_cond_base_rate"), "edge": ev.get("_cond_edge")},
            source="chain_lib:%d(src=backfill)" % m["id"],
        )
        for attempt in range(30):
            try:
                conn.execute("UPDATE asset_driver_nodes SET evidence=? WHERE id=?",
                             (payload, r["id"]))
                done += 1
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                time.sleep(0.5 * (attempt + 1))
        if done % 50 == 0:
            conn.commit()
    conn.commit()
    print("[node-source] 已回填 %d 条" % done)
    conn.close()


if __name__ == "__main__":
    main()
