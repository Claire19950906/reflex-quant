#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v404 · 反思表结算口径回填（reflection_truth_backfill）

为什么需要这个脚本
------------------
`v396` 修掉了结算器的日线兜底取价 bug（`db.price_at()` 用 `ORDER BY ts ASC`
取到了 6 天前的最老一根日线），但**反思表没有回填通道**：

    · 仓库里唯一的清理脚本 `fix_polluted_outcome_pct.py` 只处理 2026-10-26 那批
      `actual_outcome_pct`，且文件头明确"只动结算行（paper_run_id IS NULL）"；
    · 而污染行恰恰是 paper worker 写的（`paper_run_id IS NOT NULL`）→ 制度性跳过。

后果（2026-11-16 实测）：全库唯一 `hit=1` 是 decision 11816（WTI SELL），
反思表记 86.42 → +2.03% WIN，结算表记 88.335 → -0.13% LOSS。
于是"反思命中率 1/605"其实是 **0/605**，那 1 次是 bug。

口径（不新造真值，只对齐）
------------------------
· 单一真值源 = `quick_sim_trades`（v396 之后由结算器写入的正确出场价）；
· 同一 `decision_id` 在两张表都有、且结算表 `settled=1` 时才回填；
· 回填列：`exit_price / pnl_pct / mfe_pct / mae_pct / hit / actual_outcome_pct / settle_source`；
· `hit` 按结算表 `verdict='WIN'` 重算（铁律 4：不拿旧值当有效值）；
· 反思表**没有**对应结算行的（未结算 / ABANDONED）一律不动 —— 缺数据就是缺数据，
  不猜、不补（铁律 1、2）。

安全性
------
· 默认 dry-run，只打印差异；`--commit` 才落库；
· 全程单事务，异常自动回滚；
· 幂等：回填后再次运行差异应为 0。

执行
----
    python3 reflection_truth_backfill.py             # dry-run
    python3 reflection_truth_backfill.py --commit    # 落库
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

# v412-G: 本文件在 review/ 子目录下，直接 `python3 review/xxx.py` 跑时 sys.path[0]
# 是 review/ 而非 src_python/，`import db` 会 ModuleNotFoundError。这里显式补父目录，
# 保证无论是直接跑、还是被 launchd wrapper 以任意 cwd 调用，都能找到 common/db。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db

# 触发回填的最小差异（百分点）。取 0.5 是因为 30bps 往返成本 + 取价精度差异
# 不会到 0.5pp；超过它基本就是"两个不同价"而不是四舍五入。
MISMATCH_PP = 0.5

