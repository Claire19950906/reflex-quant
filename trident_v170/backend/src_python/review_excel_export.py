"""review_excel_export.py — v170 Phase D: 反思系统 XLSX 导出。

提供 `build_review_xlsx() -> bytes` 函数，把反思页涉及的所有关键表聚成 5 个 sheet：
  - Overview    顶部统计带 (paper / quick_sim / daily_journal / experiences_active)
  - Trades      真实持仓 ai_decisions（与 snapshot.paper 同口径：entry_price>0 AND settled=1）
  - Reflections 每日结构化反思 structured_reflections + decision_errors
  - Insights    review_insights 规则库
  - Experiences experience_entries 全量规则库

无 LLM 调用，纯 SQL 聚合，生成时间 < 2s（数据库 78 表、单表最大 ~1k 行）。
"""
from __future__ import annotations

import io
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List


def _open() -> sqlite3.Connection:
    """懒加载 DB 连接 + 只读模式 + 5s busy_timeout。"""
    import db as _db
    conn = sqlite3.connect(
        f"file:{_db.get_db_path()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_query(conn: sqlite3.Connection, sql: str,
                params: tuple = ()) -> List[sqlite3.Row]:
    """查询容错：表可能不存在 / 列变更 → 返回空列表。"""
    try:
        return list(conn.execute(sql, params))
    except Exception:
        return []


def _coerce(v: Any) -> Any:
    """JSON/dict/list → 字符串，方便 Excel 显示。"""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return str(v)
    return v


