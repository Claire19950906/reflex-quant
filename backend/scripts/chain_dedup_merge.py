#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chain_dedup_merge.py — v407: 因果链重复合并（可逆，默认 dry-run）

实测问题（2026-11-17 只读审计 trident_event_bus.db）
----------------------------------------------------
753 条链里按 (asset + 归一名前 14 字) 判重，有 25 个重复组、**333 条冗余（44.2%）**。
典型例子是 5 条几乎一模一样的"沙特OSP下调→WTI 偏弱"：每条命中样本被切碎成 2~3 个，
永远攒不到 chain_prior 要求的 n≥12，于是"学得再准也影响不到下单"。

规则
----
1. 分组键：asset + name 归一化（去标点/空格，取前 14 字）；命中则同组。
2. 保留者（keeper）优先级：有结算样本多 > 启用 > active > updated_at 新 > id 小。
3. outcomes 归属重指：`causal_chain_outcomes.chain_id` → keeper（样本合并，不丢）。
4. 非保留者：enabled=0 / active=0，`invalidation` 写"v407 去重合并到 #<keeper>"，
   行不删（一条 UPDATE 即可恢复）。
5. 保留者 trigger_words = 组内净化后词表并集（上限 12，走 chain_keywords 口径）。

用法
----
    python3 scripts/chain_dedup_merge.py            # dry-run
    python3 scripts/chain_dedup_merge.py --apply    # 落库
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src_python"
sys.path.insert(0, str(SRC))

DB_PATH = BACKEND / "trident_event_bus.db"
AUDIT_DIR = BACKEND / "runtime" / "chain_cleanup"
NAME_KEY_LEN = 14
MAX_KEYWORDS = 12

_PUNCT_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")


def _norm_name(s: str) -> str:
    return _PUNCT_RE.sub("", str(s or "")).lower()


def _split_words(raw: str) -> List[str]:
    out: List[str] = []
    for part in re.split(r"[,，、;；|/\s]+", str(raw or "")):
        p = part.strip()
        if p and p.lower() not in {x.lower() for x in out}:
            out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="因果链重复合并 (v407)")
    ap.add_argument("--apply", action="store_true", help="真写主库 (默认 dry-run)")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row

    settled = {int(r[0]): int(r[1]) for r in conn.execute(
        "SELECT CAST(chain_id AS INTEGER), COUNT(*) FROM causal_chain_outcomes "
        "WHERE status IN ('HIT','HIT_WEAK','MISS') GROUP BY 1")}

    rows = conn.execute(
        "SELECT id, asset, name, cause, effect, trigger_words, trigger_keywords, "
        "       enabled, COALESCE(active,1) AS active, confidence, weight, "
        "       updated_at, origin FROM causal_chains"
    ).fetchall()

    groups: Dict[Tuple[str, str], List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = (str(r["asset"] or "UNKNOWN"), _norm_name(r["name"] or r["cause"] or "")[:NAME_KEY_LEN])
        groups[key].append(r)

    plans: List[Dict] = []
    try:
        from chain_keywords import SYSTEM_TOKENS as _SYS_TOKENS
    except Exception:
        _SYS_TOKENS = set()

    for key, members in groups.items():
        if len(members) < 2:
            continue
        # 全组都已停用的（如 287 条 auto_from_insight 模板链）没有可回收价值，
        # 合并没有收益，反而会把垃圾词并进保留者 —— 跳过。
        if not any(int(m["enabled"] or 0) for m in members):
            continue

        def rank(r: sqlite3.Row):
            return (
                settled.get(int(r["id"]), 0),
                int(r["enabled"] or 0),
                int(r["active"] or 0),
                str(r["updated_at"] or ""),
                -int(r["id"]),
            )

        members_sorted = sorted(members, key=rank, reverse=True)
        keeper, dups = members_sorted[0], members_sorted[1:]
        merged_words: List[str] = []
        for m in [keeper] + dups:
            for w in _split_words(m["trigger_words"]) + _split_words(m["trigger_keywords"]):
                if w.lower() in {str(t).lower() for t in _SYS_TOKENS}:
                    continue
                if len(w) > 20:
                    continue
                if w not in merged_words:
                    merged_words.append(w)
        plans.append({
            "key": f"{key[0]}|{key[1]}",
            "asset": key[0],
            "keeper": int(keeper["id"]),
            "keeper_name": str(keeper["name"])[:60],
            "dup_ids": [int(d["id"]) for d in dups],
            "merged_keywords": merged_words[:MAX_KEYWORDS],
            "samples_before": sum(settled.get(int(m["id"]), 0) for m in members),
        })

    total_dups = sum(len(p["dup_ids"]) for p in plans)
    print("库: %s" % DB_PATH)
    print("总链 %d · 重复组 %d · 待合并冗余 %d 条" % (len(rows), len(plans), total_dups))
    for p in sorted(plans, key=lambda x: -len(x["dup_ids"]))[:8]:
        print("  %-28s keeper#%-4d 合并 %d 条 样本合计=%d | %s"
              % (p["key"][:28], p["keeper"], len(p["dup_ids"]),
                 p["samples_before"], p["keeper_name"][:30]))

    if not args.apply:
        print("（dry-run：未写库。加 --apply 落库）")
        return 0

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    applied = 0
    moved = 0
    for p in plans:
        keeper = p["keeper"]
        conn.execute(
            "UPDATE causal_chains SET trigger_words=?, updated_at=? WHERE id=?",
            (",".join(p["merged_keywords"]), now, keeper),
        )
        for did in p["dup_ids"]:
            # 同一次触发在保留者下已有记录 → 唯一键冲突，先删重复行再重指
            conn.execute(
                "DELETE FROM causal_chain_outcomes WHERE CAST(chain_id AS INTEGER)=? "
                "AND EXISTS (SELECT 1 FROM causal_chain_outcomes k "
                "            WHERE CAST(k.chain_id AS INTEGER)=? "
                "              AND k.trigger_ts=causal_chain_outcomes.trigger_ts "
                "              AND k.trigger_ref_id=causal_chain_outcomes.trigger_ref_id)",
                (did, keeper),
            )
            cur = conn.execute(
                "UPDATE causal_chain_outcomes SET chain_id=? WHERE CAST(chain_id AS INTEGER)=?",
                (str(keeper), did),
            )
            moved += cur.rowcount or 0
            conn.execute(
                "UPDATE causal_chains SET enabled=0, active=0, invalidation=?, updated_at=? "
                "WHERE id=?",
                ("v407 去重合并到 #%d（%s）" % (keeper, p["keeper_name"][:40]), now, did),
            )
            applied += 1
    conn.commit()

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit = AUDIT_DIR / ("dedup-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    with audit.open("w", encoding="utf-8") as fh:
        for p in plans:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    print("已落库: 合并停用 %d 条 · outcomes 重指 %d 条" % (applied, moved))
    print("审计: %s" % audit)
    n_on = conn.execute("SELECT COUNT(*) FROM causal_chains WHERE enabled=1").fetchone()[0]
    print("剩余 enabled=1: %d 条" % n_on)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
