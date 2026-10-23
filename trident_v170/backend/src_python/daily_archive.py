"""v191: 每日归档三件套 helper。

每天的反思都要归档,包括总结和每一笔信号的分析过程到交易过程,每天重置数据的时间点归档。

落地的 3 张表(全部在 db.py 中定义):
  1) daily_reset_log         — 每天数据重置的事件(系统启动/mode 切换/跨日清理)
  2) signal_chain_archive    — 每条 ai_decisions 的全链路快照(幂等 INSERT OR REPLACE)
  3) daily_reflection_summary— 每日聚合(数量/胜率/PnL/触发原因分布)

与已有 reflection_daily_journal 的关系:
  - reflection_daily_journal 聚焦 "LLM 反思循环 + 经验应用"维度
  - 本模块聚焦 "信号全链路 + 实盘执行 + 结算"维度

公开函数:
  - archive_daily_reset(conn, run_id, trade_mode, trigger, ...)
  - archive_signal_chain(conn, decision_id, run_id="", experience_ids=None)
  - build_daily_summary(conn, summary_date, run_id="", reset_log_id=None, ...)
  - query_daily_summary(conn, summary_date=None, run_id=None, limit=30)
  - query_signal_chain(conn, decision_id=None, asset=None, audit_status=None, decision_date=None, limit=50)
  - query_daily_resets(conn, limit=30, trigger=None)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("daily_archive")


# ── 关键 SQL:拼出每条 decision 的全链路 ──────────────────────────────
# 一次 LEFT JOIN 把 4 张表的数据组合成一行快照。
# 字段一一对应 signal_chain_archive 列,INSERT OR REPLACE 实现幂等。
_CHAIN_SELECT = """
SELECT
    d.id                AS decision_id,
    CAST(d.paper_trading_run_id AS TEXT) AS run_id,
    d.created_at        AS decision_ts,
    substr(d.created_at, 1, 10) AS decision_date,
    n.id                AS news_id,
    n.published_at      AS news_published_at,
    n.source            AS news_source,
    substr(COALESCE(n.content, ''), 1, 200) AS news_title,
    COALESCE(d.sentiment_score, n.relevance_score) AS sentiment_score,
    d.target_asset      AS target_asset,
    d.suggested_action  AS suggested_action,
    d.market_category   AS market_category,
    substr(COALESCE(d.reasoning, ''), 1, 600) AS reasoning,
    d.agent_model_id    AS agent_model_id,
    d.bias_after_reflection AS bias_after_reflection,
    a.status            AS audit_status,
    a.reason            AS audit_reason,
    a.order_id          AS order_id,
    a.symbol            AS symbol,
    a.side              AS side,
    a.notional_usdt     AS notional_usdt,
    a.quantity          AS quantity,
    a.filled_price      AS filled_price,
    d.entry_price       AS entry_price,
    a.adapter           AS adapter,
    a.mode              AS mode,
    d.is_correct        AS is_correct,
    d.exit_price        AS exit_price,
    d.forward_pnl       AS forward_pnl,
    d.mfe_pct           AS mfe_pct,
    d.mae_pct           AS mae_pct,
    CASE WHEN d.settled=1 THEN d.created_at ELSE '' END AS settled_at,
    r.layer             AS reflection_layer,
    r.ontology_signal   AS reflection_signal,
    r.applied_experience_ids AS experience_ids,
    d.decision_context  AS raw_payload
