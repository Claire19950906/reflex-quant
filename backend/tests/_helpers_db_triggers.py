"""测试用：摘掉 v464–v466 的「探针/孤儿拦截」触发器。

db.migrate() 现在会在 SQLite 内核层装三道 BEFORE INSERT 触发器：

  · trg_ai_decisions_reject_probe   —— reasoning='test' 的行直接丢弃
  · trg_raw_news_reject_probe       —— source='test' / content LIKE 'test news%' 的行直接丢弃
  · trg_quick_sim_reject_orphan     —— quick_sim_trades.decision_id 在 ai_decisions 里查不到
                                       就直接丢弃

这三道是给生产库兜底的（11-19 事故：测试脚本直连生产库写探针数据，被 ai_worker
当成真新闻认领）。但在测试里，很多用例**故意**要造这些行：孤儿检测用例造孤儿成交、
早砍用例造 decision_id=NULL 的成交、回归用例造没有父决策的成交。触发器一拦，行根本
落不进库，断言自然全挂。

所以凡是自建 in-memory / 临时库、且需要造上述三类行的用例，在建库后调一次本函数。
生产路径一行不动。
"""

from __future__ import annotations

import os
import sqlite3

GUARD_TRIGGERS = (
    "trg_ai_decisions_reject_probe",
    "trg_raw_news_reject_probe",
    "trg_quick_sim_reject_orphan",
)

# backend/tests/ → backend/trident_event_bus.db
_PROD_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trident_event_bus.db",
)


def _is_prod(conn: sqlite3.Connection) -> bool:
    """连接指向的是不是生产库（按 PRAGMA database_list 的真实路径判定）。"""
    try:
        for row in conn.execute("pragma database_list"):
            path = row[2] or ""
            if path and os.path.abspath(path) == os.path.abspath(_PROD_DB):
                return True
    except sqlite3.Error:
        return False
    return False


def drop_guard_triggers(conn: sqlite3.Connection) -> None:
    """在测试库里 drop 掉探针/孤儿拦截触发器（幂等，可重复调用）。

    2026-11-19 事故：本函数此前对任何连接都不设防，某用例把连接指到生产库后
    把三道守卫触发器摘掉，后续探针行/孤儿成交全部放行。现在先判路径：
    生产库直接拒绝（抛 RuntimeError），只对临时库/内存库动手。
    """
    if _is_prod(conn):
        raise RuntimeError(
            "drop_guard_triggers() 拒绝作用于生产库 %s —— "
            "守卫触发器只能被摘在临时库里；生产库要重建请走 db.migrate()。" % _PROD_DB
        )
    for name in GUARD_TRIGGERS:
        try:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        except sqlite3.Error:
            pass
