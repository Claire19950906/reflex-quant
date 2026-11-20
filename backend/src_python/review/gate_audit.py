# -*- coding: utf-8 -*-
"""闸门评估留痕 (v321.4 P0, 需求 #1「闸门到底在拦什么」)。

背景（实测，不是推测）:
    trade_decisions 3243 条 = ENTRY 51 / EXIT 5 / REJECT 3187。
    3187 条 REJECT 的 deny_reason 全是**规则型**:
        asset_BTC 1451 / high_strength 733 / R8:同批次兄弟决策>3 813
    **分数型拒绝 0 条** —— 而 trade_decisions **没有 final_score 列**,
    HOLD 也从不写入 → 分数门槛 (buy_min/sell_min) 的判别力
    **在数据层根本不存在**, 任何"闸门准不准"的评估都无从计算。

本模块把这些缺失的读数补上: 每个**被评估过的** ai_decision 写一行
gate_evaluations, 记录当时的 score / evidence_confidence / gate_reason
与实际拒绝码, 使「拦了什么、拦得对不对」第一次可被统计。

边界（重要）:
    · **只读观测层** —— 不参与下单判定, 不改变任何交易行为;
    · 调用方必须 try/except —— 写失败绝不能阻断交易链;
    · 幂等 —— 按 ai_decision_id 唯一, 同一条决策重复评估只留首次读数
      (拒绝码反映"它第一次被评估时的状态", 不随后续状态变化漂移)。

判据顺序必须与 quick_sim.pending_signals 的 SQL 条件一致, 否则
"passed" 会与"真的进了下单候选"错位。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 拒绝码（首个未通过的条件, 便于 GROUP BY 定位瓶颈）
REJECT_NO_DIRECTION = "no_direction"      # AI 建议不是 BUY/SELL
REJECT_BAD_ASSET = "bad_asset"            # target_asset 空 / NONE
REJECT_GATE_REASON = "gate_reason"        # trade_gate_reason 不是 passed
REJECT_EVIDENCE_HOLD = "evidence_hold"    # evidence_action == HOLD
REJECT_LOW_CONFIDENCE = "low_confidence"  # evidence_confidence < 门槛
REJECT_ASSET_NOT_ALLOWED = "asset_not_allowed"
REJECT_STATUS_BLOCKED = "status_blocked"  # status 不在可交易白名单

_DEFAULT_TRADABLE = ("UNREAD", "APPROVED", "REVIEWED", "AUTO_APPROVED")


def hold_subcode(reasoning: str, asset: str) -> str:
    """把 no_direction 再拆一层（v336）。

    实测 2026-11-11～13 闸门 816 次评估 passed=0、no_direction 811 次，
    但"模型判断无方向""品种角色标错""理由为空"三种问题的修法完全不同，
    糊在一个 reject_code 里等于没有诊断信息。
    只改 reject_detail 文本，**不改变任何放行判定**。
    """
    try:
        import opt_direction as _od  # 分析层纯函数，无副作用
        return _od.classify_no_direction(reasoning, asset)
    except Exception:
        return "unknown"


def classify(row: Dict[str, Any], *,
             passed_reason: str,
             allowed_assets: Iterable[str],
             conf_gate: float,
             tradable_statuses: Iterable[str] = _DEFAULT_TRADABLE,
             reasoning: str = "",
             ) -> Tuple[int, str, str]:
    """对单条 ai_decisions 判定 (passed, reject_code, reject_detail)。

    顺序与 quick_sim.pending_signals 的 SQL 逐条对应:
        action ∈ (BUY,SELL) → asset ∉ ('',NONE) → trade_gate_reason = passed
        → evidence_action != HOLD → evidence_confidence >= gate
        → asset ∈ 白名单 → status ∈ 可交易白名单
    """
    action = str(row.get("action") or "").upper()
    asset = str(row.get("asset") or "").upper()
    gate_reason = str(row.get("trade_gate_reason") or "")
    ev_action = str(row.get("evidence_action") or "").upper()
    status = str(row.get("status") or "").upper()
    try:
        conf = float(row.get("evidence_confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    allowed = {str(a).upper() for a in (allowed_assets or [])}
    tradable = {str(s).upper() for s in (tradable_statuses or [])}

    if action not in ("BUY", "SELL"):
        sub = hold_subcode(reasoning or row.get("reasoning") or "", asset)
        return 0, REJECT_NO_DIRECTION, f"action={action or 'EMPTY'} sub={sub}"
    if asset in ("", "NONE"):
        return 0, REJECT_BAD_ASSET, f"asset={asset or 'EMPTY'}"
    if gate_reason != passed_reason:
        return 0, REJECT_GATE_REASON, f"trade_gate_reason={gate_reason or 'EMPTY'}"
    if ev_action == "HOLD":
        return 0, REJECT_EVIDENCE_HOLD, "evidence_action=HOLD"
    if conf < float(conf_gate):
        return 0, REJECT_LOW_CONFIDENCE, f"confidence={conf:.4f} < gate={conf_gate:.4f}"
    if allowed and asset not in allowed:
        return 0, REJECT_ASSET_NOT_ALLOWED, f"asset={asset} 不在白名单"
    if status not in tradable:
        return 0, REJECT_STATUS_BLOCKED, f"status={status or 'EMPTY'}"
    return 1, "", ""


def record_gate_funnel(conn: sqlite3.Connection, *,
                       minutes: int = 180,
                       passed_reason: str,
                       allowed_assets: Iterable[str],
                       conf_gate: float,
                       tradable_statuses: Iterable[str] = _DEFAULT_TRADABLE,
                       now_ts: Optional[int] = None,
                       ) -> Dict[str, Any]:
    """把窗口内所有被评估过的 ai_decisions 落进 gate_evaluations。

    Returns: {"evaluated": n, "inserted": n, "passed": n, "rejected": {code: n}}
    """
    now = int(now_ts if now_ts is not None else time.time())
    try:
        import db as _db
        _db.ensure_gate_evaluations(conn)
    except Exception as exc:
        print(f"[gate_audit] 建表失败: {type(exc).__name__}: {exc}", flush=True)
        return {"evaluated": 0, "inserted": 0, "passed": 0, "rejected": {}}

    window = f"-{max(1, int(minutes))} minutes"
    # reasoning 列是 v336 才需要（用于把 no_direction 再拆一层）。
    # 老库/测试夹具可能没有这一列 → 先探测再用，否则整条 SELECT 报 no such column。
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    except sqlite3.Error:
        cols = set()
    reasoning_col = "ad.reasoning, " if "reasoning" in cols else ""
    try:
        rows = conn.execute(
            "SELECT ad.id AS ai_decision_id, ad.news_id, "
            "       UPPER(ad.target_asset) AS asset, UPPER(ad.suggested_action) AS action, "
            "       ad.sentiment_score AS score, ad.evidence_confidence, "
            "       ad.evidence_action, ad.trade_gate_reason, ad.status, "
            + "       " + reasoning_col +
            "       qs.id AS trade_id "
            "FROM ai_decisions ad "
            "LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id "
            "WHERE datetime(ad.created_at, 'localtime') "
            "      > datetime('now', 'localtime', ?) "
            "ORDER BY ad.id ASC",
            (window,),
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"[gate_audit] 查询失败: {type(exc).__name__}: {exc}", flush=True)
        return {"evaluated": 0, "inserted": 0, "passed": 0, "rejected": {}}

    inserted = 0
    passed_n = 0
    rejected: Dict[str, int] = {}
    # v466 P0-0a：读阶段不碰写锁，写阶段一次批量提交。
    # 旧实现：循环里逐条 execute(INSERT OR IGNORE)，第一条 INSERT 就把事务打开，
    # 之后 classify / 统计都挂在这个写事务上；quick_sim 一轮里它能撑出 7~24s 的
    # 长事务（长事务哨兵实证），期间其他 writer 排队等写锁。
    # 改为：先只读把每行分类好、攒进 params，最后单条多 VALUES 批量 INSERT + 一次 commit，
    # 持锁窗口从"整轮分类"缩到"一次批量写"。ai_decision_id UNIQUE 保证幂等。
    batch: List[Tuple[Any, ...]] = []
    for raw in rows:
        # sqlite3.Row 不能直接 dict(): 它是值序列而非键值对 → ValueErr
        if hasattr(raw, "keys"):
            row = {k: raw[k] for k in raw.keys()}
        else:  # row_factory 未设时退化为 tuple
            fields = ["ai_decision_id", "news_id", "asset", "action", "score",
                      "evidence_confidence", "evidence_action", "trade_gate_reason",
                      "status"]
            if reasoning_col:
                fields.append("reasoning")
            fields.append("trade_id")
            row = dict(zip(fields, raw))
        aid = row.get("ai_decision_id")
        if aid is None:
            continue
        p, code, detail = classify(
            row, passed_reason=passed_reason, allowed_assets=allowed_assets,
            conf_gate=conf_gate, tradable_statuses=tradable_statuses,
            reasoning=row.get("reasoning") or "")
        passed_n += p
        if not p:
            rejected[code] = rejected.get(code, 0) + 1
        ctx = {
            "window_minutes": int(minutes),
            "already_traded": bool(row.get("trade_id")),
            "score": row.get("score"),
        }
        batch.append((
            now, int(aid), row.get("news_id"), str(row.get("asset") or ""),
            str(row.get("action") or ""), row.get("score"),
            row.get("evidence_confidence"), row.get("evidence_action"),
            row.get("trade_gate_reason"), row.get("status"),
            int(p), code, detail[:200],
            json.dumps(ctx, ensure_ascii=False),
        ))

    if batch:
        placeholders = ",".join(["(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"] * len(batch))
        params: List[Any] = []
        for item in batch:
            params.extend(item)
        try:
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO gate_evaluations "
                "(evaluated_at, ai_decision_id, news_id, asset, action, score, "
                " evidence_confidence, evidence_action, trade_gate_reason, status, "
                " passed, reject_code, reject_detail, context_json) "
                f"VALUES {placeholders}",
                params,
            )
            inserted = conn.total_changes - before
        except sqlite3.Error as exc:  # 批量失败不阻断主流程
            print(f"[gate_audit] 批量写入失败: {type(exc).__name__}: {str(exc)[:100]}",
                  flush=True)
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    return {"evaluated": len(rows), "inserted": inserted,
            "passed": passed_n, "rejected": rejected}
