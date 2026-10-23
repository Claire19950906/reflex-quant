#!/usr/bin/env python3
"""历史新闻交易回溯 — 按单条新闻维度回溯已结束的模拟交易，AI 归因总结。

数据源：quick_sim_trades（settled=1 且 news_id 非空）JOIN raw_news / ai_decisions。
AI 分析复用 review_agent 的 23 类失败 / 10 类成功框架与 LLM 通道（OpenRouter→MiniMax），
结果缓存到 news_trade_reviews 表（避免重复调用 LLM）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import db
import causal_graph
import review_agent
import risk_metrics
from review_agent import (
    _FAILURE_FRAMEWORK,
    _SUCCESS_FRAMEWORK,
    _REASON_NAMES,
    _SUCCESS_REASON_NAMES,
    _llm,
    _parse_json,
)


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _clean_news(text: Optional[str], max_len: int = 160) -> str:
    return re.sub(r"\[hash:[a-fA-F0-9]+\]\s*", "", text or "")[:max_len]


def _reasoning_gist(reasoning: Optional[str], max_len: int = 90) -> str:
    """从决策 reasoning 尾部抽方向依据（优先「总结」段），供归因判断题材/方向是否判错。"""
    if not reasoning:
        return ""
    s = str(reasoning).strip()
    if not s:
        return ""
    idx = s.rfind("总结")
    tail = s[idx + 2:] if idx >= 0 else s[-160:]
    tail = re.sub(r'["{}\\]', " ", tail)
    tail = " ".join(tail.split()).strip(":： ")
    return tail[:max_len]


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (ValueError, OSError):
        return ""


def _parse_ts(text: Optional[str]) -> Optional[int]:
    """把时间字符串转 unix 秒（支持 ISO 带时区与 'YYYY-MM-DD HH:MM:SS'）。"""
    if not text:
        return None
    s = str(text).strip()
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s[:19], fmt).timestamp())
        except ValueError:
            continue
    return None


def _bucket(t: Dict[str, Any]) -> Optional[str]:
    """成败判定：优先 verdict/is_correct（WIN/LOSS），缺失时按盈亏符号兜底。"""
    c = t.get("is_correct") or t.get("verdict") or ""
    if c in ("WIN", "LOSS"):
        return c
    p = t.get("pnl_pct")
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if p > 0:
        return "WIN"
    if p < 0:
        return "LOSS"
    return None


# v180 P0+：待判定原因推导（统一口径 §5）
# 枚举值见 docs/数据通道统一口径.md §5：
#   no_price_data / extreme_unsettled / reflection_timeout / data_quarantine / model_retry_break
_PENDING_REASON_NONE: str = ""


def _derive_pending_reason(t: Dict[str, Any]) -> str:
    """对 settled=1 但未判定的交易推导 pending_reason。

    顺序（先命中先返回）：
      1. 缺 exit_price → no_price_data（K线缺失，没法判定）
      2. 缺 max_price / min_price → extreme_unsettled（持仓周期内极端点缺失）
      3. verdict/is_correct 空但 pnl_pct 已写入 → model_retry_break（LLM 重试超上限兜底）
      4. verdict/is_correct 空且 pnl_pct 也空 → reflection_timeout（worker 超时未结算）
      5. 已判定（WIN/LOSS）→ 返回空字符串，前端不显示待判定徽章
    """
    if _bucket(t) in ("WIN", "LOSS"):
        return _PENDING_REASON_NONE  # 已判定，前端按 VerdictBadge 渲染
    if not t.get("exit_price"):
        return "no_price_data"
    if not t.get("max_price") or not t.get("min_price"):
        return "extreme_unsettled"
    pnl = t.get("pnl_pct")
    if pnl is not None and str(pnl).strip() != "":
        try:
            if float(pnl) != 0:
                # pnl 写了但 verdict 还是空 — 多见于 LLM 复盘被熔断/超限
                return "model_retry_break"
        except (TypeError, ValueError):
            pass
    return "reflection_timeout"


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def news_trade_list(
    hours: int = 72,
    limit: int = 300,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """已结束交易的新闻列表（按最近结算时间倒序），含每条新闻的交易汇总。

    hours:        回看窗口（小时），0 = 全部（仅当 from_ts/to_ts 都缺省时生效）。
    from_ts/to_ts: 自定义时段（UTC 秒），二者都传时按绝对区间过滤（v180 P0+ 新增）。
                  注意：与 hours 同时存在时，hours 生效（兼容旧调用）。

    v180 P0+ 修订：增加自定义时段，配合前端 PeriodSelector（今日/近7天/近30天/自定义）。
    """
    conn = _connect()
    try:
        where = ""
        params: List[Any] = []
        if hours and hours > 0:
            where = "AND qt.exit_ts >= strftime('%s','now') - ?*3600"
            params.append(int(hours))
        elif from_ts is not None and to_ts is not None and to_ts > from_ts:
            # 自定义时段：from_ts / to_ts 均为 UTC 秒（含两端）；与 hours 互斥
            where = "AND qt.exit_ts >= ? AND qt.exit_ts <= ?"
            params.append(int(from_ts))
            params.append(int(to_ts))
        params.append(min(max(limit, 1), 500))
        rows = conn.execute(
            f"""SELECT rn.id AS news_id, rn.source, rn.content, rn.timestamp AS news_time,
                       qt.asset, qt.action, qt.pnl_pct, qt.verdict, qt.exit_ts,
                       (ntr.summary IS NOT NULL) AS has_review
                FROM quick_sim_trades qt
                JOIN raw_news rn ON rn.id = qt.news_id
                LEFT JOIN news_trade_reviews ntr ON ntr.news_id = rn.id
                WHERE qt.settled = 1 AND qt.news_id IS NOT NULL
                {where}
                ORDER BY qt.exit_ts DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    groups: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        d = dict(row)
        bucket = _bucket(d)
        g = groups.setdefault(d["news_id"], {
            "news_id": d["news_id"],
            "source": d["source"],
            "content": _clean_news(d["content"], 120),
            "news_time": d["news_time"],
            "trade_count": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "latest_exit_ts": 0,
            "has_review": False,
        })
        g["trade_count"] += 1
        if bucket == "WIN":
            g["wins"] += 1
        elif bucket == "LOSS":
            g["losses"] += 1
        if d["pnl_pct"] is not None:
            g["total_pnl"] += float(d["pnl_pct"])
        g["latest_exit_ts"] = max(g["latest_exit_ts"], int(d["exit_ts"] or 0))
        if d.get("has_review"):
            g["has_review"] = True

    out: List[Dict[str, Any]] = []
    for g in groups.values():
        total = g["trade_count"]
        # 胜率分母 = 有明确胜负的样本（WIN+LOSS），HOLD/平局不计入分母
        decided = g["wins"] + g["losses"]
        out.append({
            "news_id": g["news_id"],
            "source": g["source"],
            "content": g["content"],
            "news_time": g["news_time"],
            "trade_count": total,
            "wins": g["wins"],
            "losses": g["losses"],
            "winrate": round(g["wins"] / decided, 4) if decided else None,
            "total_pnl": round(g["total_pnl"], 4),
            "latest_exit_ts": g["latest_exit_ts"],
            "has_review": g["has_review"],
        })
    out.sort(key=lambda x: x["latest_exit_ts"], reverse=True)
    return out


