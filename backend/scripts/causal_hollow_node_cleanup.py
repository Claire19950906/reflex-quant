#!/usr/bin/env python3
"""清理因果图谱里的"空壳节点"（v427）。

问题：实测 951 个在册节点里 733 个 kind=chain 的节点 `evidence.chain` 为空 ——
它们有名字、没有机制文本，于是：
  * 方向无法推断（只能从标签里猜，实测把"鹰派→金价跌"猜成涨）
  * 无法挂链库、无法拿到条件样本
  * 却照样进 get_knowledge / inject_block，占核心与条件名额
清理策略（两步，先补后删）：
  1. **补**：用严格文本匹配（阈值 0.7）把链库里的机制文本填进节点，填得上的保留。
  2. **下线**：填不上的把 active 置 0（行保留，可回滚），evidence 打 `_hollow` 标记。

默认 dry-run；加 `--apply` 才写库。写库时输出审计 CSV（可逐条回滚）。
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
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "node_cleanup")


def _hollow(ev):
    return not (ev.get("chain") or [])


def _best_text_chain(idx, asset, text, min_score):
    """只按文本找最佳链（补机制文本用，不要求链有方向样本）。

    注意不能复用 `_match_chain_samples`：它现在要求"方向样本>0"，否则返回空，
    而补机制文本只需要语义对得上 —— 样本是核验阶段的事。
    """
    asset_u = str(asset or "").upper()
    grams = cg._bigrams(text)
    if not grams:
        return None
    best = None
    for c in idx:
        if c["asset"] and c["asset"] not in (asset_u, "MACRO"):
            continue
        if not c["grams_name"]:
            continue
        score = max(len(c["grams_name"] & grams) / len(c["grams_name"]),
                    len(c["grams_full"] & grams) / max(1, len(grams)))
        if score < min_score:
            continue
        if best is None or score > best["score"]:
            best = {"id": int(c["id"]), "name": c["name"], "score": round(score, 3)}
    return best


def scan(conn, match_min=0.7):
    """返回 [(row, ev, best_chain|None)]，best_chain 为 None 表示补不上。"""
    idx = cg._build_chain_index(conn)
    out = []
    rows = conn.execute(
        "SELECT id, asset, label, kind, is_core, evidence FROM asset_driver_nodes WHERE active=1"
    ).fetchall()
    for r in rows:
        ev = cg._parse_evidence(r["evidence"] or "")
        if not _hollow(ev):
            continue
        if r["kind"] in ("asset", "macro", "evidence"):
            # 结构锚点：不是"驱动"，没有机制文本是设计如此，不动
            continue
        label = str(r["label"] or "").strip()
        m = _best_text_chain(idx, r["asset"], label, match_min) if (idx and label) else None
        out.append((r, ev, m))
    return out


def fill_chain(conn, chain_id, label):
    """把链库里的机制文本组装成节点 chain 层。"""
    row = conn.execute(
        "SELECT name, cause, effect, mechanism FROM causal_chains WHERE id=?", (chain_id,)
    ).fetchone()
    if not row:
        return None
    layers = [str(row[0] or "").strip()]
    for k in (1, 2):
        v = str(row[k] or "").strip()
        if v and v.upper() != "NONE" and v not in layers:
            layers.append(v)
    layers = [x for x in layers if x]
    return layers or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--match-min", type=float, default=0.7, help="补链的严格阈值")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    cands = scan(conn, match_min=args.match_min)
    fillable = [(r, ev, m) for r, ev, m in cands if m]
    drop = [(r, ev, m) for r, ev, m in cands if not m]
    print(f"[hollow] 空壳驱动节点 {len(cands)}：可补机制 {len(fillable)}，需下线 {len(drop)}")
    for r, ev, m in fillable:
        print(f"  FILL #{r['id']} {r['asset']} «{r['label'][:26]}» → 链{m['id']} 分{m['score']}")

    if not args.apply:
        print("\n（dry-run，未写库；加 --apply 执行）")
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(OUT_DIR, exist_ok=True)
    audit_path = os.path.join(OUT_DIR, f"hollow_nodes_{stamp}.csv")
    filled = dropped = 0
    batch = 0

    def _commit_batch():
        """每批提交一次 + 遇锁退避重试（生产库有其它 worker 在写）。"""
        nonlocal batch
        for _ in range(30):
            try:
                conn.commit()
                batch = 0
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e):
                    raise
                time.sleep(1.0)
        raise RuntimeError("数据库长时间被占用，已放弃（未提交的批次会回滚）")

    with open(audit_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["node_id", "asset", "label", "kind", "action", "chain_ids", "prev_evidence"])
        for r, ev, m in fillable:
            cid = int(m["id"])
            layers = fill_chain(conn, cid, r["label"])
            if not layers:
                continue
            ev["chain"] = layers
            ev["_source"] = f"chain_lib:{cid}"
            ev["_status"] = ev.get("_status") or cg._STATUS_UNCONFIRMED
            ev["_hollow_filled_at"] = int(time.time())
            conn.execute(
                "UPDATE asset_driver_nodes SET evidence=?, updated_at=? WHERE id=?",
                (cg._cap_evidence(json.dumps(ev, ensure_ascii=False)), cg._now(), int(r["id"])),
            )
            w.writerow([r["id"], r["asset"], r["label"], r["kind"], "fill", cid,
                        (r["evidence"] or "")[:500]])
            filled += 1
            batch += 1
            if batch >= 100:
                fh.flush()
                _commit_batch()
        for r, ev, m in drop:
            ev["_hollow"] = 1
            ev["_hollow_reason"] = "无机制文本（空壳），由 v427 清理下线"
            ev["_hollow_at"] = int(time.time())
            conn.execute(
                "UPDATE asset_driver_nodes SET active=0, evidence=?, updated_at=? WHERE id=?",
                (cg._cap_evidence(json.dumps(ev, ensure_ascii=False)), cg._now(), int(r["id"])),
            )
            w.writerow([r["id"], r["asset"], r["label"], r["kind"], "deactivate", "", ""])
            dropped += 1
            batch += 1
            if batch >= 100:
                fh.flush()
                _commit_batch()
    _commit_batch()
    remain = conn.execute(
        "SELECT COUNT(*) FROM asset_driver_nodes WHERE active=1"
    ).fetchone()[0]
    print(f"[hollow] 已补机制 {filled}，已下线 {dropped}；在册节点 {remain}")
    print(f"[hollow] 审计: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
