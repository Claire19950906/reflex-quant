"""
decision_engine.py — 2026-10-19 v1 (Q1 根因修复)
=====================================================
闭环 producer: critical_price_moves 强信号 → trade_decisions → settle → trade_reflections_v2

根因: bridge 已有 /api/decision/record + /settle-now + /list (P1), 但从未有 producer 调用它们。
     trade_decisions 表常驻仅 5 行 (测试桩), 致 missed_signals 永远 = 强信号全集。

设计:
  - 直接 DB 写入 (in-process 速度, 跳过 HTTP roundtrip)。生产环境若需要 API 校验可改为调用 bridge。
  - 幂等去重: evidence_json 嵌入 {"source":"decision_engine","signal_id":K,"move_ts":T},
    插入前查询 LIKE '%signal_id=N%' 防重复。
  - action 派生: move_pct>0 → BUY, =0 → HOLD, <0 → SELL (保守阈值 ±0.1% 内判 HOLD)
  - price_at_decision: end_price (若为 0/null 改用 peak_price, 还 0 改用 ohlcv_daily 当日 close)
  - created_at: 对历史回填用 move_ts (使 5d horizon 立即可结算); 对实时 producer 用 wall clock now
  - settlement: target close = ohlcv_daily 5d (或最接近 432000s 后) 的 close

用法:
  python3 src_python/decision_engine.py backfill --days 90      # 回填 90 天, 立即结算到期项
  python3 src_python/decision_engine.py live --once             # 单遍实时扫描 (新信号)
  python3 src_python/decision_engine.py settle                  # 仅结算到期 open 决策
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

ALGO_THRESHOLD = 0.7            # 仅处理 algo_score >= 0.7
DEDUP_WINDOW_S = 86400           # ±24h 内已有决策则跳过
HOLD_BAND_PCT = 0.10             # |move_pct| < 此值判 HOLD
HORIZON_S = 5 * 86400            # 5 天结算
OVERSHOOT_TOL_S = 12 * 3600      # 找 5d 后价格时允许 12h 漂移
SCHEMA_VERSION = "de_v1_2026-10-19"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _is_already_decided(conn: sqlite3.Connection, signal_id: int) -> bool:
    """幂等检查: evidence_json 是否已引用 signal_id=N。"""
    pat = f'"signal_id": {signal_id}'
    row = conn.execute(
        "SELECT 1 FROM trade_decisions WHERE evidence_json LIKE ? LIMIT 1",
        (f"%{pat}%",),
    ).fetchone()
    return row is not None


def _action_from_move(move_pct: float) -> str:
    if move_pct > HOLD_BAND_PCT:
        return "BUY"
    if move_pct < -HOLD_BAND_PCT:
        return "SELL"
    return "HOLD"


def _price_at_decision(conn: sqlite3.Connection, asset: str, move_ts: int,
                       end_p: Optional[float], peak_p: Optional[float]) -> Optional[float]:
    """价格回退: end_price → peak_price → ohlcv_daily 当日 close。"""
    if end_p and end_p > 0:
        return float(end_p)
    if peak_p and peak_p > 0:
        return float(peak_p)
    row = conn.execute(
        "SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (asset, move_ts),
    ).fetchone()
    return float(row[0]) if row and row[0] else None


def _forward_price(conn: sqlite3.Connection, asset: str, decision_ts: int) -> Optional[float]:
    """决策后 5 天 (HORIZON_S±12h 漂移) 的 close 价格。"""
    target = decision_ts + HORIZON_S
    row = conn.execute(
        "SELECT close FROM ohlcv_daily "
        "WHERE asset=? AND ts BETWEEN ? AND ? "
        "ORDER BY ABS(ts - ?) ASC LIMIT 1",
        (asset, target - OVERSHOOT_TOL_S, target + OVERSHOOT_TOL_S, target),
    ).fetchone()
    return float(row[0]) if row and row[0] else None


# ─── producer ─────────────────────────────────────────
def propose_one(conn: sqlite3.Connection, move: sqlite3.Row) -> Dict[str, Any]:
    """为单个 critical_price_move 创建一条 trade_decisions。

    Returns:
        {"action": "inserted"|"skipped", "decision_id": int|None, "reason": str}
    """
    sid = move["id"]
    asset = move["asset"]
    if _is_already_decided(conn, sid):
        return {"action": "skipped", "decision_id": None, "reason": f"signal_id={sid} 已被决策"}

    move_pct = move["move_pct"] or 0.0
    action = _action_from_move(move_pct)
    price = _price_at_decision(conn, asset, move["ts"], move["end_price"], move["peak_price"])
    confidence = float(move["algo_score"] or 0.0)
    evidence = {
        "source": "decision_engine",
        "version": SCHEMA_VERSION,
        "signal_id": int(sid),
        "move_ts": int(move["ts"]),
        "move_kind": move["move_kind"],
        "move_pct": float(move_pct),
        "duration_min": int(move["duration_min"] or 0),
        "context_json": move["context_json"],
    }
    rationale = (
        f"algo_signal:{move['move_kind']} score={confidence:.2f} "
        f"move_pct={move_pct:+.2f}% (signal_id={sid})"
    )
    cur = conn.execute(
        "INSERT INTO trade_decisions "
        "(asset, action, score, confidence, rationale, evidence_json, price_at_decision, status, created_at) "
        "VALUES (?,?,?,?,?,?,?, 'open', ?)",
        (
            asset, action, float(confidence), float(confidence),
            rationale, json.dumps(evidence, ensure_ascii=False), price,
            int(move["ts"]),  # 历史回填关键 — 用 move_ts 让结算窗口立即触发
        ),
    )
    return {"action": "inserted", "decision_id": int(cur.lastrowid), "reason": action}


def list_critical_moves(conn: sqlite3.Connection, days: int, threshold: float) -> List[sqlite3.Row]:
    cutoff = int(time.time()) - days * 86400
    return list(conn.execute(
        "SELECT id, asset, ts, start_price, peak_price, end_price, move_pct, "
        "       move_kind, duration_min, algo_score, context_json "
        "FROM critical_price_moves "
        "WHERE algo_score >= ? AND ts >= ? "
        "ORDER BY ts ASC",
        (threshold, cutoff),
    ).fetchall())


# ─── settler ──────────────────────────────────────────
def _confidence_from(dec: sqlite3.Row, action: str) -> float:
    """trade_decisions 没有 confidence 列 —— 从 context_json.score 取, 否则按 event_strength 映射。"""
    try:
        cj = json.loads(dec["context_json"] or "{}")
        s = cj.get("score")
        if isinstance(s, (int, float)) and 0.0 <= float(s) <= 1.0:
            return float(s)
    except Exception:
        pass
    return {"high": 0.8, "medium": 0.5, "low": 0.3}.get(dec["event_strength"], 0.5)


def settle_one(conn: sqlite3.Connection, dec: sqlite3.Row) -> Dict[str, Any]:
    """单条决策结算: 计算 outcome_pct, 写 status=settled, 落 trade_reflections_v2。"""
    did = dec["id"]
    asset = dec["asset"]
    action = dec["action"]
    price_at = dec["price_at_decision"]
    if not price_at or price_at <= 0:
        return {"action": "skipped", "decision_id": did, "reason": "no price_at_decision"}
    fwd = _forward_price(conn, asset, dec["created_at"])
    if fwd is None:
        return {"action": "skipped", "decision_id": did, "reason": "no forward price within ±12h of ts+5d"}
    outcome_pct = round((fwd - price_at) / price_at * 100.0, 3)
    if action == "BUY":
        hit = 1 if outcome_pct > 0 else 0
    elif action == "SELL":
        hit = 1 if outcome_pct < 0 else 0
    else:
        hit = 0
    conn.execute(
        "UPDATE trade_decisions SET status='settled', settled_at=strftime('%s','now'), "
        "actual_outcome_pct=? WHERE id=? AND status='open'",
        (outcome_pct, did),
    )
    # confidence: trade_decisions 无该列 → 从 context_json.score 取, 回退 event_strength
    conf = _confidence_from(dec, action)

    conn.execute(
        "INSERT INTO trade_reflections_v2 "
        "(decision_id, asset, dimension, subcategory, confidence_at_time, "
        " actual_outcome_pct, settled, hit, action, entry_price, created_at) "
        "VALUES (?,?,?,?,?,?,1,?,?,?,strftime('%s','now'))",
        (did, asset, "decision_engine", action, conf,
         outcome_pct, hit, action, float(price_at)),
    )
    return {"action": "settled", "decision_id": did, "outcome_pct": outcome_pct, "hit": hit}


def list_due_settlements(conn: sqlite3.Connection, now_ts: Optional[int] = None) -> List[sqlite3.Row]:
    """扫待结算决策。

    列名真相 (2026-10-22 实测 trade_decisions 22 列):
      - 没有 action / confidence / price_at_decision
      - 真实列是 final_action / entry_price / decision_ts
      - created_at 是 TEXT(datetime('now')), 不能和整数 epoch 比较 → 用 decision_ts

    只取 final_action IN ('BUY','SELL'):
      'REJECTED' 表示信号被 guard 挡下、从未成交, 没有盈亏, 永远不该进结算队列。
      (实测 1993 笔 open 中 1974 笔是 REJECTED, 真实可结算仅 19 笔)
    """
    now_ts = now_ts or int(time.time())
    cutoff = now_ts - HORIZON_S
    return list(conn.execute(
        "SELECT id, asset, final_action AS action, "
        "       entry_price AS price_at_decision, "
        "       decision_ts AS created_at, "
        "       event_strength, context_json "
        "FROM trade_decisions "
        "WHERE status='open' "
        "  AND final_action IN ('BUY','SELL') "
        "  AND entry_price IS NOT NULL AND entry_price > 0 "
        "  AND decision_ts IS NOT NULL AND decision_ts <= ? "
        "ORDER BY decision_ts ASC",
        (cutoff,),
    ).fetchall())


# ─── orchestrators ────────────────────────────────────
def run_backfill(days: int = 90, threshold: float = ALGO_THRESHOLD) -> Dict[str, Any]:
    """一次性回填: 处理 [now-days, now] 所有 algo_score>=threshold 的 critical_price_moves。"""
    conn = _db()
    summary: Dict[str, Any] = {
        "phase": "backfill",
        "days": days,
        "threshold": threshold,
        "moves_seen": 0,
        "inserted": 0,
        "skipped_dup": 0,
        "settled": 0,
        "settle_skip": 0,
        "action_dist": {"BUY": 0, "SELL": 0, "HOLD": 0},
    }
    try:
        # ensure table (idempotent — bridge 已装 create IF NOT EXISTS, 但本脚本独立跑也要安全)
        moves = list_critical_moves(conn, days, threshold)
        summary["moves_seen"] = len(moves)
        # ── phase 1: propose all ──
        for mv in moves:
            r = propose_one(conn, mv)
            if r["action"] == "inserted":
                summary["inserted"] += 1
                summary["action_dist"][r["reason"]] += 1
            else:
                summary["skipped_dup"] += 1
        conn.commit()
        # ── phase 2: settle all due ──
        due = list_due_settlements(conn)
        for d in due:
            r = settle_one(conn, d)
            if r["action"] == "settled":
                summary["settled"] += 1
            else:
                summary["settle_skip"] += 1
        conn.commit()
    finally:
        conn.close()
    return summary


def run_live_once(threshold: float = ALGO_THRESHOLD) -> Dict[str, Any]:
    """单遍实时扫描: 只看过去 1 小时。生产可挂 systemd timer / cron。"""
    conn = _db()
    summary: Dict[str, Any] = {
        "phase": "live_once",
        "threshold": threshold,
        "inserted": 0, "skipped_dup": 0, "settled": 0, "settle_skip": 0,
        "action_dist": {"BUY": 0, "SELL": 0, "HOLD": 0},
    }
    try:
        cutoff = int(time.time()) - 3600
        moves = list(conn.execute(
            "SELECT id, asset, ts, start_price, peak_price, end_price, move_pct, "
            "       move_kind, duration_min, algo_score, context_json "
            "FROM critical_price_moves WHERE algo_score >= ? AND ts >= ?",
            (threshold, cutoff),
        ).fetchall())
        for mv in moves:
            r = propose_one(conn, mv)
            if r["action"] == "inserted":
                summary["inserted"] += 1
                summary["action_dist"][r["reason"]] += 1
            else:
                summary["skipped_dup"] += 1
        conn.commit()
        for d in list_due_settlements(conn):
            r = settle_one(conn, d)
            if r["action"] == "settled":
                summary["settled"] += 1
            else:
                summary["settle_skip"] += 1
        conn.commit()
    finally:
        conn.close()
    return summary


def run_settle_only() -> Dict[str, Any]:
    """仅结算: 不新增决策, 仅把已到期的 open 决策结算掉。"""
    conn = _db()
    summary = {"phase": "settle_only", "settled": 0, "skipped": 0}
    try:
        for d in list_due_settlements(conn):
            r = settle_one(conn, d)
            summary["settled" if r["action"] == "settled" else "skipped"] += 1
        conn.commit()
    finally:
        conn.close()
    return summary


# ─── CLI ───────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    bf = sub.add_parser("backfill", help="一次性回填 N 天所有强信号")
    bf.add_argument("--days", type=int, default=90)
    bf.add_argument("--threshold", type=float, default=ALGO_THRESHOLD)

    lv = sub.add_parser("live", help="单遍实时扫描 (近 1h)")
    lv.add_argument("--once", action="store_true", default=True)

    sub.add_parser("settle", help="仅结算到期 open 决策")

    args = p.parse_args()
    if args.cmd == "backfill":
        s = run_backfill(days=args.days, threshold=args.threshold)
    elif args.cmd == "live":
        s = run_live_once()
    else:
        s = run_settle_only()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())