FROM ai_decisions d
LEFT JOIN raw_news n ON n.id = d.news_id
LEFT JOIN trade_audit_log a ON a.decision_id = d.id
LEFT JOIN trade_reflections_v2 r ON r.decision_id = d.id
WHERE d.id = ?
"""


# ── 写入 SQL:35 列 INSERT OR REPLACE ──────────────────────────────────
_CHAIN_UPSERT = """
INSERT OR REPLACE INTO signal_chain_archive (
    decision_id, run_id, decision_ts, decision_date,
    news_id, news_published_at, news_source, news_title, sentiment_score,
    target_asset, suggested_action, market_category, reasoning,
    agent_model_id, bias_after_reflection,
    audit_status, audit_reason, order_id, symbol, side,
    notional_usdt, quantity, filled_price, entry_price, adapter, mode,
    is_correct, exit_price, forward_pnl, mfe_pct, mae_pct, settled_at,
    reflection_layer, reflection_signal,
    experience_ids,
    raw_payload,
    updated_at
) VALUES (
    :decision_id, :run_id, :decision_ts, :decision_date,
    :news_id, :news_published_at, :news_source, :news_title, :sentiment_score,
    :target_asset, :suggested_action, :market_category, :reasoning,
    :agent_model_id, :bias_after_reflection,
    :audit_status, :audit_reason, :order_id, :symbol, :side,
    :notional_usdt, :quantity, :filled_price, :entry_price, :adapter, :mode,
    :is_correct, :exit_price, :forward_pnl, :mfe_pct, :mae_pct, :settled_at,
    :reflection_layer, :reflection_signal,
    :experience_ids,
    :raw_payload,
    datetime('now', 'localtime')
)
"""


def _row_to_dict(cursor: sqlite3.Cursor, row: Any) -> Dict[str, Any]:
    """cursor.description → 列名,row → dict。

    同时兼容 sqlite3.Row 和普通 tuple(取决于 conn.row_factory)。
    """
    if hasattr(row, "keys"):
        return dict(row)
    cols = [c[0] for c in cursor.description]
    return dict(zip(cols, row))


# ── 写入函数 ─────────────────────────────────────────────────────────
def archive_daily_reset(
    conn: sqlite3.Connection,
    *,
    run_id: str = "",
    trade_mode: str = "",
    trigger: str = "auto_daily",
    cleared_decisions: int = 0,
    cleared_audit_rows: int = 0,
    notes: str = "",
    reset_date: Optional[str] = None,
) -> int:
    """写一条 daily_reset_log 记录,返回 rowid。

    trigger 取值:auto_daily / manual / startup / mode_change / kill_switch
    reset_date 默认今天本地日 (YYYY-MM-DD)。
    """
    if reset_date is None:
        reset_date = datetime.now().strftime("%Y-%m-%d")
    cur = conn.execute(
        """INSERT INTO daily_reset_log
           (reset_date, trigger, run_id, trade_mode,
            cleared_decisions, cleared_audit_rows, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (reset_date, trigger, run_id, trade_mode,
         int(cleared_decisions or 0), int(cleared_audit_rows or 0), notes),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def archive_signal_chain(
    conn: sqlite3.Connection,
    decision_id: int,
    *,
    run_id: str = "",
    experience_ids: Optional[List[int]] = None,
) -> bool:
    """把一条 ai_decisions 的全链路快照写入 signal_chain_archive。

    幂等(同一 decision_id 多次调用会刷新字段,不会重复写入)。
    返回 True 表示已写入/刷新,False 表示 decision 不存在。
    """
    row = conn.execute(_CHAIN_SELECT, (decision_id,)).fetchone()
    if row is None:
        logger.warning("archive_signal_chain: decision_id=%s not found", decision_id)
        return False
    cur = conn.execute(_CHAIN_SELECT, (decision_id,))
    payload = _row_to_dict(cur, row)
    if run_id:
        payload["run_id"] = run_id
    if experience_ids is not None:
        payload["experience_ids"] = ",".join(str(x) for x in experience_ids)
    elif not payload.get("experience_ids"):
        payload["experience_ids"] = ""
    # audit_status 缺失 → 标 PENDING
    if not payload.get("audit_status"):
        payload["audit_status"] = "PENDING"
    conn.execute(_CHAIN_UPSERT, payload)
    conn.commit()
    return True


def _calc_max_drawdown(pnls: List[float]) -> Optional[float]:
    """累计 PnL 序列上的最大回撤(USDT)。空序列返回 None。"""
    if not pnls:
        return None
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd if max_dd > 0 else 0.0


def _auto_key_takeaways(
    n_decisions: int, n_trades: int,
    n_wins: int, n_losses: int, win_rate: Optional[float],
    total_pnl: float, avg_mfe: Optional[float], avg_mae: Optional[float],
    layer_breakdown: Dict[str, int],
) -> str:
    """根据当日数字自动生成 3-5 条 key_takeaways(供人工可改)。"""
    out: List[str] = []
    if n_decisions == 0:
        return "当日无决策信号"
    out.append(f"决策 {n_decisions} 笔,实盘成交 {n_trades} 笔")
    if win_rate is not None and (n_wins + n_losses) > 0:
        out.append(f"胜率 {win_rate*100:.1f}% ({n_wins}胜/{n_losses}负)")
    elif n_wins + n_losses == 0:
        out.append("尚未结算完毕")
    out.append(f"总盈亏 {total_pnl:+.4f} USDT")
    if avg_mfe is not None and avg_mae is not None:
        out.append(f"平均 MFE/MAE = {avg_mfe:.2f}% / {avg_mae:.2f}%")
    if layer_breakdown:
        top_layer = max(layer_breakdown.items(), key=lambda kv: kv[1])
        out.append(f"主要失败层:{top_layer[0]} ({top_layer[1]} 次)")
    return " | ".join(out)


def build_daily_summary(
    conn: sqlite3.Connection,
    summary_date: str,
    *,
    run_id: str = "",
    reset_log_id: Optional[int] = None,
    trade_mode: str = "",
    lessons_learned: str = "",
    next_day_focus: str = "",
) -> int:
    """聚合指定日期的 signal_chain_archive,写入 daily_reflection_summary。

    可重复调用(INSERT OR REPLACE),同一 (summary_date, run_id) 刷新数据。
    返回 rowid。
    """
    rows_cur = conn.execute(
        """SELECT
              audit_status,
              target_asset,
              suggested_action,
              is_correct,
              forward_pnl,
              mfe_pct,
              mae_pct
           FROM signal_chain_archive
           WHERE decision_date = ?
             AND ( ? = '' OR run_id = ? OR run_id IS NULL OR run_id = '' )""",
        (summary_date, run_id, run_id),
    )
    rows = [_row_to_dict(rows_cur, r) for r in rows_cur.fetchall()]

    n_decisions = len(rows)
    n_trades = n_skipped = n_risk_rejected = n_adapter_errors = n_pending = 0
    n_wins = n_losses = n_hold = n_unsettled = 0
    total_pnl = 0.0
    pnls: List[float] = []
    mfes: List[float] = []
    maes: List[float] = []
    asset_breakdown: Dict[str, int] = {}
    action_breakdown: Dict[str, int] = {}

    for r in rows:
        st = r.get("audit_status") or "PENDING"
        if st == "ORDER_PLACED":
            n_trades += 1
        elif st == "ADAPTER_SKIPPED":
            n_skipped += 1
        elif st == "RISK_REJECTED":
            n_risk_rejected += 1
        elif st == "ADAPTER_ERROR":
            n_adapter_errors += 1
        else:
            n_pending += 1
        asset = r.get("target_asset") or "NONE"
        asset_breakdown[asset] = asset_breakdown.get(asset, 0) + 1
        action = r.get("suggested_action") or "HOLD"
        action_breakdown[action] = action_breakdown.get(action, 0) + 1
        ic = (r.get("is_correct") or "").upper()
        if ic == "WIN":
            n_wins += 1
        elif ic == "LOSS":
            n_losses += 1
        elif ic == "HOLD":
            n_hold += 1
        else:
            n_unsettled += 1
        if r.get("forward_pnl") is not None:
            total_pnl += float(r["forward_pnl"])
            pnls.append(float(r["forward_pnl"]))
        if r.get("mfe_pct") is not None:
            mfes.append(float(r["mfe_pct"]))
        if r.get("mae_pct") is not None:
            maes.append(float(r["mae_pct"]))

    win_rate = (n_wins / (n_wins + n_losses)) if (n_wins + n_losses) > 0 else None
    avg_pnl = (sum(pnls) / len(pnls)) if pnls else 0.0
    avg_mfe = (sum(mfes) / len(mfes)) if mfes else None
    avg_mae = (sum(maes) / len(maes)) if maes else None
    max_dd = _calc_max_drawdown(pnls)

    # 反思 layer 分布(取 signal_chain_archive.reflection_layer)
    layer_cur = conn.execute(
        """SELECT reflection_layer, COUNT(*) AS n
           FROM signal_chain_archive
           WHERE decision_date = ?
             AND ( ? = '' OR run_id = ? OR run_id IS NULL OR run_id = '' )
             AND reflection_layer IS NOT NULL AND reflection_layer != ''
           GROUP BY reflection_layer""",
        (summary_date, run_id, run_id),
    )
    layer_rows = [_row_to_dict(layer_cur, r) for r in layer_cur.fetchall()]
    layer_breakdown = {r["reflection_layer"]: int(r["n"]) for r in layer_rows}

    key_takeaways = _auto_key_takeaways(
        n_decisions, n_trades, n_wins, n_losses, win_rate,
        total_pnl, avg_mfe, avg_mae, layer_breakdown,
    )

    cur = conn.execute(
        """INSERT OR REPLACE INTO daily_reflection_summary
           (summary_date, run_id, trade_mode,
            n_decisions, n_trades, n_skipped, n_risk_rejected,
            n_adapter_errors, n_pending,
            n_wins, n_losses, n_hold, n_unsettled, win_rate,
            total_pnl_usdt, avg_pnl_usdt, avg_mfe_pct, avg_mae_pct,
            max_drawdown_pct,
            asset_breakdown, action_breakdown, layer_breakdown,
            key_takeaways, lessons_learned, next_day_focus,
            reset_log_id, updated_at)
           VALUES (?, ?, ?,
                   ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?,
                   ?, ?, ?,
                   ?, ?, ?,
                   ?, datetime('now', 'localtime'))""",
        (summary_date, run_id, trade_mode,
         n_decisions, n_trades, n_skipped, n_risk_rejected,
         n_adapter_errors, n_pending,
         n_wins, n_losses, n_hold, n_unsettled, win_rate,
         total_pnl, avg_pnl, avg_mfe, avg_mae,
         max_dd,
         json.dumps(asset_breakdown, ensure_ascii=False),
         json.dumps(action_breakdown, ensure_ascii=False),
         json.dumps(layer_breakdown, ensure_ascii=False),
         key_takeaways, lessons_learned, next_day_focus,
         reset_log_id),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


# ── 查询函数 ─────────────────────────────────────────────────────────
def query_daily_summary(
    conn: sqlite3.Connection,
    *,
    summary_date: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """按日期/Run 查询 daily_reflection_summary(降序)。"""
    sql = "SELECT * FROM daily_reflection_summary WHERE 1=1"
    params: List[Any] = []
    if summary_date:
        sql += " AND summary_date = ?"
        params.append(summary_date)
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " ORDER BY summary_date DESC, id DESC LIMIT ?"
    params.append(int(limit))
    cur = conn.execute(sql, params)
    return [_row_to_dict(cur, r) for r in cur.fetchall()]


def query_signal_chain(
    conn: sqlite3.Connection,
    *,
    decision_id: Optional[int] = None,
    asset: Optional[str] = None,
    audit_status: Optional[str] = None,
    decision_date: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """按 decision_id / asset / audit_status / date 查询 signal_chain_archive。"""
    sql = "SELECT * FROM signal_chain_archive WHERE 1=1"
    params: List[Any] = []
    if decision_id is not None:
        sql += " AND decision_id = ?"
        params.append(int(decision_id))
    if asset:
        sql += " AND target_asset = ?"
        params.append(asset)
    if audit_status:
        sql += " AND audit_status = ?"
        params.append(audit_status)
    if decision_date:
        sql += " AND decision_date = ?"
        params.append(decision_date)
    sql += " ORDER BY decision_ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    cur = conn.execute(sql, params)
    return [_row_to_dict(cur, r) for r in cur.fetchall()]


def query_daily_resets(
    conn: sqlite3.Connection,
    *,
    limit: int = 30,
    trigger: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """查询 daily_reset_log(降序)。"""
    sql = "SELECT * FROM daily_reset_log WHERE 1=1"
    params: List[Any] = []
    if trigger:
        sql += " AND trigger = ?"
        params.append(trigger)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    cur = conn.execute(sql, params)
    return [_row_to_dict(cur, r) for r in cur.fetchall()]


# ── 公开别名(供 engine / api_server 调用更直观) ─────────────────────
record_reset_event      = archive_daily_reset
record_signal_snapshot  = archive_signal_chain
build_today_summary     = build_daily_summary
