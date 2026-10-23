#!/usr/bin/env python3
"""单笔反思（P4）— 每笔模拟交易结算后的即时 AI 归因。

结算时 enqueue → reflection_worker 每 30s 处理 pending 记录：
1. 用 review_agent 的 23 类失败 / 10 类成功框架做单笔归因（LLM，走 OpenRouter→MiniMax）
2. 结果存 trade_reflections（reason_code/name/text/confidence）
3. 同类失败原因累计 ≥3 笔时，生成一条经验规则写入 review_insights（即时沉淀高频失败模式）
4. 经验命中验证：调用 review_agent._evaluate_insights（按命中归因，双向可恢复）

修复要点（本轮 P0/P1/P2 综合）：
  P0  · reflection_worker 每小时调用的命中验证改为 review_agent._evaluate_insights
       （旧版 _insight_hit_validation 已废弃：单向不可恢复、阈值写死、会覆盖人工停用）
  P1  · 批量 LLM：按 WIN/LOSS 分组一次性调 _analyze_trades_llm（每批 ≤5 笔 → 1-2 次 LLM）
  P1  · LLM 失败不再直接标 'failed'，加 retry_count/限速重试（≤3 次，1h 节流）
  P2  · _maybe_emit_insight 加 asset + action 三键联合（避免给 XAU 写规则但样本全是 BTC）
  P2  · 去重匹配加 ESCAPE（防 reason_name 含 %/_ 漏出成通配符）
  P2.3· 反思 LLM 注入生效经验：_load_trade_dict 挂 _active_insights → _analyze_trades_llm 注入 prompt，
       关闭"反思 → 沉淀 → 新反思回查"闭环
  P3  · process_once 加 PROCESS_BUDGET_SECONDS 时间预算（20s）：超时则跳过本轮 LLM、
       把整批塞回重试队列；避免慢响应阻塞 worker 30s 节奏
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import db
import review_agent
import history_review
import trade_quality
from .utils import _now


# ── 重试机制参数（防止 LLM 抖动永久丢失反思）────────────────────────────────
# 阈值说明：3 次失败才永久 'failed'；同一笔失败 1 小时内不重试（避免抖动风暴反复打 API）。
MAX_RETRY = 3
RETRY_BACKOFF_SECONDS = 3600  # 1h 限速窗口

# ── 时间预算（防止 LLM 慢响应阻塞 worker 30s 节奏）──────────────────────────────
# worker 每 30s 调一次 process_once；如果 LLM 调用卡 25s+，下一轮会"重叠执行"，
# 既浪费配额又可能让数据库长时间被锁。
# 时间预算控制：单次 process_once 的 wall-clock 上限，超时则整批进入重试队列。
# 超时后不会"硬杀"正在飞的 LLM 请求（没法可靠取消），而是停止接新批、提交已完成的部分。
PROCESS_BUDGET_SECONDS = 20.0


def _open_db() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def enqueue(
    conn: sqlite3.Connection,
    decision_id: int,
    news_id: Optional[int],
    asset: str,
    action: str,
    verdict: str,
    pnl_pct: Optional[float],
) -> None:
    """结算后把一笔交易加入待反思队列（幂等：已存在则忽略）。

    ⚠️  DEPRECATED (v190):trade_reflections(v1)表已无新写入 30+ 天。
    新反思走 trade_reflections_v2 + structured_reflection.py。
    保留本函数仅为兼容 engine/forward.py:333 旧调用,实际写入将被忽略
    (因 v1 表在生产已 0 行 + 无活跃消费者,INSERT 落表但没有 worker 拉取)。
    计划在 v191 完全移除本函数 + 删 v1 表 DDL。
    """
    conn.execute(
        """INSERT OR IGNORE INTO trade_reflections
           (decision_id, news_id, asset, action, verdict, pnl_pct, status)
           VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
        (int(decision_id), news_id, str(asset or ""), str(action or ""),
         str(verdict or ""), float(pnl_pct) if pnl_pct is not None else None),
    )


