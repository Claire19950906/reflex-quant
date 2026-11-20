#!/usr/bin/env python3
"""v331: 处理"verification_verdict='failed' 却仍 active"的 review_insights。

## 结论先行: 这 181 条一条都不该"清退"
`insight_verifier` 的判定链是:
    real_signal_pct = composite × sample_conf,  sample_conf = min(交易样本数/60, 1)
    real_signal_pct >= 0.7 → verified; >= 0.1 → partial; 其余 → failed

因为 sample_conf 是**乘性封顶**, 交易样本 n<=6 时最高分只有 0.1 (刚到 partial 门槛),
n=0 时恒为 0 —— 于是"样本太少"必然被判成"验证失败"。实测这批数据:
    n 分布 0→53, 1→17, 2→43, 3→10, 4→15, 5→39, 6→4  (**全部 < 12**)
    最高分 0.07                      (**全部 < 0.1**)

也就是说: 没有任何一条是"证据充分且被证伪", 全是**算术封顶**造成的假失败。
按原计划一刀切清退 181 条, 等于把"没验过"当"验错了", 白白丢掉 175 条本来
正被喂进 prompt 的规律 —— 这不是准确率优先, 这是误杀。

## 两档处理 (本脚本执行第 2 档, 第 1 档靠代码口径)
1. **标本兼治·口径**: `insight_verifier.verify_one` 已加 `MIN_TRADES_FOR_VERDICT=12`,
   n<12 且 K 线无明显否证 → `insufficient_sample` (新值), 不再扣 failed 帽子。
2. **存量重标**: 把库内这 181 条 `failed` 改判 `insufficient_sample`,
   **active 保持 1**(规律本身没被否证), 只留 record;
   同时 `ai_prompts.load_insight_rules()` 已把 `failed` 与 `insufficient_sample`
   都挡在 prompt 外 —— 未经验证的规律不再冒充"实战教训"。
   真到 n>=12 且被判 failed 的那天, 才由 `apply_verification --deactivate-failed` 停用。

## 用法
  python3 relabel_failed_insights.py            # dry-run
  python3 relabel_failed_insights.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from typing import Any

import db

# 与 insight_verifier.MIN_TRADES_FOR_VERDICT 保持一致
MIN_TRADES_FOR_VERDICT = 12

RELABEL = "relabel_insufficient"
RETIRE = "retire"
SKIP = "skip"


def classify(row: dict[str, Any], min_trades: int = MIN_TRADES_FOR_VERDICT) -> str:
    """纯函数: 决定一条 failed 规律的处置方式。

    · active != 1        → skip (已经不在用)
    · n < min_trades     → relabel (证据不足, 假失败)
    · n >= min_trades    → retire  (真证伪, 该停用)
    """
    if (row.get("verification_verdict") or "") != "failed":
        return SKIP
    if int(row.get("active") or 0) != 1:
        return SKIP
    n = int(row.get("verification_sample_n") or 0)
    return RELABEL if n < min_trades else RETIRE


def scan(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, asset, origin, rule, active, manual_deactivated, "
        "       verification_verdict, verification_score, verification_sample_n "
        "  FROM review_insights "
        " WHERE verification_verdict = 'failed' AND active = 1 "
        " ORDER BY verification_sample_n DESC, id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def plan(rows: list[dict[str, Any]], min_trades: int) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {RELABEL: [], RETIRE: []}
    for r in rows:
        action = classify(r, min_trades)
        if action in groups:
            groups[action].append(r)
    return {
        "relabel": groups[RELABEL],
        "retire": groups[RETIRE],
        "by_asset_relabel": Counter(r.get("asset") or "NONE" for r in groups[RELABEL]),
        "by_asset_retire": Counter(r.get("asset") or "NONE" for r in groups[RETIRE]),
        "min_trades": min_trades,
    }


def apply_plan(conn: sqlite3.Connection, res: dict[str, Any]) -> tuple[int, int]:
    n_relabel = n_retire = 0
    ids = [int(r["id"]) for r in res["relabel"]]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        conn.execute(
            "UPDATE review_insights SET verification_verdict = 'insufficient_sample' "
            f"WHERE id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        n_relabel += len(chunk)
    ids2 = [int(r["id"]) for r in res["retire"]]
    for i in range(0, len(ids2), 200):
        chunk = ids2[i:i + 200]
        conn.execute(
            "UPDATE review_insights SET active = 0, manual_deactivated = 1 "
            f"WHERE id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        n_retire += len(chunk)
    conn.commit()
    return n_relabel, n_retire


def main() -> None:
    p = argparse.ArgumentParser(description="重标假失败/停用真失败的经验规律")
    p.add_argument("--apply", action="store_true", help="真写库 (默认 dry-run)")
    p.add_argument("--min-trades", type=int, default=MIN_TRADES_FOR_VERDICT,
                   help=f"判定'证据充分'的最小交易样本数 (默认 {MIN_TRADES_FOR_VERDICT})")
    args = p.parse_args()

    conn = db.get_connection()
    rows = scan(conn)
    res = plan(rows, args.min_trades)

    print(f"待处理 (verdict=failed 且 active=1): {len(rows)} 条")
    print(f"  · 证据不足 → 重标 insufficient_sample: {len(res['relabel'])} 条  "
          f"{dict(res['by_asset_relabel'])}   (active 保持 1, 不进 prompt)")
    print(f"  · 真证伪   → 停用 (active=0):        {len(res['retire'])} 条  "
          f"{dict(res['by_asset_retire'])}")

    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return

    n_relabel, n_retire = apply_plan(conn, res)
    left = conn.execute(
        "SELECT COUNT(*) FROM review_insights WHERE verification_verdict='failed' AND active=1"
    ).fetchone()[0]
    print(f"\n已重标 {n_relabel} 条 → insufficient_sample; 已停用 {n_retire} 条")
    print(f"仍 failed 且 active: {left} 条")
    conn.close()


if __name__ == "__main__":
    main()
