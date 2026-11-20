#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""causal_meta_chain_cleanup.py — v424: 把"系统体检日志"从因果链库摘出去

背景
----
`causal_chain_expander` 早期把 review_insights 里所有孤儿记录都建成了因果链，
其中 290 条是系统对自己 prompt 的体检结论：

    cause  = "[覆盖度告警] Asset=NONE 经常漏报事件类型 …"
    effect = "NONE 资产价格/信号受影响（具体方向见 rule 全文）"

它们是占位符（effect 里没有方向、trigger_words 为空、hit_count=0），
既不可能触发、也不可能结算，却把"链库总量"和任何全库统计撑大了 38%。

实测：这 290 条**已经全是 enabled=0**（从没被启用过）。所以本脚本不做删除，
只做三件事：
  1) origin 标记为 `deprecated_meta`（下游统计一律排除）；
  2) note 前缀写清"v424 判为系统体检日志"；
  3) enabled 强制 0（幂等，防止被别的脚本误启用）。

原样保留行，随时可回滚（审计 CSV 里带全部字段）。

用法
    python3 scripts/causal_meta_chain_cleanup.py            # dry-run
    python3 scripts/causal_meta_chain_cleanup.py --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
AUDIT_DIR = BACKEND / "runtime" / "chain_cleanup"

# 判定条件：只要命中任意一条，就是"不是市场机制"的元信息链
COND = (
    "effect LIKE '%NONE%' "
    "OR effect LIKE '%具体方向见 rule 全文%' "
    "OR cause LIKE '%[覆盖度告警]%' "
    "OR cause LIKE '%Asset=NONE%' "
    "OR cause LIKE '%[真实性告警]%'"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        f"SELECT id, name, cause, effect, trigger_words, enabled, origin, "
        f"hit_count, miss_count, weight, created_at FROM causal_chains WHERE {COND} "
        f"ORDER BY id"
    ).fetchall()

    total = conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0]
    enabled_cnt = sum(1 for r in rows if int(r["enabled"] or 0) == 1)
    settled = sum(1 for r in rows if (int(r["hit_count"] or 0) + int(r["miss_count"] or 0)) > 0)
    kw = sum(1 for r in rows if str(r["trigger_words"] or "").strip())

    print(f"链库总量 {total}；命中判定 {len(rows)} 条（{len(rows) / max(total, 1):.1%}）")
    print(f"  其中 enabled=1: {enabled_cnt}")
    print(f"  有结算样本:    {settled}")
    print(f"  有触发词:      {kw}")
    print(f"  可按可回滚审计记录数: {len(rows)}")

    if not rows:
        print("没有需要处理的链。")
        return 0

    if not args.apply:
        print("\n[dry-run] 未写库。样例：")
        for r in rows[:3]:
            print(f"  #{r['id']} origin={r['origin']} enabled={r['enabled']} "
                  f"| {(r['cause'] or '')[:46]}… | {(r['effect'] or '')[:30]}…")
        print("\n加 --apply 执行。")
        return 0

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_csv = AUDIT_DIR / f"meta_chains_{stamp}.csv"
    with audit_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "name", "cause", "effect", "trigger_words", "enabled",
                    "origin_before", "origin_after", "hit_count", "miss_count", "weight"])
        for r in rows:
            w.writerow([r["id"], r["name"], r["cause"], r["effect"], r["trigger_words"],
                        r["enabled"], r["origin"], "deprecated_meta",
                        r["hit_count"], r["miss_count"], r["weight"]])

    note_tag = "[v424] 判为系统体检日志（非市场机制），不参与触发/统计"
    ids = [int(r["id"]) for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE causal_chains SET origin='deprecated_meta', enabled=0, "
        f"note=COALESCE(NULLIF(note,''),'') || ' ' || ?, "
        f"updated_at=datetime('now','localtime') WHERE id IN ({placeholders})",
        [note_tag] + ids,
    )
    conn.commit()

    after_enabled = conn.execute(
        f"SELECT COUNT(*) FROM causal_chains WHERE id IN ({placeholders}) AND enabled=1", ids
    ).fetchone()[0]
    print(f"\n[apply] 已标记 {len(ids)} 条；审计: {audit_csv}")
    print(f"[apply] 仍在启用中的: {after_enabled}")
    print(json.dumps({"marked": len(ids), "audit": str(audit_csv),
                      "still_enabled": int(after_enabled)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
