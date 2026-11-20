#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decision_ref_archive.py — v321.3 P2: 决策引用归档 (需求 #5「串联互通」防复发)
================================================================================

问题 (实测, 不是推测)
----------------------
「重置交易历史」(_clear_trading_traces) 会删 ai_decisions / raw_news / news_events,
但 **trade_decisions 的两个指针还指着它们** → 死指针。实测那次重置留下:
    trade_decisions.ai_decision_id  1998 条指向已删除的 ai_decisions
    trade_decisions.news_id        1783 条指向已删除的 raw_news
而那批最早决策的上游数据**被物理删除**, 三条恢复路径实测命中率全是 0%:
    · 用 (ts, asset, action) 匹配 ai_decisions        → 0%
    · 经 news_events.news_id 值相等反推              → 0%
    · 经 critical_price_moves → event_move_attribution → news → 0%
数据没了就是没了。

所以正解不是"事后修", 而是**事前存**: 重置前把决策 + 它的引用 + 它的语义
归档下来, 重置**不删归档**。

归档里留三样东西:
  1. 引用 (news_id / ai_decision_id) —— 等上游重建后可自动对齐回去
  2. 语义 (asset / decision_ts / action / entry_price / outcome) ——
     决策本身是**真数据**, 不该随"重置交易状态"被抹掉 (复盘/校准都要用)
  3. 批次 (batch) —— 哪次重置归档的, 便于追溯

用法
----
    from signals.decision_ref_archive import archive_decisions, restore_refs
    archive_decisions(conn, batch)          # 重置前调用
    restore_refs(conn)                      # 重置后调用 (把能对上的引用对回去)
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional

__all__ = ["archive_decisions", "restore_refs", "archive_stats"]


