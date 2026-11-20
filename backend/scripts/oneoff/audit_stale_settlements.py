#!/usr/bin/env python3
"""结算价体检 / 清理 —— v386 P0 配套脚本。

背景
----
`db.price_at()` 的日线兜底分支此前用 `ORDER BY ts ASC` + 6 天回看窗口，
取到的是窗口里**最老**的一根日线。2026-11-16 两笔 WTI（horizon=20 分钟）
因 1m 线整段断档（11-15 00:00 ~ 11-16 07:00 无数据），被结算成
11-10 收盘 86.43 / 11-11 收盘 86.42，凭空造出 -3.17%/笔 的"亏损"，
并把胜率、权益曲线、signal_performance、反思样本一起污染。

本脚本做两件事：
1. 体检：对每条已结算仓位，用**修复后**的口径重新取到期价；
   取不到价（或与库里的 exit_price 偏差过大）→ 标记为可疑结算。
2. 清理（--apply）：把可疑结算的仓位退回未结算（settled=0），
   让引擎按正确口径重新结算（到期价确实不可得时会熔断成 ABANDONED，
   不计入胜率与权益），同时清掉它们写进 signal_performance 的绩效行。

用法
----
    python3 scripts/audit_stale_settlements.py --dry-run          # 只看（默认）
    python3 scripts/audit_stale_settlements.py --apply            # 真的改库（先自动备份）
    python3 scripts/audit_stale_settlements.py --db other.db --dry-run

安全
----
--apply 之前会把 DB 复制一份到 backend/backups/，不做无备份的原地修改。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src_python"))

import db # noqa: E402

DEFAULT_DB = BACKEND / "trident_event_bus.db"
BACKUP_DIR = BACKEND / "backups"

# exit_price 与"正确口径取到的到期价"偏差超过这个百分比 → 判定为可疑
PRICE_DEVIATION_PCT = 0.5


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _nearest_bar(conn: sqlite3.Connection, asset: str, ts: int):
    """返回 (表名, 时间戳, close)，用于说明"库里最近的一根行情到底离到期多久"。"""
    row = conn.execute(
        "SELECT ts, close FROM ohlcv_intraday WHERE asset=? AND interval='1m' "
        "ORDER BY ABS(ts-?) LIMIT 1", (asset, ts)).fetchone()
    if row:
        return ("ohlcv_intraday/1m", int(row[0]), float(row[1]))
    row = conn.execute(
        "SELECT ts, close FROM ohlcv_daily WHERE asset=? "
        "ORDER BY ABS(ts-?) LIMIT 1", (asset, ts)).fetchone()
    if row:
        return ("ohlcv_daily", int(row[0]), float(row[1]))
    return None


def audit(conn: sqlite3.Connection, limit: int = 0) -> list:
    trades = _rows(
        conn,
        """SELECT id, decision_id, asset, action, entry_price, entry_ts,
                  exit_price, exit_ts, horizon_minutes, pnl_pct, pnl_usdt,
                  verdict, settled
           FROM quick_sim_trades
           WHERE settled=1 AND exit_price IS NOT NULL
           ORDER BY exit_ts DESC""",
    )
    if limit:
        trades = trades[:limit]
    findings = []
    for tr in trades:
        asset = str(tr["asset"]).upper()
        exit_ts = int(tr["exit_ts"] or 0)
        entry_ts = int(tr["entry_ts"] or 0)
        due_ts = entry_ts + int(tr["horizon_minutes"] or 0) * 60
        exit_price = float(tr["exit_price"])
        # 结算价必须对应**到期时刻**，不是"哪天扫到就算哪天"
        expected = db.price_at(conn, asset, due_ts)
        near = _nearest_bar(conn, asset, due_ts)
        lag_sec = exit_ts - due_ts
        reasons = []
        if abs(lag_sec) > 3600:
            reasons.append(f"结算时刻比到期时刻晚 {lag_sec / 60:.0f} 分钟"
                           f"（horizon 只有 {int(tr['horizon_minutes'] or 0)} 分钟）")
        if expected is None:
            if near is None:
                reasons.append("到期时刻前后找不到任何行情（当天应有价）")
            else:
                lag_min = (due_ts - near[1]) / 60.0
                reasons.append(
                    f"修复后口径取不到到期价；最近一根 {near[0]} 在 {lag_min:+.0f} 分钟处"
                    f"（close={near[2]}）")
            reasons.append(f"库里的 exit_price={exit_price} 来路不明")
        else:
            dev = abs(exit_price - expected) / expected * 100
            if dev > PRICE_DEVIATION_PCT:
                reasons.append(
                    f"exit_price={exit_price} 与到期价 {expected} 偏差 {dev:.2f}%")
        if not reasons:
            continue
        findings.append({
            **tr,
            "due_ts": due_ts,
            "expected_price": expected,
            "nearest": near,
            "reasons": reasons,
        })
    return findings


def apply_void(conn: sqlite3.Connection, findings: list) -> int:
    """把可疑结算退回未结算，并清掉它们写进 signal_performance 的绩效行。"""
    now = int(time.time())
    voided = 0
    for f in findings:
        conn.execute(
            """UPDATE quick_sim_trades
                  SET settled=0, exit_price=NULL, exit_ts=NULL, pnl_pct=NULL,
                      pnl_usdt=NULL, verdict='', max_price=entry_price,
                      min_price=entry_price
                WHERE id=?""", (f["id"],))
        conn.execute(
            """UPDATE ai_decisions
                  SET settled=0, is_correct='', forward_pnl=NULL,
                      exit_price=NULL
                WHERE id=?""", (f["decision_id"],))
        conn.execute(
            "DELETE FROM signal_performance WHERE decision_id=?",
            (f["decision_id"],))
        voided += 1
    conn.commit()
    print(f"[VOID] 已退回 {voided} 条可疑结算（settled=0），"
          f"引擎下一轮会按正确口径重结或熔断为 ABANDONED；时间戳 {now}")
    return voided


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--limit", type=int, default=0)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="只体检（默认行为）")
    g.add_argument("--apply", action="store_true", help="退回可疑结算（先自动备份）")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"[ERR] DB 不存在: {path}")
        return 2
    if args.apply:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        bak = BACKUP_DIR / f"trident_event_bus.before_void_{time.strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(path, bak)
        print(f"[BACKUP] {bak}")

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    findings = audit(conn, args.limit)
    total_settled = conn.execute(
        "SELECT COUNT(*) FROM quick_sim_trades WHERE settled=1").fetchone()[0]

    print(f"\n=== 结算价体检 {path.name} ===")
    print(f"已结算仓位 {total_settled} 条，可疑 {len(findings)} 条")
    for f in findings:
        print(f"\n#{f['id']} {f['asset']} {f['action']} "
              f"entry={f['entry_price']} exit={f['exit_price']} "
              f"pnl={f['pnl_pct']}% verdict={f['verdict']}")
        for r in f["reasons"]:
            print(f"   · {r}")

    if args.apply and findings:
        apply_void(conn, findings)
    elif findings:
        print("\n[DRY-RUN] 未改库。确认后加 --apply 退回这些结算。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
