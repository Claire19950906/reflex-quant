#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_polluted_outcome_pct.py — v310 P1: 修复 trade_reflections_v2.actual_outcome_pct
的历史污染(2026-10-26 那批 ±90%~±400% 的假收益)。

背景
----
2026-10-26T20:56:37 有一批结算行把 actual_outcome_pct 写成了 256.8 / 269.8 /
399.5 这种**物理上不可能**的 5 日收益(同批的 pnl_pct / root_cause 里是正确的
+4.01% / +4.04% —— 说明写进去的不是收益本身, 而是另一套口径的数)。后果:

    · trade_reflections_v2 非零 actual_outcome_pct 均值: +117.18%  (真实约 +1.45%)
    · 199 / 503 行绝对值 > 90%
    · 「真实持仓业绩」等下游统计全部被带偏

修复口径
--------
· 只动**结算行**(paper_run_id IS NULL) —— paper worker 写的反思行
  (paper_run_id IS NOT NULL) 两个列单位不同(pnl_pct 是比例, actual_outcome_pct
  是百分数), 绝不能互相覆盖。
· 结算行的权威值是 **pnl_pct**(v310 P0 用同源口径重算过, 与
  trade_decisions.actual_outcome_pct 一致) → 直接同步 actual_outcome_pct = pnl_pct。
· 只在两者不一致时写, 其余行原样保留。

安全性
------
· 默认 dry-run(只报告), 加 --commit 才落库。
· --commit 前自动用 sqlite3 backup API 做一份 .bak 备份。
· 全程单事务, 失败自动回滚。

用法
----
    python3 src_python/fix_polluted_outcome_pct.py            # 只报告
    python3 src_python/fix_polluted_outcome_pct.py --commit   # 备份 + 落库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"

# 与 db.MAX_PLAUSIBLE_OUTCOME_PCT 一致: 超过即视为取价异常
IMPLAUSIBLE_PCT = 90.0
EPS = 0.001

# 同步范围: **只动结算行**(paper_run_id IS NULL) 且有可信 pnl_pct。
# worker 行(paper_run_id IS NOT NULL)两列单位不同, 绝不触碰。
SYNC_WHERE = ("paper_run_id IS NULL AND pnl_pct IS NOT NULL "
              "AND actual_outcome_pct IS NOT NULL "
              "AND ABS(actual_outcome_pct - pnl_pct) > ?")
SYNC_SQL = f"UPDATE trade_reflections_v2 SET actual_outcome_pct = pnl_pct WHERE {SYNC_WHERE}"

# ── v314 P0: worker 行的「×100 单位放大」残留 ──────────────────────────────
# 2026-10-29 实测体检 (174 条 worker 行):
#     163 条  actual == pnl        (1x)  ← 正确
#       10 条  actual == pnl * 100  (100x) ← 污染残留
# 那 10 条正是本次事故里被拖成 8.85 天的超期单: pnl_pct 被 quick_sim 按
# 「百分数数值」写入(如 0.3606 表示 0.3606%), 而 actual_outcome_pct 仍停留
# 在另一套「比例语义」口径的 ×100 值(如 36.06) → 同一行里两列单位打架。
#
# 判定依据 (全表价格反查, 173 个可判定行):
#     41 行  pnl == (exit-entry)/entry*100        (BUY, k=+1)
#    132 行  pnl == -(exit-entry)/entry*100       (SELL, k=-1)
#     0 行  比例语义 (pnl == (exit-entry)/entry)
# → pnl_pct 全表统一是「百分数数值」, 不存在比例语义的行。
# 因此 actual_outcome_pct 也应与 pnl_pct 同单位 (1x)。
#
# 安全边界: 只修「恰好等于 pnl×100」的行 (单位放大的特征签名), 其余 worker 行
# 一律不动 —— 避免误伤本来就不是 ×100 口径的行。
SYNC_WORKER_100X_WHERE = (
    "paper_run_id IS NOT NULL AND pnl_pct IS NOT NULL AND pnl_pct <> 0 "
    "AND actual_outcome_pct IS NOT NULL "
    "AND ABS(actual_outcome_pct - pnl_pct * 100) <= ?")
SYNC_WORKER_100X_SQL = (
    "UPDATE trade_reflections_v2 SET actual_outcome_pct = pnl_pct WHERE "
    + SYNC_WORKER_100X_WHERE)


def sync_worker_100x_outcomes(conn: sqlite3.Connection, eps: float = EPS) -> int:
    """把 worker 行里 actual == pnl*100 的单位放大残留同步为 pnl, 返回行数。"""
    cur = conn.execute(SYNC_WORKER_100X_SQL, (eps * 100.0,))
    conn.commit()
    return cur.rowcount


