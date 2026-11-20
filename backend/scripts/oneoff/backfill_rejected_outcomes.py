#!/usr/bin/env python3
"""补 REJECTED 反思的方向结算（用户拍板的口径，2026-11-13）。

背景：trade_reflections_v2 里 2,910 条 action='REJECTED'，settled=1 但 hit=0、
signal_name 空、actual_outcome_pct 多半为 NULL —— 这些行其实"从没被验证过"，
却在统计里占着分母，把命中率拖成 0。

口径（用户确认）：
  拒绝 = 系统当时没让按这个方向入场。事后看价格走法，判断"拒绝得对不对"。
    · 信号方向由 ai_decisions 决定（suggested_action，缺失时用 sentiment_score 正负）
    · 方向 BUY（看多）被拒 → 之后跌了 = 拒对(hit=1)；涨了 = 拒错(hit=0)
    · 方向 SELL（看空）被拒 → 之后涨了 = 拒对(hit=1)；跌了 = 拒错(hit=0)
    · 变化幅度不到 ±0.5% → 方向未验证，hit 置 NULL（不硬判对错）
    · 决策本身是 HOLD / 分数为 0 / 无决策 → 无方向，同样不判
  只在有真实 forward 价时才写；取不到就保持原样，绝不编。

用法：
  python3 backfill_rejected_outcomes.py            # 只报告
  python3 backfill_rejected_outcomes.py --commit   # 写库
"""
import argparse
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Optional, Tuple

import db as _db

DB_PATH = _db.get_db_path()
FLAT_PCT = 0.5          # 幅度门槛：不到 ±0.5% 视为方向未验证
SETTLE_DAYS = 1         # 拒绝判定用"次日"反应，更贴近"这次该不该入场"


def _parse_ts(raw) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return int(datetime.strptime(s[:26], fmt).timestamp())
        except ValueError:
            continue
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _direction(conn: sqlite3.Connection, decision_id: Optional[int],
               ai_decision_id: Optional[int] = None) -> Tuple[Optional[str], str]:
    """返回 ('BUY'|'SELL'|None, 依据说明)。

    真源是 trade_decisions.intended_action（"本来想买/想卖"），reflections 里的
    decision_id 指向它。ai_decision_id 是另一套 id 空间，实测 1,838 条全部指向
    已不存在的 id（2143~2460，而 ai_decisions 从 3000+ 起），所以只能当兜底。
    """
    if decision_id:
        row = conn.execute(
            "SELECT intended_action, final_action FROM trade_decisions WHERE id=?",
            (int(decision_id),),
        ).fetchone()
        if row:
            act = str(row[0] or "").upper()
            if act in ("BUY", "SELL"):
                return act, f"意向={act}(终态={row[1]})"
    if not decision_id:
        return None, "无决策记录"
    row = conn.execute(
        "SELECT suggested_action, sentiment_score, created_at FROM ai_decisions WHERE id=?",
        (int(decision_id),),
    ).fetchone()
    if not row:
        return None, "决策已不存在"
    action = str(row[0] or "").upper()
    if action in ("BUY", "SELL"):
        return action, f"决策={action}"
    try:
        score = float(row[1] or 0)
    except (TypeError, ValueError):
        score = 0.0
    if score >= 0.5:
        return "BUY", f"决策HOLD但情绪+{score:.2f}"
    if score <= -0.5:
        return "SELL", f"决策HOLD但情绪{score:.2f}"
    return None, f"无方向(决策={action or '空'}, 情绪={score:.2f})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA busy_timeout=30000")
    stats = Counter()
    updates = []

    rows = conn.execute(
        """SELECT id, asset, ai_decision_id, created_at, actual_outcome_pct, hit,
                  decision_id
           FROM trade_reflections_v2 WHERE action='REJECTED'"""
    ).fetchall()

    for r in rows:
        rid, asset, did, created, old_out, old_hit, dec_id = (
            r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        )
        stats["总行"] += 1
        direction, why = _direction(conn, dec_id, did)
        if not direction:
            stats["无方向"] += 1
            continue

        ts = _parse_ts(created)
        if not ts:
            stats["时间不可解析"] += 1
            continue
        entry = _db.price_at(conn, asset, ts)
        if not entry or entry <= 0:
            stats["无入场价"] += 1
            continue
        outcome = _db.forward_outcome_pct(conn, asset, ts, entry, settle_days=SETTLE_DAYS)
        if outcome is None:
            stats["无远期价"] += 1
            continue

        stats["可结算"] += 1
        if abs(outcome) < FLAT_PCT:
            hit = None                       # 方向未验证，不硬判
            stats["方向未验证"] += 1
        elif direction == "BUY":
            hit = 1 if outcome < 0 else 0    # 看多被拒、之后跌了 = 拒对
            stats["拒对" if hit else "拒错"] += 1
        else:
            hit = 1 if outcome > 0 else 0
            stats["拒对" if hit else "拒错"] += 1

        evidence = f"reject_eval:信号{why}·次日{outcome:+.2f}%"
        if hit is None:
            evidence += "·幅度不足未判"
        updates.append((round(outcome, 3), hit, evidence, rid))

    print("=" * 60)
    print("REJECTED 反思方向结算")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k:<12} {v}")
    if updates:
        judged = [u for u in updates if u[1] is not None]
        n_ok = sum(1 for u in judged if u[1] == 1)
        print(f"\n  可写入 {len(updates)} 条，其中判了方向的 {len(judged)} 条，"
              f"拒对 {n_ok} 条 → 拒绝正确率 {n_ok/max(len(judged),1)*100:.1f}%")

    if not args.commit:
        print("\n[DRY-RUN] 未写库。加 --commit 生效。")
        conn.close()
        return 0

    t0 = time.time()
    for outcome, hit, evidence, rid in updates:
        conn.execute(
            "UPDATE trade_reflections_v2"
            " SET actual_outcome_pct=?, hit=?, settled=1, subcategory_evidence=?"
            " WHERE id=?",
            (outcome, hit, evidence, rid),
        )
    conn.commit()
    print(f"\n[COMMIT] 已写入 {len(updates)} 行 ({time.time()-t0:.1f}s)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
