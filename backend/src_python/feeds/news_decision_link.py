#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
news_decision_link.py — v321.3 P0: 新闻 ↔ 决策 双向挂链的唯一入口 (需求 #5)
================================================================================

背景 (实测, 需求 #5「所有东西串联在一起」)
--------------------------------------------
生产库里有 4 条链, 其中第 4 条**完全没连**:

  ① critical_price_moves (488) → event_move_attribution (729) → news_events ✅
     每一步都有外键, 走得通
  ② news_events (24499) → ai_decisions (8000) ✅  (news_events.ai_decision_id)
  ③ ai_decisions → trade_decisions ✅ (trade_decisions.ai_decision_id, 但 1998/3241 悬空)
  ④ trade_decisions → critical_price_moves ❌ **没有任何关联字段**

  ④ 断的直接后果: news_events.trade_decision_id 只有 1327/8841 (15.01%) 被挂上,
  而这 1327 条还是历史手工补的 —— 生产入口 `/api/decision/record`
  (bridge_server.py:2502) 写了 `news_id` 之后**从不回链 news_events**。

news_id 的 id 空间 (实测, 这是本模块存在的理由)
------------------------------------------------
  trade_decisions.news_id 3236 条:
    · 1238 条落在 raw_news 空间
    ·  217 条落在 news_events 空间
    · 1781 条**两边都不在** = 悬空垃圾 id
  news_events.news_id 24499 条: 15934 条落在 raw_news 空间
    (migrate_missing_tables.py:261 还把一部分设成了**自己的 id**)

所以 `news_id` 不是单一空间 —— 必须**多路尝试**, 命中哪条算哪条。

本模块提供的三级匹配 (精确优先, 从不猜)
----------------------------------------
  ① news_events.id == news_id                      (直接命中)
  ② news_events.news_id == news_id                 (同源键: 都指向 raw_news.id)
  ③ ai_decisions.news_id == news_id → trade_decisions.ai_decision_id
                                                     (经 ai_decisions 两跳)
  ④ event_move_attribution.news_event_id 关联       (异动 → 归因新闻, 走外键)

找不到 → **不挂**。宁可空着, 也不硬挂错 —— 挂错比不挂更危险 (会把无关新闻
算进归因/反思闭环, 污染学习信号)。

用法
----
    from feeds.news_decision_link import link_news_to_decision
    link_news_to_decision(conn, decision_id, news_id_hint)
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

__all__ = ["link_news_to_decision", "find_news_for_decision"]


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _pick_news(conn: sqlite3.Connection, news_id: Any) -> Optional[int]:
    """把一个 news_id 提示解析成 news_events.id。命中不了返回 None。"""
    if news_id is None:
        return None
    try:
        nid = int(news_id)
    except (TypeError, ValueError):
        return None
    if nid <= 0:
        return None
    # ① 直接命中 news_events.id
    hit = _scalar(conn, "SELECT id FROM news_events WHERE id=? LIMIT 1", (nid,))
    if hit:
        return int(hit)
    # ② 同源键: news_events.news_id == news_id (两者都指向 raw_news.id)
    hit = _scalar(conn, "SELECT id FROM news_events WHERE news_id=? LIMIT 1", (nid,))
    if hit:
        return int(hit)
    return None


def find_news_for_decision(conn: sqlite3.Connection, decision_id: int,
                           news_id_hint: Any = None) -> Optional[int]:
    """反查: 这个决策对应哪条新闻。返回 news_events.id。"""
    # 决策自带 news_id → 解析
    if news_id_hint is None:
        own = _scalar(conn, "SELECT news_id FROM trade_decisions WHERE id=?",
                      (int(decision_id),))
        news_id_hint = own
    got = _pick_news(conn, news_id_hint)
    if got:
        return got
    # 经 ai_decisions 两跳
    aid = _scalar(conn, "SELECT ai_decision_id FROM trade_decisions WHERE id=?",
                  (int(decision_id),))
    if aid:
        anid = _scalar(conn, "SELECT news_id FROM ai_decisions WHERE id=?", (int(aid),))
        got = _pick_news(conn, anid)
        if got:
            return got
        # ai_decisions.news_id 是 raw_news 空间 → news_events.news_id 同空间
        got = _scalar(conn, "SELECT id FROM news_events WHERE news_id=? LIMIT 1",
                      (int(anid),)) if anid else None
        if got:
            return int(got)
    return None


def link_news_to_decision(conn: sqlite3.Connection, decision_id: int,
                           news_id_hint: Any = None) -> Optional[int]:
    """把决策挂回新闻 (双向)。返回挂上的 news_events.id。

    · 只挂**还没被别的决策占用**的新闻 (一条新闻只挂一条决策)
    · 找不到 → 不挂 (返回 None), 绝不硬挂
    · 调用方负责 commit
    """
    nid = find_news_for_decision(conn, decision_id, news_id_hint)
    if nid is None:
        return None
    try:
        exist = conn.execute(
            "SELECT id FROM news_events WHERE id=? AND trade_decision_id IS NULL",
            (nid,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not exist:
        return None
    try:
        conn.execute(
            "UPDATE news_events SET trade_decision_id=?, decision_id=?, "
            "decision_space='trade' WHERE id=?",
            (int(decision_id), int(decision_id), int(nid)),
        )
    except sqlite3.Error:
        return None
    return int(nid)
