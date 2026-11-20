"""multi_horizon_settle — 多周期结算（v342）

背景（v342 判断层 #④）：
    AI 每条决策都带 `expected_horizon`（intraday / 1-3d / 1w+），但结算只有
    一个 5 天窗口（`decision_engine.HORIZON_S`）。
    → 日内判断被拿去用 5 天结算，"方向对、节奏错"会被判成"判断错"，
      这个错还会进反思、污染 E 码归因。

做法（纯增量）：
    · 新建 `decision_outcomes_horizon` 表，一条决策 × 5 个周期各一条记录
    · 主表 `trade_decisions.actual_outcome_pct` / `trade_reflections_v2` 的口径
      一个字不改（5 天仍是主口径），本模块只**另存**各周期读数
    · 同一决策同一周期只算一次（PK 幂等），价格取不到就如实记 status，不猜价
    · T+1 品种的日内周期不可执行 → 记 `not_executable`，不写假收益

周期与容差：
    15m / 1h / 4h / 1d 用自己的分钟线解析；5d 复用 `db.forward_price(settle_days=5)`，
    保证与主结算口径完全一致（否则两处数字会对不上）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

# (horizon, 秒, 取价容差秒) —— 5d 走主结算同一实现，容差写在这里只做记录
HORIZONS: Tuple[Tuple[str, int, int], ...] = (
    ("15m", 900, 900),
    ("1h", 3600, 1800),
    ("4h", 14400, 7200),
    ("1d", 86400, 12 * 3600),
    ("5d", 432000, 12 * 3600),
)

# 与 v310 P1 一致：|收益|>90% 必是取价异常，拒写
MAX_PLAUSIBLE_PCT = 90.0

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS decision_outcomes_horizon (
    decision_id INTEGER NOT NULL,
    horizon     TEXT    NOT NULL,
    horizon_sec INTEGER NOT NULL,
    asset       TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    entry_price REAL    NOT NULL,
    decision_ts REAL    NOT NULL,
    target_ts   INTEGER NOT NULL,
    fwd_price   REAL,
    fwd_ts      INTEGER,
    outcome_pct REAL,
    pnl_pct     REAL,
    hit         INTEGER,
    status      TEXT,      -- settled / no_price / not_executable / implausible
    note        TEXT,
    computed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (decision_id, horizon)
);
CREATE INDEX IF NOT EXISTS idx_doh_asset_horizon
    ON decision_outcomes_horizon(asset, horizon, decision_ts);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等建表（db.migrate 也会调用；单独调用便于测试）。"""
    conn.executescript(CREATE_SQL)
    conn.commit()


def _resolve_price(conn: sqlite3.Connection, asset: str, target_ts: int,
                   tolerance_s: int) -> Tuple[Optional[float], Optional[int]]:
    """取 target_ts 之后（容差内）的第一根价：分钟线优先，日线回退。"""
    sqls = (
        ("SELECT ts, close FROM ohlcv_intraday WHERE asset=? AND ts>=? AND ts<=? "
         "ORDER BY ts ASC LIMIT 50", (target_ts, target_ts + tolerance_s)),
        ("SELECT ts, close FROM ohlcv_daily WHERE asset=? AND ts>=? AND ts<=? "
         "ORDER BY ts ASC LIMIT 50", (target_ts, target_ts + tolerance_s)),
    )
    for sql, extra in sqls:
        try:
            rows = conn.execute(sql, (asset,) + extra).fetchall()
        except sqlite3.Error:
            continue
        # 逐根挑第一个可用价：窗口内若第一根是脏价（0/负/空），
        # 不能因此把整个周期记成 no_price —— 后面的干净价仍然有效。
        for row in rows or ():
            if not row or row[1] is None:
                continue
            try:
                price = float(row[1])
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price, int(row[0])
    return None, None


def _hit(action: str, pnl_pct: Optional[float]) -> int:
    """按**净收益**判输赢（与主结算同口径）。"""
    if pnl_pct is None:
        return 0
    if action == "BUY":
        return 1 if pnl_pct > 0 else 0
    if action == "SELL":
        return 1 if pnl_pct < 0 else 0
    return 0


def _upsert(conn, *, decision_id, horizon, horizon_sec, asset, action, entry_price,
            decision_ts, target_ts, fwd_price, fwd_ts, outcome_pct, pnl_pct,
            hit, status, note) -> None:
    conn.execute(
        """INSERT INTO decision_outcomes_horizon
           (decision_id, horizon, horizon_sec, asset, action, entry_price,
             decision_ts, target_ts, fwd_price, fwd_ts, outcome_pct, pnl_pct,
             hit, status, note, computed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(decision_id, horizon) DO UPDATE SET
             target_ts=excluded.target_ts, fwd_price=excluded.fwd_price,
             fwd_ts=excluded.fwd_ts, outcome_pct=excluded.outcome_pct,
             pnl_pct=excluded.pnl_pct, hit=excluded.hit,
             status=excluded.status, note=excluded.note,
             computed_at=excluded.computed_at""",
        (decision_id, horizon, int(horizon_sec), asset, action, float(entry_price),
         float(decision_ts), int(target_ts), fwd_price, fwd_ts, outcome_pct,
         pnl_pct, int(hit), status, note),
    )


