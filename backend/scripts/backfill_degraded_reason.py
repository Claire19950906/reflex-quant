#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 reason='target_none' 但已解析出标的的行，改标 asset_late_bound（v409）。

背景
----
`analysis_degraded_reason` 由 `classify_analysis_degraded()` 从 reasoning_path 的
措辞推出来（"目标资产 NONE" → target_none）。但路由会在落库之后补上
`target_asset`，于是出现「原因说没有标的、标的列却有 XAU/WTI/NVDA」这种自相矛盾：
· 读 S18「无标的占比」的人会以为这些新闻真没标的；
· 读 S16「降级率」的人会以为全是路由没打上；
· 复盘时会去找"为什么没标"，而真实情况是"模型没给出标的"。

两类原因必须分开：
  target_none      —— 到现在也没有可交易标的（NONE / 桶标签 / 空）
  asset_late_bound —— 模型判成 NONE，路由事后补了真实代码（分析仍是降级产物）

用法
----
    python3 scripts/backfill_degraded_reason.py              # dry-run，只统计
    python3 scripts/backfill_degraded_reason.py --commit     # 落库
    python3 scripts/backfill_degraded_reason.py --commit --limit 50

幂等：只改仍写着 target_none 且已解析出代码的行；跑第二遍是 0 行。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))


def _is_resolved(asset: str) -> bool:
    a = str(asset or "").strip().upper()
    if a in ("", "NONE", "OTHER", "NULL"):
        return False
    try:
        from asset_kinds import classify
        kind, _ = classify(a)
        # 桶标签（placeholder）不算"解析出来了"——它正是"没有代码"的那一档
        return kind in ("us", "a_share", "hk", "non_equity", "other_market")
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(str(BACKEND), "trident_event_bus.db"))
    ap.add_argument("--commit", action="store_true", help="真的写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少行（0=全部）")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, target_asset FROM ai_decisions "
            "WHERE analysis_degraded_reason='target_none' ORDER BY id"
        ).fetchall()
        cand = [(i, a) for i, a in rows if _is_resolved(a)]
        if args.limit and args.limit > 0:
            cand = cand[:args.limit]

        print(f"扫描：reason=target_none 共 {len(rows)} 行；其中已解析出标的 {len(cand)} 行"
              f"（本次处理 {len(cand)} 行）")
        by_asset: dict[str, int] = {}
        for _i, a in cand:
            k = str(a or "").strip().upper()
            by_asset[k] = by_asset.get(k, 0) + 1
        top = sorted(by_asset.items(), key=lambda x: -x[1])[:8]
        print("  标的分布：" + ", ".join(f"{k} {v}" for k, v in top))

        if not args.commit:
            print("dry-run：未写库。加 --commit 落库。")
            return 0

        done = 0
        for i, _a in cand:
            conn.execute(
                "UPDATE ai_decisions SET analysis_degraded_reason='asset_late_bound' "
                "WHERE id=? AND analysis_degraded_reason='target_none'", (int(i),))
            done += 1
        conn.commit()
        left = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE analysis_degraded_reason='target_none'"
        ).fetchone()[0]
        print(f"已改 {done} 行 → asset_late_bound；仍为 target_none 的剩 {left} 行")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
