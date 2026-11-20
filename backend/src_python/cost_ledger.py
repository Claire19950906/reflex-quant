"""v453 P2-E16：把 LLM token 用量写进 `t2_cost_ledger`。

## 修的是什么

`t2_cost_ledger` 一直是 **0 行**（`trident2/store.py` 有 `insert_cost`，但没人调）。
结果是：成本面板（F18）永远返回 no_data；每天 500 万+ 输入 token 无法归因，
"额度烧在哪条链上"只能靠翻日志。

## 口径（不编价格）

**只记真实 token 数，不臆造单价。** 用户明确要求"严禁假数据"。
- `currency='TOKEN'`、`amount=tokens_in+tokens_out`、`category='llm_tokens:<purpose>'`；
- note 里写 `calls / in / out / top model`，价格由人另行配置；
- F18 侧只把 `currency='USDT'` 当钱求和，TOKEN 行单独展示，避免"token 当美元"。

## 幂等

同一 `day` 先删后写（只删 `currency='TOKEN'` 的行），每小时重跑不会产生重复账。
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional


def _day_bounds(day: Optional[str] = None) -> tuple:
    if not day:
        day = time.strftime("%Y-%m-%d", time.localtime())
    start = time.mktime(time.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S"))
    end = start + 86400
    return day, int(start), int(end)


def sync_day(conn: Optional[sqlite3.Connection] = None,
             day: Optional[str] = None) -> int:
    """把指定自然日（本地时区）的 token 用量写成账本行，返回写入行数。"""
    _own = conn is None
    if _own:
        import db
        conn = db.get_connection()
    _day, _start, _end = _day_bounds(day)
    n = 0
    try:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(purpose, ''), '(未知)') AS p,"
            "       COUNT(*) AS calls,"
            "       COALESCE(SUM(tokens_in), 0) AS tin,"
            "       COALESCE(SUM(tokens_out), 0) AS tout"
            "  FROM ai_call_stats WHERE ts >= ? AND ts < ?"
            " GROUP BY p ORDER BY (tin + tout) DESC",
            (_start, _end),
        ).fetchall()
        try:
            conn.execute(
                "DELETE FROM t2_cost_ledger WHERE day = ? AND currency = 'TOKEN'"
                " AND category LIKE 'llm_tokens:%'", (_day,)
            )
        except sqlite3.Error:
            return 0
        for r in rows:
            try:
                p, calls, tin, tout = str(r[0]), int(r[1]), int(r[2]), int(r[3])
            except Exception:
                continue
            total = tin + tout
            if total <= 0:
                continue
            # 该用途下 token 最多的模型（note 里留证）
            try:
                top = conn.execute(
                    "SELECT model_id, SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)) s"
                    "  FROM ai_call_stats WHERE ts >= ? AND ts < ? AND purpose = ?"
                    " GROUP BY model_id ORDER BY s DESC LIMIT 1",
                    (_start, _end, r[0]),
                ).fetchone()
                top_model = str(top[0]) if top and top[0] else "?"
            except Exception:
                top_model = "?"
            conn.execute(
                "INSERT INTO t2_cost_ledger"
                " (ts, day, strategy_id, category, amount, currency, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), _day, None, "llm_tokens:" + p, float(total),
                 "TOKEN", f"calls={calls}; in={tin}; out={tout}; top={top_model}; price_unset"),
            )
            n += 1
        if _own:
            conn.commit()
    except Exception as e:
        print(f"[COST-LEDGER] sync failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return 0
    finally:
        if _own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if n:
        print(f"[COST-LEDGER] {_day} 写入 {n} 类 token 账", flush=True)
    return n


if __name__ == "__main__":
    import sys
    print(sync_day(day=(sys.argv[1] if len(sys.argv) > 1 else None)))
