# -*- coding: utf-8 -*-
"""纸面交易 / 反思审批路由（结构切片，2026-11-17）。

从 api_server.py 搬来 8 条 /api/paper/* 端点 + 5 个请求模型 + 1 个 KPI 同步助手：
  buy-sell-kpi（买卖点技术指标分层）、adjustments 列表/通过/拒绝、
  strategy-reflections 生成/列表/通过/拒绝。
业务逻辑逐字未改：参数调整走 param_key_aliases 解析命名空间前缀（v339），
settings 桶需显式 apply_to_settings=true；钳制后等值判定为 noop 不标 applied。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db

router = APIRouter()


class _AdjustApproveBody(BaseModel):
    apply_to_settings: bool = True
    note: str = ""


class _AdjustRejectBody(BaseModel):
    note: str = ""


@router.get("/api/paper/buy-sell-kpi")
async def get_paper_buy_sell_kpi(days: int = 60, asset: Optional[str] = None):
    """买卖点技术指标累计：按 asset × action × outcome(win/loss/breakeven) 分层。

    给前端「反思展示」面板用：胜率、平均盈亏、MFE/MAE、持仓时长、
    触发时距新闻时间分布、推荐入场分位、按事件强度分层胜率、
    BUY/SELL 后"涨/跌"的胜率差（→「分张/跌制定策略」核心数据）。
    """
    days = min(max(1, int(days)), 365)
    return await asyncio.to_thread(_buy_sell_kpi_sync, days, asset)


def _buy_sell_kpi_sync(days: int, asset: Optional[str]):
    """买卖点 KPI 同步版。SQL: quick_sim_trades + ai_decisions + raw_news 三表 LEFT JOIN。"""
    from paper_trading_kpi import compute_buy_sell_kpi
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        return compute_buy_sell_kpi(conn, days=days, asset=asset)
    finally:
        conn.close()


@router.get("/api/paper/adjustments")
def list_paper_adjustments(status: str = "pending", limit: int = 50):
    """列出 strategy_param_adjustments。status ∈ pending/approved/rejected/applied。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, asset, action, param_key, direction, magnitude,
                      sample_size, avg_confidence, evidence_summary,
                      first_suggested_at, last_suggested_at, status, note
               FROM strategy_param_adjustments
               WHERE status=?
               ORDER BY last_suggested_at DESC LIMIT ?""",
            (status, max(1, min(500, int(limit)))),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "asset": r["asset"],
                "action": r["action"],
                "param_key": r["param_key"],
                "direction": r["direction"],
                "magnitude": r["magnitude"],
                "sample_size": int(r["sample_size"] or 0),
                "avg_confidence": r["avg_confidence"],
                "evidence_summary": r["evidence_summary"],
                "first_suggested_at": r["first_suggested_at"],
                "last_suggested_at": r["last_suggested_at"],
                "status": r["status"],
                "note": r["note"] or "",
            }
            for r in rows
        ]
    finally:
        conn.close()


@router.post("/api/paper/adjustments/{adj_id}/approve")
def approve_paper_adjustment(adj_id: int, body: _AdjustApproveBody):
    """审批通过：settings 子集 → 调 paper_trading.set_settings；
    strategies 子集 → 调 strategy_store.add_version(source='reflection')。"""
    import paper_trading as _pt
    import strategy_store as _ss
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM strategy_param_adjustments WHERE id=? AND status='pending'",
            (adj_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="adjustment not found or not pending")
        adj = {k: row[k] for k in row.keys()}
        raw_key = str(adj.get("param_key") or "")
        # v339: 别名层。反思侧 key 带命名空间前缀（score./sizing./filter.），
        # 审批白名单用裸 key —— 实测 98/98 条 pending 必 400，反思进化停摆 45 天。
        from param_key_aliases import resolve_key as _resolve_key
        key, bucket, why = _resolve_key(raw_key)
        if key is None:
            raise HTTPException(
                status_code=422,
                detail=f"param_key '{raw_key}' 无法自动应用：{why}",
            )
        if bucket == "settings" and not body.apply_to_settings:
            raise HTTPException(
                status_code=422,
                detail=f"'{raw_key}' 属于运行态设置，approve 时需传 apply_to_settings=true",
            )
        applied: Dict[str, Any] = {
            "adjustment_id": adj_id, "key": raw_key,
            "canonical_key": key, "bucket": bucket,
        }
        if key in _pt._V182_SETTINGS_KEYS and body.apply_to_settings:
            try:
                change = _pt.apply_adjustment_to_settings(adj, conn)
                # v339: set_settings 对门槛有 [0.1, 0.9] 钳制，折算后等于原值时
                # 写库成功但没有实际变化。这种情况绝不能标 applied，否则就是
                # 「状态显示已应用、参数一动不动」的假通过。
                if change["new_value"] == change["old_value"]:
                    applied.update({
                        "applied_to": "noop",
                        "old_value": change["old_value"],
                        "new_value": change["new_value"],
                        "reason": "折算后与原值相同（受钳制区间限制），未改变任何参数",
                    })
                    return applied
                applied.update({
                    "applied_to": "paper_trading_settings",
                    "old_value": change["old_value"],
                    "new_value": change["new_value"],
                    "applied_at": change["applied_at"],
                })
                conn.execute(
                    """UPDATE strategy_param_adjustments
                       SET status='applied', applied_setting_change=?, updated_at=datetime('now')
                       WHERE id=?""",
                    (f"{raw_key}={change['old_value']}->{change['new_value']}", adj_id),
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"apply_settings failed: {exc}")
        else:
            cur = _ss.get_current_strategy(conn)
            sid = int(cur["id"])
            from review.review_agent import _V182_PARAM_KEYS as _ALL_KEYS
            if key not in _ALL_KEYS:
                raise HTTPException(status_code=400, detail=f"unknown param_key: {key}")
            cur_params = dict(cur.get("active_version", {}).get("params") or {})
            cur_val = cur_params.get(key)
            direction = str(adj.get("direction") or "")
            magnitude = float(adj.get("magnitude") or 0.05)
            if direction in ("on", "off"):
                cur_params[key] = 1 if direction == "on" else 0
            else:
                try:
                    base = float(cur_val or 0)
                except (TypeError, ValueError):
                    base = 0.5
                factor = 1.0 + (magnitude if direction == "up" else -magnitude)
                cur_params[key] = round(base * factor, 4)
            try:
                ver = _ss.add_version(
                    sid, cur_params, source="reflection",
                    note=f"v182 reflection approved: {key} {direction} {magnitude}",
                    connection=conn,
                )
                applied.update({
                    "applied_to": "strategy_version",
                    "strategy_id": sid,
                    "new_version_id": ver["id"],
                    "version": ver["version"],
                    "new_params": ver["params"],
                })
                conn.execute(
                    """UPDATE strategy_param_adjustments
                       SET status='applied', applied_strategy_version_id=?, updated_at=datetime('now')
                       WHERE id=?""",
                    (ver["id"], adj_id),
                )
            except ValueError as ve:
                raise HTTPException(status_code=429, detail=str(ve))
        conn.commit()
        return applied
    finally:
        conn.close()


@router.post("/api/paper/adjustments/{adj_id}/reject")
def reject_paper_adjustment(adj_id: int, body: _AdjustRejectBody):
    """审批拒绝：status='rejected'（不可逆）。"""
    from engine import reflection as _ref
    conn = db.get_connection()
    try:
        ok = _ref._reject_adjustment(conn, adj_id, body.note)
        if not ok:
            raise HTTPException(status_code=404, detail="adjustment not found or not pending")
        return {"adjustment_id": adj_id, "status": "rejected"}
    finally:
        conn.close()


class _StratReflectGenerateBody(BaseModel):
    """手动触发策略级反思生成。"""
    period_days: int = 30
    asset_filter: Optional[str] = None


class _StratReflectApproveBody(BaseModel):
    """审批通过。apply_to_settings=False 时只进 strategy_version。"""
    apply_to_settings: bool = True
    note: str = ""


class _StratReflectRejectBody(BaseModel):
    """审批拒绝。"""
    note: str = ""


@router.post("/api/paper/strategy-reflections/generate")
async def generate_strategy_reflection(body: _StratReflectGenerateBody):
    """v201 #1: 组装 BuySellKpi + 反思分布 -> LLM -> 写 strategy_reflections (pending)。"""
    from engine import strategy_reflection as _sr
    period_days = max(7, min(180, int(body.period_days)))
    asset = (body.asset_filter or "").strip().upper() or None
    rid = await asyncio.to_thread(
        _sr.generate_strategy_reflection, period_days, asset, "",
    )
    if not rid:
        return {"generated": False, "reason": "样本不足或 LLM 输出无效"}
    return {"generated": True, "reflection_id": int(rid)}