def news_trade_detail(news_id: int) -> Optional[Dict[str, Any]]:
    """单条新闻的交易详情（新闻原文 + 交易明细 + 聚合 + 已缓存的 AI 总结）。"""
    conn = _connect()
    try:
        news = conn.execute(
            "SELECT id, source, content, timestamp, gate_core_impact, "
            "content_fingerprint, first_seen_ts, cross_net_source, net_origin_info "
            "FROM raw_news WHERE id = ?",
            (news_id,),
        ).fetchone()
        if not news:
            return None
        rows = conn.execute(
            """SELECT qt.id, qt.decision_id, qt.asset, qt.action, qt.score,
                      qt.evidence_confidence, qt.gate_passed, qt.gate_reason,
                      qt.notional_usdt, qt.entry_price, qt.entry_ts, qt.horizon_minutes,
                      qt.exit_price, qt.max_price, qt.min_price, qt.exit_ts, qt.pnl_pct, qt.pnl_usdt,
                      qt.verdict AS is_correct, qt.verdict,
                      ad.reasoning, ad.decision_context,
                      ad.expected_horizon, ad.event_strength, ad.event_phase,
                      ad.prediction_type, ad.sentiment_score
               FROM quick_sim_trades qt
               JOIN ai_decisions ad ON ad.id = qt.decision_id
               WHERE qt.news_id = ? AND qt.settled = 1
               ORDER BY qt.entry_ts DESC""",
            (news_id,),
        ).fetchall()
        cached = conn.execute(
            "SELECT summary, generated_at FROM news_trade_reviews WHERE news_id = ?",
            (news_id,),
        ).fetchone()
    finally:
        conn.close()

    trades = [dict(r) for r in rows]
    news_ts = _parse_ts(news["timestamp"])
    core_imp = str(news["gate_core_impact"] or "")
    for t in trades:
        t["core_impact"] = core_imp
        # 新闻发布 → 入场的间隔（分钟），回溯入场时机用
        if news_ts and t.get("entry_ts"):
            t["interval_minutes"] = max(0, int((int(t["entry_ts"]) - news_ts) / 60))
        else:
            t["interval_minutes"] = None
        # v180 P0+：每笔交易附带 pending_reason（统一口径 §5 判定状态）
        # 判定状态：WIN/LOSS → 已判定；BREAKEVEN/None → 待判定
        # 取自 quick_sim_trades 表 verdict 列；settled=1 但 verdict 空视为「判定超时未写入」
        t["pending_reason"] = _derive_pending_reason(t)
    decided = [t for t in trades if _bucket(t) in ("WIN", "LOSS")]
    wins = sum(1 for t in decided if _bucket(t) == "WIN")
    total_pnl = sum(float(t["pnl_pct"] or 0.0) for t in trades)
    review = None
    generated_at = None
    if cached:
        try:
            review = json.loads(cached["summary"])
            generated_at = cached["generated_at"]
        except (json.JSONDecodeError, TypeError):
            review = None
    # v176 — 注入风险指标（drawdown / sharpe / winrate via risk_metrics）
    pnl_series = [t.get("pnl_pct") for t in trades if t.get("pnl_pct") is not None]
    risk = risk_metrics.compute_portfolio_metrics(pnl_series)
    # v188 P0+：跨源溯源（同 fingerprint 的全网首次采集时间序列）。
    # 用户在 /review 点 source 名字时展开。空 fingerprint 时给空壳（前端正常渲染）。
    cross_trace = _cross_source_trace(
        fingerprint=str(news["content_fingerprint"] or ""),
        current_id=int(news["id"]),
        current_first_seen_ts=str(news["first_seen_ts"] or ""),
        current_source=str(news["source"] or ""),
        primary_source=str(news["cross_net_source"] or ""),
        primary_origin_info=str(news["net_origin_info"] or ""),
    )
    return {
        "news_id": news["id"],
        "source": news["source"],
        "content": news["content"],
        "news_time": news["timestamp"],
        "trades": trades,
        "aggregate": {
            "total": len(trades),
            "decided": len(decided),
            "wins": wins,
            "losses": len(decided) - wins,
            "winrate": round(wins / len(decided), 4) if decided else None,
            "total_pnl": round(total_pnl, 4),
            # v176 新增：drawdown / sharpe
            "max_drawdown": risk["max_drawdown"],
            "current_drawdown": risk["current_drawdown"],
            "sharpe": risk["sharpe"],
        },
        "review": review,
        "review_generated_at": generated_at,
        # v188 P0+：跨源溯源（默认收起；前端点 source 名时展开）
        "cross_trace": cross_trace,
    }