def _iso_to_epoch(iso: str) -> Optional[int]:
    """ISO 时间文本 → epoch 秒；失败返回 None（供 history_review._fmt_ts 格式化）。"""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _load_trade_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    """把 trade_reflections 行 + 决策上下文拼成 history_review._analyze_trades_llm 期望的格式。

    同时把「当前生效经验」挂到 trade dict 上，供 _analyze_trades_llm 注入 prompt，
    关闭"反思 → 沉淀 → 新反思回查"的闭环。
    """
    decision_id = int(row["decision_id"])
    verdict = str(row["verdict"] or "")
    asset = str(row["asset"] or "")
    action = str(row["action"] or "")
    # 补充交易上下文（新闻原文 + 决策推理 + 快照 + 结算口径），帮助归因。
    # v181 起额外取：entry_time/entry_price/mfe/mae（结果事实反证方向）、
    # settle_flags（结算口径可置信度）、gate_chain（门卫层）、published_at/first_seen_ts（旧闻判定）。
    ctx = conn.execute(
        """SELECT
              rn.content AS news_text, rn.gate_core_impact,
              rn.published_at, rn.first_seen_ts, rn.ts AS news_ts,
              rn.source AS news_source,
              ad.reasoning, ad.decision_context, ad.reasoning_path, ad.gate_chain,
              -- P2: 结算字段全部从 quick_sim_trades 取（统一权威）
              datetime(qs.entry_ts, 'unixepoch', '+8 hours') AS entry_time,
              qs.entry_price, qs.exit_price,
              qs.mfe_pct, qs.mae_pct, qs.pnl_pct AS forward_pnl, qs.mfe_time_mins,
              ad.expected_horizon, ad.settle_flags,
              ad.market_category, ad.prediction_type,
              ad.event_phase, ad.market_confirmation, ad.invalidation_condition,
              ad.event_strength, ad.direct_catalyst, ad.timeframe_match,
              ad.asset_nature, ad.factor_ranking, ad.impact_magnitude,
              ad.verification, ad.data_anchor_ts, ad.hard_data_evidence,
              ad.event_struct, ad.extra_models_consensus,
              ad.doubao_action, ad.doubao_reasoning,
              ad.insight_hit_ids, ad.evidence_action, ad.evidence_confidence,
              ad.trade_gate_reason, ad.no_trade_reason,
              ad.attention_score, ad.elasticity_window,
              ad.heat_score, ad.impact_score, ad.asset_impact,
              ad.asset_reasoning, ad.partial_pnl_pct, ad.partial_exit_price,
              ad.agent_model_id,
              qs.max_price, qs.min_price, qs.notional_usdt, qs.pnl_usdt,
              qs.strategy_id, qs.strategy_version_id, qs.paper_trading_run_id,
              qs.gate_passed, qs.gate_reason,
              qs.entry_ts, qs.exit_ts, qs.horizon_minutes, qs.verdict AS qs_verdict
           FROM ai_decisions ad
           LEFT JOIN raw_news rn ON rn.id = ad.news_id
           LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
           WHERE ad.id = ?""",
        (decision_id,),
    ).fetchone()
    # 加载生效经验列表（id + rule）。失败时给空列表（降级），不阻断反思主流程。
    try:
        active_insights = review_agent.load_active_insight_items(conn)
    except Exception:
        active_insights = []
    entry_time = str((ctx["entry_time"] if ctx else "") or "")
    def _s(field: str, default: str = "") -> str:
        if ctx is None:
            return default
        try:
            v = ctx[field]
        except (KeyError, IndexError):
            return default
        if v is None:
            return default
        return str(v)
    def _f(field: str) -> Optional[float]:
        if ctx is None:
            return None
        try:
            v = ctx[field]
        except (KeyError, IndexError):
            return None
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    def _i(field: str) -> Optional[int]:
        if ctx is None:
            return None
        try:
            v = ctx[field]
        except (KeyError, IndexError):
            return None
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {
        # ── 元数据 ──
        "id": decision_id,
        "decision_id": decision_id,
        "asset": asset,
        "action": action,
        "is_correct": verdict,
        "pnl_pct": float(row["pnl_pct"] or 0.0),
        # ── L1 新闻原文 / 门卫 ──
        "content": _s("news_text"),
        "core_impact": _s("gate_core_impact"),
        "news_source": _s("news_source"),
        "published_at": _s("published_at"),
        "first_seen_ts": _s("first_seen_ts"),
        "news_ts": _s("news_ts"),
        # ── L2 分析过程（ai_decisions） ──
        "decision_context": _s("decision_context", "{}"),
        "reasoning": _s("reasoning"),
        "reasoning_path": _s("reasoning_path"),
        "gate_chain": _s("gate_chain"),
        "asset_reasoning": _s("asset_reasoning"),
        "asset_nature": _s("asset_nature"),
        "factor_ranking": _s("factor_ranking"),
        "impact_magnitude": _f("impact_magnitude"),
        "verification": _s("verification"),
        "data_anchor_ts": _s("data_anchor_ts"),
        "hard_data_evidence": _s("hard_data_evidence"),
        "event_struct": _s("event_struct"),
        "extra_models_consensus": _s("extra_models_consensus"),
        "doubao_action": _s("doubao_action"),
        "doubao_reasoning": _s("doubao_reasoning"),
        "insight_hit_ids": _s("insight_hit_ids"),
        "evidence_action": _s("evidence_action"),
        "evidence_confidence": _f("evidence_confidence"),
        "trade_gate_reason": _s("trade_gate_reason"),
        "no_trade_reason": _s("no_trade_reason"),
        "market_category": _s("market_category"),
        "prediction_type": _s("prediction_type"),
        "event_phase": _s("event_phase"),
        "market_confirmation": _s("market_confirmation"),
        "invalidation_condition": _s("invalidation_condition"),
        "event_strength": _s("event_strength"),
        "direct_catalyst": _i("direct_catalyst") or 0,
        "timeframe_match": _s("timeframe_match"),
        "attention_score": _f("attention_score"),
        "elasticity_window": _s("elasticity_window"),
        "heat_score": _f("heat_score"),
        "impact_score": _f("impact_score"),
        "asset_impact": _f("asset_impact"),
        "partial_pnl_pct": _f("partial_pnl_pct"),
        "partial_exit_price": _f("partial_exit_price"),
        # ── L3 交易过程（quick_sim_trades） ──
        "entry_time": entry_time,
        "entry_ts": _iso_to_epoch(entry_time),
        "entry_price": _f("entry_price"),
        "exit_price": _f("exit_price"),
        "mfe_pct": _f("mfe_pct"),
        "mae_pct": _f("mae_pct"),
        "forward_pnl": _f("forward_pnl"),
        "mfe_time_mins": _f("mfe_time_mins"),
        "max_price": _f("max_price"),
        "min_price": _f("min_price"),
        "notional_usdt": _f("notional_usdt"),
        "pnl_usdt": _f("pnl_usdt"),
        "strategy_id": _i("strategy_id"),
        "strategy_version_id": _i("strategy_version_id"),
        "paper_trading_run_id": _i("paper_trading_run_id"),
        "gate_passed": _i("gate_passed") or 0,
        "gate_reason": _s("gate_reason"),
        "agent_model_id": _s("agent_model_id"),
        "exit_ts": _i("exit_ts"),
        "horizon_minutes": _i("horizon_minutes"),
        "qs_verdict": _s("qs_verdict"),
        # ── 兼容旧字段 ──
        "expected_horizon": _s("expected_horizon"),
        "settle_flags": _s("settle_flags"),
        # ── 反思回灌（active insights） ──
        "_active_insights": active_insights,
    }


