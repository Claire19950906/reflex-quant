# -*- coding: utf-8 -*-
"""门卫反思 —— 第一层 AI 语义筛选（L3 gate）的复盘能力。

目标：评估门卫的筛选质量，回答两个问题：
  1. 正向推（precision）：门卫放行（可交易）并产生决策的新闻，后续盈亏是否兑现？
     —— 判断门卫"正向判断是否正确"。
  2. 反向推（recall）：行情 3m 异动时，门卫有没有把对应重要新闻放进来 / 标出来？
     —— 判断门卫"行情异常时是否漏掉重要新闻"。

设计：
  - 纯查询 + 复用 existing abnormal_detector 异动点（异动点由 api_server._detect_5m_abnormals 传入，
    避免本模块直接依赖 quote_sources / jin10 网络层，保持 IO 边界清晰）。
  - 无 LLM 调用；字段全部来自已落库的 raw_news.gate_* 与 ai_decisions 兑现结果。
  - 门卫 verdict 取值（v181 二分类）：可交易 / 排除 / 无信号（见 realtime_filter._harden_verdict）。
    "只能看" 仍保留 _PASS_VERDICTS 中的位置以兼容历史数据（等价于放行）。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional

import db

# 反向推时间锚回溯窗口（秒）：与 api_server._abnormal_news 的 1800 保持一致
REVERSE_WINDOW_S = 1800

# 门卫「已放行」的判级：可交易（v181）+ 只能看（历史兜底）。排除/无信号则未放行。
_PASS_VERDICTS = ("可交易", "只能看")


def _cutoff_ts(days: int) -> int:
    days = max(1, min(int(days or 3), 90))
    return int(time.time()) - days * 86400


def _judge_correct(t: Dict[str, Any]) -> Optional[bool]:
    """单条决策的"判对/判错/无结果"。

    - 未结算 -> None（无结果）
    - is_correct 显式 WIN/LOSS -> True/False
    - 否则用 forward_pnl 符号兜底
    """
    try:
        settled = int(t.get("settled") or 0)
    except (TypeError, ValueError):
        settled = 0
    if settled != 1:
        return None
    ic = str(t.get("is_correct") or "").strip().upper()
    if ic == "WIN":
        return True
    if ic == "LOSS":
        return False
    pnl = t.get("forward_pnl")
    if isinstance(pnl, (int, float)):
        if pnl > 0:
            return True
        if pnl < 0:
            return False
def _build_forward(asset: str, days: int) -> List[Dict[str, Any]]:
    """正向推：门卫放行并产生决策的新闻 → 兑现结果。"""
    cutoff = _cutoff_ts(days)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    out: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT ad.id AS decision_id, ad.news_id, ad.target_asset, ad.suggested_action,
                   COALESCE(qs.settled, 0) AS settled,
                   qs.verdict AS is_correct,
                   qs.pnl_pct AS forward_pnl,
                   qs.mfe_pct, qs.mae_pct,
                   ad.no_trade_reason, ad.trade_gate_reason, ad.created_at,
                   rn.gate_core_impact, rn.gate_verdict, rn.gate_strength,
                   rn.gate_main_asset, rn.gate_kind, rn.content, rn.ts
              FROM ai_decisions ad
              JOIN raw_news rn ON rn.id = ad.news_id
              LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
             WHERE ad.target_asset = ? AND ad.is_sibling = 0
               AND (rn.gate_verdict IS NOT NULL AND rn.gate_verdict != '')
               AND (rn.ts IS NULL OR rn.ts >= ?)
             ORDER BY ad.id DESC
             LIMIT 200
            """,
            (asset.upper(), cutoff),
        ).fetchall()
        for r in rows:
            verdict = str(r["gate_verdict"] or "").strip()
            action = str(r["suggested_action"] or "").strip()
            pnl = r["forward_pnl"]
            correct = _judge_correct(dict(r))
            out.append({
                "decision_id": r["decision_id"],
                "news_id": r["news_id"],
                "verdict": verdict,
                "strength": r["gate_strength"] or 0,
                "core_impact": str(r["gate_core_impact"] or "").strip(),
                "main_asset": str(r["gate_main_asset"] or "").strip(),
                "kind": str(r["gate_kind"] or "").strip(),
                "action": action,
                "settled": r["settled"] or 0,
                "correct": correct,
                "is_correct": str(r["is_correct"] or "").strip().upper(),  # P1: 同步暴露 is_correct 字段
                "forward_pnl": round(float(pnl), 4) if isinstance(pnl, (int, float)) else None,
                "mfe_pct": round(float(r["mfe_pct"]), 4) if isinstance(r["mfe_pct"], (int, float)) else None,
                "content": (str(r["content"] or "").strip())[:140],
            })
    except Exception as exc:  # pragma: no cover - 查询失败降级为空
        print(f"[GATE-REFLECT] forward 查询失败(忽略): {type(exc).__name__}: {exc}", flush=True)
    finally:
        conn.close()
    return out