def _cross_source_trace(
    fingerprint: str,
    current_id: int,
    current_first_seen_ts: str,
    current_source: str,
    primary_source: str,
    primary_origin_info: str,
) -> Dict[str, Any]:
    """按 content_fingerprint 抓同事实的所有源，按 first_seen_ts ASC 排序。

    返回：
      {
        "cluster_size": int,
        "sources": [
          { "id", "source", "first_seen_ts", "delay_seconds", "is_earliest", "is_current", "snippet" }
        ],
        "primary_source": str | null,          # LLM 推断的一手源
        "primary_origin_info": str | null      # LLM 自然语言溯源解释
      }

    fingerprint 为空（极旧新闻未跑过指纹）→ 返回退化壳（仅当前这条）。
    """
    if not fingerprint:
        return {
            "cluster_size": 1,
            "sources": [
                {
                    "id": current_id,
                    "source": current_source,
                    "first_seen_ts": current_first_seen_ts,
                    "delay_seconds": 0,
                    "is_earliest": True,
                    "is_current": True,
                    "snippet": "",
                }
            ],
            "primary_source": primary_source or None,
            "primary_origin_info": primary_origin_info or None,
        }

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, source, first_seen_ts, substr(content, 1, 80) AS snippet "
            "FROM raw_news WHERE content_fingerprint = ? "
            "ORDER BY first_seen_ts ASC, id ASC",
            (fingerprint,),
        ).fetchall()
    finally:
        conn.close()

    rows = [dict(r) for r in rows]
    if not rows:
        return {
            "cluster_size": 0,
            "sources": [],
            "primary_source": primary_source or None,
            "primary_origin_info": primary_origin_info or None,
        }

    base_ts = _parse_ts(rows[0]["first_seen_ts"]) or 0
    for r in rows:
        ts = _parse_ts(r["first_seen_ts"]) or 0
        r["delay_seconds"] = max(0, ts - base_ts)
        r["is_earliest"] = (r["id"] == rows[0]["id"])
        r["is_current"] = (r["id"] == current_id)
        r["snippet"] = str(r.get("snippet") or "")

    return {
        "cluster_size": len(rows),
        "sources": rows,
        "primary_source": primary_source or None,
        "primary_origin_info": primary_origin_info or None,
    }


# ---------------------------------------------------------------------------
# v180 P0+：重新判定（rejudge）
# ---------------------------------------------------------------------------

def rejudge_pending(news_id: int) -> Dict[str, Any]:
    """对该新闻下所有「卡住」的交易重新结算（统一口径 §5）。

    范围：
      - 状态：settled=1 且 _derive_pending_reason(t) != ""（即 WIN/LOSS 之外的）
      - 价格来源：复用 settle_trades 的 price_fn（如果行情源没准备好直接放弃）

    返回：
      {
        "news_id": int,
        "retried": int,        # 尝试重新结算的交易数
        "updated": [...],      # 成功重新判定的 [{trade_id, old_verdict, new_verdict, pnl_pct, reason}]
        "still_pending": [...], # 重试后仍未判定的 [{trade_id, reason}]（K线仍缺失等）
      }
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT id, decision_id, asset, action, entry_price, entry_ts,
                      horizon_minutes, notional_usdt, max_price, min_price,
                      exit_price, exit_ts, pnl_pct, pnl_usdt, verdict
               FROM quick_sim_trades
               WHERE news_id = ? AND settled = 1""",
            (news_id,),
        ).fetchall()
    finally:
        conn.close()

    # 优先用 quick_sim 现有的 _quick_sim_price 价源（多源缓存 + 引擎回退）
    # 避免重新拼装，保证和 settle_trades 行为一致。
    try:
        from api_server import _quick_sim_price  # noqa: WPS433  复用既有价源
        price_fn = _quick_sim_price
    except Exception:
        # 没有 _quick_sim_price 时直接放弃（用户重试后能看到 still_pending）
        price_fn = lambda _asset: None  # noqa: E731

    updated: List[Dict[str, Any]] = []
    still_pending: List[Dict[str, Any]] = []
    for row in rows:
        t = dict(row)
        if _bucket(t) in ("WIN", "LOSS"):
            continue  # 已判定，不动
        old_verdict = t.get("verdict") or "BREAKEVEN"
        asset = t["asset"]
        price = price_fn(asset)
        if not isinstance(price, (int, float)) or price <= 0:
            still_pending.append({
                "trade_id": t["id"],
                "reason": _derive_pending_reason(t) or "no_price_data",
            })
            continue
        entry = float(t["entry_price"])
        exit_price = float(price)
        pnl_pct = (exit_price - entry) / entry * 100
        if t["action"] == "SELL":
            pnl_pct = -pnl_pct
        pnl_usdt = float(t["notional_usdt"]) * pnl_pct / 100
        new_verdict = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "BREAKEVEN"
        # 写回 quick_sim_trades + ai_decisions
        conn = _connect()
        try:
            conn.execute(
                """UPDATE quick_sim_trades
                   SET exit_price=?, exit_ts=?, pnl_pct=?, pnl_usdt=?, verdict=?
                   WHERE id=?""",
                (round(exit_price, 6), int(time.time()),
                 round(pnl_pct, 6), round(pnl_usdt, 6), new_verdict, t["id"]),
            )
            if t.get("decision_id") is not None:
                conn.execute(
                    """UPDATE ai_decisions
                       SET is_correct = COALESCE(NULLIF(is_correct, ''), ?),
                           settled = 1,
                           forward_pnl = ?,
                           exit_price = COALESCE(exit_price, ?)
                       WHERE id = ?""",
                    (new_verdict, round(pnl_pct, 4), round(exit_price, 6),
                     t["decision_id"]),
                )
                # v20251014: 闭环校准 source_stats (按 source × asset 聚合 hit_rate).
                # 用 signal_weight_evaluator.update_source_stats_from_settlement() 增量更新.
                try:
                    import signal_weight_evaluator as _swe_hr
                    _src_row = conn.execute(
                        """SELECT rn.source, ad.target_asset
                           FROM ai_decisions ad
                           LEFT JOIN raw_news rn ON rn.id = ad.news_id
                           WHERE ad.id = ?""",
                        (t["decision_id"],),
                    ).fetchone()
                    if _src_row:
                        _src_dict = dict(_src_row) if hasattr(_src_row, "keys") else {
                            "source": _src_row[0], "target_asset": _src_row[1]
                        }
                        _swe_hr.update_source_stats_from_settlement(
                            conn,
                            news_source=_src_dict.get("source", "") or "",
                            asset=_src_dict.get("target_asset", "") or "",
                            forward_pnl=pnl_pct,
                            is_correct=new_verdict,
                        )
                except Exception as _swe_hr_exc:  # noqa: BLE001
                    print(f"[history_review] source_stats update id={t['decision_id']} failed: {_swe_hr_exc}", flush=True)
            conn.commit()
        finally:
            conn.close()
        updated.append({
            "trade_id": t["id"],
            "decision_id": t.get("decision_id"),
            "asset": asset,
            "old_verdict": old_verdict,
            "new_verdict": new_verdict,
            "pnl_pct": round(pnl_pct, 4),
        })
    return {
        "news_id": news_id,
        "retried": len(rows) - sum(1 for _ in rows if _bucket(dict(_)) in ("WIN", "LOSS")),
        "updated": updated,
        "still_pending": still_pending,
    }


