# -*- coding: utf-8 -*-
"""v380 · 反事实四条腿（counterfactual four-leg replay）

问题
----
现在能回答「这笔亏了」，但回答不了「如果当时不进场 / 反向 / 晚 10 分钟 / 半仓，
结果会怎样」。没有这四条对照，反思只能给出「方向错」这种笼统结论，
无法判断损失到底来自 **方向**、**入场时点**、还是 **仓位**。

做法
----
对单笔已结算决策，用库内 kline_bars 重放同一时间窗，输出四条路径：

| 路径 | 含义 | 回答的问题 |
|---|---|---|
| actual | 实际方向、实际入场价、实际仓位 1 | 基准 |
| skip   | 完全不进场 | 「这笔信号本来该不该发」 |
| reverse| 反向做同等仓位 | 「方向是不是反的」 |
| delay  | 推迟 10 分钟（无分钟线则退化为下一根 K） | 「是不是进早了」 |
| half   | 同方向半仓 | 「仓位是不是过大」 |

价格口径
--------
入场 = 决策 entry_price（缺失则用窗口首根 open）；出场 = 窗口末根 close；
MFE/MAE 用窗口内 high/low 对入场价计算。全部为百分比，方向反向时符号取反。

纯只读：不写库、不联网（K 线缺失就明确返回 no_kline + 可用周期）。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_ALIASES = {"GOLD": "XAU", "OIL": "WTI", "XBT": "BTC"}

# 周期 → 分钟；未知按 60
_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30,
               "1h": 60, "4h": 240, "1d": 1440}

_BUY = {"BUY", "LONG", "做多", "多"}
_SELL = {"SELL", "SHORT", "做空", "空"}


def _norm_asset(asset: str) -> str:
    a = (asset or "").upper().strip()
    return _ALIASES.get(a, a)


def _parse_ts(ts_text: str) -> Optional[int]:
    """ISO（含时区）或 'YYYY-MM-DD HH:MM:SS' → epoch 毫秒。"""
    if not ts_text:
        return None
    s = str(ts_text).strip()
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _dir_of(action: str, sentiment: float = 0.0) -> int:
    a = (action or "").upper().strip()
    if a in _BUY:
        return 1
    if a in _SELL:
        return -1
    # HOLD / NO_TRADE：用情绪分数当「如果非要做」的假设方向
    if sentiment > 0.05:
        return 1
    if sentiment < -0.05:
        return -1
    return 0


def _available_timeframes(conn, symbol: str) -> List[str]:
    try:
        rows = conn.execute(
            "SELECT timeframe, COUNT(*) c FROM kline_bars WHERE symbol=? GROUP BY timeframe",
            (symbol,),
        ).fetchall()
        found = [(str(r[0]), int(r[1] or 0)) for r in rows if int(r[1] or 0) >= 2]
        order = {k: i for i, k in enumerate(["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "1d"])}
        return [tf for tf, _ in sorted(found, key=lambda x: order.get(x[0], 99))]
    except Exception:
        return []


def _pick_timeframe(tfs: List[str], horizon_min: int) -> Optional[str]:
    """选一个「粒度足够细、又能覆盖整个窗口」的周期。"""
    if not tfs:
        return None
    for tf in tfs:
        if _TF_MINUTES.get(tf, 1440) <= max(1, horizon_min):
            return tf
    return tfs[-1]  # 只有日线时用日线


def _fetch_bars(conn, symbol: str, tf: str, start_ms: int, end_ms: int,
                pad_ms: int = 0) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT ts, open, high, low, close FROM kline_bars "
            "WHERE symbol=? AND timeframe=? AND ts>=? AND ts<=? ORDER BY ts ASC",
            (symbol, tf, int(start_ms), int(end_ms + pad_ms)),
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            out.append({"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                        "low": float(r[3]), "close": float(r[4])})
        except Exception:
            continue
    return out


def _leg(direction: int, entry: float, exit_: float,
         high: float, low: float) -> Dict[str, float]:
    if direction == 0 or entry <= 0:
        return {"pnl_pct": 0.0, "mfe_pct": 0.0, "mae_pct": 0.0}
    pnl = direction * (exit_ / entry - 1.0) * 100.0
    mfe = direction * (high / entry - 1.0) * 100.0
    mae = direction * (low / entry - 1.0) * 100.0
    return {"pnl_pct": round(pnl, 4), "mfe_pct": round(max(mfe, 0.0), 4),
            "mae_pct": round(min(mae, 0.0), 4)}


def four_legs(decision_id: int, horizon_minutes: Optional[int] = None,
              conn=None) -> Dict[str, Any]:
    """对单笔决策跑四条路径。返回结构化 dict，失败时带 reason，不抛异常。"""
    own = False
    if conn is None:
        import db as _db
        conn = _db.get_connection()
        own = True
    try:
        cur = conn.execute(
            """SELECT id, target_asset, suggested_action, entry_time, entry_price,
                      expected_horizon, holding_horizon_minutes, forward_pnl,
                      mfe_pct, mae_pct, sentiment_score, settled, created_at
                 FROM ai_decisions WHERE id=?""",
            (int(decision_id),),
        )
        row = cur.fetchone()
        if not row:
            return {"ok": False, "reason": "not_found", "decision_id": decision_id}
        keys = [d[0] for d in (cur.description or [])]
        d = dict(zip(keys, row))

        asset = _norm_asset(d.get("target_asset") or "")
        start_ms = _parse_ts(d.get("entry_time") or d.get("created_at") or "")
        if start_ms is None:
            return {"ok": False, "reason": "no_entry_time", "decision_id": decision_id}

        horizon = int(horizon_minutes or d.get("holding_horizon_minutes") or 0)
        if horizon <= 0:
            horizon = 120
        horizon = max(5, min(horizon, 60 * 24 * 30))
        end_ms = start_ms + horizon * 60_000

        tfs = _available_timeframes(conn, asset)
        tf = _pick_timeframe(tfs, horizon)
        if not tf:
            return {"ok": False, "reason": "no_kline", "asset": asset,
                    "available_timeframes": tfs,
                    "hint": "该品种库内无 K 线，无法重放；先补 K 线再试"}

        pad = _TF_MINUTES.get(tf, 60) * 60_000
        bars = _fetch_bars(conn, asset, tf, start_ms, end_ms, pad_ms=pad)
        if len(bars) < 2:
            return {"ok": False, "reason": "no_kline_in_window", "asset": asset,
                    "timeframe": tf, "available_timeframes": tfs,
                    "window": {"start_ms": start_ms, "end_ms": end_ms},
                    "hint": f"窗口内只有 {len(bars)} 根 {tf} K 线"}

        dir_actual = _dir_of(d.get("suggested_action") or "", float(d.get("sentiment_score") or 0))
        is_hold = (d.get("suggested_action") or "").upper() in ("HOLD", "NO_TRADE", "")
        entry_actual = float(d.get("entry_price") or 0) or bars[0]["open"]
        exit_bar = bars[-1]
        high_win = max(b["high"] for b in bars)
        low_win = min(b["low"] for b in bars)
        # 窗口末根 close 可能已越过 horizon（pad 拉进来的下一根），只在窗口内取
        in_win = [b for b in bars if b["ts"] <= end_ms] or bars
        exit_actual = in_win[-1]["close"]

        base = _leg(dir_actual, entry_actual, exit_actual, high_win, low_win)

        # 延迟腿：10 分钟（细周期）或下一根（日线级）
        delay_ms = 10 * 60_000 if _TF_MINUTES.get(tf, 60) <= 30 else pad
        delayed_bars = [b for b in bars if b["ts"] >= start_ms + delay_ms and b["ts"] <= end_ms]
        if len(delayed_bars) >= 1 and dir_actual != 0:
            entry_d = delayed_bars[0]["open"]
            exit_d = delayed_bars[-1]["close"]
            high_d = max(b["high"] for b in delayed_bars)
            low_d = min(b["low"] for b in delayed_bars)
            delay_leg = _leg(dir_actual, entry_d, exit_d, high_d, low_d)
            delay_entry_ts = delayed_bars[0]["ts"]
        else:
            delay_leg = {"pnl_pct": None, "mfe_pct": None, "mae_pct": None}
            delay_entry_ts = None

        reverse_leg = _leg(-dir_actual, entry_actual, exit_actual, high_win, low_win) \
            if dir_actual != 0 else {"pnl_pct": None, "mfe_pct": None, "mae_pct": None}
        half_leg = {k: (round(v * 0.5, 4) if isinstance(v, (int, float)) else None)
                    for k, v in base.items()} if dir_actual != 0 else \
            {"pnl_pct": None, "mfe_pct": None, "mae_pct": None}

        legs = [
            {"key": "actual", "label": "实际（原方向·满仓）",
             "direction": dir_actual, "entry_ts": start_ms, "entry_price": round(entry_actual, 6),
             "exit_price": round(exit_actual, 6), "horizon_minutes": horizon,
             **base, "note": "HOLD → 0%" if dir_actual == 0 else ""},
            {"key": "skip", "label": "不进场", "direction": 0, "entry_ts": None,
             "entry_price": None, "exit_price": None, "horizon_minutes": horizon,
             "pnl_pct": 0.0, "mfe_pct": 0.0, "mae_pct": 0.0,
             "note": "零损益基准：与实际对比即可判断这笔信号该不该发"},
            {"key": "reverse", "label": "反向（同仓位）", "direction": -dir_actual if dir_actual else 0,
             "entry_ts": start_ms, "entry_price": round(entry_actual, 6),
             "exit_price": round(exit_actual, 6), "horizon_minutes": horizon,
             **reverse_leg, "note": "验证方向是否反了" if dir_actual else "HOLD 无反向"},
            {"key": "delay", "label": f"延迟 {delay_ms // 60_000} 分钟",
             "direction": dir_actual, "entry_ts": delay_entry_ts,
             "entry_price": round(delayed_bars[0]["open"], 6) if delay_entry_ts else None,
             "exit_price": round(delayed_bars[-1]["close"], 6) if delay_entry_ts else None,
             "horizon_minutes": horizon, **delay_leg,
             "note": "验证是否进早了" if delay_entry_ts else f"窗口内无更晚的 {tf} K 线"},
            {"key": "half", "label": "半仓（同方向）", "direction": dir_actual,
             "entry_ts": start_ms, "entry_price": round(entry_actual, 6),
             "exit_price": round(exit_actual, 6), "horizon_minutes": horizon,
             **half_leg, "note": "验证仓位是否过大"},
        ]

        def _num(v):
            return float(v) if isinstance(v, (int, float)) else None

        ranked = [(lg["key"], _num(lg.get("pnl_pct"))) for lg in legs if _num(lg.get("pnl_pct")) is not None]
        best = max(ranked, key=lambda x: x[1]) if ranked else (None, None)
        worst = min(ranked, key=lambda x: x[1]) if ranked else (None, None)

        return {
            "ok": True,
            "decision_id": int(decision_id),
            "asset": asset,
            "timeframe": tf,
            "available_timeframes": tfs,
            "holding_horizon_minutes": horizon,
            "action": d.get("suggested_action"),
            "event_type": d.get("event_type_code") or "",
            "is_hold": is_hold,
            "stored": {
                "forward_pnl": d.get("forward_pnl"),
                "mfe_pct": d.get("mfe_pct"),
                "mae_pct": d.get("mae_pct"),
            },
            # 重算出来的 actual 与库内已结算值通常有差异（结算用快照价）；给出差值供排障
            "drift_pct": round(_num(base["pnl_pct"]) - float(d.get("forward_pnl") or 0), 4),
            "legs": legs,
            "spread_pct": round(best[1] - worst[1], 4) if best[1] is not None else None,
            "best_leg": best[0],
            "worst_leg": worst[0],
        }
    finally:
        if own:
            conn.close()


def recent_decisions(limit: int = 20, conn=None) -> List[Dict[str, Any]]:
    """给前端选择器用：最近已结算决策。"""
    own = False
    if conn is None:
        import db as _db
        conn = _db.get_connection()
        own = True
    try:
        rows = conn.execute(
            """SELECT id, target_asset, suggested_action, entry_time,
                      round(COALESCE(forward_pnl,0),3) AS pnl, settled
                 FROM ai_decisions
                WHERE COALESCE(entry_time,'') <> ''
                ORDER BY id DESC LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for r in rows:
            out.append({"id": r[0], "asset": r[1], "action": r[2],
                        "entry_time": r[3], "pnl": r[4], "settled": r[5]})
        return out
    except Exception:
        return []
    finally:
        if own:
            conn.close()


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="反事实四条腿（只读）")
    ap.add_argument("decision_id", type=int, nargs="?", help="ai_decisions.id")
    ap.add_argument("--horizon", type=int, default=0, help="覆盖持有窗口（分钟）")
    ap.add_argument("--list", type=int, default=0, help="列出最近 N 笔可重放的决策")
    args = ap.parse_args(argv)
    if args.list:
        for r in recent_decisions(args.list):
            print(r)
        return 0
    if not args.decision_id:
        ap.print_help()
        return 2
    res = four_legs(args.decision_id, horizon_minutes=args.horizon or None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
