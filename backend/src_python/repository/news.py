"""数据访问层 —— 样板：news 域。

为什么要这一层（结构问题，不是洁癖）：
    api_server.py 里散着 198 处 `.execute()`、173 处内联 SELECT。表结构一变，
    要全库 grep 一遍才知道谁在用；同一条查询常常在两个 server 各写一份，
    改一处忘一处。这就是「改一个接口要同时改两处」的根因。

本模块的约定（先立规矩，再谈搬迁）：
    1. **只读**。写操作仍然留在原 handler 里，避免这一层变成隐式的业务逻辑。
    2. **参数化查询**，不拼字符串。
    3. **返回普通 dict / list[dict]**，不返回 sqlite3.Row —— handler 不必关心驱动。
    4. **不抛业务异常**：查不到就返回 None / 空 list，由 handler 决定 404 还是空列表。
    5. SQL 一律从原 handler **原样搬来**，不做"顺手优化"。重构与改行为不能同时做。

迁移节奏：一个域一个文件、一个端点一个端点换。当前只落地 news 域，
用来验证模板；其它域（decisions / market / review）照抄这个文件的结构即可。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

# v486：统一走 db_location（支持 TRIDENT_DB_PATH 覆盖）
from db_location import DB_PATH as _DEFAULT_DB


def _db_path() -> str:
    """与 config.DB_PATH 同源：优先 $TRIDENT_DB_PATH，其次仓库内默认路径。"""
    env = os.getenv("TRIDENT_DB_PATH") or os.getenv("TRIDENT_DB")
    if env:
        return env
    try:
        import config # 延迟导入，避免 repository ↔ config 循环依赖
        return str(config.DB_PATH)
    except Exception:
        return str(_DEFAULT_DB)


def _ro() -> sqlite3.Connection:
    """只读连接：mode=ro 保证这一层不会意外写库。"""
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 已迁移：GET /api/news/{news_id}/structured
# SQL 与 api_server.py 原实现逐字一致（含 is_sibling 过滤），只是搬了位置。
# ---------------------------------------------------------------------------
_NEWS_WITH_DECISION_SQL = """
SELECT ad.structured_input, ad.structured_check,
       ad.event_type_code, ad.snapshot_proximity,
       ad.reasoning, ad.id AS decision_id, ad.created_at
FROM raw_news rn
LEFT JOIN ai_decisions ad ON ad.id = (
    SELECT MAX(x.id) FROM ai_decisions x
    WHERE x.news_id = rn.id AND (x.is_sibling = 0 OR x.is_sibling IS NULL)
)
WHERE rn.id = ?
"""


def get_news_with_latest_decision(news_id: int) -> Optional[Dict[str, Any]]:
    """单条新闻 + 其最新一条非兄弟决策。查不到新闻返回 None。"""
    conn = _ro()
    try:
        row = conn.execute(_NEWS_WITH_DECISION_SQL, (int(news_id),)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 待迁移清单（news 域，第二阶段）
#   GET  /api/news/recent            → list_recent(limit, since_ts)
#   GET  /api/news/report            → report_summary()
#   GET  /api/news/{id}/analysis     → get_analysis_by_news()
# 这些仍在 handler 里内联，按同样节奏逐个搬，不一次改完。
# ---------------------------------------------------------------------------


def count_by_status() -> Dict[str, int]:
    """各类新闻状态计数（体检与报表共用，避免每处各写一遍 SQL）。"""
    conn = _ro()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM raw_news GROUP BY status"
        ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}
    finally:
        conn.close()
