#!/usr/bin/env python3
"""数据真实性审计 —— v396 P2。

目的
----
系统可以不确定、可以没样本，但**不许出现无法追溯来源的数字**。
本模块把"假数据"拆成可判定的规则，逐条机器核对，任何一条不通过都算事故。

规则（R 编号与 docs/DATA_AUTHENTICITY.md 一致）
-------------------------------------------
S1 结算价 vs 到期价：已结算仓位的 exit_price 必须落在到期时刻 ±5 分钟的行情上（偏差 ≤0.5%）
S2 结算时刻：|exit_ts - due_ts| 超过宽限期 → 这笔不是这个 horizon 的结果
S3 报价冻结：最近 window 内 tick 数够多但只有一个价 → 该时段行情不存在
S4 分钟线断档：1m 线相邻两根间隔超过阈值 → 缺口期间不得结算、不得建仓
S5 账实相符：Σ(quick_sim_trades.pnl_usdt) 必须等于最新 sim_equity_snapshots.realized_pnl
S6 成本口径：已结算行必须带 cost_bps，否则"扣费后是否为正"无法判定
S7 悬空未结：到期已超宽限期但 settled=0（既没结算也没废弃）→ 统计样本被时间稀释

用法
----
    python3 src_python/data_authenticity.py                 # 打印体检结果
    python3 src_python/data_authenticity.py --write         # 写 reports/health/ 下的日报
    python3 src_python/data_authenticity.py --json          # 机器可读
    python3 src_python/data_authenticity.py --fail-on-bad   # 有违规时退出码 1（CI/cron 用）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import db
import config

SRC = Path(__file__).resolve().parent
BACKEND = SRC.parent
REPORTS = BACKEND / "reports" / "health"

# S1 容差
SETTLE_PRICE_DEVIATION_PCT = 0.5
# S3 冻结阈值
FROZEN_WINDOW_SEC = int(getattr(config, "QUOTE_FROZEN_WINDOW_MINUTES", 60)) * 60
FROZEN_MIN_TICKS = int(getattr(config, "QUOTE_FROZEN_MIN_TICKS", 20))
# S4 断档阈值（分钟）
GAP_THRESHOLD_MINUTES = 30


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _grace_sec() -> int:
    return int(getattr(config, "QUICK_SIM_SETTLE_GRACE_MINUTES", 60)) * 60


# ─────────────────────────── 结算类规则 ───────────────────────────


def check_settle_price(conn: sqlite3.Connection, limit: int = 200) -> List[Dict[str, Any]]:
    """S1 + S2：结算价与结算时刻是否对应「到期那一刻」。"""
    out: List[Dict[str, Any]] = []
    rows = _rows(
        conn,
        """SELECT id, decision_id, asset, action, entry_price, entry_ts,
                  exit_price, exit_ts, horizon_minutes, pnl_pct, verdict
           FROM quick_sim_trades
           WHERE settled=1 AND exit_price IS NOT NULL
           ORDER BY id DESC LIMIT ?""", (limit,))
    grace = _grace_sec()
    for r in rows:
        asset = str(r["asset"]).upper()
        due_ts = int(r["entry_ts"] or 0) + int(r["horizon_minutes"] or 0) * 60
        exit_ts = int(r["exit_ts"] or 0)
        exit_price = float(r["exit_price"])
        expected = db.price_at(conn, asset, due_ts)
        lag = exit_ts - due_ts
        if abs(lag) > grace:
            out.append({
                "rule": "S2", "id": r["id"], "asset": asset,
                "detail": f"结算时刻比到期晚 {lag/60:.0f} 分钟（horizon {int(r['horizon_minutes'] or 0)} 分钟）",
            })
            continue
        if expected is None:
            out.append({
                "rule": "S1", "id": r["id"], "asset": asset,
                "detail": f"到期时刻取不到行情，exit_price={exit_price} 无来源",
            })
            continue
        dev = abs(exit_price - expected) / expected * 100
        if dev > SETTLE_PRICE_DEVIATION_PCT:
            out.append({
                "rule": "S1", "id": r["id"], "asset": asset,
                "detail": f"exit_price={exit_price} vs 到期价 {expected}（偏差 {dev:.2f}%）",
            })
    return out


def check_dangling_open(conn: sqlite3.Connection, now_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """S7：到期超过宽限期却还挂着未结算的仓位。"""
    now = int(now_ts if now_ts is not None else time.time())
    grace = _grace_sec()
    rows = _rows(
        conn,
        """SELECT id, asset, action, entry_ts, horizon_minutes
           FROM quick_sim_trades
           WHERE settled=0
             AND entry_ts + horizon_minutes*60 + ? <= ?
           ORDER BY entry_ts ASC""", (grace, now))
    return [{
        "rule": "S7", "id": r["id"], "asset": r["asset"],
        "detail": f"到期已超宽限期 {(now - (int(r['entry_ts']) + int(r['horizon_minutes'] or 0)*60))/60:.0f} 分钟仍在挂单",
    } for r in rows]


def check_cost_discipline(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """S6：已结算行必须记成本，否则无法判断「扣费后是否为正」。"""
    rows = _rows(
        conn,
        """SELECT id, asset, cost_bps, pnl_pct FROM quick_sim_trades
           WHERE settled=1 AND pnl_pct IS NOT NULL AND COALESCE(cost_bps, 0) = 0
           ORDER BY id DESC LIMIT 500""")
    return [{
        "rule": "S6", "id": r["id"], "asset": r["asset"],
        "detail": "cost_bps=0：这笔的净收益无法与毛收益区分",
    } for r in rows]


def check_account_reconciliation(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """S5：账实相符（结算账本 vs 净值快照）。"""
    total = conn.execute(
        "SELECT COALESCE(SUM(pnl_usdt), 0) FROM quick_sim_trades WHERE settled=1"
    ).fetchone()[0] or 0.0
    last = conn.execute(
        "SELECT realized_pnl FROM sim_equity_snapshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if last is None:
        return []
    diff = abs(float(total) - float(last[0]))
    if diff > 0.01:
        return [{
            "rule": "S5", "id": 0, "asset": "ACCOUNT",
            "detail": f"账本 Σpnl={total:.2f} vs 净值快照={float(last[0]):.2f}（差 {diff:.2f}）",
        }]
    return []


# ─────────────────────────── 行情类规则 ───────────────────────────


def check_frozen_quotes(conn: sqlite3.Connection, now_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """S3：报价冻结（窗口内 tick 数够多但只有一个价）。"""
    now = int(now_ts if now_ts is not None else time.time())
    out = []
    assets = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM market_ticks WHERE ts>=?",
        (now - FROZEN_WINDOW_SEC,)).fetchall()]
    for asset in assets:
        qh = db.quote_health(conn, asset, now_ts=now,
                             window_sec=FROZEN_WINDOW_SEC, min_ticks=FROZEN_MIN_TICKS)
        if not qh or not qh.get("frozen"):
            continue
        out.append({
            "rule": "S3", "id": 0, "asset": asset,
            "detail": (f"{qh['window_sec']//60} 分钟内 {qh['n_ticks']} 笔 tick 只有 1 个价"
                       f"（最后一笔距今 {qh['age_sec']//60} 分钟）"),
        })
    return out


def check_intraday_gaps(conn: sqlite3.Connection, now_ts: Optional[int] = None,
                        lookback_sec: int = 86400,
                        threshold_min: int = GAP_THRESHOLD_MINUTES) -> List[Dict[str, Any]]:
    """S4：1m 线断档。只看最近 lookback 内、且当日有数据的品种。"""
    now = int(now_ts if now_ts is not None else time.time())
    lo = now - int(lookback_sec)
    out: List[Dict[str, Any]] = []
    # 只对"会拿去建仓/结算"的品种硬性判定；股票盘后休市天然有缺口，
    # 一律算违规会制造警报疲劳，最终等于没有告警。
    tradable = set(getattr(config, "SIM_ALLOWED_ASSETS", ("XAU", "WTI")))
    assets = [r[0] for r in conn.execute(
        "SELECT DISTINCT asset FROM ohlcv_intraday WHERE interval='1m' AND ts>=? "
        "ORDER BY asset",
        (lo,)).fetchall() if str(r[0]).upper() in tradable]
    for asset in assets:
        rows = conn.execute(
            "SELECT ts FROM ohlcv_intraday WHERE asset=? AND interval='1m' "
            "AND ts>=? AND ts<=? ORDER BY ts ASC", (asset, lo, now)).fetchall()
        if len(rows) < 2:
            out.append({'rule': 'S4', 'id': 0, 'asset': asset,
                        'detail': f'最近 {lookback_sec//3600}h 只有 {len(rows)} 根 1m 线'})
            continue
        prev = int(rows[0][0])
        worst = 0
        worst_at = 0
        for (ts,) in rows[1:]:
            gap = (int(ts) - prev) / 60.0
            if gap > worst:
                worst, worst_at = gap, prev
            prev = int(ts)
        if worst >= threshold_min:
            out.append({
                'rule': 'S4', 'id': 0, 'asset': asset,
                'detail': (f'最长断档 {worst:.0f} 分钟，起点 '
                           f'{time.strftime("%m-%d %H:%M", time.localtime(worst_at + 8*3600))}'),
            })
    return out


# ─────────────────────────── 汇总 ───────────────────────────


def run_all(conn: sqlite3.Connection, now_ts: Optional[int] = None) -> Dict[str, List[Dict[str, Any]]]:
    grace_note = f"宽限期 {int(getattr(config, 'QUICK_SIM_SETTLE_GRACE_MINUTES', 60))} 分钟"
    return {
        "S1/S2 结算价与时刻": check_settle_price(conn),
        "S3 报价冻结": check_frozen_quotes(conn, now_ts),
        "S4 分钟线断档": check_intraday_gaps(conn, now_ts),
        "S5 账实相符": check_account_reconciliation(conn),
        "S6 成本口径": check_cost_discipline(conn),
        "S7 悬空未结（" + grace_note + "）": check_dangling_open(conn, now_ts),
    }


def _render(results: Dict[str, List[Dict[str, Any]]]) -> str:
    total = sum(len(v) for v in results.values())
    lines = ["# 数据真实性体检", "", f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 违规总数：**{total}**", ""]
    for name, items in results.items():
        lines.append(f"## {name}：{len(items)}")
        if not items:
            lines.append("- 通过")
        for it in items[:20]:
            lines.append(f"- [{it['rule']}] #{it['id']} {it['asset']} — {it['detail']}")
        if len(items) > 20:
            lines.append(f"- （另有 {len(items) - 20} 条省略）")
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="数据真实性审计")
    ap.add_argument("--db", default=None)
    ap.add_argument("--write", action="store_true", help="写 reports/health/data_authenticity_YYYY-MM-DD.md")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--fail-on-bad", action="store_true", help="有违规时退出码 1")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db or db.get_db_path(), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    results = run_all(conn)
    total = sum(len(v) for v in results.values())

    if args.json:
        print(json.dumps({k: v for k, v in results.items()}, ensure_ascii=False, indent=2))
    else:
        text = _render(results)
        print(text)
        if args.write:
            REPORTS.mkdir(parents=True, exist_ok=True)
            path = REPORTS / f"data_authenticity_{time.strftime('%Y-%m-%d')}.md"
            path.write_text(text + "\n", encoding="utf-8")
            print(f"[WRITE] {path}")
    conn.close()
    if args.fail_on_bad and total:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