def count_pending_news(hours: Optional[int] = None,
                       from_ts: Optional[int] = None,
                       to_ts: Optional[int] = None) -> Dict[str, Any]:
    """统计窗口内有「待判定」交易的新闻数与交易数（不修改数据）。

    返回：
      {
        "window": {hours?, from_ts?, to_ts?},
        "news_with_pending": int,    # 有至少 1 笔 pending 交易的 news_id 数
        "pending_trades": int,       # pending 交易总数
        "pending_by_reason": {reason: n, ...}
      }
    """
    conn = _connect()
    try:
        if hours is not None:
            where = "AND qt.exit_ts >= strftime('%s','now') - ?*3600"
            params: tuple = (int(hours),)
        elif from_ts is not None and to_ts is not None:
            where = "AND qt.exit_ts >= ? AND qt.exit_ts <= ?"
            params = (int(from_ts), int(to_ts))
        else:
            where = ""
            params = ()
        rows = conn.execute(
            f"""SELECT qt.id, qt.news_id, qt.verdict, qt.exit_price, qt.max_price, qt.min_price
                FROM quick_sim_trades qt
                WHERE qt.settled = 1 {where}""",
            params,
        ).fetchall()
    finally:
        conn.close()
    pending_ids: set = set()
    pending_trades = 0
    pending_by_reason: Dict[str, int] = {}
    for r in rows:
        t = dict(r)
        reason = _derive_pending_reason(t)
        if reason:
            pending_trades += 1
            pending_ids.add(t["news_id"])
            pending_by_reason[reason] = pending_by_reason.get(reason, 0) + 1
    return {
        "window": {"hours": hours, "from_ts": from_ts, "to_ts": to_ts},
        "news_with_pending": len(pending_ids),
        "pending_trades": pending_trades,
        "pending_by_reason": pending_by_reason,
    }


def rejudge_pending_batch(hours: Optional[int] = None,
                          from_ts: Optional[int] = None,
                          to_ts: Optional[int] = None,
                          limit: int = 200) -> Dict[str, Any]:
    """批量重新结算窗口内所有「卡住」交易（统一口径 §5）。

    取窗口内所有 pending 的 news_id（去重），逐个调用 rejudge_pending。
    limit 控制最多处理多少条新闻（防止全表重算）。
    """
    conn = _connect()
    try:
        if hours is not None:
            where = "AND qt.exit_ts >= strftime('%s','now') - ?*3600"
            params: tuple = (int(hours),)
        elif from_ts is not None and to_ts is not None:
            where = "AND qt.exit_ts >= ? AND qt.exit_ts <= ?"
            params = (int(from_ts), int(to_ts))
        else:
            where = ""
            params = ()
        news_ids = [r[0] for r in conn.execute(
            f"""SELECT DISTINCT qt.news_id
                FROM quick_sim_trades qt
                WHERE qt.settled = 1 {where}
                ORDER BY qt.news_id DESC LIMIT ?""",
            (*params, int(limit)),
        ).fetchall()]
    finally:
        conn.close()

    totals = {"news": 0, "retried": 0, "updated": 0, "still_pending": 0}
    per_news: List[Dict[str, Any]] = []
    for nid in news_ids:
        try:
            r = rejudge_pending(nid)
        except Exception as e:  # 单条失败不影响整体
            per_news.append({"news_id": nid, "error": str(e)})
            continue
        totals["news"] += 1
        totals["retried"] += int(r.get("retried") or 0)
        totals["updated"] += len(r.get("updated") or [])
        totals["still_pending"] += len(r.get("still_pending") or [])
        per_news.append({
            "news_id": nid,
            "retried": r.get("retried"),
            "updated": r.get("updated"),
            "still_pending": r.get("still_pending"),
        })
    return {
        "window": {"hours": hours, "from_ts": from_ts, "to_ts": to_ts, "limit": limit},
        "summary": totals,
        "news": per_news,
    }


# ---------------------------------------------------------------------------
# AI 归因分析
# ---------------------------------------------------------------------------

