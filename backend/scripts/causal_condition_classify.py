#!/usr/bin/env python3
"""condition 节点定性：有链可验 vs 纯推理注释（v430）。

现状：99 个 active condition 节点全部 UNCONFIRMED、全部没有 `_source`，
它们既进 get_knowledge 又进 prompt，但链库里找不到对应机制 —— 等于
"没有被核验过的机制"靠占位在输出里冒充条件因素。

处置：
  · 能匹配到链（分数 ≥ 阈值）→ 写 `_source`，标 `_tier="chain_backed"`；
  · 匹配不上 → 标 `_tier="annotation"`，保留文本供展示，但**不进 prompt**
    （注入侧只认 chain_backed / 有样本的条目）。

默认 dry-run。
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))
import causal_graph as cg

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "condition_audit")


def chain_index(conn):
    idx = []
    for r in conn.execute("SELECT id, asset, name, cause, effect, mechanism FROM causal_chains"):
        full = " ".join(str(r[k] or "") for k in ("name", "cause", "effect", "mechanism"))
        idx.append({"id": int(r["id"]), "asset": (r["asset"] or "").upper(),
                    "grams_name": cg._bigrams(str(r["name"] or "")),
                    "grams_full": cg._bigrams(full)})
    return idx


def best(idx, asset, text, lo):
    asset_u = str(asset or "").upper()
    grams = cg._bigrams(text)
    if not grams:
        return None
    out = None
    for c in idx:
        if not c["asset"] or c["asset"] not in (asset_u, "MACRO", "GLOBAL"):
            continue
        score = max(len(c["grams_name"] & grams) / max(1, len(c["grams_name"])),
                    len(c["grams_full"] & grams) / max(1, len(grams)))
        if score < lo:
            continue
        if out is None or score > out["score"]:
            out = {"id": c["id"], "score": round(score, 3)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-score", type=float, default=0.6)   # v433：0.45 会把 0.46 的弱匹配也认成链库出处
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    idx = chain_index(conn)
    rows = conn.execute(
        "SELECT id, asset, label, evidence FROM asset_driver_nodes "
        "WHERE active=1 AND kind='condition' ORDER BY id").fetchall()

    plan = []
    for r in rows:
        ev = cg._parse_evidence(r["evidence"] or "")
        text = " ".join([str(r["label"] or "")] + [str(x) for x in (ev.get("chain") or [])])
        # v433：文本若是我们自己"按同族模板整理"的（_text_src=derived_*），
        # 拿它去和链库比对等于自己给自己作证（#583 因此被 1.00 认成链 716）。
        # 这类节点一律不给 chain_backed，保持 annotation。
        derived = str(ev.get("_text_src") or "").startswith("derived")
        m = None if derived else best(idx, r["asset"], text, args.min_score)
        plan.append((r, ev, m))

    linked = [p for p in plan if p[2]]
    pure = [p for p in plan if not p[2]]
    print("[condition] 共 %d 条：有链可验 %d / 纯注释 %d" % (len(plan), len(linked), len(pure)))
    for r, ev, m in linked[:6]:
        print("  链接 #%s %s %s → chain_lib:%d (%.2f)"
              % (r["id"], r["asset"], str(r["label"] or "")[:24], m["id"], m["score"]))

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "condition_tier_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["node_id", "asset", "label", "tier", "chain_id", "score", "n_cond"])
        for r, ev, m in plan:
            w.writerow([r["id"], r["asset"], (r["label"] or "")[:120],
                        "chain_backed" if m else "annotation",
                        m["id"] if m else "", m["score"] if m else "",
                        int(ev.get("_cond_n") or 0)])
    print("[condition] 明细: %s" % path)

    if not args.apply:
        print("[condition] dry-run，未写库（加 --apply 执行）")
        return

    done = 0
    for r, ev, m in plan:
        ev2 = json.loads(json.dumps(ev))
        if m:
            ev2["_tier"] = "chain_backed"
            ev2["_source"] = "chain_lib:%d(src=tier)" % m["id"]
        else:
            ev2["_tier"] = "annotation"
            # 判成注释就别留链库出处，否则"来源=chain_lib:xx"和"不进 prompt"自相矛盾
            if str(ev2.get("_source") or "").startswith("chain_lib:"):
                ev2["_source"] = ev2.get("_text_src") or None
        for attempt in range(30):
            try:
                conn.execute("UPDATE asset_driver_nodes SET evidence=?, updated_at=? WHERE id=?",
                             (cg._cap_evidence(json.dumps(ev2, ensure_ascii=False)),
                              cg._now(), int(r["id"])))
                done += 1
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                time.sleep(0.5 * (attempt + 1))
        if done % 50 == 0:
            conn.commit()
    conn.commit()
    print("[condition] 已写入 %d 条" % done)
    conn.close()


if __name__ == "__main__":
    main()
