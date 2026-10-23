"""
recompute_outcomes.py — v302+ P2 一次性脚本
=========================================
把 PENDING outcome 的 eval_deadline_ts 强制改成 now-1 (即过期), 让 evaluator 可以评估.
也把已 evaluated 的 outcomes 用新 HIT_WEAK 规则重新评估 (3 条旧样本: 2 MISS + 1 NEUTRAL).

跑法:
  python3 recompute_outcomes.py
"""
import sqlite3
import time
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from causal_chain_evaluator import evaluate_one, evaluate_pending


def main():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    # 1) 把所有 PENDING 的 deadline 改成过去, 让 evaluator 可以评估
    cur = conn.execute("SELECT COUNT(*) FROM causal_chain_outcomes WHERE status='PENDING'")
    n_pending = cur.fetchone()[0]
    conn.execute(
        "UPDATE causal_chain_outcomes SET eval_deadline_ts=? WHERE status='PENDING'",
        (int(time.time()) - 1,),
    )
    conn.commit()
    print(f"[1/3] 把 {n_pending} 条 PENDING 的 eval_deadline_ts 强制改为过去 (now-1)")
    # 2) 跑 evaluate_pending (用新 HIT_WEAK 规则)
    result = evaluate_pending(conn)
    print(f"[2/3] evaluate_pending: {result}")
    # 3) 统计最终状态
    rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM causal_chain_outcomes GROUP BY status"
    ).fetchall()
    print(f"[3/3] 最终状态分布:")
    for r in rows:
        print(f"  {r['status']}: {r['n']}")
    # 4) 算 direction_hit_rate
    hit_n = next((r['n'] for r in rows if r['status'] == 'HIT'), 0)
    weak_n = next((r['n'] for r in rows if r['status'] == 'HIT_WEAK'), 0)
    miss_n = next((r['n'] for r in rows if r['status'] == 'MISS'), 0)
    neu_n = next((r['n'] for r in rows if r['status'] == 'NEUTRAL'), 0)
    total_eval = hit_n + weak_n + miss_n + neu_n
    if total_eval > 0:
        direction_hit = hit_n + weak_n
        print(f"\n  real_hit_rate (HIT only): {hit_n}/{total_eval} = {hit_n/total_eval:.1%}")
        print(f"  direction_hit_rate (HIT+HIT_WEAK): {direction_hit}/{total_eval} = {direction_hit/total_eval:.1%}")
    conn.close()


if __name__ == "__main__":
    main()