_COLS = ("exit_price", "pnl_pct", "mfe_pct", "mae_pct", "hit",
         "actual_outcome_pct", "settle_source")


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _connect(retries: int = 5) -> sqlite3.Connection:
    """常驻进程会持写锁，短 timeout 会直接 SQLITE_BUSY；这里 90s + 重试。"""
    path = db.get_db_path()
    last: Optional[sqlite3.OperationalError] = None
    for i in range(retries):
        conn = sqlite3.connect(path, timeout=90.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=90000")
            # 先拿一次读，确认库可访问（锁死时立刻失败而不是拖到事务里）
            conn.execute("SELECT 1 FROM trade_reflections_v2 LIMIT 1").fetchone()
            return conn
        except sqlite3.OperationalError as exc:
            last = exc
            conn.close()
            time.sleep(5)
    raise last if last else RuntimeError("connect failed")


def find_mismatches(conn) -> List[Dict[str, Any]]:
    """反思表 vs 结算表：同一 decision 的两套出场价/收益对不上。"""
    rows = conn.execute(
        """
        SELECT r.id                AS reflection_id,
               r.decision_id       AS decision_id,
               r.paper_run_id      AS paper_run_id,
               r.asset             AS asset,
               r.action            AS action,
               r.exit_price        AS r_exit,
               r.pnl_pct           AS r_pnl,
               r.mfe_pct           AS r_mfe,
               r.mae_pct           AS r_mae,
               r.hit               AS r_hit,
               r.actual_outcome_pct AS r_outcome,
               q.exit_price        AS q_exit,
               q.pnl_pct           AS q_pnl,
               q.mfe_pct           AS q_mfe,
               q.mae_pct           AS q_mae,
               q.verdict           AS q_verdict
          FROM trade_reflections_v2 r
          JOIN quick_sim_trades q ON q.decision_id = r.decision_id
         WHERE q.settled = 1
           AND q.pnl_pct IS NOT NULL
        """
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        r_pnl, q_pnl = _f(r["r_pnl"]), _f(r["q_pnl"])
        if r_pnl is None:
            diff = None
        else:
            diff = abs(r_pnl - (q_pnl or 0.0))
        expect_hit = 1 if str(r["q_verdict"] or "").upper() == "WIN" else 0
        if diff is None or diff > MISMATCH_PP or int(r["r_hit"] or 0) != expect_hit:
            out.append({
                "reflection_id": int(r["reflection_id"]),
                "decision_id": int(r["decision_id"]),
                "paper_run_id": r["paper_run_id"],
                "asset": r["asset"], "action": r["action"],
                "old_exit": _f(r["r_exit"]), "new_exit": _f(r["q_exit"]),
                "old_pnl": r_pnl, "new_pnl": q_pnl,
                "old_hit": int(r["r_hit"] or 0), "new_hit": expect_hit,
                "verdict": r["q_verdict"],
                "new_mfe": _f(r["q_mfe"]), "new_mae": _f(r["q_mae"]),
            })
    return out


def apply_mismatches(conn, items: List[Dict[str, Any]]) -> int:
    """按结算表回填；单事务，逐行 UPDATE。"""
    if not items:
        return 0
    n = 0
    for it in items:
        conn.execute(
            """
            UPDATE trade_reflections_v2
               SET exit_price          = ?,
                   pnl_pct             = ?,
                   mfe_pct             = ?,
                   mae_pct             = ?,
                   hit                 = ?,
                   actual_outcome_pct  = ?,
                   settle_source       = 'quick_sim_trades'
             WHERE id = ?
            """,
            (it["new_exit"], it["new_pnl"], it["new_mfe"], it["new_mae"],
             it["new_hit"], it["new_pnl"], it["reflection_id"]),
        )
        n += 1
    return n


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="反思表结算口径回填（v404）")
    ap.add_argument("--commit", action="store_true", help="真正落库（默认 dry-run）")
    args = ap.parse_args(argv)

    conn = _connect()
    try:
        items = find_mismatches(conn)
        print(f"[backfill] 发现差异 {len(items)} 条（口径阈值 {MISMATCH_PP}pp）")
        for it in items:
            print(
                f"  #{it['reflection_id']} decision={it['decision_id']} "
                f"{it['asset']} {it['action']}: "
                f"exit {it['old_exit']}→{it['new_exit']}, "
                f"pnl {it['old_pnl']}→{it['new_pnl']}, "
                f"hit {it['old_hit']}→{it['new_hit']} ({it['verdict']})"
            )
        if not args.commit:
            print("[backfill] dry-run，未写入。加 --commit 落库。")
            return 0
        if not items:
            print("[backfill] 无差异，幂等退出。")
            return 0
        with conn:
            n = apply_mismatches(conn, items)
        print(f"[backfill] 已回填 {n} 条（事务提交）。")

        # 回填后自检：差异必须归零
        left = find_mismatches(conn)
        print(f"[backfill] 自检：剩余差异 {len(left)} 条")
        if left:
            print("[backfill] 警告：仍有差异，见上方列表。", file=sys.stderr)
            return 1
        # 命中数复核（铁律：0 才是真 0）
        sum_hit = conn.execute(
            "SELECT COALESCE(SUM(hit),0) FROM trade_reflections_v2"
        ).fetchone()[0]
        print(f"[backfill] 反思表 SUM(hit) = {int(sum_hit)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
