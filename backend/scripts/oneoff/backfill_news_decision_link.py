#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_news_decision_link.py — v321.3 P0: 新闻 → 决策 回链 (需求 #5)
================================================================================

问题 (实测)
----------
G09 实测: 有 BUY/SELL 动作的新闻 8839 条, 只有 1327 条 (15.01%) 挂了
`trade_decision_id` —— 断链的新闻永远进不了归因/反思闭环。

根因
----
`trade_decisions.news_id` 非空 3236 条, 但**只有 217 条能命中 news_events** ——
写入时用的是另一个 id 空间 (ai_decisions / raw_news)。
而 `news_events.trade_decision_id → trade_decisions.id` 这条链路本身是通的
(2955 条全部命中), 说明**回链这一步没做全**。

做法 (三级, 精确优先)
----------------------
  1. 精确: trade_decisions.news_id 直接命中 news_events.id → 回链
  2. 近邻: 同资产 + decision_ts 落在新闻 ts 的 ±LINK_WINDOW_SEC 内,
     取**时间差最小**的那条决策 (一条新闻只挂最近的一条, 不笛卡尔积)
  3. 命中不了的保持 NULL —— 不猜、不硬挂

写回字段
--------
  news_events.trade_decision_id = trade_decisions.id
  news_events.decision_id       = trade_decisions.id
  news_events.decision_space    = 'trade'
  trade_decisions.news_id       = news_events.id   (反链, 让两边同源)

REJECT 决策**同样要回链** —— "这条新闻导致系统决定不交易"也是决策,
不断链才能统计闸门的拒对率。

用法
----
    python3 backfill_news_decision_link.py --dry-run
    python3 backfill_news_decision_link.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    str(Path(__file__).resolve().parents[2]),
    "trident_event_bus.db",
)

LINK_WINDOW_SEC = 2 * 3600      # 近邻匹配窗口 (±2 小时)
MIN_ASSET_LEN = 2               # 资产名太短(如 'N')不参与匹配, 防误挂
TITLE_KEY_LEN = 60              # 标题前缀长度 (news_events ↔ raw_news 对齐用)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _build_decision_index(conn: sqlite3.Connection) -> Dict[str, List[Tuple[int, int]]]:
    """{asset: [(decision_ts, decision_id)]} (按 ts 升序)。"""
    idx: Dict[str, List[Tuple[int, int]]] = {}
    for r in _rows(conn, "SELECT id, decision_ts, asset FROM trade_decisions "
                         "WHERE decision_ts IS NOT NULL AND asset IS NOT NULL"):
        a = str(r["asset"] or "").strip().upper()
        if len(a) < MIN_ASSET_LEN:
            continue
        try:
            ts = int(r["decision_ts"])
        except (TypeError, ValueError):
            continue
        idx.setdefault(a, []).append((ts, int(r["id"])))
    for a in idx:
        idx[a].sort()
    return idx


def _nearest_decision(index: Dict[str, List[Tuple[int, int]]], asset: str,
                      ts: int) -> Optional[Tuple[int, int]]:
    """同资产下时间差最小且在窗口内的决策。超出窗口 → None。"""
    lst = index.get(asset)
    if not lst:
        return None
    lo, hi = 0, len(lst)
    while lo < hi:                       # 二分找第一个 ts >= target
        mid = (lo + hi) // 2
        if lst[mid][0] < ts:
            lo = mid + 1
        else:
            hi = mid
    best = None
    best_diff = None
    for i in (lo - 1, lo):
        if 0 <= i < len(lst):
            d_ts, d_id = lst[i]
            diff = abs(d_ts - ts)
            if diff <= LINK_WINDOW_SEC and (best_diff is None or diff < best_diff):
                best, best_diff = (d_id, diff), diff
    return best


def _backfill_asset_from_assets(conn: sqlite3.Connection, dry_run: bool) -> Dict[str, Any]:
    """用 `assets` 列回填 `asset` —— **真信息**, 不是猜。

    背景: news_events.asset 存的是**板块级标签** (INDIVIDUAL_STOCK / US_STOCK /
    山寨 / NONE), 而 assets 列里是**具体品种** (GOOGL / NVDA / 000660,005930 ...)。
    实测: 动作新闻里 asset='NONE' 有 2626 条 (29.7%), 而 assets 非空的有 925 条。
    这两条信息本该是同一件事, 但写入端只填了其中一个。

    只在 asset 为空/NONE 且 assets 有具体品种时回填; 板块标签 (INDIVIDUAL_STOCK /
    US_STOCK / 山寨) 保留不动 —— 那是**板块**, 不是品种, 硬拆成品种就是编造。
    """
    stats = {"filled": 0, "multi": 0, "skipped_block": 0}
    rows = _rows(
        conn,
        "SELECT id, asset, assets FROM news_events "
        "WHERE action IN ('BUY','SELL') "
        "  AND (asset IS NULL OR trim(asset)='' OR asset='NONE') "
        "  AND assets IS NOT NULL AND trim(assets)<>''",
    )
    updates: List[Tuple[str, int]] = []
    for r in rows:
        raw = str(r["assets"] or "").strip()
        if not raw:
            continue
        parts = [p.strip().upper() for p in raw.replace(";", ",").split(",") if p.strip()]
        parts = [p for p in parts if p and p not in ("NONE", "N/A")]
        if not parts:
            continue
        if len(parts) > 1:
            stats["multi"] += 1
        # 板块级标签不作为具体品种
        if parts[0] in ("INDIVIDUAL_STOCK", "US_STOCK", "山寨", "板块"):
            stats["skipped_block"] += 1
            continue
        updates.append((parts[0], int(r["id"])))
    if not dry_run and updates:
        conn.executemany("UPDATE news_events SET asset=? WHERE id=?", updates)
        conn.commit()
    stats["filled"] = len(updates)
    return stats


