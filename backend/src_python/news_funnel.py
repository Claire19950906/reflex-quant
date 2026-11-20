#!/usr/bin/env python3
"""新闻漏斗统计（只读）—— /api/health/models 面板的唯一天真源。

为什么单独抽出来（2026-11-17 实测事故）：
1. `/api/health/models` 里那段 SQL 原来是 `except Exception: pass`，
   DB 被引擎写锁占住时 → 查询失败 → 变量保持初值 0 → 面板显示
   「pending_news=0 / analyzed_24h=0」，看起来像"没积压也没分析"，其实是"根本没读成"。
   训诫与 v322 那次 `except: return 0` 一模一样（见 db.py 里 FILTERED 的注释）。
2. `analyzed_24h` 的 cutoff 用 `datetime('now','localtime','-24 hours')`（空格格式），
   而 `ai_decisions.created_at` 是 `2026-11-16T00:58:43.376+08:00`（T 格式）。
   字符串比较下 'T'(84) > ' '(32)，跨日边界的整段旧数据会被算进"最近 24h"。
   实测同一时刻：面板口径 521 行 vs 真实 310 行（虚高 68%）。

因此这里统一三件事：只读连接（WAL 读者不挡写者）、ISO 口径、失败必须显形（返回 None + db_error，
绝不用 0 冒充"查询成功且结果为空"）。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, Optional


def _iso(ts: float) -> str:
    """把 epoch 转成与 ai_decisions.created_at 同构的本地 ISO 串（带 offset 也兼容：截断比较）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def gate_health(db_path: str, minutes: int = 30, now: Optional[float] = None) -> Dict[str, Any]:
    """门卫（gate）**真实**成功率 —— 面板上"通道 available=true"不等于"门卫真的在判"。

    2026-11-17 实测：`/api/health/models` 显示门卫通道 available=true（每轮只探一次，14s 延迟也算通），
    而同一时刻近 30 分钟新入库 54 条里只有 4 条（7%）拿到门卫判定、20 条（37%）被冻结。
    通道探针和"真实判出率"是两个东西，必须分开显示。

    统计口径（与 ai_worker 认领/归档口径一致）：
      frozen   gate_unavailable=1 —— 主分析不认领，等回放
      degraded gate_unavailable=2 —— 门卫没给判定但明确放行
      judged   gate_verdict 非空且不是"未判定"，且未被冻结
      unlabeled 其余（走 L1/L2 关键词路径，根本没调门卫）
    """
    now = time.time() if now is None else float(now)
    since = int(now - max(1, int(minutes)) * 60)
    out: Dict[str, Any] = {
        "window_min": int(minutes), "total": None, "frozen": None,
        "degraded": None, "judged": None, "unlabeled": None,
        "judged_pct": None, "db_error": "",
    }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error as exc:
        out["db_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        return out
    try:
        conn.execute("PRAGMA query_only=1")
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(gate_unavailable,0) = 1 THEN 1 ELSE 0 END) AS frozen,
                   SUM(CASE WHEN COALESCE(gate_unavailable,0) = 2 THEN 1 ELSE 0 END) AS degraded,
                   SUM(CASE WHEN COALESCE(gate_unavailable,0) <> 1
                             AND COALESCE(gate_verdict,'') NOT IN ('', '未判定')
                        THEN 1 ELSE 0 END) AS judged,
                   SUM(CASE WHEN COALESCE(gate_unavailable,0) NOT IN (1, 2)
                             AND COALESCE(gate_verdict,'') IN ('', '未判定')
                        THEN 1 ELSE 0 END) AS unlabeled
            FROM raw_news WHERE ts IS NOT NULL AND ts > ?
            """, (since,)).fetchone()
        total = int(row[0] or 0)
        out["total"] = total
        out["frozen"] = int(row[1] or 0)
        out["degraded"] = int(row[2] or 0)
        out["judged"] = int(row[3] or 0)
        out["unlabeled"] = int(row[4] or 0)
        out["judged_pct"] = round(out["judged"] * 100.0 / total, 1) if total else None
    except sqlite3.Error as exc:
        out["db_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        for k in ("total", "frozen", "degraded", "judged", "unlabeled", "judged_pct"):
            out[k] = None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return out


def funnel_stats(db_path: str, max_age_hours: int, now: Optional[float] = None) -> Dict[str, Any]:
    """返回 {"pending_news": int|None, "analyzed_24h": int|None, "db_error": str}。

    None 表示"没读成"，调用方必须把它显示成未知，不能显示成 0。
    """
    now = time.time() if now is None else float(now)
    out: Dict[str, Any] = {"pending_news": None, "analyzed_24h": None, "db_error": ""}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error as exc:
        out["db_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        return out
    try:
        conn.execute("PRAGMA query_only=1")
        # 「待分析」只算真的会被认领的：认领门槛是 status='PENDING' AND is_noise=0
        # 且 ts 在 NEWS_ANALYZE_MAX_AGE_HOURS 以内（超龄的会被归档，永远不认领）。
        _max_age = int(max_age_hours or 0)
        _cut = (now - _max_age * 3600) if _max_age > 0 else 0
        # v444：与认领同一口径 —— 断档水位之下的新闻不会被分析，不能算进"待分析"。
        # 否则恢复后水位已经把 216 条归档掉了，面板还显示"待分析 216"，人以为要等。
        try:
            import news_cutoff as _nc  # noqa: WPS433
            _floor = int(_nc.get_floor())
        except Exception:
            _floor = 0
        row = conn.execute(
            "SELECT COUNT(*) FROM raw_news WHERE status = 'PENDING' AND is_noise = 0"
            " AND (? <= 0 OR (ts IS NOT NULL AND ts >= ?))"
            " AND (? <= 0 OR (ts IS NOT NULL AND ts >= ?))",
            (_max_age, _cut, _floor, _floor),
        ).fetchone()
        out["pending_news"] = int(row[0]) if row else 0
        row = conn.execute(
            # v432（2026-11-17）：只统计"分析决策"，不再把 routing_miss 等
            # 非分析行算进"24h 分析量"（实测 76 vs 真实 35，虚高一倍）。
            "SELECT COUNT(*) FROM ai_decisions WHERE created_at >= ?"
            " AND COALESCE(decision_kind,'') = 'analysis'",
            (_iso(now - 24 * 3600),),
        ).fetchone()
        out["analyzed_24h"] = int(row[0]) if row else 0
    except sqlite3.Error as exc:
        out["db_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        out["pending_news"] = None
        out["analyzed_24h"] = None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return out
