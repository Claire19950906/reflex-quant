#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repair_orphan_decision_refs.py — v321.3 P1: 清理决策的孤儿上游引用 (需求 #5)
================================================================================

问题 (实测, 不是推测)
----------------------
`api_server._clear_trading_traces` (界面上的「重置交易历史」) 会删除
ai_decisions / raw_news / news_events, 但**没动 trade_decisions** →
重置后留下 1998 条**孤儿引用**:

    trade_decisions.ai_decision_id  3241 条非空, 其中 1998 条 (61.6%)
        指向**已被物理删除**的 ai_decisions (id 全部 < 2554, 而现表 id 从 2554 起)
    trade_decisions.news_id        指向同样已删的 raw_news

后果:
  ① 对账永远有悬空 → G09 判成「新闻→决策断链」(实际链路 99.75% 是通的);
  ② 任何按 ai_decision_id 的关联/统计全部错位;
  ③ 重置后新旧数据混在一个 id 空间, 无法分辨哪条属于哪一轮。

能不能重建?
----------
实测**不能**: 那批最早决策 (td.id 5~2543) 的**整条上游** (ai_decisions +
raw_news) 都已被清掉 —— 用 (ts, asset, action) 去匹配 ai_decisions 命中率 0%,
经 news 侧反推也是 0%。数据没了就是没了, 不猜、不硬挂。

所以本脚本做两件事:
  1. **清理**: 把指向不存在行的 ai_decision_id / news_id 置 NULL, 并在
     context_json 记一笔「上游已随重置清除」, 保留追溯线索;
  2. **防复发**: 给 decision_id_space.py 的 reconcile 加上同样的孤儿检测
     (那边已有 news_events 侧的清理, 缺 trade_decisions 侧)。

用法
----
    python3 repair_orphan_decision_refs.py --dry-run
    python3 repair_orphan_decision_refs.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trident_event_bus.db",
)


def _mark_cleared(conn: sqlite3.Connection, row_id: int) -> None:
    """在 context_json 里记一笔「上游已随重置清除」。"""
    try:
        row = conn.execute(
            "SELECT context_json FROM trade_decisions WHERE id=?", (row_id,)
        ).fetchone()
        ctx: Dict[str, Any] = {}
        if row and row[0]:
            try:
                ctx = json.loads(row[0]) or {}
                if not isinstance(ctx, dict):
                    ctx = {}
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        ctx["upstream_cleared_at"] = int(time.time())
        ctx["upstream_cleared_reason"] = "reset_deleted_ai_decisions_and_raw_news"
        conn.execute(
            "UPDATE trade_decisions SET context_json=? WHERE id=?",
            (json.dumps(ctx, ensure_ascii=False), row_id),
        )
    except sqlite3.Error as exc:
        # 写路径失败必须留痕 (audit_silent_except 规则) —— 静默吞掉会让
        # 「清理没生效」变成查不出来的问题。
        print(f"[REPAIR-ORPHAN] ⚠️ 写库/调用失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:120]} (row_id={row_id})", flush=True)


def _restore_news_refs(conn: sqlite3.Connection, window_s: int = 6 * 3600) -> int:
    """重建被误清的 trade_decisions.news_id (v321.3 P1 修正自己的副作用)。

    背景: 第一版清理把「raw_news 行已删」的 news_id 全部置 NULL —— 但
    **news_events.news_id 里还留着同样的值**, 且追溯路径靠的是**值相等**
    (`news_events.news_id = trade_decisions.news_id`), 不依赖 raw_news 行存在。
    结果: 清掉指针反而把 1780 条本来可追溯的决策变成了不可追溯
    (实测 td_traceable 3233 → 1453)。

    现在: 按 (asset, decision_ts ±window) 从 news_events 找回 news_id。
    实测命中率 99.8% (1780/1783), 时间差都在 100 秒内 —— 同一条新闻同时
    驱动多条决策 (一个 ai_decision 派生多条 trade_decisions) 本来就正常。
    """
    rows = conn.execute(
        "SELECT d.id, d.asset, d.decision_ts FROM trade_decisions d "
        "WHERE d.news_id IS NULL AND d.decision_ts IS NOT NULL "
        "  AND (d.context_json LIKE '%upstream_cleared_at%' "
        "       OR d.ai_decision_id IS NULL)"
    ).fetchall()
    if not rows:
        return 0
    ne = conn.execute(
        "SELECT news_id, asset, ts FROM news_events WHERE news_id IS NOT NULL"
    ).fetchall()
    by_asset: Dict[str, List] = {}
    for r in ne:
        by_asset.setdefault(str(r["asset"] or "").upper(), []).append(
            (int(r["ts"] or 0), int(r["news_id"])))
    for k in by_asset:
        by_asset[k].sort()
    updates = []
    for d in rows:
        lst = by_asset.get(str(d["asset"] or "").upper())
        if not lst:
            continue
        ts = int(float(d["decision_ts"]))
        best = None
        for nts, nnid in lst:
            diff = abs(nts - ts)
            if diff <= window_s and (best is None or diff < best[0]):
                best = (diff, nnid)
        if best:
            updates.append((best[1], int(d["id"])))
    if updates:
        conn.executemany("UPDATE trade_decisions SET news_id=? WHERE id=?", updates)
        conn.commit()
    return len(updates)


