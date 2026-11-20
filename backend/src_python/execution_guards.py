# -*- coding: utf-8 -*-
"""执行层组合闸（v463，只读判断，不写库）。

在 `trade_executor` 真正下单**之前**拦两件事：

1. **同品种重复开仓**：ETH 已有 open 空单，再来的 ETH 空单直接拒绝；
   反向单也拒绝（执行器目前没有“先平后开”的原子逻辑，反向只会变成对锁）。
2. **组合杠杆上限**：单品种名义不超过权益的 `MAX_ASSET_WEIGHT_PCT`（默认 50%），
   总名义不超过权益的 `MAX_GROSS_LEVERAGE_PCT`（默认 100%）。

开关：
  - `DUP_ENTRY_GUARD=enforce`（默认）真正拒绝；`shadow` 只标注放行；`off` 关闭。
  - `ALLOW_HEDGE=1` 允许同品种反向开仓（默认 0）。
  - 拒绝原因写进 `trade_audit_log.reason`，不产生成交。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, Optional


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _side_of(action: str) -> str:
    return "long" if str(action).upper() in ("BUY", "LONG") else "short"


def _open_positions(conn: sqlite3.Connection) -> list[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, asset, side, qty, entry_price, status FROM t2_positions "
        "WHERE status='open' ORDER BY id DESC"
    ).fetchall()
    return [{"id": r[0], "asset": r[1], "side": r[2], "qty": r[3],
             "entry_price": r[4], "status": r[5]} for r in rows]


def _equity(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT cash, equity FROM t2_account ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return 0.0
    return float(row[1] or row[0] or 0.0)


def auto_stop_price(entry_price: float, side: str) -> Optional[float]:
    """开仓即带的硬止损：空单在上方、多单在下方，默认 2%。"""
    if not _env_bool("AUTO_STOP_ENABLED", True) or entry_price <= 0:
        return None
    pct = _env_float("AUTO_STOP_PCT", 2.0)
    if pct <= 0 or pct > 20:
        return None
    d = 1.0 + pct / 100.0 if _side_of(side) == "short" else 1.0 - pct / 100.0
    return round(float(entry_price) * d, 6)


def check_new_entry(conn: sqlite3.Connection, asset: str, action: str,
                    size_usdt: float) -> Dict[str, Any]:
    """返回 {allow, size_usdt, reason, warnings, mode}。

    任何异常都不在这里吞：查不到表/账户时返回 allow=False，宁可不做。
    """
    mode = (os.getenv("DUP_ENTRY_GUARD", "enforce") or "enforce").strip().lower()
    if mode == "off":
        return {"allow": True, "size_usdt": float(size_usdt), "reason": "", "warnings": [], "mode": mode}

    try:
        positions = _open_positions(conn)
        equity = _equity(conn)
    except Exception as exc:
        return {"allow": False, "size_usdt": 0.0,
                "reason": f"portfolio_guard_db_error:{exc}", "warnings": [], "mode": mode}

    if equity <= 0:
        return {"allow": False, "size_usdt": 0.0,
                "reason": "portfolio_guard_no_equity", "warnings": [], "mode": mode}

    asset = str(asset).upper()
    side = _side_of(action)
    same = [p for p in positions if str(p["asset"]).upper() == asset]
    warnings: list[str] = []

    if same:
        allow_hedge = _env_bool("ALLOW_HEDGE", False)
        same_dir = [p for p in same if str(p["side"]).lower() == side]
        opp_dir = [p for p in same if str(p["side"]).lower() != side]
        if same_dir:
            reason = f"portfolio_guard:duplicate_open:{asset}:{side}:{same_dir[0]['id']}"
        elif opp_dir and not allow_hedge:
            reason = f"portfolio_guard:opposite_open:{asset}:{opp_dir[0]['id']}"
        else:
            reason = ""
        if reason and mode == "enforce":
            return {"allow": False, "size_usdt": 0.0, "reason": reason,
                    "warnings": [f"{asset} 已有 open 持仓 #{same[0]['id']}"], "mode": mode}
        if reason:
            warnings.append(f"{reason}（shadow 放行）")

    if mode == "shadow":
        return {"allow": True, "size_usdt": float(size_usdt), "reason": "",
                "warnings": warnings, "mode": mode}

    # 单品种上限：已有同品种名义 + 新单，不得超过权益的 X%
    max_asset_pct = _env_float("MAX_ASSET_WEIGHT_PCT", 50.0)
    same_notional = sum(float(p["entry_price"] or 0) * float(p["qty"] or 0) for p in same)
    asset_cap = equity * max_asset_pct / 100.0
    allowed = min(float(size_usdt), max(0.0, asset_cap - same_notional))
    if allowed < float(size_usdt):
        warnings.append(f"单品种上限 {max_asset_pct:g}%：新单从 {size_usdt:.0f} 缩到 {allowed:.0f} USDT")

    # 总杠杆上限：所有 open 名义 + 新单，不得超过权益的 Y%
    max_gross_pct = _env_float("MAX_GROSS_LEVERAGE_PCT", 100.0)
    gross = sum(float(p["entry_price"] or 0) * float(p["qty"] or 0) for p in positions)
    gross_cap = equity * max_gross_pct / 100.0
    allowed = min(allowed, max(0.0, gross_cap - gross))
    if allowed < float(size_usdt):
        warnings.append(f"总杠杆上限 {max_gross_pct:g}%：当前已用 {gross / equity * 100:.0f}%，"
                        f"新单最多 {allowed:.0f} USDT")

    min_usdt = _env_float("PORTFOLIO_GUARD_MIN_USDT", 100.0)
    if allowed < min_usdt:
        return {"allow": False, "size_usdt": 0.0,
                "reason": f"portfolio_guard:cap_exceeded:gross={gross:.0f}/equity={equity:.0f}",
                "warnings": warnings, "mode": mode}

    return {"allow": True, "size_usdt": round(allowed, 2), "reason": "",
            "warnings": warnings, "mode": mode}