def _analyze_trades_llm(trades: List[Dict[str, Any]], framework: str,
                        names: Dict[int, str], kind: str) -> List[Dict[str, Any]]:
    """对一批交易按框架归因，返回 [{decision_id, reason_code, reason_name, reason_text, confidence}]。

    确定性优先：失败归因（kind=='失败'）先跑 review_agent._rule_based_failure
    命中硬条件（如数据缺失）直接产出，剩余才送 LLM。
    """
    if not trades:
        return []
    out: List[Dict[str, Any]] = []
    llm_needed: List[Dict[str, Any]] = []
    if kind == "失败":
        for t in trades:
            hard = review_agent._rule_based_failure(t)
            if hard is not None:
                code = int(hard["reason_code"])
                out.append({
                    "decision_id": t["decision_id"],
                    "reason_code": code,
                    "reason_name": names.get(code, "其他"),
                    "reason_text": hard["reason_text"],
                    "confidence": float(hard["confidence"]),
                    "observability": review_agent.reason_observability(code),
                    "layer": hard.get("layer") or review_agent.reason_layer(code),
                    "sublayer": hard.get("sublayer") or "",
                    "reasons": [{
                        "code": code,
                        "layer": hard.get("layer") or review_agent.reason_layer(code),
                        "sublayer": hard.get("sublayer") or "",
                        "text": hard["reason_text"],
                        "signal": hard.get("signal") or "",
                        "evidence": hard.get("evidence") or "",
                        "confidence": float(hard["confidence"]),
                        "is_primary": True,
                    }],
                })
            else:
                llm_needed.append(t)
    else:
        llm_needed = list(trades)
    if not llm_needed:
        return out
    lines = []
    for t in llm_needed[:15]:
        asset = str(t.get("asset") or "")
        ctx_s = review_agent._ctx_evidence(t.get("decision_context"), asset)
        entry = _fmt_ts(t.get("entry_ts"))
        iv = t.get("interval_minutes")
        iv_s = f" 新闻后{iv}分钟入场" if iv is not None else ""
        news = _clean_news(t.get("content"), 80) if t.get("content") else ""
        gist = _reasoning_gist(t.get("reasoning"))
        gist_s = f" 方向依据:{gist}" if gist else ""
        fact_s = review_agent._trade_fact(t)
        pnl = float(t.get("pnl_pct") or 0.0)
        core = _clean_news(t.get("core_impact"), 40) if t.get("core_impact") else ""
        core_s = f" | 筋:{core}" if core else ""
        cf_s = review_agent.counterfactual_evidence(t)
        cf_s = f" | {cf_s}" if cf_s else ""
        lines.append(
            f"#{t['decision_id']} | {asset} {t.get('action')} | 判定{t.get('is_correct')} | "
            f"盈亏{pnl:+.2f}% | 入场{entry}{iv_s} | 快照:{ctx_s} | 结果:{fact_s} | 新闻:{news}{gist_s}{core_s}{cf_s}"
        )
    # 图谱取证：把本批交易涉及资产的「涨跌本质」作为归因上下文注入。
    # 反思/归因系统作为 causal_graph 的平级消费者，独立取证；无知识/异常时静默跳过。
    graph_blocks: List[str] = []
    try:
        for a in sorted({str(t.get("asset") or "").strip().upper() for t in trades}):
            if a in ("", "NONE"):
                continue
            blk = causal_graph.inject_block(a)
            if blk and blk not in graph_blocks:
                graph_blocks.append(blk)
    except Exception:
        graph_blocks = []
    graph_hint = ("\n\n" + "\n".join(graph_blocks)) if graph_blocks else ""

    # 假设核验闭环：用本批交易产生的"事后事实"核验已有假设节点。
    # 逻辑：刚发生完一批反思，AI 的"交易结果"就是事实，用它来反向核验图谱中的假设。
    # 这样可以：
    #   - 把"暂时没被推理到的节点"留下（如果事实也不涉及，不算被证伪）
    #   - 把"被事实反驳的假设"及时标记为 CONTRADICTED（active=0）
    #   - 让"被事实支持的假设"上调置信度
    # 失败静默（核验失败不影响主流程）。
    try:
        assets_in_batch = sorted({
            str(t.get("asset") or "").strip().upper()
            for t in trades
            if str(t.get("asset") or "").strip().upper() not in ("", "NONE")
        })
        for a in assets_in_batch:
            causal_graph.verify_all_pending(a)
    except Exception:
        pass

    # 生效经验回灌（reflection 路径）：把 trade dict 上的 _active_insights 注入 prompt，
    # 关闭"反思 → 沉淀 → 新反思回查"的闭环。其他调用方（如新闻复盘）没塞该字段就跳过。
    insight_hint = ""
    try:
        if llm_needed:
            insights = llm_needed[0].get("_active_insights") or []
            # 按当前笔过滤同品种失败经验，过宽会让 prompt 变长且 LLM 注意力分散；
            # 但完全过滤又会丢失跨品种教训，所以取当前笔 asset + 通用的 ≤30 条。
            if insights:
                cur_asset = str(llm_needed[0].get("asset") or "").strip().upper()
                # 当前品种优先 + 其它兜底
                same = [x for x in insights if cur_asset and cur_asset in str(x.get("rule") or "")]
                other = [x for x in insights if x not in same]
                picked = (same + other)[:30]
                lines = [f"- #{x['id']}: {x['rule']}" for x in picked]
                insight_hint = (
                    "\n\n═════ 当前生效经验（同类失败已有规则；如本笔正在重复同一坑，"
                    "用 code 23「经验误用/过拟合」并指明 #id）═════\n"
                    + "\n".join(lines) + "\n"
                )
    except Exception:
        insight_hint = ""

    attribution_hint = ""
    if kind == "失败":
        attribution_hint = (
            "判定失败原因时用事实反证，先分「方向/题材错」还是「时机/止盈错」：\n"
            "① 看「结果」：交易后从未明显浮盈（MFE≈0 或入场后价格直接反向）→ 方向/题材一开始就错 → 归 6 或 7；"
            "曾明显浮盈后回吐（MFE 明显为正而最终亏）→ 方向对、时机/入场点/止盈问题 → 归 14 或 16。\n"
            "② 看「快照」：决策时技术面/趋势数据是否本身与该方向矛盾（矛盾 → 方向错）。\n"
            "③ 事后统计/评估/非即时催化却被当成看涨/看跌催化 → 归 6 或 7。不要因「入场时间距新闻近」就机械归 16。\n"
            "④ 新闻主体品种与交易品种错配（新闻只涉及美股/个股/科技股，却交易 BTC/ETH 等加密或商品）→ 影响力不足/题材误判 → 归 7，绝不归 16。\n"
            "⑤ 门卫结论仅作文本线索：若门卫曾判「可交易」且强度高、或门卫未挖到核心影响而结果实亏，结合快照/结果综合判断是否信号质量差 → 归 7 或 13，不要机械套用。\n"
            "⑥ 看「反共识/因果链」：推演时 [反共识结论][因果链分析] 段写了哪些对冲/联动因果；"
            "若实际翻车的那条因果链推演时根本没写进反共识段 → 归 13（漏了对冲/联动）；"
            "若写了对冲链但机理/方向本身推错 → 归 6（机理错）。两者按反共识段内容区分，不要都堆到 6。\n"
        )
    tendon_hint = ""
    if any(t.get("core_impact") for t in llm_needed):
        tendon_hint = (
            "先回到每笔交易当天门卫挖的那根筋（筋:字段），回答三问再套框架给 code：\n"
            "① 当天的筋挖对了吗？（说动了信心，实际动的可能是流动性/供需/别的）\n"
            "② 筋对了，「谁会掏真钱」兑现了吗？（真钱流入多为 WIN 的根，只喊没钱=假信号）\n"
            "③ 筋和钱都对，那错在方向/力度/时机哪一步？\n"
            "三问的答案即主因，code 只是它的归类标签。\n"
        )
    param_keys_list = ", ".join(review_agent.v182_param_keys())
    prompt = (
        framework
        + f"\n{attribution_hint}{tendon_hint}{graph_hint}{insight_hint}对以下每条{kind}交易，给出 1-3 个原因（一个 is_primary=true 为主因，其余为次要原因），输出 JSON 数组：\n"
        + '[{"decision_id": <id>, "reasons": [{"code": <code>, "sublayer": "<l1 或 l1:l2，从下面枚举选>", "text": "<=40字", "confidence": 0-1, "is_primary": bool}, ...], '
        + '"improvement_suggestion": <object|null>}, ...]\n'
        + "每条必须有 decision_id（纯数字整数，不要加 # 前缀）和至少一个原因。sublayer 必须从下方枚举中选，格式 l1（如 \"feed_unavailable\"）或 l1:l2（如 \"feed_unavailable:mt5_disconnect\"）。\n\n"
        + review_agent._build_sublayer_prompt_block()
        + "\n"
        + review_agent._IMPROVEMENT_PROMPT_BLOCK
        + "\nPARAM_KEYS 白名单（共 " + str(len(review_agent.v182_param_keys())) + " 个，限从中选）：\n"
        + param_keys_list
        + "\n\n"
        + "\n".join(lines)
    )
    result = _parse_json(
        _llm(prompt, f"你是量化交易{kind}归因分析师，只输出 JSON 数组。", max_tokens=1800)
    )
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return out
    for item in result:
        if isinstance(item, dict) and "decision_id" in item:
            item.setdefault("reason_code", 6)
            item.setdefault("reason_text", "")
            norm = review_agent.normalize_reasons(
                item, names=names, with_observability=(kind == "失败"),
                with_layer=(kind != "成功"),
            )
            # v182 改进建议：解析 improvement_suggestion（强校验后挂到归因 dict 上）
            imp_raw = item.get("improvement_suggestion")
            if imp_raw is not None:
                norm["improvement_suggestion"] = review_agent._validate_improvement_suggestion(imp_raw)
                pp = norm["improvement_suggestion"].get("primary_param") or {}
                if pp:
                    norm["suggestion_param_key"] = str(pp.get("key") or "")
                    norm["suggestion_direction"] = str(pp.get("direction") or "")
                    try:
                        norm["suggestion_magnitude"] = float(pp.get("magnitude") or 0.0) or None
                    except (TypeError, ValueError):
                        norm["suggestion_magnitude"] = None
                    try:
                        norm["suggestion_confidence"] = float(pp.get("confidence") or 0.0) or None
                    except (TypeError, ValueError):
                        norm["suggestion_confidence"] = None
                    norm["suggestion_rationale"] = str(pp.get("rationale") or "")
            out.append(norm)
    return out


