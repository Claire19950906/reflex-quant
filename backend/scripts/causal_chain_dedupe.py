#!/usr/bin/env python3
"""链库近重复清理（v427）。

为什么需要：扩触发覆盖时发现两件事叠在一起放大样本——
  ① 触发冷却放宽后单链样本变多，但链库本身是**同一机制的多份副本**
     （"中东地缘升级→原油偏多"在库里存在 3 份，"地缘升温但利率走强→黄金
     冲高回落"存在 3 份，表述不同、触发词几乎一样）；
  ② 副本之间抢同一批事件：它们各自记分，意味着同一件事被重复计成多份
     独立样本，Wilson 下界会虚高，且注入 prompt 时同一条机制说三遍。

判重口径（保守，宁少不多）：
    同品种 + 同 expected_direction + 触发词 Jaccard>=0.70
    + (cause+effect+mechanism) 字符二元组包含率>=0.30
每簇保留**已结算样本最多**的那条（并列取先建的），其余 origin 标
'deprecated_meta'、enabled=0（与脏链/元信息链同一套排除口径，可回滚）。

用法：
    python3 scripts/causal_chain_dedupe.py            # 只看
    python3 scripts/causal_chain_dedupe.py --apply    # 写库
"""
import argparse
import csv
import itertools
import os
import sqlite3
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "src_python"))

import causal_graph as cg

KW_JACCARD = 0.70
TEXT_RATIO = 0.30


def _kwset(raw):
    return set(k.strip() for k in (raw or "").replace("，", ",").split(",") if k.strip())


def _text(row):
    return " ".join([row["cause"] or "", row["effect"] or "", row["mechanism"] or ""])


def _load(conn):
    return conn.execute(
        """SELECT c.id, c.asset, c.cause, c.effect, c.mechanism,
                  c.expected_direction, c.trigger_keywords, c.created_at,
                  COUNT(o.id) AS n
           FROM causal_chains c
           LEFT JOIN causal_chain_outcomes o
             ON o.chain_id = c.id AND o.status <> 'PENDING'
           WHERE c.enabled = 1 AND c.origin != 'deprecated_meta'
           GROUP BY c.id
           ORDER BY c.id"""
    ).fetchall()


def find_clusters(rows):
    """并查集聚类；返回 [(keeper_row, [dup_rows...]), ...]。"""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in itertools.combinations(rows, 2):
        if (a["asset"] or "").upper() != (b["asset"] or "").upper():
            continue
        if (a["expected_direction"] or "BOTH").upper() != \
                (b["expected_direction"] or "BOTH").upper():
            continue
        ka, kb = _kwset(a["trigger_keywords"]), _kwset(b["trigger_keywords"])
        jac = len(ka & kb) / max(1, len(ka | kb))
        if jac < KW_JACCARD:
            continue
        if cg._contain_ratio(_text(a), _text(b)) < TEXT_RATIO:
            continue
        union(a["id"], b["id"])

    groups = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)

    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (-int(r["n"] or 0), int(r["id"])))
        out.append((members[0], members[1:]))
    return out


def apply_disable(conn, ids, notes):
    done = 0
    for cid, note in zip(ids, notes):
        for attempt in range(30):
            try:
                cur = conn.execute(
                    "UPDATE causal_chains SET origin='deprecated_meta', enabled=0, "
                    "note=COALESCE(note,'')||? WHERE id=?",
                    (note, cid),
                )
                conn.commit()
                done += cur.rowcount
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                time.sleep(0.5 * (attempt + 1))
        else:
            print("  [warn] 链 %s 写入仍被锁，跳过" % cid)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH")
                    or os.path.join(BACKEND, "trident_event_bus.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=20)
    conn.row_factory = sqlite3.Row
    rows = _load(conn)
    clusters = find_clusters(rows)
    dup_rows = [r for _, dups in clusters for r in dups]

    print("[dedupe] 启用链 %d | 重复簇 %d | 涉及 %d 条 | 可下线 %d"
          % (len(rows), len(clusters),
             sum(1 + len(d) for _, d in clusters), len(dup_rows)))
    for keeper, dups in sorted(clusters, key=lambda x: -len(x[1]))[:10]:
        print("  保留 #%d(%s n=%d) ← 下线 %s"
              % (keeper["id"], keeper["asset"], keeper["n"],
                 ", ".join("#%d(n=%d)" % (d["id"], d["n"]) for d in dups)))

    out_dir = os.path.join(BACKEND, "runtime", "chain_dedupe")
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "chain_dedupe_%s.csv"
                            % time.strftime("%Y%m%d_%H%M%S"))
    if dup_rows:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["action", "chain_id", "asset", "n_settled",
                        "cause", "effect", "trigger_keywords", "merged_into"])
            for keeper, dups in clusters:
                for d in dups:
                    w.writerow(["disable", d["id"], d["asset"], d["n"],
                                d["cause"], d["effect"], d["trigger_keywords"],
                                keeper["id"]])
            for keeper, dups in clusters:
                w.writerow(["keep", keeper["id"], keeper["asset"], keeper["n"],
                            keeper["cause"], keeper["effect"],
                            keeper["trigger_keywords"], ""])
        print("[dedupe] 明细: %s" % csv_path)

    if args.apply and dup_rows:
        notes = ["[deprecated_meta:v427 重复机制并入#%d]" % k["id"]
                 for k, dups in clusters for _ in dups]
        n = apply_disable(conn, [d["id"] for d in dup_rows], notes)
        print("[dedupe] 已下线 %d 条重复链" % n)
    elif args.apply:
        print("[dedupe] 无需下线")
    conn.close()


if __name__ == "__main__":
    main()