@router.get("/api/paper/strategy-reflections")
def list_strategy_reflections(status: str = "pending", limit: int = 50):
    """列出 strategy_reflections。status ∈ pending/approved/rejected/applied/expired。"""
    valid = {"pending", "approved", "rejected", "applied", "expired"}
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(valid)}")
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, period_start, period_end, period_days, sample_size,
                      llm_summary, strategy_patterns_json, recommendations_json,
                      affected_dimension, evidence_summary, overall_confidence,
                      status, admin_note, created_at, approved_at,
                      applied_strategy_version_id
                 FROM strategy_reflections
                WHERE status=?
                ORDER BY created_at DESC LIMIT ?""",
            (status, max(1, min(200, int(limit)))),
        ).fetchall()
        out = []
        for r in rows:
            try:
                patterns = json.loads(r["strategy_patterns_json"] or "[]")
            except Exception:
                patterns = []
            try:
                recs = json.loads(r["recommendations_json"] or "[]")
            except Exception:
                recs = []
            out.append({
                "id": int(r["id"]),
                "period_start": r["period_start"],
                "period_end": r["period_end"],
                "period_days": int(r["period_days"] or 0),
                "sample_size": int(r["sample_size"] or 0),
                "llm_summary": r["llm_summary"] or "",
                "strategy_patterns": patterns,
                "recommendations": recs,
                "affected_dimension": r["affected_dimension"] or "",
                "evidence_summary": r["evidence_summary"] or "",
                "overall_confidence": r["overall_confidence"],
                "status": r["status"],
                "admin_note": r["admin_note"] or "",
                "created_at": r["created_at"],
                "approved_at": r["approved_at"],
                "applied_strategy_version_id": r["applied_strategy_version_id"],
            })
        return out
    finally:
        conn.close()


@router.post("/api/paper/strategy-reflections/{reflection_id}/approve")
async def approve_strategy_reflection_endpoint(reflection_id: int,
                                               body: _StratReflectApproveBody):
    """审批通过：循环 recommendations -> apply 到 settings / strategy_version。"""
    from engine import strategy_reflection as _sr
    result = await asyncio.to_thread(
        _sr.approve_strategy_reflection, int(reflection_id),
        bool(body.apply_to_settings), body.note,
    )
    if result.get("status") in ("not_found_or_not_pending", "error"):
        status_code = 404 if result.get("status") == "not_found_or_not_pending" else 500
        raise HTTPException(status_code=status_code, detail=result)
    return result


@router.post("/api/paper/strategy-reflections/{reflection_id}/reject")
async def reject_strategy_reflection_endpoint(reflection_id: int,
                                              body: _StratReflectRejectBody):
    """驳回：status='rejected'（不可逆）。"""
    from engine import strategy_reflection as _sr
    ok = await asyncio.to_thread(
        _sr.reject_strategy_reflection, int(reflection_id), body.note,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="reflection not found or not pending")
    return {"reflection_id": int(reflection_id), "status": "rejected"}


# v505: 函数体全是同步 sqlite3 查询、零 await —— 用 def 让 FastAPI 丢线程池，
# 不要改回 async def，否则事件循环会被磁盘 IO 堵住（quality_gates 会拦）。
@router.get("/api/ledger/reconcile")
def get_ledger_reconcile() -> Dict[str, Any]:
    """两套模拟账本对账：把"系统到底赚没赚钱"这个问题摊开。

    现状（2026-11-19 22:50 实测）：
      · `t2_positions`：二阶段持仓模拟，10 笔已平仓、3 胜 7 负、合计 −80.11 USDT
      · `quick_sim_trades`：单笔快测，只有 1 笔（−0.60 USDT）
    两套账本口径不同、样本量差 10 倍，如果页面各取一套，"这系统赚不赚钱"
    会得到两个互相矛盾的答案 —— 而真正的原因是**样本根本不够**，
    不是哪套算错了。这个接口把两边同时给出，并由后端明确标出"官方绩效口径"。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row   # 下面按列名取值
    try:
        # 官方绩效口径：quick_sim_trades（analysis_yield 的 scorecard 就认这套）
        qs = conn.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) settled, "
            "SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) wins, "
            "SUM(pnl_usdt) net, AVG(pnl_usdt) avg_pnl, "
            "SUM(CASE WHEN cost_bps>0 THEN 1 ELSE 0 END) with_cost "
            "FROM quick_sim_trades").fetchone()
        # t2 二阶段模拟
        t2 = conn.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) closed, "
            "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_n, "
            "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins, "
            "SUM(realized_pnl) net, AVG(realized_pnl) avg_pnl, "
            "SUM(fee_paid) fees "
            "FROM t2_positions").fetchone()
        # 决策侧：有多少决策没进官方账本（衡量"分析过但没成交"的规模）
        orphan = conn.execute(
            "SELECT COUNT(*) n FROM ai_decisions ad "
            "WHERE ad.created_at >= datetime('now','-7 day') "
            "AND NOT EXISTS (SELECT 1 FROM quick_sim_trades q WHERE q.decision_id=ad.id)"
        ).fetchone()
        dec_7d = conn.execute(
            "SELECT COUNT(*) n FROM ai_decisions WHERE created_at >= datetime('now','-7 day')"
        ).fetchone()
        # t2 的每一行能否回溯到某条决策 —— 表里根本没有 decision_id 外键，
        # 所以答案只可能是 0；这个数字本身就是一条结论。
        t2_traceable = conn.execute(
            "SELECT COUNT(*) n FROM t2_positions p WHERE p.thesis_json LIKE '%decision%'"
        ).fetchone()
    finally:
        conn.close()

    q_n = int(qs["n"] or 0)
    t_n = int(t2["n"] or 0)
    orphan_n = int(orphan["n"] or 0)
    dec_n = int(dec_7d["n"] or 0)

    ledgers = [
        {
            "key": "quick_sim",
            "name": "单笔快测（官方绩效口径）",
            "official": True,
            "table": "quick_sim_trades",
            "n": q_n,
            "closed": int(qs["settled"] or 0),
            "open": q_n - int(qs["settled"] or 0),
            "wins": int(qs["wins"] or 0),
            "net": round(qs["net"] or 0, 2),
            "avg": round(qs["avg_pnl"] or 0, 2),
            "note": "每笔决策按固定名义本金跑一个短周期，只记真正过闸进场的单子；"
                    "跑得少不是因为坏了，是因为闸门拦掉 97%。",
        },
        {
            "key": "t2",
            "name": "二阶段持仓模拟",
            "official": False,
            "table": "t2_positions",
            "n": t_n,
            "closed": int(t2["closed"] or 0),
            "open": int(t2["open_n"] or 0),
            "wins": int(t2["wins"] or 0),
            "net": round(t2["net"] or 0, 2),
            "avg": round(t2["avg_pnl"] or 0, 2),
            "note": "带止损/目标/费率的两阶段持仓，持仓更久、单笔金额更大；"
                    "它衡量的是「持仓管理」，不是「信号质量」。",
        },
    ]

    verdicts = []
    for L in ledgers:
        n = L["closed"]
        if n == 0:
            verdicts.append(f"{L['name']}：0 笔结算，无法判断")
        elif n < 30:
            verdicts.append(f"{L['name']}：{n} 笔结算（<30），只能当轶事")
        else:
            wr = round(100.0 * L["wins"] / n, 1)
            verdicts.append(f"{L['name']}：{n} 笔、胜率 {wr}%、净 {L['net']}，"
                            f"{'可用' if L['net'] > 0 else '为负，需先修策略'}")

    return {
        "ok": True,
        "ledgers": ledgers,
        "official": "quick_sim",
        "coverage": {
            "decisions_7d": dec_n,
            "not_in_official_ledger_7d": orphan_n,
            "t2_rows_traceable_to_decision": int(t2_traceable["n"] or 0),
            "note": ("近 7 天决策里没有进官方账本的条数 —— 这部分是「分析过但没成交」，"
                     "不等于漏账。t2_positions 没有 decision_id，私人持仓无法回溯到"
                     "具体决策，只能当独立的持仓管理实验看。"),
        },
        "verdict": "；".join(verdicts),
        "disclaimer": ("两套账本口径不同，不能相加：quick_sim 测信号、t2 测持仓管理。"
                       "判断「能不能赚钱」只看 official 那套，且至少 30 笔结算。"),
    }