def sync_settlement_outcomes(conn: sqlite3.Connection, eps: float = EPS) -> int:
    """把结算行的 actual_outcome_pct 同步为 pnl_pct, 返回更新行数。

    单事务版本(供测试与无并发场景使用); main() 走分批版本以降低写锁占用。
    """
    cur = conn.execute(SYNC_SQL, (eps,))
    conn.commit()
    return cur.rowcount


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _stats(conn: sqlite3.Connection, where: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       SUM(CASE WHEN ABS(actual_outcome_pct) > ? THEN 1 ELSE 0 END) AS n_extreme, "
        "       AVG(CASE WHEN actual_outcome_pct IS NOT NULL AND actual_outcome_pct != 0 "
        "                THEN actual_outcome_pct END) AS avg_nonzero, "
        "       MAX(ABS(actual_outcome_pct)) AS max_abs, "
        "       AVG(pnl_pct) AS avg_pnl "
        "FROM trade_reflections_v2 WHERE " + where,
        (IMPLAUSIBLE_PCT,),
    ).fetchone()
    return {
        "n": row["n"] or 0,
        "n_extreme": row["n_extreme"] or 0,
        "avg_nonzero": row["avg_nonzero"],
        "max_abs": row["max_abs"],
        "avg_pnl": row["avg_pnl"],
    }


def _fmt(tag: str, s: dict) -> str:
    avg = "n/a" if s["avg_nonzero"] is None else f"{s['avg_nonzero']:+.3f}%"
    mx = "n/a" if s["max_abs"] is None else f"{s['max_abs']:.2f}%"
    ap = "n/a" if s["avg_pnl"] is None else f"{s['avg_pnl']:+.3f}%"
    return (f"  {tag:<10} 行数={s['n']:<6} |幅度|>90%|={s['n_extreme']:<6} "
            f"actual均值(非零)={avg:<12} 最大|actual|={mx:<12} pnl均值={ap}")


