"""
review_snapshot.py — /api/review/snapshot 的**唯一实现**
(8000 api_server 与 8100 bridge_server 共用同一份代码)

为什么独立成模块
----------------
8000 (api_server) 和 8100 (bridge_server) 都要提供 /api/review/snapshot。
之前 8100 用的是**自己的本地简化实现**, 与 8000 口径完全不一致:

  - 数据源取 trade_reflections_v2 (反思记录), 而不是真实持仓 ai_decisions
  - **忽略时间窗口**: days / from_ts / to_ts 全都没用上 → "今日"显示全期累计
  - tracking 恒 0、settled = total (跟踪中的仓位被当成已结算)
  - avg_pnl = AVG(actual_outcome_pct) —— 而该列被污染(错配资产价格序列,
    取值 -50 ~ 399.5), 于是界面上出现"平均 +117.44%"这种假收益
  - profit_loss_ratio / expected_value_per_trade 恒为 null

2026-10-29 用户看到"真实持仓战绩 2208 笔 / 平均 +117.71%"就是这么来的:
它是**反思表污染列的平均值**, 不是任何真实盈亏; 与模拟盘
(/api/replay/stats: 15 笔 / 胜率 50% / 平均 -0.81%) 完全不同源。

口径 (逐字段对齐 api_server.get_replay_stats + quick_sim.evaluation)
------------------------------------------------------------------
  paper      = 真实持仓 ai_decisions (entry_price > 0) 的结算统计
  quick_sim  = quick_sim_trades 结算统计
  today      = 今日新建 BUY/SELL 决策数 / 今日结算的快速评测笔数
  window     = 选定窗口内的信号数 / 已结算数
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

try:  # Python 3.9+ 标准库
    from zoneinfo import ZoneInfo

    TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 老 Python 兜底
    TZ_SHANGHAI = timezone(timedelta(hours=8))


def resolve_window(
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Tuple[Optional[int], Optional[int]]:
    """把 hours / from_ts / to_ts 归一为 (lo, hi)（UTC 秒）。

    与 quick_sim._resolve_window 同口径: from/to 优先, 其次 hours,
    都没有 = 全期 (None, None)。
    """
    if from_ts is not None or to_ts is not None:
        return from_ts, to_ts
    if hours is not None and hours > 0:
        return int(time.time()) - hours * 3600, None
    return None, None


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False



def paper_overall(
    conn: sqlite3.Connection,
    lo: Optional[int] = None,
    hi: Optional[int] = None,
) -> Dict[str, Any]:
    """真实持仓 (ai_decisions) 的结算统计 —— 与 api_server.get_replay_stats 同口径。

    注意: 这里**不**用 trade_reflections_v2, 也不读任何被污染的派生列;
    胜负判定用 forward_pnl 符号 (真实持仓结算权威值)。
    """
    filt = "entry_price IS NOT NULL AND entry_price > 0"
    params: list = []
    if lo is not None:
        filt += " AND ts >= ?"
        params.append(lo)
    if hi is not None:
        filt += " AND ts <= ?"
        params.append(hi)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN settled = 0 THEN 1 ELSE 0 END) AS tracking,
            SUM(CASE WHEN settled = 1 THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN settled = 1 AND forward_pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN settled = 1 AND forward_pnl < 0 THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN settled = 1 AND (forward_pnl IS NULL OR forward_pnl = 0)
                     THEN 1 ELSE 0 END) AS holds,
            AVG(CASE WHEN settled = 1 AND forward_pnl IS NOT NULL AND forward_pnl <> 0
                     THEN forward_pnl END) AS avg_pnl,
            AVG(CASE WHEN settled = 1 AND forward_pnl > 0 THEN forward_pnl END) AS avg_win,
            AVG(CASE WHEN settled = 1 AND forward_pnl < 0 THEN forward_pnl END) AS avg_loss,
            AVG(CASE WHEN settled = 1 THEN mfe_pct END) AS avg_mfe,
            AVG(CASE WHEN settled = 1 THEN mae_pct END) AS avg_mae,
            MAX(CASE WHEN settled = 1 AND forward_pnl IS NOT NULL AND forward_pnl <> 0
                     THEN forward_pnl END) AS best_trade,
            MIN(CASE WHEN settled = 1 AND forward_pnl IS NOT NULL AND forward_pnl <> 0
                     THEN forward_pnl END) AS worst_trade
        FROM ai_decisions
        WHERE {filt}
        """,
        tuple(params),
    ).fetchone()
    overall = dict(row) if row is not None else {}
    return _finish_paper(overall)