def _build_title_index(conn: sqlite3.Connection) -> Dict[str, int]:
    """{标题前缀: raw_news.id} —— news_events ↔ raw_news 唯一的可靠桥。

    实测出的 id 空间地图:
      raw_news.id             8794 条  ← 源头表, content 里带标题
      news_events.news_id     24499 条非空, 但只有 15934 命中 raw_news.id
                               (migrate_missing_tables.py:261 还把一部分设成了**自己的 id**)
      ai_decisions.news_id     8000 条**全部**命中 raw_news.id  ✓
      trade_decisions.news_id  3236 条, 只有 1238 命中 raw_news.id
                               (217 命中 news_events, **1781 条两边都不在** = 悬空)
    所以 news_events → raw_news 只能靠**标题文本**对齐 (raw_news.content 开头即标题)。
    """
    idx: Dict[str, int] = {}
    for r in _rows(conn, "SELECT id, content FROM raw_news "
                         "WHERE content IS NOT NULL AND trim(content)<>''"):
        try:
            content = str(r["content"] or "").strip()
            if not content:
                continue
            key = content[:TITLE_KEY_LEN]
            if key and key not in idx:
                idx[key] = int(r["id"])
        except (TypeError, ValueError):
            continue
    return idx


def _match_raw_news(title_index: Dict[str, int], headline: str) -> Optional[int]:
    """用标题在 raw_news 索引里找源 id。找不到 → None (不猜)。"""
    h = str(headline or "").strip()
    if not h:
        return None
    hit = title_index.get(h[:TITLE_KEY_LEN])
    if hit is not None:
        return hit
    # 标题超长时按更短的前缀再试一次
    if len(h) > TITLE_KEY_LEN:
        return title_index.get(h[:TITLE_KEY_LEN // 2])
    return None


def _build_ai_map(conn: sqlite3.Connection,
                  title_index: Optional[Dict[str, int]] = None) -> Dict[int, int]:
    """{news_events.id: trade_decisions.id}。

    两条通路:
      A. news_events.news_id == trade_decisions.news_id (同源键, 两者都指向 raw_news.id)
      B. news_events.headline → raw_news.id → trade_decisions.news_id
         (news_events↔raw_news 没有外键, 只能靠标题文本)
    """
    out: Dict[int, int] = {}
    for r in _rows(
        conn,
        "SELECT n.id AS n_id, d.id AS d_id "
        "FROM news_events n "
        "JOIN trade_decisions d ON d.news_id = n.news_id "
        "WHERE n.news_id IS NOT NULL",
    ):
        try:
            nid, did = int(r["n_id"]), int(r["d_id"])
        except (TypeError, ValueError):
            continue
        if nid not in out or did < out[nid]:
            out[nid] = did
    if not title_index:
        return out
    # B 通路: raw_news.id → 决策
    raw2dec: Dict[int, int] = {}
    for r in _rows(conn, "SELECT news_id, id FROM trade_decisions WHERE news_id IS NOT NULL"):
        try:
            rid, did = int(r["news_id"]), int(r["id"])
        except (TypeError, ValueError):
            continue
        if rid not in raw2dec or did < raw2dec[rid]:
            raw2dec[rid] = did
    for r in _rows(conn, "SELECT id, headline FROM news_events "
                         "WHERE action IN ('BUY','SELL') AND headline IS NOT NULL"):
        try:
            nid = int(r["id"])
        except (TypeError, ValueError):
            continue
        if nid in out:
            continue
        rid = _match_raw_news(title_index, r["headline"])
        if rid is not None and rid in raw2dec:
            out[nid] = raw2dec[rid]
    return out


def _repair_dangling_decision_id(conn: sqlite3.Connection, dry_run: bool) -> Dict[str, Any]:
    """修复悬空的 news_events.decision_id (decision_space='trade')。

    症状: decision_id 既不在 trade_decisions 也不在 ai_decisions 里, 但
    trade_decision_id 有值且**有效** —— 说明 decision_id 写错了 (写成了
    news_events 自己的 id), 而 trade_decision_id 才是对的。
    修法: 把 decision_id 纠正为 trade_decision_id。
    只处理「trade_decision_id 确实存在」的行 —— 其余一律不动。
    """
    rows = _rows(
        conn,
        "SELECT n.id, n.trade_decision_id FROM news_events n "
        "WHERE n.decision_id IS NOT NULL AND n.trade_decision_id IS NOT NULL "
        "  AND n.decision_id != n.trade_decision_id "
        "  AND NOT EXISTS (SELECT 1 FROM trade_decisions d WHERE d.id = n.decision_id) "
        "  AND NOT EXISTS (SELECT 1 FROM ai_decisions a WHERE a.id = n.decision_id) "
        "  AND EXISTS (SELECT 1 FROM trade_decisions d2 WHERE d2.id = n.trade_decision_id)",
    )
    if not dry_run and rows:
        conn.executemany(
            "UPDATE news_events SET decision_id=? WHERE id=?",
            [(int(r["trade_decision_id"]), int(r["id"])) for r in rows],
        )
        conn.commit()
    return {"repaired": len(rows)}


def run(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    index = _build_decision_index(conn)
    title_index = _build_title_index(conn)
    ai_map = _build_ai_map(conn, title_index)
    # 精确匹配索引: trade_decisions.news_id == news_events.id (一次扫, 不逐条查库)
    exact_by_news_id: Dict[int, int] = {}
    for r in _rows(conn, "SELECT news_id, id FROM trade_decisions WHERE news_id IS NOT NULL"):
        try:
            nid, did = int(r["news_id"]), int(r["id"])
        except (TypeError, ValueError):
            continue
        if nid not in exact_by_news_id or did < exact_by_news_id[nid]:
            exact_by_news_id[nid] = did
    stats = {"exact": 0, "via_ai": 0, "near": 0, "skipped": 0, "already": 0, "total": 0}
    asset_stats = _backfill_asset_from_assets(conn, dry_run)
    news_rows = _rows(
        conn,
        "SELECT id, ts, asset, action, news_id, trade_decision_id, decision_id "
        "FROM news_events WHERE action IN ('BUY','SELL') AND ts IS NOT NULL",
    )
    stats["total"] = len(news_rows)
    updates: List[Tuple[int, int, int, int]] = []
    for n in news_rows:
        nid = int(n["id"])
        if n["trade_decision_id"]:
            stats["already"] += 1
            continue
        asset = str(n["asset"] or "").strip().upper()
        ts = int(n["ts"])
        matched = None
        # ① 经 ai_decisions 两跳 (真实链路, 精确) + 标题桥
        if nid in ai_map:
            matched = ai_map[nid]
            stats["via_ai"] += 1
        # ② 精确: trade_decisions.news_id == news_events.id (走内存 map, 不逐条查库)
        if matched is None:
            hit = exact_by_news_id.get(nid)
            if hit is not None:
                matched = hit
                stats["exact"] += 1
        # ③ 近邻 (同资产 + 时间窗, 取最近)
        if matched is None and asset not in ("", "NONE"):
            cand = _nearest_decision(index, asset, ts)
            if cand is not None:
                matched = cand[0]
                stats["near"] += 1
        if matched is None:
            stats["skipped"] += 1
            continue
        # 元组顺序 = (news_id, trade_decision_id, decision_id, matched)
        # ⚠️ v321.3 修正: 之前写成 (nid, matched, nid, matched) → 解包后
        #    decision_id 变成了 **news_events 自己的 id** → 258 条悬空
        #    (decision_id 既不在 trade_decisions 也不在 ai_decisions)。
        updates.append((nid, matched, matched, matched))

    if not dry_run and updates:
        conn.executemany(
            "UPDATE news_events SET trade_decision_id=?, decision_id=?, decision_space='trade' "
            "WHERE id=?",
            [(td, did, nid) for nid, td, did, _m in updates],
        )
        # ⚠️ 不改 trade_decisions.news_id —— 那一列属于 **raw_news 空间**
        # (实测 trade_decisions.news_id → raw_news.id)。往里塞 news_events.id
        # 会破坏空间不变式, 让后续按 news_id 的关联全部错位。
        conn.commit()
    stats["linked"] = len(updates)
    stats["asset_backfill"] = asset_stats
    stats["dangling_repair"] = _repair_dangling_decision_id(conn, dry_run)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="新闻 → 决策 回链")
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
    print("[NEWS-LINK] %s" % st)
    return 0


__all__ = ["run", "LINK_WINDOW_SEC"]

if __name__ == "__main__":
    raise SystemExit(main())
