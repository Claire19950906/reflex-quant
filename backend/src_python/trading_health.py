# -*- coding: utf-8 -*-
"""v479 交易段健康度（只读，供 API / 前端 / 巡检共用）。"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, List

import account_ledger as al


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



def _clean_env_value(v: str) -> str:
    """去掉未加引号值后面的行内注释（`KEY=90   # 说明` → `90`）。

    原来直接把整行当值写进 os.environ，导致 `int(os.getenv(...))` 在 import 期就炸，
    `feeds.quote_sources` 整个模块导入失败，行情路由全部退化成 unknown。
    """
    v = (v or "").strip()
    if v[:1] in ('"', "'"):
        q = v[0]
        end = v.rfind(q)
        return v[1:end] if end > 0 else v.strip(q)
    for sep in (" #", "\t#"):
        idx = v.find(sep)
        if idx >= 0:
            v = v[:idx]
    return v.strip()

def _load_env_defaults() -> None:
    """读 backend/.env 作为默认阈值（不覆盖已有环境变量），否则杠杆上限会回落 100%。"""
    envf = os.path.join(BACKEND_DIR, ".env")
    if not os.path.exists(envf):
        return
    for line in open(envf, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), _clean_env_value(v)
        if k and k not in os.environ:
            os.environ[k] = v


def build(db_path: str) -> Dict[str, Any]:
    if not os.path.exists(db_path):
        return {"ok": False, "error": f"db_not_found:{db_path}", "ts": int(time.time())}
    _load_env_defaults()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        c = al.canonical(conn)
        issues = al.reconcile(conn)
        blocks = [i["code"] for i in issues if i.get("severity") == "block"]
        warns = [i["code"] for i in issues if i.get("severity") != "block"]
        last_tick = conn.execute("SELECT MAX(ts) FROM market_ticks").fetchone()[0]
        # v494：按"此刻该不该更新"判断新鲜度；美股收盘后不再算过期（否则告警永远在响）
        try:
            import market_rules as _mr
            stale_limit = float(os.getenv("HEALTH_TICK_STALE_SEC", "300") or 300)
            market_state: List[Dict[str, Any]] = []
            open_stale: List[str] = []
            for r in conn.execute(
                    "SELECT symbol, MAX(ts) AS ts FROM market_ticks GROUP BY symbol"):
                age = int(time.time() - int(r["ts"] or 0))
                kind = _mr.classify(r["symbol"])
                if kind == "unknown":
                    # 库里有 A 股/ETF 等未路由品种（只做新闻/归因，不参与交易），
                    # 不能把它们当成"该更新却没有"，否则告警永远响。
                    is_open, why = False, "未路由（不参与交易时段）"
                else:
                    is_open, why = _mr.session_open(r["symbol"])
                market_state.append({"symbol": r["symbol"], "age_s": age,
                                      "open": bool(is_open), "session": why})
                if is_open and age > stale_limit:
                    open_stale.append(f"{r['symbol']}({age}s)")
            if open_stale:
                warns.append("TICK_STALE_OPEN")
        except Exception:
            market_state, open_stale = [], []
            stale_limit = 300.0
        try:
            alerts_open = conn.execute(
                "SELECT COUNT(*) FROM t2_alerts WHERE resolved_at IS NULL").fetchone()[0]
        except Exception:
            alerts_open = 0
        positions: List[Dict[str, Any]] = []
        for p in conn.execute(
            "SELECT id, asset, side, qty, entry_price, stop_price, trailing_bp, time_stop_min,"
            " entry_ts FROM t2_positions WHERE status='open' ORDER BY id"
        ):
            d = dict(p)
            mark = conn.execute(
                "SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC, rowid DESC LIMIT 1",
                (p["asset"],)).fetchone()
            d["mark"] = float(mark[0]) if mark and mark[0] else None
            try:
                d["market_open"], d["session"] = _mr.session_open(p["asset"])
            except Exception:
                d["market_open"], d["session"] = True, "unknown"
            if d["mark"]:
                sign = 1.0 if str(p["side"]).lower() in ("long", "buy") else -1.0
                d["upnl"] = round((d["mark"] - float(p["entry_price"])) * float(p["qty"]) * sign, 4)
            positions.append(d)
        return {
            "ok": not blocks,
            "ts": int(time.time()),
            "cash": round(float(c["cash"]), 4),
            "equity": round(float(c["equity"]), 4),
            "realized_pnl": round(float(c["realized"]), 4),
            "unrealized": round(float(c["unrealized"]), 4),
            "gross_exposure": round(float(c["gross_exposure"]), 2),
            "leverage_pct": round(float(c["leverage_pct"]), 2),
            "n_open": int(c["n_open"]),
            "blocks": blocks,
            "warnings": warns,
            "alerts_open": int(alerts_open or 0),
            "last_tick_age_s": int(time.time() - int(last_tick)) if last_tick else None,
            "market_state": market_state,
            "stale_open_markets": open_stale,
            "positions": positions,
        }
    finally:
        conn.close()
