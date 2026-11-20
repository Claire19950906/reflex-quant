#!/usr/bin/env python3
"""v405 P0-3 收口 · 方向标注两遍交叉复核（冲突即 BOTH）

为什么需要这一步：
    同一批链再标一次，实测自一致性只有 **80%** —— 也就是每 5 条里就有 1 条方向会翻。
    方向是拿来算命中率的，翻来翻去等于没标。单遍标注只能当"假设"，不能当"真值"。

规则（保守优先）：
    · 两遍一致 → 采纳（置信度取两遍里更低的那个，别自我拔高）；
    · 两遍不一致 → **BOTH**（宁可说"看不出方向"，也不赌某一边）；
    · 任一通判 BOTH → BOTH。

用法：
    python3 scripts/chain_direction_reconcile.py \
        --pass1 runtime/chain_cleanup/direction-xxx.jsonl \
        --pass2 runtime/chain_cleanup/direction-yyy.jsonl [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

OVERLAY = os.path.join(os.path.dirname(_HERE), "runtime", "chain_direction", "llm_labels.json")
OUT_DIR = os.path.join(os.path.dirname(_HERE), "runtime", "chain_cleanup")


def _read(path: str) -> dict[int, dict]:
    out = {}
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
            out[int(d["id"])] = d
        except (ValueError, KeyError, TypeError):
            continue
    return out


def reconcile(p1: dict[int, dict], p2: dict[int, dict]) -> tuple[list[dict], dict]:
    merged, stats = [], {"agree": 0, "conflict": 0, "one_side_both": 0, "only_one": 0}
    for cid in sorted(set(p1) | set(p2)):
        a, b = p1.get(cid), p2.get(cid)
        if not a or not b:
            stats["only_one"] += 1
            continue
        if a["direction_new"] == b["direction_new"]:
            final = a["direction_new"]
            stats["agree"] += 1
            conf = min(float(a.get("confidence") or 0), float(b.get("confidence") or 0))
        elif "BOTH" in (a["direction_new"], b["direction_new"]):
            final = "BOTH"
            stats["one_side_both"] += 1
            conf = 0.0
        else:
            final = "BOTH"
            stats["conflict"] += 1
            conf = 0.0
        merged.append({
            "id": cid,
            "asset": a.get("asset"),
            "name": a.get("name", "")[:60],
            "direction_old": a.get("direction_old"),
            "direction_new": final,
            "confidence": conf,
            "pass1": a["direction_new"],
            "pass2": b["direction_new"],
            "why": (a.get("why") or "")[:40],
        })
    return merged, stats


def save_overlay(merged: list[dict]) -> str:
    os.makedirs(os.path.dirname(OVERLAY), exist_ok=True)
    labels = {}
    try:
        with open(OVERLAY, encoding="utf-8") as fh:
            labels = dict((json.load(fh) or {}).get("labels") or {})
    except (OSError, ValueError):
        labels = {}
    for d in merged:
        labels[str(d["id"])] = d["direction_new"]
    with open(OVERLAY, "w", encoding="utf-8") as fh:
        json.dump({"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "count": len(labels), "labels": labels}, fh,
                  ensure_ascii=False, indent=1, sort_keys=True)
    return OVERLAY


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass1", required=True)
    ap.add_argument("--pass2", required=True)
    ap.add_argument("--db", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    p1, p2 = _read(args.pass1), _read(args.pass2)
    merged, stats = reconcile(p1, p2)
    print(f"两遍交叉：一致 {stats['agree']} · 冲突→BOTH {stats['conflict']}"
          f" · 一侧 BOTH {stats['one_side_both']} · 只标到一遍 {stats['only_one']}")

    up = sum(1 for d in merged if d["direction_new"] == "UP")
    dn = sum(1 for d in merged if d["direction_new"] == "DOWN")
    print(f"合并结论：UP {up} · DOWN {dn} · BOTH {len(merged) - up - dn}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"direction-merged-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    with open(out, "w", encoding="utf-8") as fh:
        for d in merged:
            fh.write(json.dumps({**d, "pass1": d["pass1"], "pass2": d["pass2"]},
                                ensure_ascii=False) + "\n")
    print(f"审计: {out}")

    if not args.apply:
        print("dry-run：未写库（加 --apply 落库）")
        return 0

    import db
    from causal_chain_evaluator import resnapshot_predictions
    path = args.db or db.get_db_path()
    conn = sqlite3.connect(path, timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    # 比较基准必须是**库里当前值**，不是 pass1 记的"原始值"：
    # pass1 已经写过一轮，用它当基准会把"第二遍改成 BOTH"漏掉。
    cur = {r[0]: (r[1] or "BOTH").upper() for r in conn.execute(
        "SELECT id, expected_direction FROM causal_chains WHERE id IN (%s)"
        % ",".join("?" * len(merged)),
        [d["id"] for d in merged])}
    changed = 0
    for d in merged:
        if d["direction_new"] == cur.get(d["id"]):
            continue
        conn.execute(
            "UPDATE causal_chains SET expected_direction=?, updated_at=? WHERE id=?",
            (d["direction_new"], now, d["id"]),
        )
        changed += 1
    conn.commit()
    snap = resnapshot_predictions(conn)
    conn.commit()
    conn.close()
    print(f"已落库: expected_direction 更新 {changed} 条 · 预测快照 {snap}")
    print(f"覆盖层: {save_overlay(merged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
