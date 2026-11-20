"""backfill_sentiment_center.py —— 修正历史 sentiment_score 的中心偏移（可回滚）

背景（2026-11-14）：
  旧归一化把 0-100 档按 `v/100` 映射，而 prompt 规定 50 = 中性。
  于是 40（偏空）被存成 +0.40（强多），**任何 1~100 的输入都变成正数**。
  证据：suggested_action=SELL 的 146 条决策里 90 条情绪分为正 —— 方向与动作打架。

换算（(a)/(b) 两档在存库空间里是同一个式子）：
  new = 2*s - 1       仅对 s > 0 的行；s <= 0 的行不动（-1~1 档本来就以 0 为中心）
  例: +0.05 → -0.90 · +0.305 → -0.39 · +0.60 → +0.20 · +1.0 → +1.0

做法：
  · 只改 sentiment_score 一个字段，不改 suggested_action / reasoning / 权重
  · 写库前把原值存进 ai_decisions_sentiment_backfill（整表可回滚）
  · 默认 dry-run

用法：
  python3 backfill_sentiment_center.py            # 只看会改多少
  python3 backfill_sentiment_center.py --commit
  python3 backfill_sentiment_center.py --rollback
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
RB_TABLE = "ai_decisions_sentiment_backfill"


def candidates(conn: sqlite3.Connection, scope: str = "sell") -> List[Dict[str, Any]]:
    """默认只修 **SELL 且情绪分为正** 的行。

    为什么不一刀切全部正分：
      SELL + 正分 = 方向与动作自相矛盾，这条证据是硬的（90/146）。
      BUY + 正分 本身是自洽的，其中一部分可能是 (b) 档"0.5=中"的原始输出，
      一律按 (2s-1) 拉伸会**制造**新的矛盾（+0.219 的 BUY 会变成 -0.562）。
      只改能证明错的那批，不动只能猜的那批。
    """
    where = "sentiment_score > 0"
    if scope != "all":
        where += " AND UPPER(COALESCE(suggested_action,'')) = 'SELL'"
    rows = conn.execute(
        f"SELECT id, sentiment_score s FROM ai_decisions "
        f"WHERE sentiment_score IS NOT NULL AND {where}"
    ).fetchall()
    out = []
    for r in rows:
        old = float(r["s"])
        new = max(-1.0, min(1.0, round(2.0 * old - 1.0, 4)))
        out.append({"id": int(r["id"]), "old": old, "new": new,
                    "flip": new < 0 <= old})
    return out


def consistency(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return conn.execute(
        "SELECT suggested_action a, SUM(sentiment_score>0) pos, "
        "       SUM(sentiment_score<0) neg, COUNT(*) n "
        "FROM ai_decisions WHERE sentiment_score<>0 GROUP BY 1").fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="连 BUY/HOLD 的正分也一起拉伸（风险更高，会制造新矛盾）")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    if args.rollback:
        if RB_TABLE not in {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            print(f"没有 {RB_TABLE}，无法回滚")
            return 1
        n = conn.execute(f"SELECT COUNT(*) FROM {RB_TABLE}").fetchone()[0]
        conn.execute(
            "UPDATE ai_decisions SET sentiment_score = "
            f"(SELECT b.old_score FROM {RB_TABLE} b WHERE b.decision_id = ai_decisions.id) "
            f"WHERE id IN (SELECT decision_id FROM {RB_TABLE})")
        conn.commit()
        print(f"已回滚 {n} 条 sentiment_score")
        return 0

    rows = candidates(conn, "all" if args.all else "sell")
    n_all = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    flips = sum(1 for r in rows if r["flip"])
    print(f"ai_decisions {n_all} 条 · 待修正 {len(rows)} 条（s>0）"
          f" · 其中会翻成负数 {flips} 条")

    print("\n动作一致性（当前）: action / 正分 / 负分 / 合计")
    for r in consistency(conn):
        print(f"  {r['a']:<5} {r['pos']:>5} {r['neg']:>5} {r['n']:>6}")

    if not args.commit:
        print("\n[dry-run] 未写入。样例:")
        for r in rows[:5]:
            print(f"  id={r['id']} {r['old']:+.3f} → {r['new']:+.3f}")
        print("加 --commit 执行（会先建回滚表）")
        return 0

    import time as _t
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {RB_TABLE} ("
        f" decision_id INTEGER PRIMARY KEY, old_score REAL NOT NULL,"
        f" backed_up_at INTEGER NOT NULL)")
    now = int(_t.time())
    conn.executemany(f"INSERT OR REPLACE INTO {RB_TABLE} VALUES (?,?,?)",
                     [(r["id"], r["old"], now) for r in rows])
    conn.executemany("UPDATE ai_decisions SET sentiment_score = ? WHERE id = ?",
                     [(r["new"], r["id"]) for r in rows])
    conn.commit()

    print(f"\n[commit] 已改写 {len(rows)} 条 · 回滚表 {RB_TABLE}")
    print("动作一致性（修正后）:")
    for r in consistency(conn):
        print(f"  {r['a']:<5} {r['pos']:>5} {r['neg']:>5} {r['n']:>6}")
    print(f"撤销: python3 backfill_sentiment_center.py --rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
