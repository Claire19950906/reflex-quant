"""回填 trade_reflections_v2.reflection_effective（v412-G）。

## 口径（先读这一段，别把 1 当成"这条反思赚了钱"）

`reflection_effective` 回答的是**"这条反思对自己的错因判断是否被独立测量证实"**，
不是"这条反思让后续交易变好了"。依据只有一个：`reflection_3layer.verdict`。

| 3layer.verdict | reflection_effective | 含义 |
|---|---|---|
| 验证 | 1 | 独立测量后，这条反思判的错因确实成立 |
| 否证 | 0 | 独立测量后，判的错因不成立（反思本身判错了） |
| 不可分离 | NULL | 客观缺数据，判不了 —— **保持 NULL，绝不写成 0** |
| 不适用 | NULL | 本来就没有错误，无需验证 |

铁律：NULL = 未验证，不得当 0 使用。所以这里**只写 1 和 0**，其余一律不动。
且只更新 `reflection_effective IS NULL` 的行 —— 幂等，可反复跑。

## 用法
    python3 reflection_effective_backfill.py --dry-run
    python3 reflection_effective_backfill.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB = BACKEND / "trident_event_bus.db"

MAP = {"验证": 1, "否证": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB.exists():
        print(f"[ERR] 库不存在: {DB}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(DB), timeout=90)
    try:
        conn.execute("PRAGMA busy_timeout=90000")

        # 先看能回填多少（只算 NULL 的，幂等）
        rows = conn.execute(
            """SELECT t.verdict, COUNT(*)
                 FROM reflection_3layer t
                 JOIN trade_reflections_v2 r ON r.id = t.reflection_id
                WHERE r.reflection_effective IS NULL
                  AND t.verdict IN ('验证','否证')
             GROUP BY t.verdict"""
        ).fetchall()
        plan = {v: n for v, n in rows}
        total = sum(plan.values())
        print(f"可回填 {total} 条（验证 {plan.get('验证', 0)} / 否证 {plan.get('否证', 0)}）")

        if args.dry_run or not total:
            # 顺手报一下"保持 NULL"的数量，避免以后有人以为漏了
            keep = conn.execute(
                """SELECT COUNT(*) FROM reflection_3layer t
                     JOIN trade_reflections_v2 r ON r.id = t.reflection_id
                    WHERE r.reflection_effective IS NULL
                      AND t.verdict IN ('不可分离','不适用')"""
            ).fetchone()[0]
            print(f"保持 NULL（不可分离/不适用）{keep} 条 —— 未验证不得当 0")
            return 0

        updated = 0
        for verdict, flag in MAP.items():
            for attempt in range(3):
                try:
                    with conn:
                        cur = conn.execute(
                            """UPDATE trade_reflections_v2
                                  SET reflection_effective = ?
                                WHERE reflection_effective IS NULL
                                  AND id IN (SELECT reflection_id FROM reflection_3layer
                                              WHERE verdict = ?)""",
                            (flag, verdict))
                    updated += cur.rowcount
                    break
                except sqlite3.OperationalError as e:
                    if attempt == 2:
                        raise
                    print(f"[BACKFILL] 锁冲突重试 {attempt + 1}/3: {e}", file=sys.stderr)
                    time.sleep(1.5 * (attempt + 1))

        print(f"已回填 {updated} 条")
        dist = conn.execute(
            "SELECT COALESCE(CAST(reflection_effective AS TEXT),'NULL'), COUNT(*) "
            "FROM trade_reflections_v2 GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        print("回填后分布: " + " / ".join(f"{k}={v}" for k, v in dist))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
