#!/usr/bin/env python3
"""把有明确方向预期的注释节点转成可评估的链（v431）。

背景：99 个 condition 节点里 87 个没有方向预期（`_expect` 为空），转成链也只能
是 BOTH —— 而 BOTH 链的方向命中已被证明无意义（v429 清掉 611 条假命中）。
所以只转**有明确 up/down 预期**的 12 个，并且必须过三道闸门：
  1) 同品种无同机制链（复用 causal_chain_expander._same_mechanism_exists）；
  2) 触发词能从标签里抽出来（≥2 个，长度 2~20）；
  3) 30 天内真实触发 ≥3 次（空转的链不进库）。

默认 dry-run。
"""
import argparse
import csv
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))
import causal_graph as cg
import causal_chain_expander as cce
import chain_keywords as ck
import causal_chain_evaluator as ev

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "node_backfill")
MIN_TRIGGERS_30D = 3
_SPLIT = "、，,;；/()（）·|"


def _keywords(label):
    parts = [p.strip() for p in str(label or "").replace("（", "(").replace("）", ")")
             .replace("(", "|").replace(")", "|").split("|")]
    chunks = []
    for p in parts:
        chunks.extend(p.split("/"))
    kws = []
    for raw in chunks:
        for p in raw.replace("，", ",").split(","):
            p = p.strip(" 、;；·")
            if 2 <= len(p) <= ck.MAX_TOKEN_LEN:
                kws.append(p)
    out = []
    for k in kws:
        if k not in out and k.lower() not in ev._CHAIN_STOPWORDS:
            out.append(k)
    return out[:ck.MAX_KEYWORDS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, asset, label, evidence FROM asset_driver_nodes "
        "WHERE active=1 AND kind='condition' ORDER BY id").fetchall()

    cands = []
    for r in rows:
        evd = cg._parse_evidence(r["evidence"] or "")
        expect = (evd.get("_expect") or "").lower()
        if expect not in ("up", "down"):
            continue
        kws = _keywords(r["label"])
        if len(kws) < 2:
            continue
        dup = None
        try:
            dup = cce._same_mechanism_exists(conn, r["asset"], str(r["label"]), expect, kws)
        except Exception:
            dup = None
        cands.append({"node": r, "expect": expect, "kws": kws, "dup": dup})

    # 触发测频（用临时 ChainRow 跑新闻触发）
    for c in cands:
        if c["dup"]:
            c["fire"] = 0
            continue
        row = ev.ChainRow(chain_id=-int(c["node"]["id"]), asset=c["node"]["asset"],
                          trigger_keywords=c["kws"], expected_direction=c["expect"].upper(),
                          magnitude_lo=ev.DEFAULT_MAG_LO, magnitude_hi=ev.DEFAULT_MAG_HI,
                          confidence=0.5, cause=str(c["node"]["label"] or ""),
                          cause_tone=ev._tone_of(str(c["node"]["label"] or "")))
        n = 0
        try:
            for _ in ev.find_news_triggers(conn, row, 30):
                n += 1
        except Exception:
            n = 0
        c["fire"] = n

    ready = [c for c in cands if not c["dup"] and c["fire"] >= MIN_TRIGGERS_30D]
    print("[backfill] 有方向预期候选 %d 条 → 可建链 %d 条" % (len(cands), len(ready)))
    for c in cands:
        print("  #%s %s %-28s expect=%s 重复链=%s 30天触发=%d"
              % (c["node"]["id"], c["node"]["asset"], str(c["node"]["label"])[:28],
                 c["expect"], c["dup"], c["fire"]))

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "node_chain_backfill_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["node_id", "asset", "label", "expect", "keywords", "dup_chain", "fire_30d", "action"])
        for c in cands:
            w.writerow([c["node"]["id"], c["node"]["asset"], str(c["node"]["label"])[:100],
                        c["expect"], " / ".join(c["kws"]), c["dup"] or "", c["fire"],
                        "create" if c in ready else "skip"])
    print("[backfill] 明细: %s" % path)

    if not args.apply:
        print("（dry-run，加 --apply 执行）")
        return

    created = 0
    for c in ready:
        node = c["node"]
        evd = cg._parse_evidence(node["evidence"] or "")
        layers = evd.get("chain") or []
        cause = layers[0] if layers else str(node["label"])
        effect = layers[-1] if len(layers) > 1 else ""
        mech = " | ".join(str(x) for x in layers[1:-1]) or ""
        cur = conn.execute(
            """INSERT INTO causal_chains
               (name, asset, expected_direction, cause, effect, mechanism, trigger_words,
                enabled, origin, weight, created_at, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(node["label"])[:200], node["asset"], c["expect"].upper(), cause[:500],
             effect[:500], mech[:1000], "Asset,%s, %s" % (node["asset"], ", ".join(c["kws"])),
             1, "node_backfill", 0.5, int(time.time()),
             "v431 由 condition 节点 #%s 转链，触发实测 %d/30天" % (node["id"], c["fire"])))
        cid = cur.lastrowid
        conn.execute("UPDATE asset_driver_nodes SET evidence=? WHERE id=?",
                     (cg._cap_evidence(json_dumps_safe(evd, cid)), node["id"]))
        created += 1
    conn.commit()
    print("[backfill] 已建链 %d 条" % created)
    conn.close()


def json_dumps_safe(evd, cid):
    import json
    out = dict(evd)
    out["_source"] = "chain_lib:%d(src=node_backfill)" % cid
    out["_tier"] = "chain_backed"
    return json.dumps(out, ensure_ascii=False)


if __name__ == "__main__":
    main()
