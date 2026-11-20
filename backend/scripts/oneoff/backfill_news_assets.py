#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_news_assets.py —— 给「无品种映射」的新闻补品种 (v337)

背景
----
能力验收 C4「事件状态积压」实测：31,487 条新闻里 2,778 条 PENDING，且**全部**
是无品种映射的孤儿（8.82% > 5% 阈值 → FAIL）。孤儿进不了打分、进不了样本池，
额度恢复后也只能排在队列末尾。

更上游的问题：全表 31,452 行里 28,327 行 `assets` 为空（90%）——"这条新闻利好谁"
在源头就答不出，需求 #13 直接失效。

本脚本用现成的 `news_asset_router.route()`（v335 规则：只认 direct 点名 /
transmit 强传导，weak 一律不动）给这些行补主品种，写回 `assets`。
宁可不补，也不把新闻推到影响不大的品种上 —— 与 backfill_asset_routing 同一口径。

用法:
    python3 backfill_news_assets.py --limit 3000            # 只报
    python3 backfill_news_assets.py --limit 3000 --commit   # 落库
    python3 backfill_news_assets.py --pending-only --commit # 只补 PENDING 积压
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db # noqa: E402
from feeds import news_asset_router as router  # noqa: E402

ACCEPT_VERDICTS = ("direct", "transmit")


def run(limit: int = 3000, commit: bool = False, pending_only: bool = False,
        days: int = 30) -> dict:
    t0 = time.time()
    conn = sqlite3.connect(str(db.get_db_path()), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        # `asset` 是口径列（验收 C4 就按它判孤儿），`assets` 是采集端的原始标注列；
        # 实测 2,780 条 PENDING 里 asset 全是字面量 'NONE'（不是 NULL），
        # 只补 assets 不动 asset → 孤儿数一条都不会变。两列都要补。
        where = [
            "COALESCE(asset,'') IN ('','NONE')",
            "COALESCE(headline,'')<>''",
            f"ts >= strftime('%s','now') - {int(days)}*86400",
        ]
        if pending_only:
            where.append("status='PENDING'")
        rows = conn.execute(
            f"""SELECT id, headline, body, source, status, ts
                  FROM news_events
                 WHERE {' AND '.join(where)}
                 ORDER BY id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        out = {"scanned": len(rows), "mapped": 0, "by_verdict": {}, "by_asset": {},
               "commit": commit, "samples": [], "elapsed_sec": 0.0}
        for r in rows:
            try:
                res = router.route(r["headline"] or "", r["body"] or "")
            except Exception:  # noqa: BLE001
                continue
            verdict = res.get("verdict") or "none"
            out["by_verdict"][verdict] = out["by_verdict"].get(verdict, 0) + 1
            if verdict not in ACCEPT_VERDICTS or not res.get("primary"):
                continue
            asset = res["primary"][0]["asset"]
            out["mapped"] += 1
            out["by_asset"][asset] = out["by_asset"].get(asset, 0) + 1
            if len(out["samples"]) < 8:
                out["samples"].append({
                    "id": r["id"], "asset": asset, "verdict": verdict,
                    "headline": (r["headline"] or "")[:50],
                })
            if commit:
                conn.execute(
                    "UPDATE news_events SET assets=?, asset=? WHERE id=? "
                    "AND (COALESCE(asset,'') IN ('','NONE') OR COALESCE(assets,'')='')",
                    (asset, asset, r["id"]),
                )
        if commit:
            conn.commit()
        out["elapsed_sec"] = round(time.time() - t0, 1)
        return out
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pending-only", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run(limit=args.limit, commit=args.commit,
              pending_only=args.pending_only, days=args.days)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(f"[news-assets] scanned={res['scanned']} mapped={res['mapped']} "
              f"({res['mapped'] / max(res['scanned'], 1):.1%}) · "
              f"{'已写库' if res['commit'] else 'dry-run'} · {res['elapsed_sec']}s")
        print("  档位:", res["by_verdict"])
        print("  品种:", dict(sorted(res["by_asset"].items(), key=lambda x: -x[1])[:8]))
        for s in res["samples"]:
            print(f"  #{s['id']} → {s['asset']} ({s['verdict']}) {s['headline']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