def _finish_paper(overall: Dict[str, Any]) -> Dict[str, Any]:
    """把 SQL 聚合结果整理成前端要的字段集 (逐字段对齐 api_server)。"""
    for key in ("total", "tracking", "settled", "wins", "losses", "holds"):
        overall[key] = int(overall.get(key) or 0)
    decided = overall["wins"] + overall["losses"]
    overall["winrate"] = round(overall["wins"] / decided, 4) if decided else 0.0
    overall["winrate_pct"] = round(overall["winrate"] * 100, 2) if decided else None
    try:
        aw = float(overall.get("avg_win") or 0.0)
    except (TypeError, ValueError):
        aw = 0.0
    try:
        al = float(overall.get("avg_loss") or 0.0)
    except (TypeError, ValueError):
        al = 0.0
    # JSON 不允许 Infinity → 从未亏损用字符串 "Infinity"
    if aw > 0 and al < 0:
        overall["profit_loss_ratio"] = round(aw / abs(al), 4)
    elif aw > 0:
        overall["profit_loss_ratio"] = "Infinity"
    else:
        overall["profit_loss_ratio"] = None
    if decided > 0 and (aw > 0 or al < 0):
        wr = float(overall.get("winrate") or 0.0)
        overall["expected_value_per_trade"] = round(wr * aw + (1 - wr) * al, 6)
        ev = overall["expected_value_per_trade"]
        overall["expectancy_label"] = "正向" if ev > 0 else ("负向" if ev < 0 else "中性")
    else:
        overall["expected_value_per_trade"] = None
        overall["expectancy_label"] = "数据不足"
    return overall



def build_snapshot(
    conn: sqlite3.Connection,
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """构造 /api/review/snapshot 的完整响应 (paper / quick_sim / today / window)。"""
    lo, hi = resolve_window(hours, from_ts, to_ts)
    has_window = lo is not None or hi is not None

    paper = paper_overall(conn, lo, hi)

    # quick_sim: 复用 quick_sim.evaluation (同一份口径, 不重写)
    quick_overall: Dict[str, Any] = {}
    try:
        import quick_sim

        qs = quick_sim.evaluation(conn, hours=hours, from_ts=from_ts, to_ts=to_ts)
        quick_overall = (qs or {}).get("overall") or {}
    except Exception as exc:
        print(f"[review_snapshot] quick_sim 段降级: {type(exc).__name__}: {exc}",
              flush=True)

    today_str = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
    today_signals = 0
    today_settled = 0
    try:
        today_signals = int(
            conn.execute(
                """SELECT COUNT(*) FROM ai_decisions
                   WHERE suggested_action IN ('BUY','SELL')
                     AND substr(created_at, 1, 10) = ?""",
                (today_str,),
            ).fetchone()[0]
        )
    except sqlite3.Error:
        pass
    try:
        today_settled = int(
            conn.execute(
                """SELECT COUNT(*) FROM quick_sim_trades
                   WHERE settled = 1
                     AND substr(datetime(exit_ts, 'unixepoch'), 1, 10) = ?""",
                (today_str,),
            ).fetchone()[0]
        )
    except sqlite3.Error:
        pass

    if has_window:
        win_signals_sql = (
            "SELECT COUNT(*) FROM ai_decisions WHERE suggested_action IN ('BUY','SELL')"
        )
        win_signals_params: list = []
        if lo is not None:
            win_signals_sql += " AND ts >= ?"
            win_signals_params.append(lo)
        if hi is not None:
            win_signals_sql += " AND ts <= ?"
            win_signals_params.append(hi)
        win_settled_sql = "SELECT COUNT(*) FROM quick_sim_trades WHERE settled = 1"
        win_settled_params: list = []
        if lo is not None:
            win_settled_sql += " AND exit_ts >= ?"
            win_settled_params.append(lo)
        if hi is not None:
            win_settled_sql += " AND exit_ts <= ?"
            win_settled_params.append(hi)
        try:
            window_signals = int(
                conn.execute(win_signals_sql, tuple(win_signals_params)).fetchone()[0]
            )
            window_settled = int(
                conn.execute(win_settled_sql, tuple(win_settled_params)).fetchone()[0]
            )
        except sqlite3.Error:
            window_signals = None
            window_settled = None
        window_data = {
            "from_ts": lo,
            "to_ts": hi,
            "signals": window_signals,
            "settled": window_settled,
        }
    else:
        window_data = {
            "from_ts": None,
            "to_ts": None,
            "signals": None,
            "settled": None,
        }

    return {
        "paper": paper,
        "quick_sim": quick_overall,
        "today": {
            "date": today_str,
            "signals": today_signals,
            "settled": today_settled,
        },
        "window": window_data,
        "period": {
            "hours": hours,
            "from_ts": from_ts,
            "to_ts": to_ts,
        },
    }
