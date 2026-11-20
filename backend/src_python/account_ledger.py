#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账户唯一口径 —— 按市价重估、按笔计费、可对账（v476）。

为什么需要这个模块（2026-11-19 实测）：
    同一套系统里有三本互不相认的账：
      · `t2_account`（cash / equity / realized）只在平仓时加减毛利，
        从不按市价重估；4 笔 5,000 USDT 的持仓浮亏 4.35 USDT，权益里看不到。
      · `sim_equity_snapshots` 由 `sim_equity.py` 从 `quick_sim_trades`
        单独累加，是另一条 20/40 分钟快打轨道，和 T2 完全不通。
      · `t2_positions` 里有 1 笔平仓 `realized_pnl` 为 NULL，账上加不进去。
    于是"账户现在值多少"有三个答案：10030.87 / 9997.36 / 10000-4.35。

本模块给出唯一公式（所有数字都可从库里的行重新算出来，不依赖缓存）：

    现金 cash      = 起始本金 + Σ已平仓毛利 - Σ已入账手续费
    浮动盈亏 upnl  = Σ(标记价 - 入场价) × 数量 × 方向
    权益 equity    = cash + upnl - 未入账的开仓手续费（权责发生制）
    已实现 realized= Σ已平仓毛利 - Σ已入账手续费

口径要点：
    · 标记价只取 `market_ticks` 最新一笔；没有真实价就不估值（绝不编价）。
    · 手续费按 `t2_account.fee_bps`（默认 8bps = 0.08%/边）双边计。
    · 开仓手续费在开仓时入账（`book_entry_fee`），平仓手续费在平仓时入账
      （`trident2.store.close_position` 已改为计费）。
    · 历史行（p0 重置那批）没有手续费字段，`--rebuild --backfill-closed`
      可补算，默认只补未入账的开仓手续费并给出差额告警。

命令：
    python3 account_ledger.py --show
    python3 account_ledger.py --reconcile
    python3 account_ledger.py --rebuild --apply
    python3 account_ledger.py --rebuild --apply --backfill-closed
    python3 account_ledger.py --snapshot --apply
    python3 account_ledger.py --repair-runs --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

MARK_STALE_S = 300
_ACCOUNT_TABLE = "t2_account"


# ── 基础读取 ────────────────────────────────────────────────────────────
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def initial_capital() -> float:
    """起始本金。优先 PAPER_INITIAL_EQUITY_USDT，其次 SIM_INITIAL_CAPITAL。"""
    for name in ("PAPER_INITIAL_EQUITY_USDT", "SIM_INITIAL_CAPITAL"):
        raw = os.getenv(name)
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return 10000.0


def connect(path: Optional[str] = None, readonly: bool = False) -> sqlite3.Connection:
    import db  # 延迟导入，便于单元测试注入 TRIDENT_DB_PATH

    if readonly and path:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(path or db.get_db_path(), timeout=20.0)
        conn.execute("PRAGMA busy_timeout=20000")
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _direction(side: str) -> int:
    return -1 if str(side or "").lower() in ("short", "sell") else 1