def _analyze_holds_llm(holds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """HOLD 样本归因：复用 8/22/12（中间态样本不设专属码），给 code 8/22 提供真实入口。

    确定性优先：code 2（非活跃时段）由规则自动判定；剩余交 LLM 在 {8,12,22} 里选。
    HOLD 语义是「新闻未驱动行情」——要么影响周期长、2h 窗口太短没兑现（8），
    要么旧闻/已定价（12/22），要么根本没人交易（2，规则已处理）。
    """
    if not holds:
        return []
    names = review_agent._HOLD_REASON_CODES
    out: List[Dict[str, Any]] = []
    llm_needed: List[Dict[str, Any]] = []
    for t in holds:
        hard = review_agent._rule_based_hold(t)
        if hard is not None:
            code = int(hard["reason_code"])
            out.append({
                "decision_id": t.get("decision_id", t.get("id")),
                "reason_code": code,
                "reason_name": names.get(code, "其他"),
                "reason_text": hard["reason_text"],
                "confidence": float(hard["confidence"]),
                "layer": hard.get("layer") or review_agent.reason_layer(code),
                "sublayer": hard.get("sublayer") or "",
                "reasons": [{
                    "code": code,
                    "layer": hard.get("layer") or review_agent.reason_layer(code),
                    "sublayer": hard.get("sublayer") or "",
                    "text": hard["reason_text"],
                    "signal": hard.get("signal") or "",
                    "evidence": hard.get("evidence") or "",
                    "confidence": float(hard["confidence"]),
                    "is_primary": True,
                }],
            })
        else:
            llm_needed.append(t)
    if not llm_needed:
        return out

    lines = []
    for t in llm_needed[:15]:
        asset = str(t.get("asset") or "")
        entry_ts = t.get("entry_ts")
        entry = _fmt_ts(entry_ts) if entry_ts is not None else (str(t.get("entry_time") or "")[:16])
        iv = t.get("interval_minutes")
        iv_s = f" 新闻后{iv}分钟入场" if iv is not None else ""
        news = _clean_news(t.get("content"), 80) if t.get("content") else ""
        fact_s = review_agent._trade_fact(t)
        pnl = float(t.get("pnl_pct") or 0.0)
        core = _clean_news(t.get("core_impact"), 40) if t.get("core_impact") else ""
        core_s = f" | 筋:{core}" if core else ""
        ctx_s = review_agent._ctx_evidence(t.get("decision_context"), asset)
        lines.append(
            f"#{t.get('decision_id', t.get('id'))} | {asset} {t.get('action')} | 判定HOLD | "
            f"盈亏{pnl:+.2f}% | 入场{entry}{iv_s} | 快照:{ctx_s} | 结果:{fact_s} | 新闻:{news}{core_s}"
        )

    hint = (
        "这是一批「HOLD」单（2h 结算窗口内价格未突破阈值，既没判 WIN 也没判 LOSS）。"
        "HOLD 只在三种情形下发生，请按事实选主因（code 只能是 8/12/22）：\n"
        "  8 时间窗口不匹配：新闻影响周期长（如 1-3d/周级别），2h 内还没兑现，属于窗口截断；\n"
        "  12 新闻重复/旧闻：该消息是旧闻重发或同源重复，行情早已定价，不会再有新驱动；\n"
        "  22 新闻已定价/抢跑：消息发布前市场已预先反应（抢跑/已计入价格），发布后无新增方向。\n"
        "若入场时段本身是非活跃时段，规则已自动归 code 2，不会送到这里。\n"
    )
    param_keys_list = ", ".join(review_agent.v182_param_keys())
    prompt = (
        f"{hint}对以下每条 HOLD 交易给 1-2 个原因（一个 is_primary=true 为主因），输出 JSON 数组：\n"
        + '[{"decision_id": <id>, "reasons": [{"code": <8/12/22>, "sublayer": "<l1 或 l1:l2，从下面枚举选>", "text": "<=40字", "confidence": 0-1, "is_primary": bool}, ...], '
        + '"improvement_suggestion": <object|null>}, ...]\n'
        + "每条必须有 decision_id（纯数字整数）和至少一个原因，code 限 8/12/22。sublayer 必须从下方枚举中选。\n\n"
        + review_agent._build_sublayer_prompt_block()
        + "\n"
        + review_agent._IMPROVEMENT_PROMPT_BLOCK
        + "\nPARAM_KEYS 白名单（共 " + str(len(review_agent.v182_param_keys())) + " 个，限从中选）：\n"
        + param_keys_list
        + "\n\n"
        + "\n".join(lines)
    )
    result = _parse_json(
        _llm(prompt, "你是量化交易 HOLD 单归因分析师，只输出 JSON 数组。", max_tokens=1400)
    )
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return out
    for item in result:
        if isinstance(item, dict) and "decision_id" in item:
            # 强制收敛到 8/12/22（LLM 偶发输出 2 或越界码）
            rc = None
            try:
                rc = int(item.get("reason_code"))
            except (TypeError, ValueError):
                rc = None
            if rc not in names:
                primary_code = None
                for rr in (item.get("reasons") or []):
                    if isinstance(rr, dict):
                        try:
                            c = int(rr.get("code", rr.get("reason_code")))
                        except (TypeError, ValueError):
                            continue
                        if c in names:
                            primary_code = c
                            break
                rc = primary_code or 8
            item["reason_code"] = rc
            item.setdefault("reason_text", "")
            norm = review_agent.normalize_reasons(item, names=names)
            # v182 improvement（holds 通常无，但接口统一保留）
            imp_raw = item.get("improvement_suggestion")
            if imp_raw is not None:
                norm["improvement_suggestion"] = review_agent._validate_improvement_suggestion(imp_raw)
                pp = norm["improvement_suggestion"].get("primary_param") or {}
                if pp:
                    norm["suggestion_param_key"] = str(pp.get("key") or "")
                    norm["suggestion_direction"] = str(pp.get("direction") or "")
                    try:
                        norm["suggestion_magnitude"] = float(pp.get("magnitude") or 0.0) or None
                    except (TypeError, ValueError):
                        norm["suggestion_magnitude"] = None
                    try:
                        norm["suggestion_confidence"] = float(pp.get("confidence") or 0.0) or None
                    except (TypeError, ValueError):
                        norm["suggestion_confidence"] = None
                    norm["suggestion_rationale"] = str(pp.get("rationale") or "")
            out.append(norm)
    return out


def _summary_llm(detail: Dict[str, Any], failures: List[Dict[str, Any]],
                 successes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """LLM 生成单条新闻的整体总结（结论 + 核心要点 + 展开分析 + 经验校验）。"""
    agg = detail["aggregate"]
    news = _clean_news(detail.get("content"), 200)
    winrate = (agg.get("winrate") or 0.0) * 100
    fail_lines = [
        f"#{a['decision_id']} {a['reason_name']}: {a['reason_text']}" for a in failures[:8]
    ]
    succ_lines = [
        f"#{a['decision_id']} {a['reason_name']}: {a['reason_text']}" for a in successes[:8]
    ]
    # 当前生效经验（回灌中的规则），让 AI 校验本次交易是否违反/符合
    try:
        insights = review_agent.load_active_insights(limit=8, min_samples=5)
        insights_s = "；".join(insights) if insights else "（暂无生效经验）"
    except Exception:
        insights_s = "（经验加载失败）"
    # 交易明细行（含入场间隔与事件阶段，帮助回溯时机）
    trade_lines = []
    for t in detail["trades"][:10]:
        iv = t.get("interval_minutes")
        iv_s = f"{iv}分钟" if iv is not None else "-"
        trade_lines.append(
            f"#{t['decision_id']} {t['asset']} {t['action']} 盈亏{float(t.get('pnl_pct') or 0.0):+.2f}% "
            f"判定{t.get('is_correct')} 新闻后{iv_s}入场 阶段{t.get('event_phase') or '-'} 周期{t.get('expected_horizon') or '-'}"
        )
    prompt = f"""新闻（{detail.get('source')} {detail.get('news_time')}）：{news}

该新闻关联的模拟交易 {agg['total']} 笔，已判定 {agg['decided']} 笔（胜 {agg['wins']} / 负 {agg['losses']}），
胜率 {winrate:.0f}%，总盈亏 {agg['total_pnl']:+.2f}%。

交易明细：
{'；'.join(trade_lines) if trade_lines else '无'}

失败原因归类：{'；'.join(fail_lines) if fail_lines else '无'}
成功原因归类：{'；'.join(succ_lines) if succ_lines else '无'}

当前生效经验（AI 分析正在参考的规则）：{insights_s}

请输出 JSON：
{{
  "verdict": "这条新闻驱动的交易整体是成功还是失败，一句话结论（<=30字）",
  "key_insight": "核心教训/可复制的要点（<=60字）",
  "detail": "结合新闻内容、入场时机与归因结果展开分析（<=200字，说明成功/失败的主要原因与改进方向）",
  "rule_check": "对照生效经验，本次交易是否符合或违反了哪些经验（<=80字；无明显关联则写'无明显关联'）"
}}
只输出 JSON 对象。"""
    result = _parse_json(
        _llm(prompt, "你是量化交易复盘分析师，只输出 JSON 对象。", max_tokens=1100)
    )
    if not isinstance(result, dict):
        return {"verdict": "", "key_insight": "", "detail": "AI 分析失败，请稍后重试。", "rule_check": ""}
    return {
        "verdict": str(result.get("verdict") or "")[:40],
        "key_insight": str(result.get("key_insight") or "")[:80],
        "detail": str(result.get("detail") or "")[:400],
        "rule_check": str(result.get("rule_check") or "")[:100],
    }


def run_news_review(news_id: int) -> Dict[str, Any]:
    """AI 归因总结单条新闻的交易（失败→23 类框架 / 成功→10 类框架 + 整体总结），结果缓存。"""
    detail = news_trade_detail(news_id)
    if not detail:
        return {"ok": False, "error": "news not found"}
    if not detail["trades"]:
        return {"ok": False, "error": "该新闻没有已结束的模拟交易"}

    wins = [t for t in detail["trades"] if _bucket(t) == "WIN"]
    losses = [t for t in detail["trades"] if _bucket(t) == "LOSS"]

    # 归因需要新闻原文上下文（交易行本身不含 content）
    for t in detail["trades"]:
        t["content"] = detail["content"]

    failure_analysis = _analyze_trades_llm(
        losses, _FAILURE_FRAMEWORK, _REASON_NAMES, "失败"
    )
    success_analysis = _analyze_trades_llm(
        wins, _SUCCESS_FRAMEWORK, _SUCCESS_REASON_NAMES, "成功"
    )
    summary = _summary_llm(detail, failure_analysis, success_analysis)
    # LLM 通道不可用时归因/总结均为空占位——不写缓存，避免把“分析失败”固化为缓存
    # （否则下次访问会读到占位而不重试，也无法重算）。让调用方拿到 error 以便重试。
    if not failure_analysis and not success_analysis and not (summary.get("verdict") or "").strip():
        return {"ok": False, "error": "AI 归因失败（LLM 通道不可用），请稍后重试"}
    summary["failures"] = failure_analysis
    summary["successes"] = success_analysis
    summary["aggregate"] = detail["aggregate"]

    conn = _connect()
    try:
        # 写库撞锁重试（引擎/API 并发写库时，busy_timeout 之外再兜底）
        for attempt in range(5):
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO news_trade_reviews (news_id, summary, generated_at) "
                    "VALUES (?, ?, datetime('now','localtime'))",
                    (news_id, json.dumps(summary, ensure_ascii=False)),
                )
                conn.commit()
                break
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt < 4:
                    time.sleep(2)
                    continue
                raise
        # 归因复核闭环：失败归因明细落库（人工可复核/纠错回灌）
        attribution_rows = review_agent.build_attribution_rows(
            losses, failure_analysis,
            match_id_key="decision_id", stored_id_key="id", result_id_key="decision_id",
        )
        review_agent.save_attribution_details(conn, "news", news_id, attribution_rows)
        # 按筋分堆沉淀认知：每次复盘完重算「这根筋爱兑现还是爱嘴炮」的经验
        run_tendon_review(conn)
    finally:
        conn.close()
    return {
        "ok": True,
        "summary": summary,
        "generated_at": datetime.now(config.TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 按筋分堆沉淀认知（刨根问底主线的收口）：已结算交易按门卫挖出的「筋」分组，
# 统计真钱兑现（WIN）率，沉淀「这根筋爱兑现还是爱嘴炮」的经验写进 review_insights
# (scope='tendon')，经既有 review_insights 回灌机制反哺八段核对（不注入门卫）。
# ---------------------------------------------------------------------------

# 门卫六根筋（顺序即匹配优先级，core_impact 首句「动了[X]这根筋」里的 X 会最先命中）
_TENDON_NAMES = ["兜底", "信心", "资金成本", "流动性", "供需", "预期"]


def _tendon_of(core: str) -> str:
    for name in _TENDON_NAMES:
        if name in (core or ""):
            return name
    return ""


def run_tendon_review(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """按筋分堆沉淀认知（幂等：先清空 scope='tendon' 再按最新已结算样本重算）。

    只对已结算笔数足够（>= 最小样本门限）的筋生成认知，避免单笔噪音；
    胜率 >=60% 记「多为兑现」、<=40% 记「多为嘴炮」、居中记「需资金面坐实」。
    静默失败：不影响主流程（复盘仍照常完成）。
    """
    own = conn is None
    conn = conn or _connect()
    created: List[str] = []
    try:
        rows = conn.execute(
            """SELECT qt.verdict, rn.gate_core_impact
               FROM quick_sim_trades qt
               JOIN raw_news rn ON rn.id = qt.news_id
               WHERE qt.settled = 1 AND rn.gate_core_impact != ''"""
        ).fetchall()
        stats: Dict[str, Dict[str, int]] = {}
        for r in rows:
            tendon = _tendon_of(str(r["gate_core_impact"] or ""))
            if not tendon:
                continue
            s = stats.setdefault(tendon, {"n": 0, "wins": 0, "losses": 0})
            s["n"] += 1
            v = str(r["verdict"] or "")
            if v == "WIN":
                s["wins"] += 1
            elif v == "LOSS":
                s["losses"] += 1
        conn.execute("DELETE FROM review_insights WHERE scope = 'tendon'")
        min_n = 5  # 少于 5 笔已结算，不沉淀（与 review_insights 回灌 min_samples 门槛一致）
        for tendon, s in stats.items():
            if s["n"] < min_n:
                continue
            decided = s["wins"] + s["losses"]
            if decided <= 0:
                continue
            winrate = s["wins"] / decided
            if winrate >= 0.6:
                flavor = "多为兑现（真钱坐实）"
            elif winrate <= 0.4:
                flavor = "多为嘴炮（只喊没钱）"
            else:
                flavor = "兑现/嘴炮参半，需资金面坐实"
            rule = (
                f"[筋·{tendon}] 历史{decided}笔已结算，胜率{winrate:.0%}，{flavor}。"
                f"复盘或分析遇到「{tendon}」这根筋时：先追问谁掏真钱、钱坐实了没，再据此给方向。"
            )
            conn.execute(
                "INSERT INTO review_insights (rule, asset, scope, active, source_samples, origin, kind)"
                " VALUES (?, '', 'tendon', 1, ?, 'tendon', 1)",
                (rule, s["n"]),
            )
            created.append(rule)
        conn.commit()
    except Exception as exc:
        print(f"[TENDON] 筋认知沉淀失败(忽略): {exc}", flush=True)
    finally:
        if own:
            conn.close()
    return {"ok": True, "created": created}
