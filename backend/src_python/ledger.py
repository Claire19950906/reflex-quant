# -*- coding: utf-8 -*-
"""模拟盘账本 / 持仓对账（v462，只读）。

系统里已经有 `t2_account / t2_positions / t2_orders`，但没有一页能回答：

  - 这 5000 USDT 的 XAU 空单，占账户多少？
  - 有没有止损？没有的话，亏多少算“一注”？
  - 成交了 1 笔，手续费记了没有？
  - 重置之后到底平过几笔，胜率能不能算？

账本只做三件事：**按最新行情标记、按账户口径算敞口、把对不上的地方列出来**。
不写库、不下单、不补历史（重置前的数据不回溯）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

# 行情超过这个秒数就算“旧价”，标记出来但不假装没有
MARK_STALE_S = 300
# 账户快照超过这个秒数就算“账本久未更新”
ACCOUNT_STALE_S = 3600


def _ro_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _latest_mark(conn: sqlite3.Connection, asset: str) -> Dict[str, Any]:
    """最新价优先 tick，其次 1m/5m 收盘；都没有就返回 None，绝不编价。"""
    row = conn.execute(
        "SELECT price, ts FROM market_ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (asset.upper(),),
    ).fetchone()
    if row and row["price"]:
        return {"price": float(row["price"]), "ts": int(row["ts"]), "source": "tick"}
    for tf in ("1m", "5m"):
        row = conn.execute(
            "SELECT close, ts FROM kline_bars WHERE symbol=? AND timeframe=? "
            "ORDER BY ts DESC LIMIT 1",
            (asset.upper(), tf),
        ).fetchone()
        if row and row["close"]:
            ts = int(row["ts"])
            ts = ts / 1000 if ts > 10_000_000_000 else ts
            return {"price": float(row["close"]), "ts": int(ts), "source": tf}
    return {"price": None, "ts": None, "source": "none"}


def _direction(side: str) -> int:
    return -1 if str(side).lower() in ("short", "sell") else 1


def _decision_id(thesis_json: Optional[str], note: Optional[str]) -> Optional[int]:
    if thesis_json:
        try:
            obj = json.loads(thesis_json)
            if obj.get("decision_id") is not None:
                return int(obj["decision_id"])
        except (ValueError, TypeError):
            pass
    m = re.search(r"decision#?(\d+)", note or "")
    return int(m.group(1)) if m else None


def build(db_path: Optional[Path] = None, order_limit: int = 50) -> Dict[str, Any]:
    now = int(time.time())
    conn = _ro_conn(db_path)
    try:
        acct = conn.execute("SELECT * FROM t2_account ORDER BY id DESC LIMIT 1").fetchone()
        acct = dict(acct) if acct else {}
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM t2_positions WHERE status='open' ORDER BY id DESC LIMIT 200")]
        closed_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM t2_positions WHERE status='closed' ORDER BY closed_ts DESC LIMIT 200")]
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM t2_orders ORDER BY id DESC LIMIT ?", (int(order_limit),))]
        cost_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM t2_cost_ledger ORDER BY id DESC LIMIT 500")] \
            if _table_exists(conn, "t2_cost_ledger") else []
        marks = {p["asset"]: _latest_mark(conn, p["asset"]) for p in open_rows}
    finally:
        conn.close()

    cash = float(acct.get("cash") or 0.0)
    stored_equity = float(acct.get("equity") or 0.0)
    fee_bps = float(acct.get("fee_bps") or 0.0)
    realized = float(acct.get("realized_pnl") or 0.0)

    positions: List[Dict[str, Any]] = []
    for p in open_rows:
        mark = marks.get(p["asset"]) or {"price": None, "ts": None, "source": "none"}
        d = _direction(p["side"])
        qty = float(p.get("qty") or 0.0)
        entry = float(p.get("entry_price") or 0.0)
        last = mark["price"]
        notional_entry = qty * entry
        notional_mark = qty * last if last is not None else None
        if last is not None and entry > 0:
            upnl_pct = (last / entry - 1.0) * 100.0 * d
            upnl = (last - entry) * qty * d
        else:
            upnl_pct, upnl = None, None
        mark_age = (now - int(mark["ts"])) if mark.get("ts") else None
        positions.append({
            "id": p["id"],
            "asset": p["asset"],
            "side": p["side"],
            "direction": d,
            "qty": round(qty, 8),
            "entry_price": entry,
            "entry_ts": p.get("entry_ts"),
            "age_min": round((now - int(p["entry_ts"])) / 60.0, 1) if p.get("entry_ts") else None,
            "mark": last,
            "mark_ts": mark.get("ts"),
            "mark_source": mark["source"],
            "mark_age_s": mark_age,
            "mark_stale": bool(mark_age is not None and mark_age > MARK_STALE_S),
            "unrealized_pct": round(upnl_pct, 3) if upnl_pct is not None else None,
            "unrealized_usdt": round(upnl, 2) if upnl is not None else None,
            "notional_entry": round(notional_entry, 2),
            "notional_mark": round(notional_mark, 2) if notional_mark is not None else None,
            "stop_price": p.get("stop_price"),
            "target_price": p.get("target_price"),
            "decision_id": _decision_id(p.get("thesis_json"), p.get("note")),
            "strategy_id": p.get("strategy_id"),
            "note": p.get("note"),
        })

    equity_mtm = cash + sum(x["unrealized_usdt"] or 0.0 for x in positions)
    gross = sum(x["notional_mark"] or x["notional_entry"] for x in positions)
    net = sum((x["notional_mark"] or x["notional_entry"]) * x["direction"] for x in positions)

    # 单品种合并：同品种两笔同向持仓，本质是一个押注，不能只看单笔
    by_asset: Dict[str, Dict[str, Any]] = {}
    for x in positions:
        n = x["notional_mark"] or x["notional_entry"]
        slot = by_asset.setdefault(x["asset"], {"notional": 0.0, "count": 0, "dirs": set()})
        slot["notional"] += n
        slot["count"] += 1
        slot["dirs"].add(x["direction"])
    concentration_asset = max(by_asset, key=lambda a: by_asset[a]["notional"], default=None)
    concentration = (by_asset[concentration_asset]["notional"] / equity_mtm * 100.0
                     if concentration_asset and equity_mtm > 0 else 0.0)

    realized_closed = sum(float(x.get("realized_pnl") or 0.0) for x in closed_rows)
    # t2_cost_ledger 同时记 LLM Token 与交易费用，必须分开，不能把 270 万 TOKEN 当 USDT
    fee_rows = [c for c in cost_rows
                if str(c.get("currency") or "").upper() in ("USDT", "USD")
                and ("fee" in str(c.get("category") or "").lower()
                     or "commission" in str(c.get("category") or "").lower())]
    token_rows = [c for c in cost_rows if str(c.get("currency") or "").upper() == "TOKEN"]
    fee_total = sum(float(c.get("amount") or 0.0) for c in fee_rows)
    token_total = sum(float(c.get("amount") or 0.0) for c in token_rows)
    filled_notional = sum(float(o.get("notional_usdt") or 0.0) for o in orders
                          if str(o.get("status")).lower() in ("filled", "closed"))
    expected_fee = filled_notional * fee_bps / 10_000.0

    warnings: List[Dict[str, str]] = []
    if positions:
        for x in positions:
            if x["mark"] is None:
                warnings.append({"level": "warn", "key": f"mark-{x['id']}",
                                 "text": f"{x['asset']} 没有行情价，浮盈/敞口按入场价估算"})
            elif x["mark_stale"]:
                warnings.append({"level": "warn", "key": f"stale-{x['id']}",
                                 "text": f"{x['asset']} 行情已旧 {x['mark_age_s']}s，标记价可能不是当前价"})
            if not x["stop_price"]:
                warnings.append({"level": "warn", "key": f"stop-{x['id']}",
                                 "text": f"{x['asset']} 无止损：单笔名义 {x['notional_entry']:.0f} USDT，"
                                          f"价格反向 2% 约亏 {x['notional_entry'] * 0.02:.0f} USDT"})
            if not x["decision_id"]:
                warnings.append({"level": "error", "key": f"orphan-{x['id']}",
                                 "text": f"{x['asset']} 持仓 #{x['id']} 找不到来源决策，无法复盘"})
        for asset, slot in by_asset.items():
            share = slot["notional"] / equity_mtm * 100.0 if equity_mtm > 0 else 0.0
            if share >= 40 or slot["count"] > 1:
                extra = (f"（{slot['count']} 笔{'同向' if len(slot['dirs']) == 1 else '双向'}持仓，"
                         f"实际是一个{'押注' if len(slot['dirs']) == 1 else '品种'}）"
                         if slot["count"] > 1 else "")
                warnings.append({"level": "warn", "key": f"conc-{asset}",
                                 "text": f"{asset} 合计名义 {slot['notional']:.0f} USDT、"
                                         f"占权益 {share:.0f}%{extra}"})
        if equity_mtm > 0 and gross / equity_mtm >= 1.0:
            warnings.append({"level": "error", "key": "gross",
                             "text": f"总名义敞口 {gross:.0f} USDT 已超过权益，"
                                     f"执行层组合闸会拒绝新单，先把敞口降下来"})
    if filled_notional > 0 and fee_rows == []:
        warnings.append({"level": "warn", "key": "fee",
                         "text": f"成交名义 {filled_notional:.0f} USDT、按 {fee_bps:g}bps 应记手续费约 "
                                 f"{expected_fee:.2f} USDT，但成本账本没有 USDT 手续费行——手续费未入账"})
    elif expected_fee > 0 and abs(fee_total - expected_fee) > max(0.5, expected_fee * 0.5):
        warnings.append({"level": "warn", "key": "fee-gap",
                         "text": f"成本账本已记手续费 {fee_total:.2f} USDT，按 {fee_bps:g}bps 应记约 "
                                 f"{expected_fee:.2f} USDT，差 {fee_total - expected_fee:+.2f} USDT"})
    if closed_rows == []:
        warnings.append({"level": "info", "key": "no-closed",
                         "text": "重置后尚无已平仓样本：胜率 / 盈亏比 / 最大回撤都不可计算"})
    if acct.get("updated_at") and now - int(acct["updated_at"]) > ACCOUNT_STALE_S:
        warnings.append({"level": "warn", "key": "acct-stale",
                         "text": f"账户快照已 {int((now - int(acct['updated_at']))/60)} 分钟未更新"})

    open_count = len(positions)
    if open_count == 0:
        headline = (f"空仓：账户 {equity_mtm:,.0f} USDT，已实现 {realized:,.2f}；"
                    f"没有持仓就没有风险敞口")
    else:
        worst = min([x["unrealized_pct"] for x in positions
                     if x["unrealized_pct"] is not None], default=None)
        upnl_txt = f"，浮盈亏 {sum(x['unrealized_usdt'] or 0 for x in positions):+,.2f} USDT"
        headline = (f"模拟盘 {equity_mtm:,.0f} USDT · {open_count} 笔持仓 · "
                    f"最大单品种占权益 {concentration:.0f}%{upnl_txt}")
        if worst is not None and worst < -1.0:
            headline += f"；最差一笔 {worst:+.2f}%"

    return {
        "generated_at": now,
        "account": {
            "mode": acct.get("mode"),
            "cash": cash,
            "equity_stored": stored_equity,
            "equity_mtm": round(equity_mtm, 2),
            "equity_gap": round(equity_mtm - stored_equity, 2),
            "realized_pnl": realized,
            "fee_bps": fee_bps,
            "updated_at": acct.get("updated_at"),
            "age_s": (now - int(acct["updated_at"])) if acct.get("updated_at") else None,
        },
        "positions": positions,
        "orders": orders,
        "closed": [{k: r.get(k) for k in
                    ("id", "asset", "side", "qty", "entry_price", "exit_price",
                     "entry_ts", "closed_ts", "realized_pnl", "strategy_id")}
                   for r in closed_rows],
        "costs": {"rows": len(cost_rows),
                  "fee_rows": len(fee_rows), "fee_total": round(fee_total, 2),
                  "token_rows": len(token_rows), "token_total": round(token_total, 0),
                  "filled_notional": round(filled_notional, 2),
                  "expected_fee": round(expected_fee, 2)},
        "risk": {
            "open_count": open_count,
            "gross_exposure": round(gross, 2),
            "net_exposure": round(net, 2),
            "concentration_pct": round(concentration, 1),
            "concentration_asset": concentration_asset,
            "realized_closed": round(realized_closed, 2),
        },
        "reconciliation": warnings,
        "headline": headline,
        "note": ("口径：账户取 t2_account，持仓取 t2_positions(status=open)，同品种多笔合并算敞口；"
                 "标记价取 market_ticks 最新，没有 tick 才退到 1m/5m 收盘，没有价就不估值。"
                 "成本账本只把 USDT/USD 的 fee/commission 行算手续费，TOKEN 行单独统计。"
                 "重置前的数据不回溯，账本只从当前快照往后算。"),
    }


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="模拟盘账本")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = build()
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(rep["headline"])
        for p in rep["positions"]:
            print(f"  {p['asset']} {p['side']} {p['qty']} @ {p['entry_price']} "
                  f"mark={p['mark']} uPnL={p['unrealized_pct']}% "
                  f"notional={p['notional_entry']}")
        for w in rep["reconciliation"]:
            print(f"  [{w['level']}] {w['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
