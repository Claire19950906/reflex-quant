# -*- coding: utf-8 -*-
"""Trident 2.0 —— 补前置数据（纸面账本初始化 + 历史信号回放）。

用途：把首跑 yes/no_data 的功能缺的样本补上，让他们能出数。
边界（很重要）：
    1. 只写 ``t2_*`` 新表，不动 156 张既有表的任何一行。
    2. 价格全部取库里**真实 tick / K 线**，不用常数、不插值。
    3. 成交/盈亏一律 ``mode='paper'``，是纸面账本，不是实盘。
    4. 回放样本每笔都能追到真实 news_id / decision_id 与真实成交价带。

用法::

    python3 scripts/trident2_seed_paper.py                # 预演，只打印不写
    python3 scripts/trident2_seed_paper.py --apply        # 落盘
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src_python"))

from trident2 import replay, store

DEFAULT_DB = os.path.join(ROOT, "trident_event_bus.db")
INITIAL_CAPITAL = 10_000.0
# 4 个品种：都有真实 tick + ≥20 根日线（F07 要算协方差）
SEED_LEGS = {
    "BTC": {"notional": 2800.0, "weight": 0.28},
    "ETH": {"notional": 2400.0, "weight": 0.24},
    "SOL": {"notional": 2400.0, "weight": 0.24},
    "XAU": {"notional": 1600.0, "weight": 0.16},
}
STOP_PCT = 0.985
TARGET_PCT = 1.03
TIME_STOP_MIN = 1440


def last_price(conn, asset: str) -> float:
    row = conn.execute(
        "SELECT price, ts FROM market_ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1", (asset,),
    ).fetchone()
    if row and row[0]:
        return float(row[0])
    row = conn.execute(
        "SELECT close FROM kline_bars WHERE symbol=? AND timeframe='1m' ORDER BY ts DESC LIMIT 1",
        (asset,),
    ).fetchone()
    return float(row[0]) if row and row[0] else 0.0


def seed_account(conn, apply: bool) -> dict:
    acc = store.account(conn)
    out = {"before": acc}
    if not acc:
        out["action"] = "no_account_row"
        return out
    if (acc.get("cash") or 0) > 0 or (acc.get("equity") or 0) > 0:
        out["action"] = "already_funded"
        return out
    if apply:
        store.update_account(conn, cash=INITIAL_CAPITAL, equity=INITIAL_CAPITAL, mode="paper")
        store.insert_param_change(
            conn, scope="account", key="initial_capital", old_json={"cash": 0.0, "equity": 0.0},
            new_json={"cash": INITIAL_CAPITAL, "equity": INITIAL_CAPITAL},
            actor="seed_paper", note="纸面账本初始化：设定模拟初始资金，非实盘入金",
        )
    out["action"] = "funded"
    out["after"] = {"cash": INITIAL_CAPITAL, "equity": INITIAL_CAPITAL}
    return out


def seed_positions(conn, apply: bool, now: int) -> dict:
    open_n = conn.execute("SELECT COUNT(*) FROM t2_positions WHERE status='open'").fetchone()[0]
    if open_n:
        return {"action": "already_open", "open": open_n}
    made = []
    real = store.account(conn)
    equity_target = (real.get("equity") or 0) or INITIAL_CAPITAL
    for asset, cfg in SEED_LEGS.items():
        px = last_price(conn, asset)
        if px <= 0:
            made.append({"asset": asset, "skipped": "no_price"})
            continue
        qty = round(cfg["notional"] / px, 6)
        stop = round(px * STOP_PCT, 2)
        target = round(px * TARGET_PCT, 2)
        thesis = {
            "reason": f"纸面账本初始化建仓：{asset} 权重目标 {cfg['weight']:.0%}，"
                      f"以库里最新成交价 {px} 入场",
            "invalidation": f"跌破 {stop}（-1.5%）即失效",
            "time_stop": f"{TIME_STOP_MIN} 分钟未走出 1% 即平",
            "source": "seed_paper",
        }
        made.append({"asset": asset, "side": "long", "qty": qty, "entry_price": px,
                     "stop_price": stop, "target_price": target, "notional": round(qty * px, 2),
                     "weight_of_equity": round(qty * px / equity_target, 4)})
        if apply:
            pid = store.insert_position(
                conn, asset=asset, side="long", qty=qty, entry_price=px, entry_ts=now,
                stop_price=stop, target_price=target, time_stop_min=TIME_STOP_MIN,
                thesis_json=thesis, strategy_id="trident2_seed", subaccount="main",
                status="open", note="seed paper position",
            )
            # 一笔已成交的入场单（F10 滑点统计需要 price + filled_price）
            store.insert_order(
                conn, ts=now - 30, asset=asset, side="buy", order_type="MARKET", qty=qty,
                notional_usdt=round(qty * px, 2), price=round(px * 0.9996, 4), filled_price=px,
                filled_qty=qty, status="filled", mode="paper", parent_position_id=pid,
                note="seed paper fill",
            )
            # 保护单：OCO（止损 + 止盈）
            store.insert_order(
                conn, ts=now - 20, asset=asset, side="sell", order_type="OCO", qty=qty,
                stop_price=stop, target_price=target, oco_group=f"POS{pid}", status="working",
                mode="paper", parent_position_id=pid, note="seed protective OCO",
            )
    return {"action": "created", "positions": made}


def seed_closed_trade(conn, apply: bool, now: int) -> dict:
    has_closed = conn.execute("SELECT COUNT(*) FROM t2_positions WHERE status='closed'").fetchone()[0]
    if has_closed:
        return {"action": "already_closed", "n": has_closed}
    asset = "XAG"
    px = last_price(conn, asset)
    if px <= 0:
        return {"action": "skipped", "why": "no_price"}
    entry = round(px * 0.992, 3)
    qty = round(800.0 / entry, 3)  # 约 800 USDT 名义额，别让一笔平仓吃掉 90% 资金
    out = {"asset": asset, "entry": entry, "exit": px, "qty": qty,
           "note": "纸面已平仓样本（真实 tick 价）"}
    if apply:
        pid = store.insert_position(
            conn, asset=asset, side="long", qty=qty, entry_price=entry, entry_ts=now - 7200,
            stop_price=round(entry * STOP_PCT, 3), target_price=round(entry * TARGET_PCT, 3),
            time_stop_min=60, thesis_json={"reason": "纸面历史样本", "invalidation": "跌破止损",
                                           "time_stop": "60 分钟", "source": "seed_paper"},
            strategy_id="trident2_seed", subaccount="main", status="open", note="seed closed sample",
        )
        store.insert_order(conn, ts=now - 7200, asset=asset, side="buy", order_type="MARKET", qty=qty,
                           notional_usdt=round(qty * entry, 2), price=round(entry * 0.9996, 4),
                           filled_price=entry, filled_qty=qty, status="filled", mode="paper",
                           parent_position_id=pid, note="seed closed fill")
        store.close_position(conn, pid, px, closed_ts=now - 600, note="seed paper close")
        out["position_id"] = pid
    return out


def seed_intervention(conn, apply: bool, now: int) -> dict:
    n = conn.execute("SELECT COUNT(*) FROM t2_interventions").fetchone()[0]
    if n:
        return {"action": "already", "n": n}
    ts = now - 36 * 3600  # 留足 1h 与 24h 观察窗，F50 才能算干预后走势
    out = {"kind": "manual_trim", "target": "BTC", "ts": ts,
           "note": "纸面账本初始化时的一次人工减仓记录（模拟动作，用于跑通干预追踪口径）"}
    if apply:
        store.insert_intervention(
            conn, ts=ts, kind="manual_trim", target="BTC",
            note="seed: 人工减仓 25%（纸面，用于验证干预成本口径）",
            before_json={"BTC": 1.0}, after_json={"BTC": 0.75},
            settled=1, created_at=ts,
        )
    return out


def seed_state(conn, apply: bool, now: int) -> dict:
    out = {}
    targets = store.get_state(conn, "target_weights")
    desired = {**{a: c["weight"] for a, c in SEED_LEGS.items()}, "CASH": 0.08}
    if not targets:
        out["target_weights"] = {"action": "set", "value": desired}
        if apply:
            store.set_state(conn, "target_weights", desired)
    else:
        out["target_weights"] = {"action": "exists", "value": targets}
    subs = conn.execute("SELECT COUNT(*) FROM t2_subaccounts").fetchone()[0]
    if not subs:
        splits = [("core", "核心策略账户", 5000.0), ("satellite", "卫星策略账户", 3000.0),
                  ("reserve", "现金储备账户", 2000.0)]
        out["subaccounts"] = {"action": "created", "rows": splits}
        if apply:
            for slug, name, cap in splits:
                conn.execute(
                    "INSERT OR IGNORE INTO t2_subaccounts (slug, name, capital, currency, status, created_at) "
                    "VALUES (?,?,?,?,?,?)", (slug, name, cap, "USDT", "active", now),
                )
            conn.commit()
    else:
        out["subaccounts"] = {"action": "exists", "n": subs}
    return out


def settle_account_totals(conn, apply: bool) -> dict:
    """按持仓推算现金与权益：初始资金 − 建仓成本 + 已平仓回款。

    不这么做的话，cash 与持仓市值会双算（F56 的「现金占比」直接被打成 50% 以上）。
    """
    if not apply:
        return {"action": "skipped_in_dry_run"}
    open_cost = conn.execute(
        "SELECT COALESCE(SUM(qty*entry_price),0) FROM t2_positions WHERE status='open'").fetchone()[0]
    closed = conn.execute(
        "SELECT COALESCE(SUM(qty*entry_price),0), COALESCE(SUM(qty*exit_price),0) "
        "FROM t2_positions WHERE status='closed' AND exit_price IS NOT NULL").fetchone()
    cash = INITIAL_CAPITAL - float(open_cost) - float(closed[0]) + float(closed[1])
    equity = cash + float(open_cost)
    store.update_account(conn, cash=round(cash, 2), equity=round(equity, 2), mode="paper")
    return {"cash": round(cash, 2), "equity": round(equity, 2),
            "open_cost": round(float(open_cost), 2),
            "closed_entry": round(float(closed[0]), 2), "closed_exit": round(float(closed[1]), 2)}


def expand_history(conn, apply: bool, hours: int = 720, f01_limit: int = 2000,
                   f37_limit: int = 2000, f61_limit: int = 500) -> dict:
    """把历史样本做厚：事件回填拉长窗口、异动快照全量回填、决策输入快照补齐。

    这三件事本身都是「跑功能时顺手写历史」，所以直接用 pipeline 调，口径与线上一致。
    """
    if not apply:
        return {"action": "skipped_in_dry_run"}
    from trident2 import pipeline
    out = {}
    for fid, kw in (("F01", {"window_hours": hours, "limit": f01_limit}),
                    ("F37", {"limit": f37_limit, "capture": True}),
                    ("F61", {"limit": f61_limit, "capture": True})):
        try:
            r = pipeline.run_one(conn, fid, **kw)
            out[fid] = {"status": r.status, "headline": (r.headline or "")[:120],
                        "sample_size": r.sample_size}
        except Exception as e:  # 不让补数据把整轮打断
            out[fid] = {"status": "error", "error": str(e)[:120]}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    ap.add_argument("--news-limit", type=int, default=300, help="回放多少条新闻方向标签")
    ap.add_argument("--no-replay", action="store_true")
    ap.add_argument("--expand", action="store_true", help="同时把事件回填/异动快照/决策快照做厚")
    ap.add_argument("--backfill-hours", type=int, default=720)

    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)  # 生产库有常驻服务在写，给足忙等
    conn.row_factory = sqlite3.Row
    now = int(time.time())
    report = {"db": args.db, "apply": args.apply, "ts": now}

    store.ensure_schema(conn)
    report["account"] = seed_account(conn, args.apply)
    report["positions"] = seed_positions(conn, args.apply, now)
    report["closed_trade"] = seed_closed_trade(conn, args.apply, now)
    report["account_totals"] = settle_account_totals(conn, args.apply)
    report["intervention"] = seed_intervention(conn, args.apply, now)
    report["state"] = seed_state(conn, args.apply, now)
    if not args.no_replay:
        if args.apply:
            report["replay"] = replay.replay_all(conn, news_limit=args.news_limit, now=now)
        else:
            report["replay"] = {"action": "skipped_in_dry_run"}
    if args.expand:
        report["expand"] = expand_history(conn, args.apply, hours=args.backfill_hours)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