def _news_in_window(asset: str, lo: int, hi: int) -> List[Dict[str, Any]]:
    """异动锚点窗口内的该品种放行新闻（带门卫判定，供反向推判断门卫是否抓到）。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    out: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT rn.id AS news_id, rn.content, rn.ts,
                   rn.gate_verdict, rn.gate_strength, rn.gate_core_impact,
                   ne.action, ne.score
              FROM news_events ne
              JOIN raw_news rn ON rn.id = ne.news_id
             WHERE ne.is_noise = 0 AND ne.asset = ?
               AND ne.ts >= ? AND ne.ts <= ?
             ORDER BY ne.ts DESC
             LIMIT 20
            """,
            (asset.upper(), lo, hi),
        ).fetchall()
        for r in rows:
            out.append({
                "news_id": r["news_id"],
                "content": (str(r["content"] or "").strip())[:140],
                "verdict": str(r["gate_verdict"] or "").strip(),
                "strength": r["gate_strength"] or 0,
                "core_impact": str(r["gate_core_impact"] or "").strip(),
            })
    except Exception as exc:  # pragma: no cover
        print(f"[GATE-REFLECT] reverse 新闻查询失败(忽略): {type(exc).__name__}: {exc}", flush=True)
    finally:
        conn.close()
    return out


def _build_reverse(asset: str, abnormals: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """反向推：每个异动点 → 窗口内门卫是否放行了相关新闻（漏检 = 未放行）。"""
    out: List[Dict[str, Any]] = []
    for a in (abnormals or []):
        ts = int(a.get("ts") or 0)
        if not ts:
            continue
        news = _news_in_window(asset, ts - REVERSE_WINDOW_S, ts)
        passed = [n for n in news if n["verdict"] in _PASS_VERDICTS]
        excluded = [n for n in news if n["verdict"] in ("排除", "无信号", "")]
        covered = len(passed) > 0
        out.append({
            "ts": ts,
            "pct_change": a.get("pct_change"),
            "vol_ratio": a.get("vol_ratio"),
            "trigger_type": a.get("trigger_type") or "",
            "total_news": len(news),
            "covered": covered,
            "miss": (not covered),
            "passed_count": len(passed),
            "excluded_count": len(excluded),
            "passed_news": passed[:6],
            "excluded_news": excluded[:6],
        })
    return out
def gate_reflection(asset: str, days: int = 3,
                    abnormals: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """主入口：返回门卫反思的 summary + forward（正向）+ reverse（反向）。

    asset: 英文代码（BTC/ETH/SOL/XAU/WTI）。abnormals 由 api_server._detect_5m_abnormals 传入。
    """
    asset = (asset or "").upper()
    forward = _build_forward(asset, days)
    reverse = _build_reverse(asset, abnormals)

    decided = [f for f in forward if f["correct"] is not None]
    n_win = sum(1 for f in decided if f["correct"] is True)
    n_loss = sum(1 for f in decided if f["correct"] is False)
    decided_n = n_win + n_loss
    precision = round(n_win / decided_n * 100, 1) if decided_n else None

    n_abnormal = len(reverse)
    n_covered = sum(1 for r in reverse if r["covered"])
    n_miss = sum(1 for r in reverse if r["miss"])
    recall = round(n_covered / n_abnormal * 100, 1) if n_abnormal else None

    # 放行后胜率 = 有结果的放行决策里 WIN 占比（与 precision 同口径，保留独立字段便于后续拆分）
    pass_winrate = precision

    return {
        "asset": asset,
        "days": max(1, min(int(days or 3), 90)),
        "summary": {
            "n_judged": len(forward),
            "n_decided": decided_n,
            "n_win": n_win,
            "n_loss": n_loss,
            "precision": precision,
            "pass_winrate": pass_winrate,
            "n_abnormal": n_abnormal,
            "n_covered": n_covered,
            "n_miss": n_miss,
            "recall": recall,
        },
        "forward": forward,
        "reverse": reverse,
    }
    return None