def _rows_to_sheet(ws, rows, max_col_width: int = 36):
    """把 sqlite3.Row 列表写到 openpyxl sheet，自动列宽 + 冻结首行 + 自动筛选。"""
    from openpyxl.styles import Alignment, Font, PatternFill
    if not rows:
        ws.append(["（无数据）"])
        return
    headers = list(rows[0].keys())
    ws.append(headers)
    header_font = Font(bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", fgColor="1F2937")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    for r in rows:
        ws.append([_coerce(v) for v in r])
    # v170 P2: 冻结首行 + 开启自动筛选 → 大表滚动时表头始终可见，且可按列筛选
    last_row = ws.max_row
    last_col_letter = ws.cell(row=1, column=len(headers)).column_letter
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{last_col_letter}{last_row}"
    for col_idx, header in enumerate(headers, start=1):
        longest = len(str(header))
        for r in rows:
            v = r[col_idx - 1]
            if v is None:
                continue
            longest = min(max(longest, len(str(v))), max_col_width)
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = longest + 2


def _write_dict_list(ws, items: List[Dict[str, Any]], max_col_width: int = 36):
    """把 dict 列表写到 sheet：自动列宽 + 冻结首行 + 自动筛选。"""
    from openpyxl.styles import Alignment, Font, PatternFill
    if not items:
        ws.append(["（无数据）"])
        return
    headers = list(items[0].keys())
    ws.append(headers)
    header_font = Font(bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", fgColor="1F2937")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    for item in items:
        ws.append([_coerce(item.get(h)) for h in headers])
    # v170 P2: 冻结首行 + 开启自动筛选
    last_row = ws.max_row
    last_col_letter = ws.cell(row=1, column=len(headers)).column_letter
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{last_col_letter}{last_row}"
    for col_idx, header in enumerate(headers, start=1):
        longest = len(str(header))
        for it in items:
            v = it.get(header)
            if v is None:
                continue
            longest = min(max(longest, len(str(v))), max_col_width)
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = longest + 2


def _build_overview(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Overview: paper totals + quick_sim totals + daily journals + insights count."""
    def _scalar(sql: str, default=0):
        rows = _safe_query(conn, sql)
        return rows[0][0] if rows else default

    paper_total = _scalar("SELECT COUNT(*) FROM ai_decisions")
    paper_settled = _scalar(
        "SELECT COUNT(*) FROM trade_reflections_v2 WHERE settled_at IS NOT NULL")
    paper_wins = _scalar(
        "SELECT COUNT(*) FROM trade_reflections_v2 "
        "WHERE settled_at IS NOT NULL AND pnl_pct > 0")
    paper_losses = _scalar(
        "SELECT COUNT(*) FROM trade_reflections_v2 "
        "WHERE settled_at IS NOT NULL AND pnl_pct < 0")
    paper_avg_pnl = _scalar(
        "SELECT AVG(pnl_pct) FROM trade_reflections_v2 WHERE pnl_pct IS NOT NULL")

    qs_total = _scalar("SELECT COUNT(*) FROM quick_sim_trades")
    qs_settled = _scalar("SELECT COUNT(*) FROM quick_sim_trades WHERE exit_price IS NOT NULL")
    qs_open = qs_total - qs_settled

    insights_active = _scalar("SELECT COUNT(*) FROM review_insights WHERE active=1")
    insights_total = _scalar("SELECT COUNT(*) FROM review_insights")
    exp_total = _scalar("SELECT COUNT(*) FROM experience_entries")

    journals = _safe_query(conn, """
        SELECT journal_date, run_id, n_decisions, n_wins, n_losses, win_rate, pnl_usdt
          FROM reflection_daily_journal
         ORDER BY journal_date DESC LIMIT 7
    """)

    rows: List[Dict[str, Any]] = [
        {"section": "Paper (历史真实持仓)", "metric": "总决策数", "value": paper_total, "unit": "笔"},
        {"section": "Paper (历史真实持仓)", "metric": "已结算", "value": paper_settled, "unit": "笔"},
        {"section": "Paper (历史真实持仓)", "metric": "胜", "value": paper_wins, "unit": "笔"},
        {"section": "Paper (历史真实持仓)", "metric": "负", "value": paper_losses, "unit": "笔"},
        {"section": "Paper (历史真实持仓)", "metric": "胜率",
         "value": f"{(paper_wins/paper_settled*100):.1f}%" if paper_settled else "—", "unit": ""},
        {"section": "Paper (历史真实持仓)", "metric": "平均 PnL%",
         "value": f"{(paper_avg_pnl or 0)*100:.2f}%" if paper_avg_pnl is not None else "—",
         "unit": ""},
        {"section": "Quick Sim (快速评测)", "metric": "总单数", "value": qs_total, "unit": "笔"},
        {"section": "Quick Sim (快速评测)", "metric": "已结算", "value": qs_settled, "unit": "笔"},
        {"section": "Quick Sim (快速评测)", "metric": "待结算", "value": qs_open, "unit": "笔"},
        {"section": "经验规则", "metric": "生效中", "value": insights_active, "unit": "条"},
        {"section": "经验规则", "metric": "总规则 (含停用)", "value": insights_total, "unit": "条"},
        {"section": "经验规则", "metric": "experience_entries 行数", "value": exp_total, "unit": "条"},
    ]
    for j in journals:
        rows.append({
            "section": f"DailyJournal {j['journal_date']}",
            "metric": f"run={j['run_id']} decisions={j['n_decisions']}",
            "value": f"胜率 {(j['win_rate'] or 0)*100:.1f}% / PnL ${j['pnl_usdt']:.2f}",
            "unit": "",
        })
    return rows


def build_review_xlsx() -> bytes:
    """主入口：返回完整 .xlsx 的二进制内容（5 sheets）。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws_overview = wb.active
    ws_overview.title = "Overview"
    ws_trades = wb.create_sheet("Trades")
    ws_reflections = wb.create_sheet("Reflections")
    ws_insights = wb.create_sheet("Insights")
    ws_experiences = wb.create_sheet("Experiences")

    conn = _open()
    try:
        _write_dict_list(ws_overview, _build_overview(conn))

        trade_rows = _safe_query(conn, """
            -- v170-P0+ Trades 第 1 列 = trade_uuid（全链路主键，去重身份 ID）
            -- 同时废弃冗余 id 列（与 decision_id 重复），避免和 review 端重复计数。
            SELECT trade_uuid,
                   'paper' AS source,
                   id AS decision_id,
                   UPPER(target_asset) AS asset,
                   UPPER(suggested_action) AS action,
                   entry_price,
                   exit_price,
                   forward_pnl AS pnl_pct,
                   CASE WHEN forward_pnl > 0 THEN 'win'
                        WHEN forward_pnl < 0 THEN 'loss'
                        WHEN forward_pnl IS NOT NULL THEN 'flat'
                        ELSE NULL END AS verdict,
                   trade_gate_reason AS reason_code,
                   created_at,
                   entry_time AS settled_at
              FROM ai_decisions
             WHERE entry_price IS NOT NULL
               AND entry_price > 0
               AND settled = 1
             ORDER BY id DESC LIMIT 500
        """)
        _rows_to_sheet(ws_trades, trade_rows)

        sref_rows = _safe_query(conn, """
            SELECT id, decision_id, news_id, asset, event_type_code, verdict,
                   score_coverage, score_fabrication, created_at
              FROM structured_reflections
             ORDER BY id DESC LIMIT 500
        """)
        err_rows = _safe_query(conn, """
            SELECT id, decision_id, error_code, origin, severity, source, detail, created_at
              FROM decision_errors
             ORDER BY id DESC LIMIT 200
        """)
        merged: List[Dict[str, Any]] = []
        for r in sref_rows:
            d = dict(r); d["source"] = "structured_reflection"; merged.append(d)
        for r in err_rows:
            d = dict(r); d.setdefault("source", "decision_error"); merged.append(d)
        _write_dict_list(ws_reflections, merged)

        insight_rows = _safe_query(conn, """
            SELECT id, kind, scope, asset, rule, active, origin,
                   source_samples, created_at, manual_deactivated
              FROM review_insights
             ORDER BY active DESC, created_at DESC LIMIT 300
        """)
        _rows_to_sheet(ws_insights, insight_rows)

        exp_rows = _safe_query(conn, """
            SELECT id, kind, title, content, applicable_assets, applicable_kinds,
                   applicable_actors, weight, priority, status, created_at, updated_at,
                   apply_count, hit_count, applied_win_rate, control_win_rate, lift
              FROM experience_entries
             ORDER BY apply_count DESC, created_at DESC LIMIT 200
        """)
        _rows_to_sheet(ws_experiences, exp_rows)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    wb.properties.creator = "trident_v170"
    wb.properties.title = "反思系统导出"
    wb.properties.description = (
        f"导出时间 (UTC): {datetime.now(timezone.utc).isoformat()}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    data = build_review_xlsx()
    out = f"/tmp/trident_review_{int(time.time())}.xlsx"
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"OK: {len(data)} bytes -> {out}")