#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cron_causal_outcome_eval.py — 因果链 outcome 评估调度入口 (v337)

为什么需要这个模块
------------------
causal_chain_evaluator.evaluate_pending() 原本**只有一个触发点**：
bridge_server 的 GET /api/v291/causal-chains/feedback?auto_run=true。
也就是说「因果链预测对不对」这件事，只在有人打开那个接口时才被算一次，
没有任何定时调度 → 到期的 PENDING outcome 会一直挂着。

实测（2026-11-14）：3 条 11-09 触发、11-14 03:48 到期的 outcome 超期 4 小时
仍是 PENDING，全表最后一次评估停在 01:59。这是能力验收 #6「因果图谱闭环」
唯一 FAIL 的根因 —— 图谱写了、预测下了，但「事后核对」这一步没人按表走。

本脚本：每条到期 PENDING 都用真实行情评估（方向/幅度/失效），
算完写 status + direction_hit + magnitude_hit + actual_pct_5d，
并顺手输出活跃链的命中率与校准缺口（stated_confidence vs real_hit_rate）。

用法:
    python3 cron_causal_outcome_eval.py            # 评估所有到期 PENDING
    python3 cron_causal_outcome_eval.py --force    # 忽略 deadline 强制评估
    python3 cron_causal_outcome_eval.py --max-age-days 7 --json
退出码: 0 = 正常；1 = 评估抛异常
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import causal_chain_evaluator as cce
import db


def _open_write() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db.get_db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _pending_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS n_total,
                  SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS n_pending,
                  SUM(CASE WHEN status='PENDING' AND eval_deadline_ts<=? THEN 1 ELSE 0 END) AS n_overdue,
                  MAX(CASE WHEN status<>'PENDING' THEN eval_deadline_ts END) AS last_eval_deadline
             FROM causal_chain_outcomes""",
        (int(time.time()),),
    ).fetchone()
    return {
        "outcomes_total": int(row["n_total"] or 0),
        "pending": int(row["n_pending"] or 0),
        "overdue": int(row["n_overdue"] or 0),
        "last_eval_deadline": int(row["last_eval_deadline"] or 0),
    }


# v427：本 cron 以前**只评估、不入队** —— insert_pending_outcome 唯一的调用方是
# backfill()，而 backfill 只有 evaluator 的命令行会跑。结果是链库样本只靠人工
# 回填才涨，"到期核对"这一步一直空转：核对的是别人手动塞进去的那批。实测
# 178 条启用链、7 天窗口，靠人工跑一次才进 1450 条，之后十几天没有新增。
# 这里补上前半步：每轮先按小窗口入队（只扫最近 2 天，覆盖 15 分钟调度间隔
# 绰绰有余，成本 ~2s），再评估到期样本，闭环才真的转起来。
INGEST_DAYS = 2
# 入队不必跟评估同频：样本要 5 个交易日才结算，15 分钟抓一次纯属浪费 IO
# （实测 2 天窗口一轮 20s）。这里做小时级节流，评估仍按 plist 的 15 分钟跑。
INGEST_MIN_INTERVAL_SEC = 3600
_STATE_PATH = os.path.expanduser("~/.trident/state/last_causal_ingest.ts")


def _ingest_due() -> bool:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as fh:
            last = float(fh.read().strip() or 0)
    except (OSError, ValueError):
        last = 0.0
    return time.time() - last >= INGEST_MIN_INTERVAL_SEC


def _mark_ingested() -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
        os.replace(tmp, _STATE_PATH)
    except OSError:
        pass


def _ingest(conn, days: int = INGEST_DAYS) -> dict:
    """抓取最近事件 → 追加 PENDING outcome。锁争用视为可跳过，不阻断评估。"""
    if not _ingest_due():
        return {"skipped": "throttled", "lookback_days": days}
    try:
        r = cce.backfill(conn, days=days)
        conn.commit()
        _mark_ingested()
        return {"ingested": int(r.get("outcomes_inserted", 0)),
                "chains_scanned": int(r.get("chains_scanned", 0)),
                "lookback_days": days}
    except sqlite3.OperationalError as exc:
        conn.rollback()
        return {"ingested": 0, "error": str(exc)[:120]}


def run(force: bool = False, max_age_days=None, ingest_days: int = INGEST_DAYS) -> dict:
    t0 = time.time()
    conn = _open_write()
    try:
        ing = _ingest(conn, days=ingest_days) if ingest_days and ingest_days > 0 else {}
        before = _pending_stats(conn)
        res = cce.evaluate_pending(conn, force=force, max_age_days=max_age_days)
        conn.commit()
        after = _pending_stats(conn)
        summary = cce.get_feedback_summary(conn, days=14)
        chains = summary.get("chains", []) if isinstance(summary, dict) else []
        res.update({
            "ingest": ing,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "before": before,
            "after": after,
            "elapsed_sec": round(time.time() - t0, 1),
            "active_chains": len(chains),
            "chains_needing_recalibration": [
                {"chain_id": c.get("chain_id"), "asset": c.get("asset"),
                 "real_hit_rate": c.get("real_hit_rate"),
                 "stated_confidence": c.get("stated_confidence")}
                for c in chains if c.get("needs_recalibration")
            ][:8],
        })
        return res
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略 deadline 强制评估所有 PENDING")
    ap.add_argument("--max-age-days", type=int, default=None, help="只评估 trigger 在 N 天内的")
    ap.add_argument("--ingest-days", type=int, default=INGEST_DAYS,
                    help="入队回看天数（0 = 只评估不入队）")
    ap.add_argument("--json", action="store_true", help="机读输出")
    args = ap.parse_args()
    try:
        res = run(force=args.force, max_age_days=args.max_age_days,
                  ingest_days=args.ingest_days)
    except Exception as exc:
        print(f"[causal-eval] FAIL {type(exc).__name__}: {exc}")
        return 1
    if args.json:
        print(json.dumps(res, ensure_ascii=False, default=str))
    else:
        print(
            f"[causal-eval] evaluated={res['evaluated']} "
            f"hit={res['hit']} hit_weak={res['hit_weak']} miss={res['miss']} "
            f"neutral={res['neutral']} expired={res['expired']} | "
            f"pending {res['before']['pending']}->{res['after']['pending']} "
            f"(overdue {res['after']['overdue']}) | {res['elapsed_sec']}s"
        )
        recal = res.get("chains_needing_recalibration") or []
        if recal:
            print("需重校准链:", "; ".join(
                f"{r['chain_id']}({r['asset']}) 实hit={r['real_hit_rate']:.2f} vs 声称{r['stated_confidence']}"
                for r in recal
            ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