def _num(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mark(conn: sqlite3.Connection, asset: str) -> Dict[str, Any]:
    """最新真实价：只认 market_ticks；退到 1m/5m 收盘也标注来源。"""
    row = conn.execute(
        "SELECT price, ts FROM market_ticks WHERE symbol=? ORDER BY ts DESC, rowid DESC LIMIT 1",
        (str(asset).upper(),),
    ).fetchone()
    if row and row["price"]:
        return {"price": float(row["price"]), "ts": int(row["ts"]), "source": "tick"}
    if _table_exists(conn, "kline_bars"):
        for tf in ("1m", "5m"):
            row = conn.execute(
                "SELECT close, ts FROM kline_bars WHERE symbol=? AND timeframe=? "
                "ORDER BY ts DESC, rowid DESC LIMIT 1",
                (str(asset).upper(), tf),
            ).fetchone()
            if row and row["close"]:
                ts = int(row["ts"])
                if ts > 10_000_000_000:
                    ts = ts // 1000
                return {"price": float(row["close"]), "ts": ts, "source": tf}
    return {"price": None, "ts": None, "source": "none"}


# ── 唯一口径 ────────────────────────────────────────────────────────────
def canonical(conn: sqlite3.Connection, now: Optional[int] = None) -> Dict[str, Any]:
    """从 positions 行重新算出账户，不信任 t2_account 缓存。

    对最小测试库/老库保持兼容：表或列缺失时按 0 处理，不抛异常。
    """
    now = int(now or time.time())
    acc = {}
    if _table_exists(conn, _ACCOUNT_TABLE):
        row = conn.execute(f"SELECT * FROM {_ACCOUNT_TABLE} ORDER BY id DESC LIMIT 1").fetchone()
        acc = dict(row) if row else {}
    bps = _num(acc.get("fee_bps")) or 8.0
    initial = initial_capital()

    closed_rows: List[Dict[str, Any]] = []
    open_rows: List[Dict[str, Any]] = []
    live_rows: List[Dict[str, Any]] = []
    if _table_exists(conn, "t2_positions"):
        closed_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM t2_positions WHERE status='closed' ORDER BY id")]
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM t2_positions WHERE status='open' ORDER BY id")]
        # v477：实盘/实盘演练仓位不进模拟账（否则真钱和纸钱混在一个权益里）
        live_sub = {"live", "live_dry", "live_real"}
        live_rows = [r for r in closed_rows + open_rows
                     if str(r.get("subaccount") or "").strip() in live_sub]
        if live_rows:
            _live_ids = {id(r) for r in live_rows}
            closed_rows = [r for r in closed_rows if id(r) not in _live_ids]
            open_rows = [r for r in open_rows if id(r) not in _live_ids]
    # 空账本 + 已有权益：把账上权益当起始本金（测试库/换过本金的库）
    if not closed_rows and not open_rows and _num(acc.get("equity")) > 0:
        initial = _num(acc.get("equity"))

    closed_net = 0.0
    realized_gross = 0.0
    fees_booked = 0.0
    pnl_null: List[int] = []
    closed_paid: List[int] = []
    for r in closed_rows:
        fee = _num(r.get("fee_paid"))
        fees_booked += fee
        if r.get("realized_pnl") is None:
            pnl_null.append(int(r["id"]))
            continue
        closed_net += _num(r.get("realized_pnl"))
        realized_gross += _num(r.get("realized_pnl")) + fee
        if fee <= 0:
            closed_paid.append(int(r["id"]))

    positions: List[Dict[str, Any]] = []
    upnl = 0.0
    gross_exposure = 0.0
    unbooked_entry_fee = 0.0
    missing_marks: List[str] = []
    stale_marks: List[str] = []
    for p in open_rows:
        mark = _mark(conn, p.get("asset"))
        d = _direction(p.get("side"))
        qty = _num(p.get("qty"))
        entry = _num(p.get("entry_price"))
        notional = abs(entry * qty)
        gross_exposure += notional
        fee_paid = _num(p.get("fee_paid"))
        if fee_paid <= 0:
            entry_fee = notional * bps / 10_000.0
            unbooked_entry_fee += entry_fee
        else:
            entry_fee = fee_paid
        last = mark["price"]
        age = (now - int(mark["ts"])) if mark.get("ts") else None
        if last is None:
            missing_marks.append(str(p.get("asset")))
            p_upnl = 0.0
        else:
            p_upnl = (last - entry) * qty * d
            upnl += p_upnl
            if age is not None and age > MARK_STALE_S:
                stale_marks.append(str(p.get("asset")))
        positions.append({
            "id": int(p["id"]), "asset": p.get("asset"), "side": p.get("side"), "direction": d,
            "qty": round(qty, 8), "entry_price": entry, "notional": round(notional, 2),
            "mark": last, "mark_ts": mark.get("ts"), "mark_source": mark["source"],
            "mark_age_s": age, "mark_stale": bool(age is not None and age > MARK_STALE_S),
            "unrealized": round(p_upnl, 4),
            "entry_fee": round(entry_fee, 4), "fee_paid": round(fee_paid, 4),
            "entry_ts": int(p.get("entry_ts") or 0),
            "stop_price": p.get("stop_price"),
            "age_min": round((now - int(p.get("entry_ts") or now)) / 60.0, 1),
        })

    # 口径：开仓费在开仓时不计现金，而是作为“持仓占用费”在现金里扣；
    # 平仓时 realized_pnl 已是扣掉双边费的净额（见 store.close_position）。
    open_fees = sum(x["entry_fee"] for x in positions)
    cash = initial + closed_net - open_fees
    reported_realized = closed_net
    equity = cash + upnl

    cap_pct = _env_float("MAX_GROSS_LEVERAGE_PCT", 100.0)
    leverage_pct = (gross_exposure / equity * 100.0) if equity > 0 else 0.0

    return {
        "ts": now,
        "initial_capital": round(initial, 2),
        "fee_bps": bps,
        "cash": round(cash, 4),
        "realized": round(reported_realized, 4),
        "realized_gross": round(realized_gross, 4),
        "fees_booked": round(fees_booked, 4),
        "unbooked_entry_fee": round(unbooked_entry_fee, 4),
        "unrealized": round(upnl, 4),
        "equity": round(equity, 4),
        "gross_exposure": round(gross_exposure, 2),
        "leverage_pct": round(leverage_pct, 1),
        "leverage_cap_pct": cap_pct,
        "n_open": len(open_rows),
        "n_closed": len(closed_rows),
        "n_live_segregated": len(live_rows),
        "positions": positions,
        "stored": {
            "cash": round(_num(acc.get("cash")), 4),
            "equity": round(_num(acc.get("equity")), 4),
            "realized": round(_num(acc.get("realized_pnl")), 4),
            "updated_at": acc.get("updated_at"),
        },
        "pnl_null_ids": pnl_null,
        "closed_without_fee_ids": closed_paid,
        "missing_marks": missing_marks,
        "stale_marks": stale_marks,
    }


def revalue_equity(conn: sqlite3.Connection, now: Optional[int] = None) -> Dict[str, Any]:
    """把 canonical 结果写回 t2_account（缓存），缺价时不写 equity。"""
    c = canonical(conn, now)
    if not _table_exists(conn, _ACCOUNT_TABLE):
        return c
    ts = int(time.time())
    if c["missing_marks"]:
        conn.execute(
            f"UPDATE {_ACCOUNT_TABLE} SET cash=?, realized_pnl=?, updated_at=? WHERE id=1",
            (c["cash"], c["realized"], ts),
        )
    else:
        conn.execute(
            f"UPDATE {_ACCOUNT_TABLE} SET cash=?, equity=?, realized_pnl=?, updated_at=? WHERE id=1",
            (c["cash"], c["equity"], c["realized"], ts),
        )
    conn.commit()
    return c


def book_entry_fee(conn: sqlite3.Connection, position_id: int) -> float:
    """开仓手续费入账（幂等：已入账直接返回 0）。"""
    if "fee_paid" not in _columns(conn, "t2_positions"):
        return 0.0
    row = conn.execute(
        "SELECT id, asset, qty, entry_price, fee_paid FROM t2_positions WHERE id=?",
        (int(position_id),),
    ).fetchone()
    if row is None:
        return 0.0
    if _num(row["fee_paid"]) > 0:
        return 0.0
    acc = conn.execute(f"SELECT fee_bps FROM {_ACCOUNT_TABLE} WHERE id=1").fetchone()
    bps = _num(acc["fee_bps"]) if acc else 8.0
    fee = abs(_num(row["entry_price"]) * _num(row["qty"])) * (bps or 8.0) / 10_000.0
    conn.execute("UPDATE t2_positions SET fee_paid=? WHERE id=?", (fee, int(position_id)))
    conn.commit()
    revalue_equity(conn)
    return fee


# ── 对账 ────────────────────────────────────────────────────────────────
def reconcile(conn: sqlite3.Connection, now: Optional[int] = None) -> List[Dict[str, Any]]:
    c = canonical(conn, now)
    issues: List[Dict[str, Any]] = []

    def add(code: str, severity: str, detail: str) -> None:
        issues.append({"code": code, "severity": severity, "detail": detail})

    # 空账本（无持仓、无平仓）不比对缓存：账上权益即起始本金，没有可比对象
    if c["n_open"] == 0 and c["n_closed"] == 0:
        pass
    else:
        # R1 已实现盈亏缓存 vs 重算
        if abs(c["stored"]["realized"] - c["realized"]) > 0.01:
            add("R1_REALIZED_DRIFT", "block",
                f"账上 realized={c['stored']['realized']:.2f}，重算={c['realized']:.2f}"
                f"（差 {c['stored']['realized'] - c['realized']:+.2f}）")
        # R2 现金缓存 vs 重算
        if abs(c["stored"]["cash"] - c["cash"]) > 0.01:
            add("R2_CASH_DRIFT", "block",
                f"账上 cash={c['stored']['cash']:.2f}，重算={c['cash']:.2f}"
                f"（差 {c['stored']['cash'] - c['cash']:+.2f}）")
        # R3 权益缓存 vs 市价重估
        gap = c["stored"]["equity"] - c["equity"]
        tol = max(0.5, abs(c["equity"]) * 0.002)
        if abs(gap) > tol:
            add("R3_EQUITY_STALE", "block",
                f"账上 equity={c['stored']['equity']:.2f}，市价重估={c['equity']:.2f}"
                f"（差 {gap:+.2f}，超过 {tol:.2f}）")
    # R4 平仓行缺盈亏
    if c["pnl_null_ids"]:
        add("R4_PNL_NULL", "block",
            f"已平仓但 realized_pnl 为空：{c['pnl_null_ids']}（这笔在账上等于没发生）")
    # R5 持仓无价 / 价太旧
    if c["missing_marks"]:
        add("R5_MARK_MISSING", "block",
            f"持仓没有真实行情：{c['missing_marks']}，无法估值")
    if c["stale_marks"]:
        add("R5_MARK_STALE", "warn",
            f"行情超过 {MARK_STALE_S}s：{c['stale_marks']}，标记价可能不是当前价")
    # R12 实盘仓位被隔离出模拟账（提示：实盘需要独立账户口径）
    if c.get("n_live_segregated"):
        add("R12_LIVE_SEGREGATED", "warn",
            f"{c['n_live_segregated']} 笔实盘仓位未计入模拟账，需独立账户口径")
    # R6 重复 RUNNING run
    if _table_exists(conn, "paper_trading_runs"):
        runs = conn.execute(
            "SELECT id, strategy_id, spec_version, started_at FROM paper_trading_runs "
            "WHERE status='RUNNING' ORDER BY started_at DESC, id DESC"
        ).fetchall()
        if len(runs) > 1:
            add("R6_DUP_RUNS", "block",
                f"{len(runs)} 条 RUNNING run 同时存在：{[int(r['id']) for r in runs]}"
                "（成交会被重复记账，先跑 --repair-runs）")
    # R7 敞口超限
    if c["equity"] > 0 and c["leverage_pct"] > c["leverage_cap_pct"] + 0.1:
        add("R7_LEVERAGE", "block",
            f"总名义 {c['gross_exposure']:.0f} = 权益的 {c['leverage_pct']:.0f}%，"
            f"超过上限 {c['leverage_cap_pct']:.0f}%：只能减仓，不能开新仓")
    # R8 未入账的开仓手续费
    if c["unbooked_entry_fee"] > 0.01:
        add("R8_FEE_UNBOOKED", "warn",
            f"未入账开仓手续费 {c['unbooked_entry_fee']:.2f} USDT（--rebuild --apply 可补）")
    # R9 历史平仓没手续费
    if c["closed_without_fee_ids"]:
        add("R9_FEE_HISTORY", "warn",
            f"{len(c['closed_without_fee_ids'])} 笔历史平仓没记手续费："
            f"{c['closed_without_fee_ids']}（历史净值偏高，别据此算胜率）")
    # R10 两轨并存
    if _table_exists(conn, "quick_sim_trades") and _table_exists(conn, "sim_equity_snapshots"):
        qs = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdt),0) FROM quick_sim_trades WHERE settled=1"
        ).fetchone()[0]
        snap = conn.execute(
            "SELECT realized_pnl FROM sim_equity_snapshots ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if snap is not None and abs(_num(qs) - _num(snap[0])) > 0.01:
            add("R10_CROSS_TRACK", "warn",
                f"quick_sim 累计 {_num(qs):.2f} vs 其净值快照 {_num(snap[0]):.2f}（另一条轨道，不并入 T2）")
        if qs:
            n_qs = conn.execute("SELECT COUNT(*) FROM quick_sim_trades WHERE settled=1").fetchone()[0]
            add("R10_TWO_TRACKS", "warn",
                f"quick_sim 另有 {_num(qs):+.2f} USDT（{n_qs} 笔）；"
                f"T2 权益 {c['equity']:.2f} + 该轨 {_num(qs):+.2f} = 合计 {c['equity'] + _num(qs):.2f} ——"
                "对外只报这个合计，不要把两套权益相加当收益")
    # R11 持仓无止损
    no_stop = [p["asset"] for p in c["positions"] if not p["stop_price"]]
    if no_stop:
        add("R11_NO_STOP", "warn", f"持仓没有止损：{no_stop}（单笔按 2% 反向估算）")
    return issues


# ── 修复 ────────────────────────────────────────────────────────────────
def rebuild(conn: sqlite3.Connection, apply: bool = False,
            backfill_closed: bool = False) -> Dict[str, Any]:
    """按 positions 重算并写回账户；可补历史手续费。"""
    before = canonical(conn)
    bps = before["fee_bps"]

    # 平仓行 realized_pnl 为空：用入场/出场价补算（有 exit_price 才补，绝不猜）
    if apply and before["pnl_null_ids"]:
        for pid in before["pnl_null_ids"]:
            row = conn.execute(
                "SELECT entry_price, exit_price, qty, side, fee_paid FROM t2_positions WHERE id=?",
                (int(pid),),
            ).fetchone()
            if not row or row["exit_price"] is None:
                continue
            qty = _num(row["qty"])
            entry = _num(row["entry_price"])
            exit_p = _num(row["exit_price"])
            d = _direction(row["side"])
            gross = (exit_p - entry) * qty * d
            exit_fee = abs(exit_p * qty) * bps / 10_000.0
            fee_before = _num(row["fee_paid"])
            if fee_before <= 0:
                # 开仓费也没入账：双边一起补，避免和后面 --backfill-closed 反复改
                fee_total = (abs(entry * qty) + abs(exit_p * qty)) * bps / 10_000.0
            else:
                fee_total = fee_before + exit_fee
            net = gross - fee_total
            conn.execute(
                "UPDATE t2_positions SET realized_pnl=?, gross_pnl=?, fee_paid=?, "
                "note=COALESCE(note,'')||' |null_pnl_fix_v476' WHERE id=?",
                (net, gross, fee_total, int(pid)),
            )

    if backfill_closed:
        for pid in before["closed_without_fee_ids"]:
            row = conn.execute(
                "SELECT entry_price, exit_price, qty, realized_pnl, fee_paid FROM t2_positions WHERE id=?",
                (int(pid),),
            ).fetchone()
            if not row or _num(row["fee_paid"]) > 0:
                # 已在 NULL 修复阶段补过费的行跳过，避免二次扣费
                continue
            notional = abs(_num(row["entry_price"]) * _num(row["qty"]))
            exit_notional = abs(_num(row["exit_price"]) * _num(row["qty"]))
            fee = (notional + exit_notional) * bps / 10_000.0
            # 历史上 realized_pnl 记的是毛利（没扣费）：补费同时把净盈亏降下来，
            # 否则 fee_paid 有值、realized 仍是毛利，等于手续费白记。
            net = _num(row["realized_pnl"]) - fee
            if apply:
                conn.execute(
                    "UPDATE t2_positions SET realized_pnl=?, fee_paid=?, "
                    "gross_pnl=COALESCE(gross_pnl, ?), "
                    "note=COALESCE(note,'')||' |fee_backfill_v476' WHERE id=?",
                    (net, fee, net + fee, int(pid)),
                )
    # 未入账的开仓手续费
    for p in before["positions"]:
        if p["fee_paid"] <= 0:
            if apply:
                conn.execute("UPDATE t2_positions SET fee_paid=? WHERE id=?",
                             (p["entry_fee"], p["id"]))

    if apply:
        conn.commit()
    after = canonical(conn)
    if apply:
        conn.execute(
            f"UPDATE {_ACCOUNT_TABLE} SET cash=?, equity=?, realized_pnl=?, updated_at=? WHERE id=1",
            (after["cash"], after["equity"], after["realized"], int(time.time())),
        )
        conn.commit()
        after = canonical(conn)
    return {
        "applied": apply, "backfill_closed": backfill_closed,
        "planned": {
            "null_pnl_ids": before["pnl_null_ids"],
            "unbooked_entry_fee": before["unbooked_entry_fee"],
            "backfill_fee_ids": before["closed_without_fee_ids"],
        },
        "before": {k: before[k] for k in
                   ("cash", "equity", "realized", "unrealized", "gross_exposure",
                    "leverage_pct", "n_open", "n_closed", "fees_booked")},
        "after": {k: after[k] for k in
                  ("cash", "equity", "realized", "unrealized", "gross_exposure",
                   "leverage_pct", "n_open", "n_closed", "fees_booked")},
    }


_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    ts           INTEGER PRIMARY KEY,
    cash         REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    unrealized   REAL NOT NULL DEFAULT 0,
    equity       REAL NOT NULL DEFAULT 0,
    gross_exposure REAL NOT NULL DEFAULT 0,
    leverage_pct REAL NOT NULL DEFAULT 0,
    n_open       INTEGER NOT NULL DEFAULT 0,
    peak_equity  REAL NOT NULL DEFAULT 0,
    drawdown_pct REAL NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT ''
)
"""


def snapshot(conn: sqlite3.Connection, apply: bool = True) -> Dict[str, Any]:
    c = canonical(conn)
    conn.execute(_SNAPSHOT_DDL) if apply else None
    peak_row = None
    if apply:
        peak_row = conn.execute("SELECT MAX(peak_equity) FROM account_snapshots").fetchone()
    peak = max(_num(peak_row[0]) if peak_row else 0.0, c["equity"], c["initial_capital"])
    dd = ((c["equity"] - peak) / peak * 100.0) if peak > 0 else 0.0
    row = {
        "ts": int(time.time()), "cash": c["cash"], "realized_pnl": c["realized"],
        "unrealized": c["unrealized"], "equity": c["equity"],
        "gross_exposure": c["gross_exposure"], "leverage_pct": c["leverage_pct"],
        "n_open": c["n_open"], "peak_equity": round(peak, 4), "drawdown_pct": round(dd, 4),
        "note": f"open={c['n_open']} lev={c['leverage_pct']:.0f}%",
    }
    if apply:
        conn.execute(
            "INSERT OR REPLACE INTO account_snapshots "
            "(ts,cash,realized_pnl,unrealized,equity,gross_exposure,leverage_pct,"
            "n_open,peak_equity,drawdown_pct,note) VALUES "
            "(:ts,:cash,:realized_pnl,:unrealized,:equity,:gross_exposure,:leverage_pct,"
            ":n_open,:peak_equity,:drawdown_pct,:note)", row,
        )
        conn.commit()
    row["applied"] = apply
    return row


def repair_runs(conn: sqlite3.Connection, apply: bool = False) -> Dict[str, Any]:
    """只保留最新一条 RUNNING run，其余置 STOPPED，并把 settings 指回去。"""
    if not _table_exists(conn, "paper_trading_runs"):
        return {"runs": 0, "kept": None, "stopped": [], "applied": apply}
    runs = [dict(r) for r in conn.execute(
        "SELECT id, strategy_id, spec_version, started_at, status FROM paper_trading_runs "
        "WHERE status='RUNNING' ORDER BY started_at DESC, id DESC")]
    if len(runs) <= 1:
        return {"runs": len(runs), "kept": runs[0]["id"] if runs else None,
                "stopped": [], "applied": apply}
    kept = int(runs[0]["id"])
    # 优先保留 settings 指的那一条（它可能已经产生成交），否则保留最新
    if _table_exists(conn, "paper_trading_settings"):
        active = conn.execute(
            "SELECT active_run_id FROM paper_trading_settings WHERE id=1").fetchone()
        if active and active[0] and int(active[0]) in {int(r["id"]) for r in runs}:
            kept = int(active[0])
    stopped = [int(r["id"]) for r in runs if int(r["id"]) != kept]
    if apply:
        now = int(time.time())
        conn.executemany(
            "UPDATE paper_trading_runs SET status='STOPPED', stopped_at=? WHERE id=?",
            [(now, sid) for sid in stopped],
        )
        if _table_exists(conn, "paper_trading_settings"):
            conn.execute("UPDATE paper_trading_settings SET active_run_id=? WHERE id=1", (kept,))
        conn.commit()
    return {"runs": len(runs), "kept": kept, "stopped": stopped, "applied": apply}


# ── CLI ─────────────────────────────────────────────────────────────────
def _print_show(c: Dict[str, Any]) -> None:
    print(f"起始本金 {c['initial_capital']:.2f} · 手续费 {c['fee_bps']:g}bps/边")
    print(f"  现金 {c['cash']:.2f} · 已实现 {c['realized']:+.2f}（毛利 {c['realized_gross']:+.2f}，"
          f"手续费 {c['fees_booked']:.2f}）")
    print(f"  浮盈亏 {c['unrealized']:+.2f} · 未入账开仓费 {c['unbooked_entry_fee']:.2f}")
    print(f"  权益 {c['equity']:.2f}（账上缓存 {c['stored']['equity']:.2f}，"
          f"差 {c['stored']['equity'] - c['equity']:+.2f}）")
    print(f"  持仓 {c['n_open']} 笔 · 名义 {c['gross_exposure']:.0f} "
          f"= 权益 {c['leverage_pct']:.0f}%（上限 {c['leverage_cap_pct']:.0f}%）")
    for p in c["positions"]:
        print(f"    #{p['id']} {p['asset']} {p['side']} {p['qty']} @ {p['entry_price']:g} "
              f"mark={p['mark']} 浮盈 {p['unrealized']:+.2f} ({p['age_min']:.0f}分钟)")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="账户唯一口径 / 对账")
    ap.add_argument("--show", action="store_true", help="打印口径（默认）")
    ap.add_argument("--reconcile", action="store_true", help="对账，有 block 级差异返回 1")
    ap.add_argument("--rebuild", action="store_true", help="按持仓重算账户")
    ap.add_argument("--backfill-closed", action="store_true", help="补算历史平仓手续费")
    ap.add_argument("--snapshot", action="store_true", help="写一条 account_snapshots")
    ap.add_argument("--repair-runs", action="store_true", help="合并重复 RUNNING run")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)

    conn = connect(a.db)
    try:
        if a.repair_runs:
            out = repair_runs(conn, apply=a.apply)
            print(json.dumps(out, ensure_ascii=False) if a.json else
                  f"RUNNING {out['runs']} 条 → 保留 #{out['kept']}，停掉 {out['stopped']}"
                  f"{'（已写库）' if a.apply else '（试运行）'}")
            return 0
        if a.rebuild:
            out = rebuild(conn, apply=a.apply, backfill_closed=a.backfill_closed)
            if a.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            else:
                b, f = out["before"], out["after"]
                print(f"rebuild {'已写库' if a.apply else '试运行'}：")
                for k in ("cash", "realized", "unrealized", "equity", "gross_exposure"):
                    print(f"  {k:14s} {b[k]:>12.2f} → {f[k]:>12.2f}")
                print(f"  {'leverage_pct':14s} {b['leverage_pct']:>12.1f} → {f['leverage_pct']:>12.1f}")
                p = out["planned"]
                if p["null_pnl_ids"]:
                    print(f"  补 NULL 盈亏：{p['null_pnl_ids']}")
                if p["unbooked_entry_fee"]:
                    print(f"  补开仓手续费：{p['unbooked_entry_fee']:.2f}")
                if p["backfill_fee_ids"]:
                    print(f"  补历史平仓费（{'已做' if out['backfill_closed'] else '未开 --backfill-closed'}）："
                          f"{p['backfill_fee_ids']}")
            return 0
        if a.snapshot:
            out = snapshot(conn, apply=a.apply)
            print(json.dumps(out, ensure_ascii=False) if a.json else
                  f"快照 equity={out['equity']:.2f} peak={out['peak_equity']:.2f} "
                  f"dd={out['drawdown_pct']:.2f}%{'（已写库）' if a.apply else '（试运行）'}")
            return 0
        if a.reconcile:
            issues = reconcile(conn)
            c = canonical(conn)
            if a.json:
                print(json.dumps({"canonical": c, "issues": issues}, ensure_ascii=False, indent=2))
            else:
                _print_show(c)
                print("\n对账结果：")
                if not issues:
                    print("  ✓ 全部对平")
                for i in issues:
                    print(f"  [{i['severity']:5s}] {i['code']}: {i['detail']}")
            return 1 if any(i["severity"] == "block" for i in issues) else 0
        c = canonical(conn)
        if a.json:
            print(json.dumps(c, ensure_ascii=False, indent=2))
        else:
            _print_show(c)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