def settle_horizons(conn: sqlite3.Connection, *, decision_id: int, asset: str,
                    action: str, entry_price: float, decision_ts: float,
                    now_ts: Optional[int] = None, commit: bool = False,
                    min_ts: Optional[int] = None) -> Dict[str, Any]:
    """给一条决策补齐各周期读数。未到期的周期直接跳过（不写空行）。"""
    now_ts = int(now_ts or time.time())
    asset = str(asset or "").strip().upper()
    action = str(action or "").strip().upper()
    entry_price = float(entry_price)
    decision_ts = float(decision_ts)
    if not asset or entry_price <= 0:
        return {"decision_id": decision_id, "written": 0, "reason": "bad input"}

    # T+1 品种的最早可平仓时点（与主结算同源）
    if min_ts is None:
        try:
            import cost_model as _cm
            min_ts = int(_cm.earliest_exit_ts(asset, int(decision_ts)))
        except Exception:
            min_ts = int(decision_ts)

    written = 0
    results: Dict[str, Any] = {}
    for horizon, horizon_sec, tol in HORIZONS:
        if horizon == "5d":
            # 与主结算严格同源（settle_days=5 → target = ts + 4d）
            target_ts = int(decision_ts) + 4 * 86400
            if target_ts > now_ts:
                continue
            try:
                import db as _db
                fwd = _db.forward_price(conn, asset, decision_ts, settle_days=5,
                                        min_ts=min_ts)
            except Exception:
                fwd = None
            fwd_ts = target_ts if fwd is not None else None
        else:
            target_ts = int(decision_ts) + horizon_sec
            if target_ts > now_ts:
                continue
            if target_ts < min_ts:
                _upsert(conn, decision_id=decision_id, horizon=horizon,
                        horizon_sec=horizon_sec, asset=asset, action=action,
                        entry_price=entry_price, decision_ts=decision_ts,
                        target_ts=target_ts, fwd_price=None, fwd_ts=None,
                        outcome_pct=None, pnl_pct=None, hit=0,
                        status="not_executable",
                        note="T+1 品种，周期早于最早可平仓时点")
                results[horizon] = {"status": "not_executable"}
                written += 1
                continue
            fwd, fwd_ts = _resolve_price(conn, asset, target_ts, tol)

        if fwd is None:
            _upsert(conn, decision_id=decision_id, horizon=horizon,
                    horizon_sec=horizon_sec, asset=asset, action=action,
                    entry_price=entry_price, decision_ts=decision_ts,
                    target_ts=target_ts, fwd_price=None, fwd_ts=None,
                    outcome_pct=None, pnl_pct=None, hit=0,
                    status="no_price", note="该周期内无可用行情")
            results[horizon] = {"status": "no_price"}
            written += 1
            continue

        raw = round((float(fwd) - entry_price) / entry_price * 100.0, 3)
        if abs(raw) > MAX_PLAUSIBLE_PCT:
            _upsert(conn, decision_id=decision_id, horizon=horizon,
                    horizon_sec=horizon_sec, asset=asset, action=action,
                    entry_price=entry_price, decision_ts=decision_ts,
                    target_ts=target_ts, fwd_price=fwd, fwd_ts=fwd_ts,
                    outcome_pct=raw, pnl_pct=None, hit=0,
                    status="implausible", note=f"|收益|>90% 视为取价异常: {raw:+.2f}%")
            results[horizon] = {"status": "implausible", "outcome_pct": raw}
            written += 1
            continue

        try:
            import cost_model as _cm2
            net = round(float(_cm2.apply_costs(raw, asset, action)), 3)
        except Exception:
            net = raw
        hit = _hit(action, net)
        _upsert(conn, decision_id=decision_id, horizon=horizon,
                horizon_sec=horizon_sec, asset=asset, action=action,
                entry_price=entry_price, decision_ts=decision_ts,
                target_ts=target_ts, fwd_price=fwd, fwd_ts=fwd_ts,
                outcome_pct=raw, pnl_pct=net, hit=hit, status="settled", note=None)
        results[horizon] = {"status": "settled", "outcome_pct": raw,
                            "pnl_pct": net, "hit": hit}
        written += 1

    if commit:
        conn.commit()
    return {"decision_id": decision_id, "written": written, "horizons": results}


