# -*- coding: utf-8 -*-
"""v200 P0+ monitor_llm_value: 监控 LLM 增量价值 (24h 周期评估).

设计动机: 之前反思系统把所有 ai_decisions 都当 LLM 真输出,
  24h daemon 提炼的"经验"注入 prompt 时, 也全是 LLM 角度的经验.
  事实上: 大量字段是规则兜底(rule_*) / 业务知识兜底(business_kb_v176_2) /
  历史模式兜底(historical_pattern). 这些"非 LLM"的字段, 经验价值
  来自规则本身, 与 LLM 是否调用无关.

  本模块评估 LLM 在「跨资产推理 + 因素排序 + 历史反例」这些
  LLM 有增量价值的环节, 是不是真的比纯规则 + 业务知识 + 历史模式好.

输出 4 类 verdict:
  - llm_positive_value: LLM 子样本 win_rate/avg_pnl 显著高于非 LLM 子样本
  - llm_negative_value: LLM 子样本 win_rate/avg_pnl 显著低于非 LLM 子样本
  - llm_neutral:          差异不显著, LLM 没产生增量价值
  - insufficient_data:    样本不足(< 30 个 LLM 真输出), 无法下结论

严谨性:
  - "LLM 子样本" = completeness_score >= 0.9 (几乎全 LLM 真输出)
  - "非 LLM 子样本" = completeness_score < 0.5 (兜底为主)
  - 中间 0.5~0.9 不参与对比(避免污染两组样本)
  - 至少各 30 个 settled decision 才下结论, 否则 insufficient_data
  - win_rate 差 >= 10% 且 avg_pnl 差 >= 0.005 (0.5%) 才算显著
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional


_WIN_RATE_DIFF_THRESHOLD = 0.10
_AVG_PNL_DIFF_THRESHOLD  = 0.005
_MIN_SAMPLES_PER_GROUP   = 30


def evaluate_llm_incremental_value(
    conn: sqlite3.Connection,
    *,
    window_days: int = 30,
) -> Dict[str, Any]:
    """评估 LLM 增量价值 (相对规则 + 业务知识 + 历史模式)."""
    now = int(time.time())
    cutoff = now - window_days * 86400

    llm_sql = """
        SELECT
            COUNT(*) AS n,
            AVG(CASE WHEN is_correct = 'WIN' THEN 1.0
                     WHEN is_correct = 'LOSS' THEN 0.0 ELSE NULL END) AS win_rate,
            AVG(forward_pnl) AS avg_pnl,
            AVG(completeness_score) AS avg_completeness
        FROM ai_decisions
        WHERE settled = 1
          AND forward_pnl IS NOT NULL
          AND COALESCE(completeness_score, 1.0) >= 0.9
          AND llm_field_count >= 5
          AND created_at > datetime(?, 'unixepoch')
    """
    not_llm_sql = """
        SELECT
            COUNT(*) AS n,
            AVG(CASE WHEN is_correct = 'WIN' THEN 1.0
                     WHEN is_correct = 'LOSS' THEN 0.0 ELSE NULL END) AS win_rate,
            AVG(forward_pnl) AS avg_pnl,
            AVG(completeness_score) AS avg_completeness
        FROM ai_decisions
        WHERE settled = 1
          AND forward_pnl IS NOT NULL
          AND COALESCE(completeness_score, 1.0) < 0.5
          AND created_at > datetime(?, 'unixepoch')
    """

    llm_row = conn.execute(llm_sql, (cutoff,)).fetchone()
    not_llm_row = conn.execute(not_llm_sql, (cutoff,)).fetchone()

    def _extract(row):
        if not row:
            return {"n": 0, "win_rate": 0.0, "avg_pnl": 0.0, "avg_completeness": 0.0}
        return {
            "n": int(row[0] or 0),
            "win_rate": float(row[1] or 0.0),
            "avg_pnl": float(row[2] or 0.0),
            "avg_completeness": float(row[3] or 0.0),
        }

    llm = _extract(llm_row)
    not_llm = _extract(not_llm_row)

    delta_win_rate = llm["win_rate"] - not_llm["win_rate"]
    delta_avg_pnl = llm["avg_pnl"] - not_llm["avg_pnl"]

    if llm["n"] < _MIN_SAMPLES_PER_GROUP or not_llm["n"] < _MIN_SAMPLES_PER_GROUP:
        verdict = "insufficient_data"
        action = (f"insufficient: LLM={llm['n']}, not-LLM={not_llm['n']}, "
                  f"need >= {_MIN_SAMPLES_PER_GROUP} each")
    elif (delta_win_rate >= _WIN_RATE_DIFF_THRESHOLD
          and delta_avg_pnl >= _AVG_PNL_DIFF_THRESHOLD):
        verdict = "llm_positive_value"
        action = (f"LLM +{delta_win_rate*100:.1f}% win, +{delta_avg_pnl*100:.2f}% pnl "
                  f"vs rule fallback - keep LLM call")
    elif (delta_win_rate <= -_WIN_RATE_DIFF_THRESHOLD
          and delta_avg_pnl <= -_AVG_PNL_DIFF_THRESHOLD):
        verdict = "llm_negative_value"
        action = (f"WARNING LLM {delta_win_rate*100:+.1f}% win, "
                  f"{delta_avg_pnl*100:+.2f}% pnl worse than rule fallback")
    else:
        verdict = "llm_neutral"
        action = (f"LLM ~ rule fallback (win_delta={delta_win_rate*100:+.1f}%, "
                  f"pnl_delta={delta_avg_pnl*100:+.2f}%) - keep but no strong gain")

    return {
        "window_days": window_days,
        "llm_n": llm["n"],
        "llm_win_rate": llm["win_rate"],
        "llm_avg_pnl": llm["avg_pnl"],
        "llm_avg_completeness": llm["avg_completeness"],
        "not_llm_n": not_llm["n"],
        "not_llm_win_rate": not_llm["win_rate"],
        "not_llm_avg_pnl": not_llm["avg_pnl"],
        "not_llm_avg_completeness": not_llm["avg_completeness"],
        "delta_win_rate": delta_win_rate,
        "delta_avg_pnl": delta_avg_pnl,
        "verdict": verdict,
        "action_recommended": action,
    }


def persist_evaluation(conn: sqlite3.Connection, evaluation: Dict[str, Any]) -> int:
    """持久化评估结果到 monitor_llm_value 表."""
    cur = conn.execute("""
        INSERT INTO monitor_llm_value
            (window_days, llm_n, llm_win_rate, llm_avg_pnl, llm_avg_completeness,
             not_llm_n, not_llm_win_rate, not_llm_avg_pnl, not_llm_avg_completeness,
             delta_win_rate, delta_avg_pnl, verdict, action_recommended)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        int(evaluation["window_days"]),
        int(evaluation["llm_n"]),
        float(evaluation["llm_win_rate"]),
        float(evaluation["llm_avg_pnl"]),
        float(evaluation["llm_avg_completeness"]),
        int(evaluation["not_llm_n"]),
        float(evaluation["not_llm_win_rate"]),
        float(evaluation["not_llm_avg_pnl"]),
        float(evaluation["not_llm_avg_completeness"]),
        float(evaluation["delta_win_rate"]),
        float(evaluation["delta_avg_pnl"]),
        str(evaluation["verdict"]),
        str(evaluation["action_recommended"]),
    ))
    conn.commit()
    return int(cur.lastrowid)


def run_evaluation_and_persist(conn: sqlite3.Connection,
                                window_days: int = 30) -> Dict[str, Any]:
    """运行一次评估并落库."""
    ev = evaluate_llm_incremental_value(conn, window_days=window_days)
    try:
        ev["_rowid"] = persist_evaluation(conn, ev)
    except Exception as e:
        ev["_persist_error"] = f"{type(e).__name__}: {e}"
    return ev


if __name__ == "__main__":
    import db
    conn = db.get_connection()
    result = run_evaluation_and_persist(conn, window_days=30)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    conn.close()
