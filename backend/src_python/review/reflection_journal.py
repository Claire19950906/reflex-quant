#!/usr/bin/env python3
"""v189-2: 反思系统每日归档 + 周期日志记录。

提供 3 个核心函数:
  - record_cycle(decision_id, run_id, ...): 写入 reflection_cycle_log (每条反思后调)
  - rollup_daily(date, run_id): 聚合写入 reflection_daily_journal (每日 UTC 23:55 调)
  - gen_summary(date): 输出当日完整日报 (供前端 / LLM 摄入)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

import db


def record_cycle(
    decision_id: int,
    run_id: str,
    *,
    reflection_id: Optional[int] = None,
    outcome: str = "pending",
    llm_invoked: int = 0,
    latency_ms: Optional[int] = None,
    ontology_signal: str = "",
    error: str = "",
) -> Optional[int]:
    """单条反思周期日志。返回 rowid, 失败 None。"""
    conn = db.get_connection()
    try:
        # v189-fix: ON CONFLICT DO NOTHING 防止 worker 重跑/锁重试时重复写入.
        # UNIQUE INDEX idx_rcl_dec_run(decision_id, run_id) 约束已加.
        cur = conn.execute(
            """INSERT INTO reflection_cycle_log
                 (decision_id, run_id, reflection_id, outcome,
                  llm_invoked, latency_ms, ontology_signal, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(decision_id, run_id) DO NOTHING""",
            (decision_id, run_id, reflection_id, outcome,
             int(bool(llm_invoked)), latency_ms, ontology_signal, error))
        if cur.rowcount > 0:
            new_id = cur.lastrowid
        else:
            # 已存在: 找出现有 row id 返回 (让调用方知道写了)
            row = conn.execute(
                "SELECT id FROM reflection_cycle_log WHERE decision_id=? AND run_id=?",
                (decision_id, run_id)).fetchone()
            new_id = int(row[0]) if row else None
        conn.commit()
        return new_id
    except Exception as exc:
        print(f"[journal] record_cycle failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        conn.close()


def rollup_daily(journal_date: Optional[str] = None,
                 run_id: Optional[str] = None) -> Dict[str, Any]:
    """按日期+run_id 聚合, 写 reflection_daily_journal。"""
    conn = db.get_connection()
    try:
        if journal_date is None:
            journal_date = datetime.utcnow().strftime("%Y-%m-%d")
        if run_id is None:
            row = conn.execute(
                "SELECT run_id FROM backtest_daily_runs WHERE archived=0 ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            run_id = row[0] if row else ""
        # v189-fix: ai_decisions 没有 outcome/pnl_usdt 列, win/loss 在
        # reflection_cycle_log.outcome, pnl 在 ai_decisions.forward_pnl.
        # 旧 SQL 拼表/字段名 4 处错, 永远抛 OperationalError → rollup_daily 永不落库.
        # 新逻辑: 所有 LIVE 统计以 ai_decisions.created_at 为"决策日期"主轴,
        # 通过 decision_id LEFT JOIN reflection_cycle_log 拿 outcome/ontology_signal.
        # 这样 paper_trading_run_id IS NOT NULL + settled=1 的 60 行决策能被正确聚到 2026-09-24.
        is_live = (run_id == '' or run_id is None or
                   (isinstance(run_id, str) and run_id.startswith('LIVE:')))
        if is_live:
            # P1: 结算字段改读 quick_sim_trades（结算唯一权威）；决策上下文仍来自 ai_decisions。
            stats = conn.execute(
                """SELECT COUNT(*), SUM(qs.pnl_pct)
                   FROM quick_sim_trades qs
                   JOIN ai_decisions ad ON ad.id = qs.decision_id
                   WHERE date(ad.created_at)=?
                     AND qs.settled=1
                     AND ad.paper_trading_run_id IS NOT NULL""",
                (journal_date,)).fetchone()
            n, pnl = (stats[0] or 0), (stats[1] or 0)
            wl_row = conn.execute(
                """SELECT SUM(CASE WHEN rcl.outcome='win' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN rcl.outcome='loss' THEN 1 ELSE 0 END)
                   FROM quick_sim_trades qs
                   JOIN ai_decisions ad ON ad.id = qs.decision_id
                   LEFT JOIN reflection_cycle_log rcl ON rcl.decision_id = ad.id
                                                     AND (rcl.run_id='' OR rcl.run_id LIKE 'LIVE:%')
                   WHERE date(ad.created_at)=?
                     AND qs.settled=1
                     AND ad.paper_trading_run_id IS NOT NULL""",
                (journal_date,)).fetchone()
            n_wins, n_losses = (wl_row[0] or 0), (wl_row[1] or 0)
        else:
            # backtest / 指定 run
            stats = conn.execute(
                """SELECT COUNT(*), SUM(forward_pnl)
                   FROM ai_decisions
                   WHERE run_id=? AND date(created_at)=?""",
                (run_id, journal_date)).fetchone()
            n, pnl = (stats[0] or 0), (stats[1] or 0)
            wl_row = conn.execute(
                """SELECT SUM(CASE WHEN rcl.outcome='win' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN rcl.outcome='loss' THEN 1 ELSE 0 END)
                   FROM ai_decisions ad
                   LEFT JOIN reflection_cycle_log rcl ON rcl.decision_id = ad.id
                                                         AND rcl.run_id = ad.run_id
                   WHERE ad.run_id=? AND date(ad.created_at)=?""",
                (run_id, journal_date)).fetchone()
            n_wins, n_losses = (wl_row[0] or 0), (wl_row[1] or 0)
        win_rate = (n_wins / n) if n else None
        ont_rows = conn.execute(
            """SELECT COALESCE(rcl.ontology_signal, 'unknown') AS sig, COUNT(*)
               FROM quick_sim_trades qs
               JOIN ai_decisions ad ON ad.id = qs.decision_id
               LEFT JOIN reflection_cycle_log rcl ON rcl.decision_id = ad.id
                                                 AND (rcl.run_id='' OR rcl.run_id LIKE 'LIVE:%')
               WHERE date(ad.created_at)=?
                 AND qs.settled=1
                 AND ad.paper_trading_run_id IS NOT NULL
               GROUP BY sig ORDER BY 2 DESC LIMIT 20""",
            (journal_date,)).fetchall()
        ont_breakdown = {r[0]: r[1] for r in ont_rows}
        unknown = ont_breakdown.get("unknown", 0)
        unknown_rate = (unknown / n) if n else 0
        llm_n = conn.execute(
            """SELECT COUNT(*)
               FROM quick_sim_trades qs
               JOIN ai_decisions ad ON ad.id = qs.decision_id
               JOIN reflection_cycle_log rcl ON rcl.decision_id = ad.id
                                            AND (rcl.run_id='' OR rcl.run_id LIKE 'LIVE:%')
                                            AND rcl.llm_invoked=1
               WHERE date(ad.created_at)=?
                 AND qs.settled=1
                 AND ad.paper_trading_run_id IS NOT NULL""",
            (journal_date,)).fetchone()[0]
        # v189-fix: experience_applications 时间字段是 applied_at (TEXT),
        # ai_decisions 是 created_at. 旧 SQL 字段名错误 → OperationalError.
        top_exp_rows = conn.execute(
            """SELECT experience_id, COUNT(*) FROM experience_applications
               WHERE date(applied_at)=?
                 AND (run_id='' OR run_id IS NULL)
               GROUP BY experience_id ORDER BY 2 DESC LIMIT 5""",
            (journal_date,)).fetchall()
        top_exp_text = ", ".join(f"exp#{eid}({n})" for eid, n in top_exp_rows)
        top_miss_rows = conn.execute(
            """SELECT id, missed_signal FROM ai_decisions
               WHERE date(created_at)=?
                 AND (run_id='' OR run_id IS NULL)
                 AND missed_signal=1
               ORDER BY id DESC LIMIT 5""",
            (journal_date,)).fetchall()
        top_miss_text = ", ".join(f"#{did}({c})" for did, c in top_miss_rows)
        conn.execute(
            """INSERT INTO reflection_daily_journal
                 (journal_date, run_id, n_decisions, n_wins, n_losses, win_rate, pnl_usdt,
                  ontology_breakdown, unknown_rate, llm_invoked_count,
                  top_experiences, top_missed_signals)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(journal_date, run_id) DO UPDATE SET
                 n_decisions=excluded.n_decisions, n_wins=excluded.n_wins,
                 n_losses=excluded.n_losses, win_rate=excluded.win_rate,
                 pnl_usdt=excluded.pnl_usdt,
                 ontology_breakdown=excluded.ontology_breakdown,
                 unknown_rate=excluded.unknown_rate,
                 llm_invoked_count=excluded.llm_invoked_count,
                 top_experiences=excluded.top_experiences,
                 top_missed_signals=excluded.top_missed_signals""",
            (journal_date, run_id, n, n_wins, n_losses, win_rate, pnl,
             json.dumps(ont_breakdown, ensure_ascii=False), unknown_rate, llm_n,
             top_exp_text, top_miss_text))
        conn.commit()
        return {"ok": True, "journal_date": journal_date, "run_id": run_id,
                "n_decisions": n, "win_rate": win_rate, "unknown_rate": unknown_rate,
                "llm_invoked_count": llm_n, "ontology_breakdown": ont_breakdown}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def gen_summary(journal_date: str, run_id: Optional[str] = None) -> Dict[str, Any]:
    """返回日报全文 + 关键指标 + ontology 分布。"""
    conn = db.get_connection()
    try:
        row = conn.execute(
            """SELECT journal_date, run_id, n_decisions, n_wins, n_losses, win_rate, pnl_usdt,
                      ontology_breakdown, unknown_rate, llm_invoked_count,
                      top_experiences, top_missed_signals, reflections_summary, created_at
               FROM reflection_daily_journal
               WHERE journal_date=? AND (run_id=? OR ? IS NULL)
               ORDER BY created_at DESC LIMIT 1""",
            (journal_date, run_id, run_id)).fetchone()
        if not row:
            return {"ok": False, "error": f"no journal for {journal_date}"}
        return {"ok": True,
                "date": row[0], "run_id": row[1],
                "metrics": {"n_decisions": row[2], "n_wins": row[3],
                            "n_losses": row[4], "win_rate": row[5],
                            "pnl_usdt": row[6], "unknown_rate": row[8],
                            "llm_invoked_count": row[9]},
                "ontology_breakdown": json.loads(row[7] or "{}"),
                "top_experiences": row[10],
                "top_missed_signals": row[11],
                "reflections_summary": row[12],
                "created_at": row[13]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "rollup"
    if cmd == "rollup":
        print(json.dumps(rollup_daily(), ensure_ascii=False, indent=2))
    elif cmd == "summary":
        d = sys.argv[2] if len(sys.argv) > 2 else datetime.utcnow().strftime("%Y-%m-%d")
        print(json.dumps(gen_summary(d), ensure_ascii=False, indent=2))