def run_once(conn: sqlite3.Connection, now_ts: Optional[int] = None,
             limit: int = 200, commit: bool = True) -> Dict[str, Any]:
    """扫一批已成交决策，补齐未写过的周期。幂等：重复跑不会产生重复行。"""
    now_ts = int(now_ts or time.time())
    init_schema(conn)
    rows = conn.execute(
        """SELECT id, asset, final_action, entry_price, decision_ts
           FROM trade_decisions
           WHERE final_action IN ('BUY','SELL')
             AND entry_price IS NOT NULL AND entry_price > 0
             AND decision_ts IS NOT NULL AND decision_ts <= ?
           ORDER BY decision_ts DESC LIMIT ?""",
        (now_ts - 900, int(limit)),
    ).fetchall()
    # v482 第二遍：补「长周期」。
    # 第一遍只扫最近 limit 条（ORDER BY decision_ts DESC），而 1d/5d 要等到 T+1/T+4 才成熟。
    # 一条决策一旦掉出这个窗口，它的 1d/5d 行就**永远不会被写入** —— 白天决策多时必踩。
    # 这里从最老的往前补一批（只 bootstrap 不覆盖：幂等 upsert），30 天封顶。
    seen_ids = {int(r[0]) for r in rows}
    backfill_rows: List[Any] = []
    try:
        backfill_rows = conn.execute(
            """SELECT id, asset, final_action, entry_price, decision_ts
               FROM trade_decisions
               WHERE final_action IN ('BUY','SELL')
                 AND entry_price IS NOT NULL AND entry_price > 0
                 AND decision_ts IS NOT NULL AND decision_ts <= ?
                 AND decision_ts >= ?
               ORDER BY decision_ts ASC LIMIT ?""",
            (now_ts - 3600, now_ts - 30 * 86400, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        backfill_rows = []
    todo = list(rows) + [r for r in backfill_rows if int(r[0]) not in seen_ids]
    summary = {"decisions": len(todo), "written": 0, "settled": 0,
               "backfill": len(todo) - len(rows)}
    for r in todo:
        res = settle_horizons(
            conn, decision_id=int(r[0]), asset=r[1], action=r[2],
            entry_price=float(r[3]), decision_ts=float(r[4]), now_ts=now_ts,
            commit=False,
        )
        summary["written"] += res.get("written", 0)
        summary["settled"] += sum(
            1 for v in (res.get("horizons") or {}).values()
            if v.get("status") == "settled")
    # v342 第二件：把新读数翻成「方向错 / 节奏错」—— 判定失败不得阻断结算
    try:
        import horizon_reflection as _hr
        summary["reflection"] = _hr.run_once(conn, limit=limit, commit=False)
    except Exception as _hr_exc:
        summary["reflection"] = {"error": f"{type(_hr_exc).__name__}: {str(_hr_exc)[:80]}"}
    if commit:
        conn.commit()
    return summary


def read_horizons(conn: sqlite3.Connection, decision_id: int) -> List[Dict[str, Any]]:
    """读一条决策的各周期读数（供复盘/前端）。"""
    rows = conn.execute(
        """SELECT horizon, status, outcome_pct, pnl_pct, hit, target_ts, fwd_price, note
           FROM decision_outcomes_horizon WHERE decision_id=?
           ORDER BY horizon_sec ASC""",
        (decision_id,),
    ).fetchall()
    return [dict(r) if isinstance(r, sqlite3.Row) or hasattr(r, "keys") else
            {"horizon": r[0], "status": r[1], "outcome_pct": r[2], "pnl_pct": r[3],
             "hit": r[4], "target_ts": r[5], "fwd_price": r[6], "note": r[7]}
            for r in rows]


# ─── CLI ───────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    """v482: 独立入口 —— 让「多周期读数」能由 launchd 常驻跑，不必等主结算。

    为什么加这个入口：`run_once()` 此前只在 `decision_engine settle` 里被顺带调用，
    而没有任何定时任务跑那个命令 —— 实测 `decision_outcomes_horizon` 长期 0 行，
    15m/1h/4h 这些「当天就能读」的窗口等于没有。这里把它做成幂等、可限流、
    可只读演练的独立命令。
    """
    p = argparse.ArgumentParser(description="多周期结算（15m/1h/4h/1d/5d）")
    p.add_argument("--apply", action="store_true", help="真正写库（默认只演练）")
    p.add_argument("--limit", type=int, default=500, help="每轮最多扫多少条决策")
    p.add_argument("--decision-id", type=int, default=None, help="只结算指定决策")
    p.add_argument("--json", action="store_true", help="输出 JSON（默认也是 JSON）")
    args = p.parse_args(argv)

    import db as _db

    conn = _db.get_connection()
    try:
        if args.decision_id:
            row = conn.execute(
                "SELECT id, asset, final_action, entry_price, decision_ts"
                " FROM trade_decisions WHERE id=?", (args.decision_id,)).fetchone()
            if not row:
                print(json.dumps({"ok": False, "error": "decision_not_found",
                                  "decision_id": args.decision_id},
                                 ensure_ascii=False))
                return 2
            res = settle_horizons(conn,
                                  decision_id=int(row[0]), asset=row[1],
                                  action=row[2], entry_price=float(row[3]),
                                  decision_ts=float(row[4]),
                                  now_ts=int(time.time()), commit=args.apply)
            summary: Dict[str, Any] = {"decision_id": args.decision_id,
                                       "applied": args.apply, **res}
        else:
            summary = run_once(conn, limit=args.limit, commit=args.apply)
            summary["applied"] = args.apply
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
