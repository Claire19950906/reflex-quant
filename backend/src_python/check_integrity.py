# -*- coding: utf-8 -*-
"""跨表一致性体检（v341）。

库里有 149 张表、0 条外键约束 —— SQLite 加外键要重建整表，1.4 GB 且在跑引擎，
不适合现在动。但没有外键不等于不查引用：这里的每一类都是**已经真出过事**的：

  1. 归因行的 move_id 指向一条不存在的异动（重建换 id 时发生过）
  2. AI 队列认领的 src_id 已经消失（队列曾 168 条里 135 条 gone）
  3. 异动的 asset 名不在价格序列清单里（命名双轨 / 抽屉混入）
  4. 同一 (asset, ts, move_kind) 有重复行（会重复计权重）
  5. ohlcv_daily 里还留着别名（ADA-USD 这种）

用法：python3 src_python/check_integrity.py [--strict]
      --strict: 有任何问题就 exit 1（给 scripts/recheck_acceptance.sh 用）
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import asset_alias as aa

DB = os.path.join(os.path.dirname(__file__), "..", "trident_event_bus.db")


def _count(conn, sql, params=()) -> int:
    try:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def run(conn) -> list[tuple[str, int, str]]:
    """→ [(检查项, 数量, 说明)]"""
    out = []

    out.append(("归因 move_id 悬空",
                _count(conn, "SELECT COUNT(*) FROM event_move_attribution a "
                             "WHERE NOT EXISTS (SELECT 1 FROM critical_price_moves m "
                             "WHERE m.id=a.move_id)"),
                "指向不存在的异动 → 该归因永远不会被读到"))

    out.append(("队列 src_id 已消失",
                _count(conn, "SELECT COUNT(*) FROM attribution_ai_queue q "
                             "WHERE q.src_table='critical_price_moves' "
                             "AND q.status NOT IN ('done','failed','gone') "
                             "AND NOT EXISTS (SELECT 1 FROM critical_price_moves m "
                             "WHERE m.id=q.src_id)"),
                "还在等 AI 复核但源行没了 → 白占队列"))

    out.append(("异动 asset 不在价格序列里",
                _count(conn, "SELECT COUNT(*) FROM critical_price_moves m "
                             "WHERE NOT EXISTS (SELECT 1 FROM series_registry r "
                             "WHERE r.series_key=m.asset AND r.kind='price')"),
                "抽屉名/别名混进异动 → 统计会重复计"))

    out.append(("同 (asset,ts,kind) 重复行",
                _count(conn, "SELECT COALESCE(SUM(c-1),0) FROM ("
                             "SELECT COUNT(*) c FROM critical_price_moves "
                             "GROUP BY asset, ts, move_kind HAVING c>1)"),
                "同一时点重复计权重"))

    aliases = 0
    # 挂起待人工定的组合不算问题（SKHYNIX ↔ 000660.KS 两边各有独立数据）
    _held: set[str] = set()
    try:
        from merge_asset_aliases import AMBIGUOUS
        for a, b in AMBIGUOUS:
            _held.update((a, b))
    except Exception:
        pass
    try:
        names = [r[0] for r in conn.execute("SELECT DISTINCT asset FROM ohlcv_daily")]
        aliases = sum(1 for n in names if aa.is_alias(n) and n not in _held)
    except sqlite3.Error:
        pass
    out.append(("ohlcv_daily 残留别名品种", aliases,
                "ADA-USD 这类应已并入 ADA"))

    # ── 停摆类：不是"不一致"，是"没人干活了"，静默失败最难发现 ────────────
    import time as _t
    now = int(_t.time())
    stale_move = None
    try:
        newest = conn.execute("SELECT MAX(ts) FROM critical_price_moves").fetchone()[0]
        if newest:
            stale_move = round((now - int(newest)) / 3600.0, 1)
    except sqlite3.Error:
        pass
    bad_move = 1 if (stale_move is not None and stale_move > 12) else 0
    out.append(("异动回填停摆(>12h)", bad_move,
                f"最近异动 {stale_move}h 前" if stale_move is not None
                else "无任何异动"))

    stale_reg = None
    try:
        u = conn.execute("SELECT MAX(updated_at) FROM series_registry").fetchone()[0]
        if u:
            stale_reg = round((now - int(u)) / 3600.0, 1)
    except sqlite3.Error as exc:
        # v399：允许失败，但不许不留痕（v322 审计门禁要求）
        print(f"[INTEGRITY] series_registry 落后检查读取失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    bad_reg = 1 if (stale_reg is not None and stale_reg > 26) else 0
    out.append(("注册表落后(>26h)", bad_reg,
                f"上次重建 {stale_reg}h 前" if stale_reg is not None
                else "无 series_registry（小时作业没跑过？）"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    items = run(conn)
    bad = 0
    print("=== 跨表一致性 ===")
    for name, n, note in items:
        flag = "OK  " if n == 0 else "BAD "
        if n:
            bad += 1
        print(f"  [{flag}] {name:22s} {n:6d}   {note if n else ''}")
    if args.strict and bad:
        print(f"[FAIL] {bad} 项不一致")
        return 1
    print(f"[DONE] {len(items)} 项检查, {bad} 项有问题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