def _safe_int_id(v: Any, fallback: int = -1) -> int:
    """把 LLM 返回的 decision_id 安全转 int。

    LLM 会模仿 prompt 里的 `#<id>` 写法，把 decision_id 输出成 '#1'/'#207' 这类脏
    字符串；旧实现直接 int() 会抛 ValueError 让整批反思 failed。这里统一清洗。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    s = str(v or "").strip()
    if s.startswith("#"):
        s = s[1:].strip()
    try:
        return int(s)
    except (TypeError, ValueError):
        return fallback


def _pick_results(results: List[Dict[str, Any]], decision_id: int) -> Optional[Dict[str, Any]]:
    """从批量 _analyze_trades_llm 结果里挑出当前 decision 的归因。"""
    for r in results or []:
        if _safe_int_id(r.get("decision_id")) == decision_id:
            return r
    return None


def _summarize(r: Dict[str, Any]) -> Dict[str, Any]:
    """把 _analyze_trades_llm 的输出归一为 trade_reflections 列字段。

    P0 修复：LLM 偶发把 reason_code 写成 '#207' / 'N/A' / 'null' 这类脏字符串，
    直接 int() 会抛 ValueError 阻断整批反思。改为：
      · reason_code 转换加 try/except，失败 → None（让外层 fallback 走 failed 分支）
      · confidence 转换加 clamp 到 [0, 1]，避免越界
    """
    rc_raw = r.get("reason_code")
    try:
        if rc_raw is None or rc_raw == "":
            reason_code: Optional[int] = None
        else:
            reason_code = int(rc_raw)  # 只接受纯数字；脏字符串 ('#207' / 'N/A') 走 except
    except (TypeError, ValueError):
        reason_code = None
    try:
        conf = float(r.get("confidence") or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    return {
        "reason_code": reason_code,
        "reason_name": str(r.get("reason_name") or "")[:60],
        "reason_text": str(r.get("reason_text") or "")[:200],
        "confidence": conf,
        "layer": str(r.get("layer") or ""),
        "sublayer": str(r.get("sublayer") or ""),
    }


def _maybe_emit_insight(conn: sqlite3.Connection, asset: str, action: str,
                        reason_name: str, reason_text: str) -> None:
    """同类失败原因累计 ≥5 笔（asset+action+reason 三键联合）→ 生成一条经验规则。

    修复 P2：
      ① 计数三键联合（asset + action + reason_name），避免 XAU 规则被 BTC 样本"污染"
      ② LIKE 匹配加 ESCAPE '\\'，防止 reason_name 含 SQL 通配符（%/_）漏出
      ③ 触发门槛从 3 提到 5（与 _evaluate_insights 最小样本一致，沉淀更稳健）
    """
    if not reason_name:
        return
    rows = conn.execute(
        """SELECT COUNT(*) AS n FROM trade_reflections
           WHERE status = 'done' AND verdict = 'LOSS'
             AND reason_name = ? AND asset = ? AND action = ?""",
        (reason_name, asset or "", action or ""),
    ).fetchone()
    count = int(rows["n"] or 0)
    if count < 5:
        return
    # 去重匹配：ESCAPE 让 reason_name 中的 % / _ 当字面字符处理（防通配符漏出）
    exists = conn.execute(
        "SELECT 1 FROM review_insights"
        " WHERE rule LIKE ? ESCAPE '\\' AND active = 1 LIMIT 1",
        (f"%主因[{reason_name}]%",),
    ).fetchone()
    if exists:
        return
    rule = (
        f"{asset or '?'} {action or '?'}单亏损主因[{reason_name}]："
        f"{reason_text or '注意该类风险'}（近{count}笔同类失败沉淀）"
    )
    conn.execute(
        "INSERT INTO review_insights (rule, asset, scope, active, source_samples, origin)"
        " VALUES (?, ?, 'review', 1, ?, 'reflection')",
        (rule, asset or "", count),
    )
    print(f"  [REFLECT] 沉淀经验: {rule}", flush=True)


def _insight_hit_validation(conn: sqlite3.Connection) -> Dict[str, Any]:
    """【已废弃】请改用 review_agent._evaluate_insights（双向可恢复 + 阈值配置化）。

    保留此函数仅为兼容旧 import；reflection_worker 已切换到新版，本函数不再被调用。
    调用方请直接 review_agent._evaluate_insights(conn)。
    """
    import review_agent as _ra
    return _ra._evaluate_insights(conn)


def process_once(loop: asyncio.AbstractEventLoop) -> int:
    """处理一批待反思交易（最多 5 条），返回处理条数。

    修复 P1：
      · 选 pending 时把 'failed' 且 retry_count < MAX_RETRY 且失败超 1h 的纳入重试
      · 按 WIN/LOSS 分组，一次性调 _analyze_trades_llm（批 LLM，避免 N 次调用）
      · 异常时累加 retry_count 并写入 last_error；达 MAX_RETRY 才永久 'failed'

    修复 P3：加 PROCESS_BUDGET_SECONDS 时间预算；超时则提交已完成的部分，
    未完成的部分让 worker 下轮重试（避免 LLM 慢响应阻塞 30s 节奏）。
    """
    start = time.monotonic()
    conn = _open_db()
    try:
        pending = conn.execute(
            """SELECT * FROM trade_reflections
               WHERE status = 'pending'
                  OR (status = 'failed' AND retry_count < ?
                      AND (last_error_at IS NULL
                           OR (julianday('now') - julianday(last_error_at)) * 86400 >= ?))
               ORDER BY decision_id LIMIT 5""",
            (MAX_RETRY, RETRY_BACKOFF_SECONDS),
        ).fetchall()
        if not pending:
            return 0

        # 构建 trade dict + 按 verdict 分组（这步纯 DB IO，不会触发 LLM，预算前做完）
        loss_trades: List[Dict[str, Any]] = []
        win_trades: List[Dict[str, Any]] = []
        hold_trades: List[Dict[str, Any]] = []  # v181: HOLD 也归因（补中间态样本）
        rows_by_id: Dict[int, sqlite3.Row] = {}
        for row in pending:
            eid = int(row["decision_id"])
            rows_by_id[eid] = row
            try:
                trade = _load_trade_dict(conn, row)
            except Exception as exc:
                _mark_failed(conn, eid, f"load_trade_dict: {type(exc).__name__}: {str(exc)[:60]}")
                continue
            verdict = str(row["verdict"] or "")
            if verdict == "LOSS":
                loss_trades.append(trade)
            elif verdict == "WIN":
                win_trades.append(trade)
            elif verdict == "HOLD":
                hold_trades.append(trade)
            else:
                # 未知 verdict：没归因意义，仍标 done 避免反复重试
                conn.execute(
                    """UPDATE trade_reflections
                       SET status='done', reason_code=NULL, reason_name='',
                           reason_text='', confidence=NULL,
                           reflected_at=datetime('now','localtime')
                       WHERE decision_id=?""",
                    (eid,),
                )

        # 时间预算检查：DB 加载阶段已超预算就跳过 LLM 调用（让下轮再做）
        elapsed = time.monotonic() - start
        if elapsed >= PROCESS_BUDGET_SECONDS:
            err = f"process_budget_exceeded_at_load({elapsed:.1f}s)"
            print(f"  [REFLECT] {err}; 跳过本批 LLM，下轮重试", flush=True)
            for row in pending:
                _mark_failed(conn, int(row["decision_id"]), err)
            conn.commit()
            return len(pending)

        # 批量 LLM：失败组一次、成功组一次（每批 ≤5 笔）
        # P3 关键：被预算跳过的 trade 必须在 _apply_results 之前标记完成，
        # 否则 _apply_results 会再用 retry_count+1 覆盖 _mark_failed 的写入
        # （双读+双写导致 retry_count 涨 2、last_error 被覆盖成 llm_no_reason_code）。
        loss_results: List[Dict[str, Any]] = []
        win_results: List[Dict[str, Any]] = []
        hold_results: List[Dict[str, Any]] = []
        loss_skipped: List[int] = []  # 被预算跳过的损失组 id（稍后一并标 failed）
        win_skipped: List[int] = []
        hold_skipped: List[int] = []
        try:
            if loss_trades and (time.monotonic() - start) < PROCESS_BUDGET_SECONDS:
                loss_results = history_review._analyze_trades_llm(
                    loss_trades, review_agent._FAILURE_FRAMEWORK,
                    review_agent._REASON_NAMES, "失败",
                )
            elif loss_trades:
                loss_skipped = [int(t["decision_id"]) for t in loss_trades]
                loss_trades = []
            if win_trades and (time.monotonic() - start) < PROCESS_BUDGET_SECONDS:
                win_results = history_review._analyze_trades_llm(
                    win_trades, review_agent._SUCCESS_FRAMEWORK,
                    review_agent._SUCCESS_REASON_NAMES, "成功",
                )
            elif win_trades:
                win_skipped = [int(t["decision_id"]) for t in win_trades]
                win_trades = []
            # v181: HOLD 独立归因（复用 8/22/12/2，中间态样本不丢）
            if hold_trades and (time.monotonic() - start) < PROCESS_BUDGET_SECONDS:
                hold_results = history_review._analyze_holds_llm(hold_trades)
            elif hold_trades:
                hold_skipped = [int(t["decision_id"]) for t in hold_trades]
                hold_trades = []
            _apply_results(conn, rows_by_id, loss_results + win_results + hold_results)
            # 预算跳过的笔：在 _apply_results 之后一次性标 failed（避免双读+1）
            elapsed = time.monotonic() - start
            for eid in loss_skipped + win_skipped + hold_skipped:
                err_kind = "loss" if eid in loss_skipped else ("win" if eid in win_skipped else "hold")
                _mark_failed(conn, eid, f"process_budget_exceeded_before_{err_kind}_llm({elapsed:.1f}s)")
        except Exception as exc:
            # LLM 调用本身挂掉：整批标记失败 + 限速重试
            err = f"{type(exc).__name__}: {str(exc)[:80]}"
            print(f"  [REFLECT] 批量 LLM 失败: {err}", flush=True)
            for row in pending:
                eid = int(row["decision_id"])
                _mark_failed(conn, eid, err)

        elapsed = time.monotonic() - start
        if elapsed > PROCESS_BUDGET_SECONDS:
            print(
                f"  [REFLECT] 本批耗时 {elapsed:.1f}s 超出预算 {PROCESS_BUDGET_SECONDS}s；"
                f"已正常提交，下轮若积压会减少 batch",
                flush=True,
            )

        # v182 反思改进·累计：本批处理完，写入的 improvement_suggestion 落到建议池
        try:
            n_agg = _aggregate_improvements(conn)
            if n_agg:
                print(f"  [REFLECT] v182 aggregated {n_agg} improvements → strategy_param_adjustments",
                      flush=True)
        except Exception as agg_exc:
            print(f"  [REFLECT] v182 _aggregate_improvements 异常: {agg_exc}", flush=True)

        conn.commit()
        return len(pending)
    finally:
        conn.close()


def _load_price_quality(conn: sqlite3.Connection, decision_id: int) -> tuple:
    """从 quick_sim_trades 取持仓区间价格，算反思双质量维度；失败返回 (None, None)。

    两个维度都来自客观价格事实（entry / max / min），与 LLM 归因互相独立，
    作为「方向 vs 时机 vs 买点」的量化拆解落库，供复盘页展示与后续回灌。
    """
    try:
        row = conn.execute(
            """SELECT action, entry_price, max_price, min_price
               FROM quick_sim_trades WHERE decision_id = ?""",
            (decision_id,),
        ).fetchone()
        if not row:
            return None, None
        q = trade_quality.trade_quality(
            row["action"], row["entry_price"], row["min_price"], row["max_price"],
        )
        return q["entry_accuracy"], q["trend_fit"]
    except Exception:
        return None, None


def _extract_improvement_fields(picked: Dict[str, Any]) -> Tuple[str, str, str, Optional[float], Optional[float], str]:
    """从 picked 提取 improvement_suggestion 字段，序列化为 trade_reflections 6 列。

    返回 (improvement_suggestion_json, suggestion_param_key, suggestion_direction,
          suggestion_magnitude, suggestion_confidence, suggestion_rationale)。
    若 picked 不含 improvement_suggestion 或 primary_param 为空，6 列填空。
    """
    imp = picked.get("improvement_suggestion") if isinstance(picked, dict) else None
    if not isinstance(imp, dict):
        return "", "", "", None, None, ""
    pp = imp.get("primary_param") or {}
    if not isinstance(pp, dict) or not pp:
        # 仍写整 improvement_suggestion（保留 secondary_params + evidence_summary）
        try:
            j = json.dumps(imp, ensure_ascii=False)
        except (TypeError, ValueError):
            j = ""
        return j[:4000], "", "", None, None, ""
    try:
        j = json.dumps(imp, ensure_ascii=False)
    except (TypeError, ValueError):
        j = ""
    try:
        magnitude = float(pp.get("magnitude") or 0.0)
    except (TypeError, ValueError):
        magnitude = None
    try:
        confidence = float(pp.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = None
    return (
        j[:4000],
        str(pp.get("key") or "")[:64],
        str(pp.get("direction") or "")[:16],
        magnitude,
        confidence,
        str(pp.get("rationale") or "")[:1000],
    )


def _apply_results(conn: sqlite3.Connection, rows_by_id: Dict[int, sqlite3.Row],
                   results: List[Dict[str, Any]]) -> None:
    """把批量归因结果按 decision_id 回写到 trade_reflections。

    注意：
    1. 未知 verdict 已经在 process_once 提前 UPDATE 成 'done'；HOLD 走独立归因（v181）。
       这里只处理 LOSS/WIN/HOLD，避免"未命中"分支把未知 verdict 覆盖成 'failed'。
    2. P3 修复：被预算跳过的笔不会出现在 results 里（没调 LLM），必须在这里 skip，
       否则会被 fallback 分支误标 retry_count+1 + 'llm_no_reason_code'，
       覆盖掉 process_once 后置 _mark_failed 写入的真实 last_error。
       用 results 集合判断「这批有没有被 LLM 处理过」。
    """
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 把"LLM 实际处理过的 decision_id"做成集合，未命中表示该笔未送 LLM（被 P3 跳过或 HOLD）
    processed_ids = {_safe_int_id(r.get("decision_id"), 0) for r in results or []}
    for eid, row in rows_by_id.items():
        verdict = str(row["verdict"] or "")
        if verdict not in ("LOSS", "WIN", "HOLD"):
            continue  # 未知 verdict 已在 process_once 标 done
        if eid not in processed_ids:
            # 未送 LLM：被 P3 跳过 / LLM 返回空 / 解析失败 — 这些交给 _mark_failed 处理
            continue
        # ── P0 修复：单条 trade 的 reason_code 解析/写入异常 → 不阻塞其他 trade ──
        # 原实现：异常会冒泡到 process_once 的 except，整批 _mark_failed，
        #         导致后续 reflection worker 累加 retry_count 直至永久 failed。
        try:
            picked = _pick_results(results, eid)
            if picked and picked.get("reason_code"):
                info = _summarize(picked)
                # _summarize 已容错（reason_code=None → fallback），但保险起见：
                if info.get("reason_code") is None:
                    raise ValueError(
                        f"bad_reason_code: {picked.get('reason_code')!r} (non-integer)"
                    )
                # v181: 结算口径可置信度——settle_flags 非空 → verdict 边缘情形（阈值乘数/
                # 慢反应/双 idle/入场缺失/窗口截断），归因结果标注 verdict_uncertain=1，
                # 不进入经验沉淀，避免把结算口径噪音写成"教训"。
                uncertain = 0
                try:
                    sf_row = conn.execute(
                        "SELECT settle_flags FROM ai_decisions WHERE id = ?", (eid,)
                    ).fetchone()
                    if sf_row and str(sf_row["settle_flags"] or "").strip():
                        uncertain = 1
                except Exception:
                    uncertain = 0
                acc, fit = _load_price_quality(conn, eid)
                # v182 改进建议：从 picked 解析 improvement_suggestion 并序列化（白名单已校验过）
                imp_json, sk, sd, sm_v, sc, sr = _extract_improvement_fields(picked)
                conn.execute(
                    """UPDATE trade_reflections
                       SET reason_code=?, reason_name=?, reason_text=?, confidence=?,
                           status='done', reflected_at=?, retry_count=0,
                           last_error='', last_error_at=NULL,
                           entry_accuracy=?, trend_fit=?, layer=?, sublayer=?,
                           verdict_uncertain=?,
                           -- v182 改进建议 6 列
                           improvement_suggestion=?,
                           suggestion_param_key=?,
                           suggestion_direction=?,
                           suggestion_magnitude=?,
                           suggestion_confidence=?,
                           suggestion_rationale=?
                       WHERE decision_id=?""",
                    (info["reason_code"], info["reason_name"], info["reason_text"],
                     info["confidence"], now_iso, acc, fit,
                     info.get("layer") or "", info.get("sublayer") or "",
                     uncertain,
                     imp_json, sk, sd, sm_v, sc, sr,
                     eid),
                )
                # v182：归因完成后累计 improvement 到建议池（不在 process_once 里调）
                # 累计在 _aggregate_improvements 里统一做（避免分散调用风险）
                # 失败单同类原因累计 → 沉淀经验（结算口径不可信时不沉淀）
                if str(row["verdict"] or "") == "LOSS" and not uncertain:
                    _maybe_emit_insight(conn, str(row["asset"] or ""), str(row["action"] or ""),
                                        info["reason_name"], info["reason_text"])
            else:
                # LLM 解析失败 / 未给出 reason_code → 单笔标 failed（计入 retry）
                conn.execute(
                    """UPDATE trade_reflections
                       SET status='failed',
                           last_error='llm_no_reason_code',
                           last_error_at=?,
                           retry_count=retry_count+1
                       WHERE decision_id=?""",
                    (now_iso, eid),
                )
        except Exception as exc:
            # 单笔异常不影响本批其他 trade；本笔 _mark_failed 计入 retry
            err_msg = f"_apply_results:{type(exc).__name__}:{str(exc)[:80]}"
            print(f"  [REFLECT] 单条 {eid} 异常已隔离: {err_msg}", flush=True)
            _mark_failed(conn, int(eid), err_msg)


def _mark_failed(conn: sqlite3.Connection, eid: int, err: str) -> None:
    """标 'failed' + 累加 retry_count + 记录 last_error。超 MAX_RETRY 才永久。"""
    row = conn.execute(
        "SELECT retry_count FROM trade_reflections WHERE decision_id = ?", (eid,)
    ).fetchone()
    cur = int((row["retry_count"] if row else 0) or 0)
    final_retry = min(cur + 1, MAX_RETRY)
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """UPDATE trade_reflections
           SET status='failed', retry_count=?, last_error=?, last_error_at=?
           WHERE decision_id=?""",
        (final_retry, err[:200], now_iso, eid),
    )


# ── v182 反思改进·累计与晋级 ──
_V182_ADJUSTMENT_SAMPLE_THRESHOLD = 5     # 累计样本下限（同向至少 5 笔）
_V182_ADJUSTMENT_CONFIDENCE_THRESHOLD = 0.65   # 平均置信下限
_V182_DAILY_VERSION_LIMIT = 1                 # 每日最多 1 个自动 strategy_version


def _aggregate_improvements(conn: sqlite3.Connection) -> int:
    """把 trade_reflections (v1) + trade_reflections_v2 (v2) 累计到 strategy_param_adjustments。

    触发时机：process_once 末尾（每批 ≤5 笔一次）。
    返回：累计的笔数。

    v188 后真实写入路径是 trade_reflections_v2（layer/sub_layer/signal_name + root_cause/lesson +
    depth_score）。本函数同时跑两路：v1 仍按原始字段聚合（兼容遗留），v2 把 ontology
    三层分类映射到 (param_key, direction, magnitude, confidence)，使『模拟盘反思改进』面板
    不再因 v1 空表而永远空白。
    """
    n_v1 = _aggregate_improvements_v1(conn)
    n_v2 = _aggregate_improvements_v2(conn)
    return n_v1 + n_v2


def _aggregate_improvements_v1(conn: sqlite3.Connection) -> int:
    """v1 路径：trade_reflections → strategy_param_adjustments（保留兼容）。"""
    rows = conn.execute(
        """SELECT decision_id, asset, action, suggestion_param_key, suggestion_direction,
                  suggestion_magnitude, suggestion_confidence, suggestion_rationale
           FROM trade_reflections
           WHERE status='done'
                AND suggestion_param_key != ''
                AND suggestion_direction != ''
                AND suggestion_confidence IS NOT NULL
                AND reflected_at >= datetime('now', '-10 minutes')"""
        ).fetchall()
    if not rows:
        return 0
    aggregated = 0
    for r in rows:
        did = int(r["decision_id"] or 0)
        if did <= 0:
            continue
        asset = str(r["asset"] or "*")
        action = str(r["action"] or "*")
        pk = str(r["suggestion_param_key"] or "")
        sd = str(r["suggestion_direction"] or "")
        if not pk or sd not in ("up", "down", "on", "off"):
            continue
        try:
            magnitude = float(r["suggestion_magnitude"]) if r["suggestion_magnitude"] is not None else None
        except (TypeError, ValueError):
            magnitude = None
        try:
            conf = float(r["suggestion_confidence"] or 0.0)
        except (TypeError, ValueError):
            continue
        rationale = str(r["suggestion_rationale"] or "")[:200]
        existing = conn.execute(
            """SELECT id, sample_size, avg_confidence, evidence_summary
               FROM strategy_param_adjustments
               WHERE asset=? AND action=? AND param_key=? AND direction=? AND status='pending'
               ORDER BY id DESC LIMIT 1""",
            (asset, action, pk, sd),
        ).fetchone()
        if existing:
            old_n = int(existing["sample_size"] or 0)
            old_c = float(existing["avg_confidence"] or 0.0)
            new_n = old_n + 1
            new_c = (old_c * old_n + conf) / new_n if new_n else conf
            new_evi = (str(existing["evidence_summary"] or "") + " | #" + str(did) + ": " + rationale)[-2000:]
            conn.execute(
                """UPDATE strategy_param_adjustments
                   SET sample_size=?, avg_confidence=?, magnitude=?,
                       evidence_summary=?, last_suggested_at=datetime('now'),
                       updated_at=datetime('now')
                   WHERE id=?""",
                (new_n, new_c, magnitude, new_evi, int(existing["id"])),
            )
        else:
            conn.execute(
                """INSERT INTO strategy_param_adjustments
                       (asset, action, param_key, direction, magnitude, sample_size,
                        avg_confidence, evidence_summary)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (asset, action, pk, sd, magnitude, conf, f"[#{did}] {rationale}"[:2000]),
            )
        aggregated += 1
    conn.commit()
    return aggregated


