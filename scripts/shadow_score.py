#!/usr/bin/env python3
"""影子打分 CLI：把 BUY/SELL 决策按 1h/4h/24h 的真实走势打分。

不依赖开仓 —— 决策被闸门拦掉、或 HOLD 之外的方向性判断，都能进样本。
这就是"一晚只成交几笔"时唯一能把样本量拉起来的办法。

用法：
    python3 scripts/shadow_score.py                 # 只打印（不写库）
    python3 scripts/shadow_score.py --commit        # 落库 shadow_scores
    python3 scripts/shadow_score.py --days 30       # 回看 30 天
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend", "src_python"))

import db
import shadow_score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    rows = shadow_score.compute_scores(conn, days_back=args.days, limit=args.limit)
    if args.commit:
        shadow_score.ensure_schema(conn)
        n = shadow_score.persist(conn, rows)
        print(f"写入 {n} 条 shadow_scores（共算出 {len(rows)} 条）")
    else:
        print(f"算出 {len(rows)} 条（未写库；加 --commit 落库）")

    stats = shadow_score.summarize(rows)
    if stats:
        print("\n| 指纹 | 窗口 | 样本 | 胜 | 负 | 胜率 | 均值净收益% |")
        print("|---|---|---|---|---|---|---|")
        for s in stats:
            h = next(lb for sec, lb in shadow_score.HORIZONS if sec == s["horizon_sec"])
            wr = f"{s['win_rate'] * 100:.1f}%" if s["win_rate"] is not None else "—"
            print(f"| `{s['fingerprint']}` | {h} | {s['n']} | {s['win']} | "
                  f"{s['loss']} | {wr} | {s['avg_net_pct']:+.3f} |")
        print("\n注：净收益已扣 8bps 往返成本；样本 < 30 不看胜率，只看方向和成本。")
    # 幂等：重复跑不会重复插入（UNIQUE(decision_id, horizon_sec)）
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