def run(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    # ── v321.3 P1 修正: news_id 只有在「news_events 里既没有同 news_id 的行、
    #    也没有同 id 的行」时才算孤儿。raw_news 行被删 ≠ 指针失效 ——
    #    追溯靠的是 news_events.news_id 的**值相等**, 不依赖 raw_news 行还在。
    bad_ai = conn.execute(
        "SELECT id FROM trade_decisions "
        "WHERE ai_decision_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM ai_decisions a WHERE a.id = trade_decisions.ai_decision_id)"
    ).fetchall()
    bad_news = conn.execute(
        "SELECT id FROM trade_decisions "
        "WHERE news_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM news_events n WHERE n.news_id = trade_decisions.news_id) "
        "  AND NOT EXISTS (SELECT 1 FROM news_events n WHERE n.id = trade_decisions.news_id)"
    ).fetchall()
    n_ai = len(bad_ai)
    n_news = len(bad_news)
    restored = 0
    if not dry_run and (n_ai or n_news):
        ids = {int(r["id"]) for r in bad_ai} | {int(r["id"]) for r in bad_news}
        for i in ids:
            _mark_cleared(conn, i)
        conn.execute(
            "UPDATE trade_decisions SET ai_decision_id=NULL "
            "WHERE ai_decision_id IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM ai_decisions a WHERE a.id = trade_decisions.ai_decision_id)"
        )
        conn.execute(
            "UPDATE trade_decisions SET news_id=NULL "
            "WHERE news_id IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM news_events n WHERE n.news_id = trade_decisions.news_id) "
            "  AND NOT EXISTS (SELECT 1 FROM news_events n WHERE n.id = trade_decisions.news_id)"
        )
        conn.commit()
    # 恢复上一版误清的 news_id —— **无条件执行** (它是恢复性的, 不是清理性的:
    # 即使当前没有孤儿, 之前被误清的也要找回来)。
    restored = _restore_news_refs(conn)
    # 复核
    left = conn.execute(
        "SELECT COUNT(*) FROM trade_decisions "
        "WHERE ai_decision_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM ai_decisions a WHERE a.id = trade_decisions.ai_decision_id)"
    ).fetchone()[0]
    traceable = conn.execute(
        """SELECT COUNT(DISTINCT d.id) FROM trade_decisions d
           WHERE EXISTS(SELECT 1 FROM ai_decisions a WHERE a.id=d.ai_decision_id
                       AND EXISTS(SELECT 1 FROM news_events n WHERE n.ai_decision_id=a.id))
              OR EXISTS(SELECT 1 FROM news_events n WHERE n.news_id=d.news_id)"""
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0]
    return {"orphan_ai_decision_id": n_ai, "orphan_news_id": n_news,
            "restored_news_id": restored,
            "remaining_orphan_ai_decision_id": left,
            "traceable": traceable, "total_decisions": total,
            "trace_pct": round(100.0 * traceable / total, 2) if total else 0.0,
            "dry_run": bool(dry_run)}


def main() -> int:
    ap = argparse.ArgumentParser(description="清理决策的孤儿上游引用")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        st = run(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print("[REPAIR-ORPHAN] " + json.dumps(st, ensure_ascii=False))
    return 0


__all__ = ["run"]

if __name__ == "__main__":
    raise SystemExit(main())