def _apply_in_batches(conn: sqlite3.Connection, sql: str, ids: list,
                      eps: float, batch: int = 50) -> tuple:
    """分批执行 UPDATE (每批 50 行), 返回 (成功行数, 失败 id 列表)。

    分批提交的理由: 生产库有常驻写者 (engine 的 forward_tracker 单次持锁可达
    30~49s), 一次性大 UPDATE 抢不到锁; 小事务持锁窗口只有毫秒级。
    """
    updated = 0
    failed: list = []
    if not ids:
        return 0, failed
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        done = False
        # 短 busy_timeout + 多次重试: SQLite 不排队, 长 busy_timeout 会被
        # 高频写者"饿死"; 短超时快速轮询反而更容易抢到写锁窗口。
        for _attempt in range(120):
            try:
                conn.execute("PRAGMA busy_timeout=500;")
                conn.execute(sql % ",".join("?" * len(chunk)), (*chunk, eps))
                conn.commit()
                conn.execute("PRAGMA busy_timeout=30000;")
                updated += len(chunk)
                done = True
                break
            except sqlite3.OperationalError as e:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                if "locked" not in str(e) and "busy" not in str(e):
                    raise
                time.sleep(0.5)
        if not done:
            failed.extend(chunk)
        print(f"  批次 {i // batch + 1}/{(len(ids) + batch - 1) // batch} "
              f"完成(累计 {updated} 行)", flush=True)
    return updated, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库 (默认只报告)")
    args = ap.parse_args()

    conn = _connect()
    try:
        print("=" * 78)
        print("修复前")
        print("=" * 78)
        before_all = _stats(conn, "1=1")
        before_set = _stats(conn, "paper_run_id IS NULL")
        before_work = _stats(conn, "paper_run_id IS NOT NULL")
        print(_fmt("全部", before_all))
        print(_fmt("结算行", before_set))
        print(_fmt("worker行", before_work))

        rows = conn.execute(
            "SELECT id, decision_id, asset, pnl_pct, actual_outcome_pct "
            "FROM trade_reflections_v2 "
            "WHERE paper_run_id IS NULL AND pnl_pct IS NOT NULL "
            "  AND actual_outcome_pct IS NOT NULL "
            "  AND ABS(actual_outcome_pct - pnl_pct) > ? "
            "ORDER BY ABS(actual_outcome_pct - pnl_pct) DESC",
            (EPS,),
        ).fetchall()
        mismatched = conn.execute(
            "SELECT COUNT(*) FROM trade_reflections_v2 r "
            "JOIN trade_decisions d ON d.id = r.decision_id "
            "WHERE r.paper_run_id IS NULL AND r.pnl_pct IS NOT NULL "
            "  AND d.actual_outcome_pct IS NOT NULL "
            "  AND ABS(d.actual_outcome_pct - r.pnl_pct) > ?",
            (EPS,),
        ).fetchone()[0]
        total_set = conn.execute(
            "SELECT COUNT(*) FROM trade_reflections_v2 "
            "WHERE paper_run_id IS NULL AND pnl_pct IS NOT NULL").fetchone()[0]

        worker_100x = conn.execute(
            "SELECT id, decision_id, asset, pnl_pct, actual_outcome_pct "
            "FROM trade_reflections_v2 WHERE " + SYNC_WORKER_100X_WHERE,
            (EPS * 100.0,),
        ).fetchall()

        print()
        print(f"待同步行数: {len(rows)} / 结算行有 pnl_pct 共 {total_set} 行")
        print(f"待同步(worker 行 ×100 单位放大残留): {len(worker_100x)} 行")
        if worker_100x:
            print("  样例(前 5 行):")
            for r in worker_100x[:5]:
                print(f"    id={r['id']:<6} decision_id={r['decision_id']:<6} "
                      f"{r['asset']:<6} pnl={r['pnl_pct']:<10} "
                      f"actual={r['actual_outcome_pct']}")
        print(f"交叉核对: pnl_pct 与 trade_decisions.actual_outcome_pct 不一致 {mismatched} 行 "
              f"(0 = pnl_pct 可作为权威值)")
        if rows:
            print("  样例(前 5 行, 幅度差最大):")
            for r in rows[:5]:
                print(f"    id={r['id']:<6} decision_id={r['decision_id']:<6} "
                      f"{r['asset']:<6} pnl={r['pnl_pct']:<10} "
                      f"actual={r['actual_outcome_pct']}")

        if not args.commit:
            print()
            print("dry-run: 未写库(加 --commit 落库)")
            return 0

        # 备份(sqlite backup API —— WAL 模式下也拿一致快照)
        bak = DB_PATH.with_suffix(f".bak.{time.strftime('%Y%m%d-%H%M%S')}")
        dst = sqlite3.connect(str(bak))
        try:
            conn.backup(dst)
        finally:
            dst.close()
        print(f"\n已备份 → {bak}")

        try:
            # 分批提交(每批 50 行) —— 理由见 _apply_in_batches 的说明
            target_ids = [r["id"] for r in rows]
            updated, failed = _apply_in_batches(
                conn,
                "UPDATE trade_reflections_v2 SET actual_outcome_pct = pnl_pct "
                "WHERE id IN (%s) AND " + SYNC_WHERE,
                target_ids, EPS,
            )
            if failed:
                print(f"写库未完成: {len(failed)} 行拿不到写锁, 已回滚这些批次", file=sys.stderr)
                return 1
            print(f"已更新 {updated} 行(结算行)")

            # v314 P0: worker 行里 actual == pnl*100 的单位放大残留
            worker_ids = [r["id"] for r in worker_100x]
            w_updated, w_failed = _apply_in_batches(
                conn,
                "UPDATE trade_reflections_v2 SET actual_outcome_pct = pnl_pct "
                "WHERE id IN (%s) AND " + SYNC_WORKER_100X_WHERE,
                worker_ids, EPS * 100.0,
            )
            if w_failed:
                print(f"worker 行写库未完成: {len(w_failed)} 行拿不到写锁, 已回滚这些批次",
                      file=sys.stderr)
                return 1
            print(f"已更新 {w_updated} 行(worker 行 ×100 残留)")
        except Exception as e:
            conn.rollback()
            print(f"写库失败, 已回滚: {e}", file=sys.stderr)
            return 1

        print()
        print("=" * 78)
        print("修复后")
        print("=" * 78)
        after_all = _stats(conn, "1=1")
        after_set = _stats(conn, "paper_run_id IS NULL")
        after_work = _stats(conn, "paper_run_id IS NOT NULL")
        print(_fmt("全部", after_all))
        print(_fmt("结算行", after_set))
        print(_fmt("worker行", after_work))
        print()
        print(f"结算行 actual_outcome_pct 均值: "
              f"{before_set['avg_nonzero']} → {after_set['avg_nonzero']}")
        print(f"结算行 |幅度|>90% 行数: {before_set['n_extreme']} → {after_set['n_extreme']}")
        print(f"worker 行(未触碰): {before_work['n']} → {after_work['n']} 行, "
              f"均值 {before_work['avg_nonzero']} → {after_work['avg_nonzero']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