def archive_decisions(conn: sqlite3.Connection, batch: Optional[str] = None,
                      note: str = "") -> Dict[str, Any]:
    """把当前 trade_decisions 的引用 + 语义归档。重置**不删**这张表。"""
    batch = batch or time.strftime("reset-%Y%m%d-%H%M%S")
    now = int(time.time())
    try:
        cur = conn.execute(
            "INSERT OR REPLACE INTO decision_ref_archive "
            "(decision_id, batch, asset, decision_ts, action, news_id, "
            " ai_decision_id, entry_price, outcome_pct, status, created_at, note) "
            "SELECT d.id, ?, COALESCE(d.asset,''), d.decision_ts, "
            "       COALESCE(d.intended_action, d.final_action, ''), d.news_id, "
            "       d.ai_decision_id, d.entry_price, d.actual_outcome_pct, "
            "       COALESCE(d.status,''), ?, ? "
            "FROM trade_decisions d",
            (batch, now, note),
        )
        conn.commit()
        n = cur.rowcount or 0
    except sqlite3.Error as exc:
        print(f"[DECISION-ARCHIVE] ⚠️ 归档失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"archived": 0, "batch": batch, "error": str(exc)[:120]}
    return {"archived": n, "batch": batch}


def restore_refs(conn: sqlite3.Connection, window_s: int = 3600) -> Dict[str, Any]:
    """把归档里的引用与现存数据对齐 —— 能对上的对回去, 对不上的**如实留空**。

    三条对齐路径 (按可靠性):
      ① ai_decision_id: 归档值在 ai_decisions 里存在 → 直接恢复 (最可靠)
      ② news_id 值相等: news_events.news_id == 归档.news_id → 恢复 (顺带补 ai_decision_id)
      ③ 语义对齐: 同 asset + decision_ts ±window → 从 news_events 里找最近的
         (上游重建后自动接回)
    """
    stats = {"ai_restored": 0, "news_restored": 0, "semantic_restored": 0,
             "unresolved": 0, "total": 0}
    try:
        rows = conn.execute(
            "SELECT a.decision_id, a.asset, a.decision_ts, a.news_id, a.ai_decision_id, "
            "       a.action AS want_action, "
            "       d.news_id AS cur_news, d.ai_decision_id AS cur_ai "
            "FROM decision_ref_archive a "
            "LEFT JOIN trade_decisions d ON d.id = a.decision_id"
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"[DECISION-ARCHIVE] ⚠️ 归档读取失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return stats
    stats["total"] = len(rows)

    # 索引: news_id → ai_decision_id ; asset → [(ts, news_id, ai_decision_id, action)]
    ne_by_newsid: Dict[int, int] = {}
    ne_by_asset: Dict[str, List] = {}
    try:
        # 一律用**索引**访问 (不依赖 row_factory —— db.get_connection() 不设它)
        for r in conn.execute(
                "SELECT id, news_id, asset, ts, ai_decision_id, action FROM news_events "
                "WHERE news_id IS NOT NULL OR ai_decision_id IS NOT NULL").fetchall():
            nid = r[1]
            aid = r[4]
            if nid is not None and aid is not None:
                ne_by_newsid.setdefault(int(nid), int(aid))
            ne_by_asset.setdefault(str(r[2] or "").upper(), []).append(
                (int(r[3] or 0), int(nid) if nid is not None else None, aid, r[5]))
    except sqlite3.Error as exc:
        print(f"[DECISION-ARCHIVE] ⚠️ news_events 索引失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
    for k in ne_by_asset:
        ne_by_asset[k].sort()

    for a in rows:
        # 列序: decision_id, asset, decision_ts, news_id, ai_decision_id,
        #       want_action, cur_news, cur_ai
        did = int(a[0])
        cur_news = a[6]
        cur_ai = a[7]
        # ── 有效性校验 ──────────────────────────────────────────────────────
        # 非空 ≠ 有效: 上游被重置删掉后, 指针**还留着数字**但已指向不存在的行。
        # 不校验就会把"死指针"当成"已恢复" → 既不重建也不标注 (实测踩到)。
        if cur_news is not None:
            hit = conn.execute(
                "SELECT 1 FROM news_events WHERE id=? OR news_id=? LIMIT 1",
                (int(cur_news), int(cur_news))).fetchone()
            if not hit:
                cur_news = None
        if cur_ai is not None:
            hit = conn.execute("SELECT 1 FROM ai_decisions WHERE id=?",
                               (int(cur_ai),)).fetchone()
            if not hit:
                cur_ai = None
        if cur_news is not None and cur_ai is not None:
            continue
        new_news = cur_news
        new_ai = cur_ai
        semantic = False
        # 已经有效的引用本身就是**精确**来源 (不是推断出来的)
        method = "exact" if (cur_news is not None or cur_ai is not None) else ""
        link_diff = None
        # ① 归档里的 ai_decision_id 还在 → 恢复 (最可靠)
        if new_ai is None and a[4] is not None:
            hit = conn.execute("SELECT 1 FROM ai_decisions WHERE id=?",
                               (int(a[4]),)).fetchone()
            if hit:
                new_ai = int(a[4])
                stats["ai_restored"] += 1
                method = "exact_ai"
        # ② news_id 值相等 → 恢复 (顺带补 ai_decision_id)
        if new_news is None and a[3] is not None:
            if int(a[3]) in ne_by_newsid:
                new_news = int(a[3])
                stats["news_restored"] += 1
                if new_ai is None:
                    new_ai = ne_by_newsid[int(a[3])]
                method = method or "same_news_id"
        # ③ 语义对齐: 同资产 + 时间最近 + **方向一致优先**
        #    为什么要方向一致优先: 实测纯"最近"挂链的方向同向率只有 84.2%
        #    (反向 15.8%) —— 那 15.8% 是误挂, 会污染归因/反思闭环。
        #    优先选 action 与决策 action 一致的新闻, 同向率能到 ~99%。
        if (new_news is None or new_ai is None) and a[2]:
            lst = ne_by_asset.get(str(a[1] or "").upper()) or []
            ts = int(float(a[2]))
            want = str(a[5] or "").strip().upper()
            best = None
            for nts, nnid, naid, nact in lst:
                diff = abs(nts - ts)
                if diff > window_s:
                    continue
                same_dir = bool(want and nact and str(nact).strip().upper() == want)
                rank = (0 if same_dir else 1, diff)   # 同向优先, 同向里取最近
                if best is None or rank < best[0]:
                    best = (rank, nnid, naid, diff)
            if best:
                semantic = True
                link_diff = int(best[3])
                if new_news is None and best[1] is not None:
                    new_news = best[1]
                    stats["news_restored"] += 1
                if new_ai is None and best[2] is not None:
                    new_ai = best[2]
                    stats["ai_restored"] += 1
                method = method or "semantic"
            if best:
                semantic = True
                if new_news is None and best[1] is not None:
                    new_news = best[1]
                    stats["news_restored"] += 1
                if new_ai is None and best[2] is not None:
                    new_ai = best[2]
                    stats["ai_restored"] += 1
        if semantic:
            stats["semantic_restored"] += 1
        if new_news is None and new_ai is None:
            stats["unresolved"] += 1
            continue
        try:
            conn.execute(
                "UPDATE trade_decisions SET news_id=?, ai_decision_id=?, "
                "news_link_method=?, news_link_diff_sec=? WHERE id=?",
                (new_news, new_ai, method or "none", link_diff, did),
            )
        except sqlite3.Error as exc:
            print(f"[DECISION-ARCHIVE] ⚠️ 引用回填失败（原先静默吞掉）: "
                  f"{type(exc).__name__}: {str(exc)[:120]} (decision_id={did})", flush=True)
    conn.commit()
    return stats


def archive_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    """归档现状 (供端点/门禁读)。"""
    out: Dict[str, Any] = {"batches": [], "total": 0}
    try:
        out["total"] = conn.execute(
            "SELECT COUNT(*) FROM decision_ref_archive").fetchone()[0]
        rows = conn.execute(
            "SELECT batch, COUNT(*) n, MIN(created_at) mn, MAX(created_at) mx "
            "FROM decision_ref_archive GROUP BY batch ORDER BY mx DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            # 不依赖 row_factory (db.get_connection() 不设它, dict(r) 会炸)
            out["batches"].append({
                "batch": r[0], "n": r[1], "min_created_at": r[2], "max_created_at": r[3],
            })
    except sqlite3.Error as exc:
        print(f"[DECISION-ARCHIVE] ⚠️ 归档统计失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
    return out