def _aggregate_improvements_v2(conn: sqlite3.Connection) -> int:
    """v2 路径：trade_reflections_v2 → strategy_param_adjustments。

    v2 schema 无 suggestion_* 列。映射规则（v188 ontology 三层分类）：
      param_key   ← f"{layer}.{sub_layer}.{signal_name}"    三层信号聚合为唯一 key
      direction   ← 按 action+outcome 推：
                      BUY/SELL + WIN              → "up"   这信号起作用，加权
                      BUY/SELL + LOSS / pnl<-0.005→ "down" 这信号拖后腿，降权
                      HOLD + filter-reject 型号   → "off"  这道闸太严，建议放松
                      HOLD + correct-no-trade 型号→ "on"   这道闸起作用，建议保留
                      其它 (FLAT 噪声)            → skip
      magnitude   ← depth_score * 0.05   0-10 分 → 0-0.5 步长（与 _apply_param_change 一致）
      confidence  ← depth_score / 10.0   0-1 归一化，作为 avg_confidence 跑加权平均
      evidence    ← f"[#{did}] {layer}.{sub_layer}.{signal_name} pnl={pnl:+.4f} "
                    f"{root_cause[:120]} | {lesson[:120]}"

    窗口：最近 24h（v2 反映已 settle 的实盘，比 v1 的 10min 更宽）。
    跳过 signal_name='unknown' / layer='' 行。
    """
    rows = conn.execute(
        """SELECT t.id, t.decision_id, t.asset, t.action,
                  COALESCE(NULLIF(t.layer, ''), '')        AS layer,
                  COALESCE(NULLIF(t.sub_layer, ''), '')    AS sub_layer,
                  COALESCE(NULLIF(t.signal_name, ''), '')  AS signal_name,
                  COALESCE(NULLIF(t.root_cause, ''), '')   AS root_cause,
                  COALESCE(NULLIF(t.lesson, ''), '')       AS lesson,
                  COALESCE(t.depth_score, 0)              AS depth_score,
                  a.forward_pnl                            AS pnl,
                  a.is_correct                             AS is_correct
             FROM trade_reflections_v2 t
        LEFT JOIN ai_decisions a ON a.id = t.decision_id
            WHERE t.created_at >= datetime('now', '-24 hours')
              AND COALESCE(t.signal_name, '') NOT IN ('', 'unknown')
              AND COALESCE(t.layer, '') != ''"""
    ).fetchall()
    if not rows:
        return 0
    aggregated = 0
    for r in rows:
        did = int(r["decision_id"] or 0)
        tid = int(r["id"] or 0)
        if did <= 0:
            continue
        layer = str(r["layer"] or "")
        sub_layer = str(r["sub_layer"] or "")
        signal_name = str(r["signal_name"] or "")
        pk = f"{layer}.{sub_layer}.{signal_name}"
        asset = str(r["asset"] or "*")
        action = str(r["action"] or "*").upper()

        # 方向：按 action+outcome 推
        pnl_raw = r["pnl"]
        is_correct = str(r["is_correct"] or "").upper()
        try:
            pnl = float(pnl_raw) if pnl_raw is not None else 0.0
        except (TypeError, ValueError):
            pnl = 0.0

        if action in ("BUY", "SELL"):
            # 实盘出手了，按盈亏分
            if is_correct == "WIN" or pnl > 0:
                sd = "up"
            elif is_correct == "LOSS" or pnl < -0.005:
                sd = "down"
            else:
                continue  # BUY/SELL 但无 pnl 数据（实盘未平仓？）— 跳过
        elif action == "HOLD":
            # 闸门拦住没出手，按信号分类
            # filter 层 + main_asset_missed / asset_match / sentiment 错过型 → 闸太严，放松
            if layer == "filter" or "filter" in sub_layer or signal_name in (
                "main_asset_missed", "asset_match", "sentiment_vs_market",
                "exp_misinterpreted", "gate_rejected",
            ):
                sd = "off"
            # analysis 层 + 正确识别 no_trade → 闸起作用，保留
            elif signal_name in ("no_trade", "correctly_held", "no_edge"):
                sd = "on"
            else:
                # 兜底：HOLD 视作 FLAT，跳过避免噪声
                continue
        else:
            continue  # 未知 action，跳过

        try:
            depth = float(r["depth_score"] or 0)
        except (TypeError, ValueError):
            depth = 0.0
        magnitude = round(min(0.5, max(0.01, depth * 0.05)), 4)
        conf = round(min(1.0, max(0.0, depth / 10.0)), 4)

        rc = str(r["root_cause"] or "")[:120]
        ls = str(r["lesson"] or "")[:120]
        rationale = f"{pk} pnl={pnl:+.4f} {rc} | {ls}"

        existing = conn.execute(
            """SELECT id, sample_size, avg_confidence, evidence_summary, note
               FROM strategy_param_adjustments
               WHERE asset=? AND action=? AND param_key=? AND direction=? AND status='pending'
               ORDER BY id DESC LIMIT 1""",
            (asset, action, pk, sd),
        ).fetchone()
        if existing:
            old_n = int(existing["sample_size"] or 0)
            old_c = float(existing["avg_confidence"] or 0.0)
            # Watermark dedupe: skip if this v2.id was already aggregated
            old_note = str(existing["note"] or "")
            m = re.search(r"v2_wm=(\d+)", old_note)
            last_wm = int(m.group(1)) if m else 0
            if tid and last_wm and tid <= last_wm:
                continue  # 已被上一轮聚合过
            new_n = old_n + 1
            new_c = (old_c * old_n + conf) / new_n if new_n else conf
            new_evi = (str(existing["evidence_summary"] or "") + " | #" + str(did) + ": " + rationale)[-2000:]
            new_note = f"v2_wm={tid}" if tid else old_note
            conn.execute(
                """UPDATE strategy_param_adjustments
                   SET sample_size=?, avg_confidence=?, magnitude=?,
                       evidence_summary=?, note=?,
                       last_suggested_at=datetime('now'),
                       updated_at=datetime('now')
                   WHERE id=?""",
                (new_n, new_c, magnitude, new_evi, new_note, int(existing["id"])),
            )
        else:
            new_note = f"v2_wm={tid}" if tid else ""
            conn.execute(
                """INSERT INTO strategy_param_adjustments
                       (asset, action, param_key, direction, magnitude, sample_size,
                        avg_confidence, evidence_summary, note)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (asset, action, pk, sd, magnitude, conf,
                 f"[#{did}] {rationale}"[:2000], new_note),
            )
        aggregated += 1
    if aggregated:
        conn.commit()
    return aggregated


def _pending_adjustments(conn: sqlite3.Connection,
                        min_samples: int = _V182_ADJUSTMENT_SAMPLE_THRESHOLD,
                        min_conf: float = _V182_ADJUSTMENT_CONFIDENCE_THRESHOLD) -> List[Dict[str, Any]]:
    """列出满足累计阈值的 pending 候选（待人工审批）。"""
    rows = conn.execute(
        """SELECT id, asset, action, param_key, direction, magnitude,
                  sample_size, avg_confidence, evidence_summary,
                  first_suggested_at, last_suggested_at
           FROM strategy_param_adjustments
           WHERE status='pending'
                AND sample_size >= ? AND avg_confidence >= ?
                AND applied_strategy_version_id IS NULL
                AND applied_setting_change = ''
           ORDER BY last_suggested_at DESC""",
        (min_samples, min_conf),
    ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def _approve_adjustment(conn: sqlite3.Connection, adj_id: int,
                       new_strategy_version_id: Optional[int] = None,
                       applied_setting_change: str = "") -> bool:
    """审批通过：status=approved/applied。前提：当前 status='pending'，否则 no-op 返回 False。"""
    try:
        cur_status = conn.execute(
            "SELECT status FROM strategy_param_adjustments WHERE id=?",
            (int(adj_id),),
        ).fetchone()
        if cur_status is None or cur_status["status"] != "pending":
            return False
        if new_strategy_version_id is not None:
            conn.execute(
                """UPDATE strategy_param_adjustments
                   SET status='applied', applied_strategy_version_id=?,
                       applied_setting_change=?, updated_at=datetime('now')
                   WHERE id=? AND status='pending'""",
                (int(new_strategy_version_id), str(applied_setting_change or ""), int(adj_id)),
            )
        else:
            conn.execute(
                """UPDATE strategy_param_adjustments
                   SET status='approved', applied_setting_change=?,
                       updated_at=datetime('now')
                   WHERE id=? AND status='pending'""",
                (str(applied_setting_change or ""), int(adj_id)),
            )
        conn.commit()
        return conn.total_changes > 0
    except Exception:
        conn.rollback()
        return False


def _reject_adjustment(conn: sqlite3.Connection, adj_id: int, note: str = "") -> bool:
    """审批拒绝：status=rejected（不再升级）。"""
    try:
        conn.execute(
            """UPDATE strategy_param_adjustments
               SET status='rejected', note=?, updated_at=datetime('now')
               WHERE id=? AND status='pending'""",
            (str(note or "")[:500], int(adj_id)),
        )
        conn.commit()
        return conn.total_changes > 0
    except Exception:
        conn.rollback()
        return False


async def reflection_worker() -> None:
    """单笔反思后台任务：每 30s 处理一批 pending，每小时做经验评估（review_agent._evaluate_insights）。

    修复 P0：命中验证改用 review_agent._evaluate_insights（双向可恢复 / 阈值配置化 /
    跳过 manual_deactivated）；旧版 reflection._insight_hit_validation 已废弃。
    """
    print("[REFLECT] Reflection worker started")
    last_validation = 0.0
    while True:
        try:
            loop = asyncio.get_running_loop()
            done = await loop.run_in_executor(None, process_once, loop)
            if done:
                print(f"  [REFLECT] 处理 {done} 笔单笔反思", flush=True)
            # 每小时跑一次经验评估（低效停用 + 胜率回升复活 + 误杀自愈）
            now = time.monotonic()
            if now - last_validation >= 3600:
                conn = _open_db()
                try:
                    res = review_agent._evaluate_insights(conn)
                    changes = (
                        len(res.get("deactivated") or [])
                        + len(res.get("reactivated") or [])
                        + len(res.get("rescued_no_sample") or [])
                    )
                    if changes:
                        print(
                            f"  [REFLECT] 经验评估: 停用{len(res.get('deactivated') or [])}/"
                            f"复活{len(res.get('reactivated') or [])}/"
                            f"自愈{len(res.get('rescued_no_sample') or [])}",
                            flush=True,
                        )
                finally:
                    conn.close()
                last_validation = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"  [REFLECT] worker error: {type(exc).__name__}: {str(exc)[:80]}")
        await asyncio.sleep(30)
