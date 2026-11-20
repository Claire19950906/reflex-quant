# -*- coding: utf-8 -*-
"""开仓位置闸 + 同向确认闸（v446 建立，v447 校准）。

要解决的不是"准不准"，而是 11-18 复盘里最贵的一件事：

  11 笔已结算里，5 笔进在区间极值（SELL 在窗口底部、BUY 在窗口顶部），
  这 5 笔贡献了 **96% 的亏损**；入场位置与 20 分钟盈亏的相关系数 r=0.69。
  另一条：决策前 30 分钟盘面已同向时命中 80%，未同向只有 25%（n=13，
  11-17 口径 48.7% vs 31.4% 同向）。

**v447 校准结论（2030 笔历史信号、60 天、`scripts/gate_sweep.py` 网格）**：

  · 位置闸被证伪：分方向相关系数只有 ±0.05（n≈1,100），1 小时档上
    "追极值"组反而更好（−0.275% vs −0.336%，t=−2.1）。11 笔样本里的
    r=0.69 是行情单边下跌造成的假象。**所以位置只记录、默认不拦单。**
  · 同向闸被证实，但窗口要短：只有 **20 分钟 / 0.10%** 这一档在 20m、1h、4h
    三个持有期上同时为正；30 分钟 / 0.05% 在 1 小时档是反向的。
    1 小时档放行 111 笔、毛 +0.084%/笔（0bps），扣 5bps 后 +0.034%/笔
    （Δ=+0.163pt，t=3.16）。**盈亏平衡成本约 8.4bps**，高于它就不赚。

所以这里保留两道"开仓前"的闸，但默认只让同向闸拦单：

  1. 位置闸（默认 `ENTRY_GUARD_POSITION_ENABLED=0`）：BUY 已在窗口顶部
     / SELL 已在窗口底部 → 标注 + 给回踩建议价，但**不拦**；
  2. 同向闸（默认生效判定）：决策前 20 分钟盘面没朝同一方向走出 0.10%
     → 只记影子，enforce 模式下不真开仓。

三条硬规矩：
  - **不许前视**：只取决策时刻及之前的 tick，回踩价只是建议、不是成交价；
  - **取不到就不判**：一个窗口少于 MIN_TICKS 个 tick，返回 available=False，
    既不拦也不放，绝不把"没数据"当成"安全"；
  - **默认影子模式**：ENTRY_GUARD_MODE=shadow 时只标注、不改线上动作；
    要真生效必须显式设 enforce（改动可回滚，一次环境变量的事）。

另外：校准出来的正期望只在**持有 1 小时**上成立（20 分钟档 Δ 仅 +0.013pt），
所以 evaluate() 会一并给出 recommended_hold_min=60，执行侧应按它改持有期。

用法:
  python3 entry_guard.py --report --days 7
  python3 entry_guard.py --check BTC SELL 1794964908
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

TRADABLE = ("BTC", "ETH", "SOL", "WTI", "XAU")

# 影子 / 生效 / 关闭
MODE = os.getenv("ENTRY_GUARD_MODE", "shadow").strip().lower()
# 位置闸阈值：BUY 在顶部 15% 内、SELL 在底部 15% 内视为"追"
BUY_MAX_POS = float(os.getenv("ENTRY_GUARD_BUY_MAX_POS", "0.85"))
SELL_MIN_POS = float(os.getenv("ENTRY_GUARD_SELL_MIN_POS", "0.15"))
# v447：位置闸默认**不拦单**（2030 笔实测为噪声，1 小时档甚至反向）。
# 留它是因为归档时"这条单追在哪"仍然有用；要真拦必须显式打开。
POSITION_GATE_ENABLED = os.getenv("ENTRY_GUARD_POSITION_ENABLED", "0").strip() in ("1", "true", "yes")
# 同向闸：v447 校准为 20 分钟 / 0.10%（30 分钟 / 0.05% 在 1 小时档是反向的）
CONFIRM_WINDOW_MIN = int(os.getenv("ENTRY_GUARD_CONFIRM_WINDOW", "20"))
CONFIRM_MIN_MOVE_PCT = float(os.getenv("ENTRY_GUARD_CONFIRM_MIN_MOVE_PCT", "0.10"))
# 校准出的正期望只在 1 小时持有上成立；20 分钟档 Δ 只有 +0.013pt
RECOMMENDED_HOLD_MIN = int(os.getenv("ENTRY_GUARD_HOLD_MIN", "60"))
# v447：只有 BTC 上被验证过（94 笔放行、1h +0.031%、4h +0.080%）；
# NVDA 上同一道闸是负优化（放行组 −0.74% vs 拦掉组 −0.11%），ETH/SOL 样本不足。
# 所以 enforce 默认只对 BTC 生效，其他品种只标注不拦。
ENFORCE_ASSETS = tuple(a.strip().upper() for a in
                       os.getenv("ENTRY_GUARD_ENFORCE_ASSETS", "BTC").split(",")
                       if a.strip())
# 计算位置用的窗口
WINDOWS_MIN = (20, 60)
# 少于这个 tick 数就当"看不到区间"，不下判断
MIN_TICKS = 5
# 扫描时判定"能下结论"的最小样本
MIN_CONCLUSION_N = 30


# ────────────────────────── 基础设施 ──────────────────────────
def _ro_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def load_ticks(conn: sqlite3.Connection, asset: str, start_ts: float,
               end_ts: float) -> List[Tuple[int, float]]:
    """取 [start_ts, end_ts] 内的 tick，按时间升序。"""
    rows = conn.execute(
        "SELECT ts, price FROM market_ticks "
        "WHERE symbol=? AND ts>=? AND ts<=? AND price>0 "
        "ORDER BY ts ASC",
        (asset.upper(), int(math.floor(start_ts)), int(math.ceil(end_ts))),
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


# ────────────────────────── 位置 / 同向 ──────────────────────────
def range_metrics(ticks: Sequence[Tuple[int, float]],
                  entry_price: Optional[float] = None) -> Dict[str, Any]:
    """当前价在窗口内的位置：0=最低点，1=最高点。

    entry_price 缺省取窗口最后一笔（决策时刻最近价）。
    区间跨度为 0 时返回 position=None —— 价格没动，"位置"没有意义。
    """
    if len(ticks) < MIN_TICKS:
        return {"available": False, "reason": f"tick 不足（{len(ticks)} < {MIN_TICKS}）",
                "samples": len(ticks)}
    prices = [p for _, p in ticks]
    high, low = max(prices), min(prices)
    entry = float(entry_price) if entry_price else prices[-1]
    span = high - low
    if span <= 0 or entry <= 0:
        return {"available": False, "reason": "区间跨度为 0（窗口内价格没动）",
                "samples": len(ticks), "high": high, "low": low, "entry": entry}
    pos = max(0.0, min(1.0, (entry - low) / span))
    return {
        "available": True,
        "position": round(pos, 4),
        "high": round(high, 6),
        "low": round(low, 6),
        "entry": round(entry, 6),
        "span_pct": round(span / low * 100.0, 4) if low > 0 else None,
        "samples": len(ticks),
    }


def pre_confirm(ticks: Sequence[Tuple[int, float]], action: str,
                window_min: int = CONFIRM_WINDOW_MIN,
                min_move_pct: float = CONFIRM_MIN_MOVE_PCT) -> Dict[str, Any]:
    """决策前 window_min 分钟，盘面是否已经朝 action 的方向走。"""
    act = (action or "").upper()
    if act not in ("BUY", "SELL"):
        return {"available": False, "reason": "非方向性动作"}
    if len(ticks) < MIN_TICKS:
        return {"available": False, "reason": f"tick 不足（{len(ticks)} < {MIN_TICKS}）",
                "samples": len(ticks)}
    now_ts, now_price = ticks[-1]
    target = now_ts - window_min * 60
    # 取窗口起点最近的一笔（起点之前不算，避免把更早的行情算进来）
    base: Optional[Tuple[int, float]] = None
    for ts, p in ticks:
        if ts >= target:
            base = (ts, p)
            break
    if base is None or base[1] <= 0:
        return {"available": False, "reason": "窗口起点无 tick", "samples": len(ticks)}
    move_pct = (now_price / base[1] - 1.0) * 100.0
    same = (move_pct >= min_move_pct) if act == "BUY" else (move_pct <= -min_move_pct)
    return {
        "available": True,
        "pre_confirm": 1 if same else 0,
        "move_pct": round(move_pct, 4),
        "window_min": window_min,
        "threshold_pct": min_move_pct,
        "samples": len(ticks),
    }


def evaluate(conn: sqlite3.Connection, asset: str, action: str,
             ts: Optional[float] = None,
             window_min: int = WINDOWS_MIN[0]) -> Dict[str, Any]:
    """给一条方向决策打"位置 + 同向"标签，并给出是否该拦。

    返回里 block=True 只代表"按规则该拦"，是否真的拦由调用方按 MODE 决定。
    """
    act = (action or "").upper()
    asset_u = (asset or "").upper()
    ts = float(ts if ts is not None else time.time())
    out: Dict[str, Any] = {
        "available": False,
        "mode": MODE,
        "asset": asset_u,
        "action": act,
        "asof_ts": int(ts),
        "position": None,
        "pre_confirm": None,
        "block": False,
        "block_reasons": [],
        "suggested_entry": None,
        "position_gate": {"enabled": POSITION_GATE_ENABLED, "would_block": False},
        "recommended_hold_min": RECOMMENDED_HOLD_MIN,
        "notes": [],
    }
    if act not in ("BUY", "SELL"):
        out["notes"].append("非方向性动作，位置闸不适用")
        return out
    if asset_u not in TRADABLE:
        out["notes"].append(f"{asset_u} 不在可结算品种白名单，位置闸不适用")
        return out

    # 位置：主窗口 20 分钟
    t_start = ts - window_min * 60
    ticks = load_ticks(conn, asset_u, t_start, ts)
    m = range_metrics(ticks)
    out["position"] = m.get("position")
    out["window_min"] = window_min
    out["range"] = {k: m.get(k) for k in ("available", "high", "low", "entry",
                                          "span_pct", "samples", "reason")}
    if not m.get("available"):
        out["notes"].append(f"位置不可算：{m.get('reason')}")

    # 同向：从窗口起点前 30 分钟一直到决策时刻
    c_start = ts - CONFIRM_WINDOW_MIN * 60
    cticks = load_ticks(conn, asset_u, c_start, ts)
    pc = pre_confirm(cticks, act)
    out["pre_confirm"] = pc.get("pre_confirm")
    out["confirm_move_pct"] = pc.get("move_pct")
    out["confirm"] = pc
    if not pc.get("available"):
        out["notes"].append(f"同向不可判：{pc.get('reason')}")

    reasons: List[str] = []
    pos_gate: Dict[str, Any] = {"enabled": POSITION_GATE_ENABLED, "would_block": False,
                                "thresholds": {"buy_max_pos": BUY_MAX_POS,
                                               "sell_min_pos": SELL_MIN_POS}}
    if m.get("available"):
        pos = float(m["position"])
        chase_text = None
        if act == "BUY" and pos >= BUY_MAX_POS:
            chase_text = (f"追在区间顶部：价格位于 {window_min} 分钟窗口的 {pos:.0%} 分位"
                          f"（≥{BUY_MAX_POS:.0%}），上沿 {m['high']}")
        if act == "SELL" and pos <= SELL_MIN_POS:
            chase_text = (f"追在区间底部：价格位于 {window_min} 分钟窗口的 {pos:.0%} 分位"
                          f"（≤{SELL_MIN_POS:.0%}），下沿 {m['low']}")
        pos_gate["would_block"] = bool(chase_text)
        pos_gate["reason"] = chase_text
        if chase_text and POSITION_GATE_ENABLED:
            # 只有显式打开位置闸才真拦；默认只标注（v447 校准为噪声）
            reasons.append(chase_text)
        # 回踩建议价：回到窗口中部（50% 分位）——不论是否拦单都给，供人工参考
        if m["low"] > 0:
            out["suggested_entry"] = round(m["low"] + 0.5 * (m["high"] - m["low"]), 6)
    out["position_gate"] = pos_gate
    out["recommended_hold_min"] = RECOMMENDED_HOLD_MIN
    if pc.get("available") and pc.get("pre_confirm") == 0:
        reasons.append(
            f"盘面未同向：决策前 {CONFIRM_WINDOW_MIN} 分钟涨跌 {pc['move_pct']:+.2f}%，"
            f"未达到 {CONFIRM_MIN_MOVE_PCT:.2f}% 的同向门槛"
        )
    out["block_reasons"] = reasons
    out["block"] = bool(reasons)
    out["available"] = bool(m.get("available") or pc.get("available"))
    if out["suggested_entry"]:
        out["suggestion_text"] = (
            f"不追现价，等回踩到 {out['suggested_entry']}（窗口 50% 分位）再评估"
        )
    return out


def annotate(row: Dict[str, Any], result: Dict[str, Any],
             conn: Optional[sqlite3.Connection] = None,
             asof_ts: Optional[float] = None) -> Dict[str, Any]:
    """给 decision_guard 的结果挂上 entry_guard 读数；enforce 模式下改动作。

    只在 action 是 BUY/SELL 时调用；任何异常都由调用方兜住，不影响主判定。
    """
    own_conn = conn is None
    conn = conn or _ro_conn()
    try:
        asset = str(row.get("target_asset") or row.get("asset") or "NONE")
        action = str(result.get("action") or "HOLD")
        ts = asof_ts
        if ts is None:
            for key in ("decision_ts", "decided_ts", "ts_ms", "ts"):
                v = row.get(key)
                if v is None:
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                ts = f / 1000.0 if f > 1e12 else f
                break
        if ts is None:
            ts = time.time()
        info = evaluate(conn, asset, action, ts=ts)
    finally:
        if own_conn:
            conn.close()
    info["enforced"] = False
    info["enforce_assets"] = list(ENFORCE_ASSETS)
    # v447：enforce 只对已验证品种生效，未验证品种一律只标注
    if (MODE == "enforce" and action in ("BUY", "SELL") and info.get("block")
            and str(asset).upper() in ENFORCE_ASSETS):
        # 生效模式：把方向动作降为 HOLD，理由写清楚（可复盘）
        result["action"] = "HOLD"
        result["entry_guard_blocked_action"] = action
        result["verdict"] = (str(result.get("verdict") or "") +
                             " | 开仓闸拦截：" + "；".join(info["block_reasons"]))[:600]
        hr = result.get("hold_reason")
        if isinstance(hr, dict):
            hr["entry_guard"] = info["block_reasons"]
        elif hr:
            result["hold_reason"] = f"{hr}；开仓闸：{'；'.join(info['block_reasons'])}"
        else:
            result["hold_reason"] = {"entry_guard": info["block_reasons"]}
        info["enforced"] = True
    return info


# ────────────────────────── 回放 / 报告 ──────────────────────────
def _dir_return_pct(action: str, move_pct: Optional[float]) -> Optional[float]:
    """把价格涨跌换成分方向收益。"""
    if move_pct is None:
        return None
    act = (action or "").upper()
    if act in ("BUY", "LONG"):
        return float(move_pct)
    if act in ("SELL", "SHORT"):
        return -float(move_pct)
    return None


def _cost_pct(asset: str) -> float:
    try:
        import cost_model as cm
        return float(cm.round_trip_cost_bps(asset)) / 100.0
    except Exception:
        return 0.30  # 兜底按最贵档，宁可把结论压保守


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    usable = [r for r in rows if r.get("net_pct") is not None]
    if not usable:
        return {"n": 0, "note": "没有可结算样本"}
    nets = [float(r["net_pct"]) for r in usable]
    gross = [float(r["gross_pct"]) for r in usable]
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": len(usable),
        "avg_gross_pct": round(sum(gross) / len(gross), 4),
        "avg_net_pct": round(sum(nets) / len(nets), 4),
        "win_rate": round(wins / len(usable), 4),
        "sum_net_pct": round(sum(nets), 4),
    }


def scan(days: int = 7, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """用影子账本（4h 已结算、方向是闸门前的真实判断）回放两道闸。

    为什么用影子账本：它是"每条方向决策都记、到期结算"的那张表，
    不含执行层的手工干预，正好测"闸门本身有没有用"。
    """
    conn = _ro_conn(db_path)
    try:
        since = int(time.time()) - days * 86400
        raw = conn.execute(
            "SELECT decision_id, decided_ts, asset, direction, action_after, "
            "entry_price, exit_price, move_pct, hit, horizon_min "
            "FROM shadow_ledger "
            "WHERE hit IS NOT NULL AND settled_at IS NOT NULL "
            "AND decided_ts >= ? AND direction IN ('BUY','SELL') "
            "ORDER BY decided_ts ASC",
            (since,),
        ).fetchall()
        rows: List[Dict[str, Any]] = []
        for r in raw:
            asset = str(r["asset"] or "").upper()
            action = str(r["direction"] or "").upper()
            ts = float(r["decided_ts"])
            info = evaluate(conn, asset, action, ts=ts)
            gross = _dir_return_pct(action, r["move_pct"])
            cost = _cost_pct(asset)
            net = (gross - cost) if gross is not None else None
            rows.append({
                "decision_id": int(r["decision_id"]),
                "asset": asset,
                "action": action,
                "decided_ts": int(ts),
                "position": info.get("position"),
                "pre_confirm": info.get("pre_confirm"),
                "block": bool(info.get("block")),
                "block_reasons": info.get("block_reasons") or [],
                "range": info.get("range") or {},
                "confirm_move_pct": info.get("confirm_move_pct"),
                "gross_pct": round(gross, 4) if gross is not None else None,
                "cost_pct": cost,
                "net_pct": round(net, 4) if net is not None else None,
                "hit": int(r["hit"] or 0),
                "action_after": r["action_after"],
            })
    finally:
        conn.close()

    baseline = _summary(rows)
    blocked_rows = [r for r in rows if r["block"] and r["net_pct"] is not None]
    gated_rows = [r for r in rows if not r["block"] and r["net_pct"] is not None]
    blocked = _summary(blocked_rows)
    gated = _summary(gated_rows)
    if blocked_rows:
        winners_blocked = sum(1 for r in blocked_rows if float(r["net_pct"]) > 0)
        spared = sum(float(r["net_pct"]) for r in blocked_rows if float(r["net_pct"]) < 0)
        lost = sum(float(r["net_pct"]) for r in blocked_rows if float(r["net_pct"]) > 0)
        blocked["winners_blocked"] = winners_blocked
        blocked["losers_blocked"] = len(blocked_rows) - winners_blocked
        blocked["spared_pct"] = round(-spared, 4)      # 拦掉的亏损（正数=省下）
        blocked["forgone_pct"] = round(-lost, 4)       # 拦掉的盈利（负数）
        blocked["net_effect_pct"] = round(-sum(float(r["net_pct"]) for r in blocked_rows), 4)

    # 三个口径分开看：只位置、只同向、两个都要
    variants = {}
    for name, pred in (
        ("location_only", lambda r: any("区间" in x for x in r["block_reasons"])),
        ("confirm_only", lambda r: any("未同向" in x for x in r["block_reasons"])),
        ("both", lambda r: bool(r["block"])),
    ):
        kept = [r for r in rows if not pred(r)]
        variants[name] = _summary(kept)

    # 位置分桶：位置与结果到底有没有关系
    buckets = []
    for i in range(5):
        lo, hi = i / 5, (i + 1) / 5
        sel = [r for r in rows
               if r["position"] is not None and lo <= float(r["position"]) < hi
               or (i == 4 and r["position"] is not None and float(r["position"]) == 1.0)]
        s = _summary(sel)
        s["bucket"] = f"{int(lo*100)}–{int(hi*100)}%"
        s["avg_position"] = (round(sum(float(r["position"]) for r in sel) / len(sel), 4)
                             if sel else None)
        buckets.append(s)

    by_asset = []
    for asset in sorted({r["asset"] for r in rows}):
        sel = [r for r in rows if r["asset"] == asset]
        s = _summary(sel)
        s["asset"] = asset
        s["blocked"] = sum(1 for r in sel if r["block"])
        by_asset.append(s)

    total = len(rows)
    verdict = _verdict(total, baseline, blocked, gated)
    return {
        "generated_at": int(time.time()),
        "days": days,
        "mode": MODE,
        "config": {
            "buy_max_pos": BUY_MAX_POS,
            "sell_min_pos": SELL_MIN_POS,
            "window_min": WINDOWS_MIN[0],
            "confirm_window_min": CONFIRM_WINDOW_MIN,
            "confirm_min_move_pct": CONFIRM_MIN_MOVE_PCT,
            "min_ticks": MIN_TICKS,
            "min_conclusion_n": MIN_CONCLUSION_N,
        },
        "baseline": baseline,
        "blocked": blocked,
        "gated": gated,
        "variants": variants,
        "buckets": buckets,
        "by_asset": by_asset,
        "rows": rows,
        "verdict": verdict,
        "note": ("样本来自 shadow_ledger 的 4h 已结算方向单；毛收益按方向算，"
                 "净收益扣除该品种往返成本。样本 <30 一律不下结论。"),
    }


def _verdict(total: int, baseline: Dict[str, Any], blocked: Dict[str, Any],
             gated: Dict[str, Any]) -> str:
    if total < MIN_CONCLUSION_N:
        return (f"样本 {total} 笔 < {MIN_CONCLUSION_N}，任何差值都不构成结论；"
                f"当前只说明闸门会拦掉多少单，不代表它有效。")
    b = blocked.get("n") or 0
    if b == 0:
        return f"{total} 笔样本里没有一笔触发开仓闸，阈值可能过松或行情不在极值区。"
    saved = abs(blocked.get("spared_pct") or 0)
    forgone = abs(blocked.get("forgone_pct") or 0)
    delta = gated.get("avg_net_pct", 0) - baseline.get("avg_net_pct", 0)
    if delta <= 0:
        quality = "负优化：放行组比全样本更差，这道闸在当前样本上是错的"
    elif delta >= 0.05:
        quality = "正优化：放行组明显好于全样本"
    else:
        quality = "几乎没差别：可能是筛了个寂寞"
    return (f"{total} 笔样本里闸门拦掉 {b} 笔：拦掉的亏损 {saved:.3f}%、"
            f"拦掉的盈利 {forgone:.3f}%；放行组均值 {gated.get('avg_net_pct', 0):+.3f}% "
            f"vs 全样本 {baseline.get('avg_net_pct', 0):+.3f}%（差 {delta:+.3f}pt）→ {quality}。")


# ────────────────────────── 历史信号回放（v423 同一批 2030 笔） ──────────────────────────
_SIG_HORIZONS = {
    "20m": ("5m", 300, 1_200),
    "1h": ("15m", 900, 3_600),
    "4h": ("1h", 3_600, 14_400),
}


def load_ohlc(conn: sqlite3.Connection, symbol: str, tf: str,
              since_ts: int) -> List[Dict[str, float]]:
    rows = conn.execute(
        "SELECT ts, high, low, close FROM kline_bars "
        "WHERE symbol=? AND timeframe=? AND ts>=? ORDER BY ts ASC",
        (symbol.upper(), tf, int(since_ts) * 1000),
    ).fetchall()
    out = []
    for r in rows:
        ts = int(r[0])
        ts = ts // 1000 if ts > 10_000_000_000 else ts
        if not r[3]:
            continue
        out.append({"ts": ts, "high": float(r[1] or 0),
                    "low": float(r[2] or 0), "close": float(r[3] or 0)})
    return out


def _prior_bars(bars: List[Dict[str, float]], entry_ts: int,
                bar_sec: int, n: int) -> List[Dict[str, float]]:
    """只取**决策前已收盘**的 bar —— 不许用包含入场时刻的那根（前视）。"""
    prior = [b for b in bars if b["ts"] + bar_sec <= entry_ts]
    return prior[-n:] if prior else []


def _bar_position(prior: List[Dict[str, float]],
                  entry_price: float) -> Dict[str, Any]:
    if len(prior) < 2:
        return {"available": False, "reason": "决策前 K 线不足 2 根"}
    high = max(b["high"] for b in prior)
    low = min(b["low"] for b in prior)
    span = high - low
    if span <= 0 or entry_price <= 0:
        return {"available": False, "reason": "区间跨度为 0"}
    pos = max(0.0, min(1.0, (entry_price - low) / span))
    return {"available": True, "position": round(pos, 4),
            "high": high, "low": low, "bars": len(prior)}


def _bar_pre_confirm(prior_all: List[Dict[str, float]], entry_ts: int,
                     entry_price: float, action: str) -> Dict[str, Any]:
    start_ts = entry_ts - CONFIRM_WINDOW_MIN * 60
    base = None
    for b in prior_all:
        if b["ts"] >= start_ts:
            base = b
            break
    if base is None or base["close"] <= 0:
        return {"available": False, "reason": "30 分钟前无收盘价"}
    move = (entry_price / base["close"] - 1.0) * 100.0
    act = action.upper()
    same = move >= CONFIRM_MIN_MOVE_PCT if act == "BUY" else move <= -CONFIRM_MIN_MOVE_PCT
    return {"available": True, "pre_confirm": 1 if same else 0,
            "move_pct": round(move, 4)}


def _welch_t(a: List[float], b: List[float]) -> Optional[float]:
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return round((mb - ma) / se, 2) if se > 0 else None


def _gate_verdict(n: int, delta: Optional[float], t: Optional[float],
                  kept: Dict[str, Any]) -> str:
    if n < MIN_CONCLUSION_N:
        return f"样本不足（{n}<{MIN_CONCLUSION_N}），不下结论"
    if delta is None:
        return "无法比较（缺净收益）"
    if delta <= -0.02:
        return f"反向：被拦的那档反而更好 {delta:+.3f}pt，不能启用"
    if delta < 0.05:
        return f"几乎没有差别（{delta:+.3f}pt）"
    if t is not None and abs(t) >= 2.0:
        tail = "；但放行组仍是负期望" if (kept.get("avg") or 0) < 0 else "；放行组为正"
        return f"能筛掉更差的一档（{delta:+.3f}pt，|t|={abs(t):.2f}）{tail}"
    return f"方向对但不显著（{delta:+.3f}pt，|t|={abs(t) if t is not None else '—'}）"


def scan_signals(days: int = 60, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """用 t2_sim_trades 的 2030 笔历史信号验证位置闸/同向闸。

    与 shadow_ledger 的区别：这里样本大、可以分持有期看。
    关键口径：位置只用决策前已收盘的 K 线；出场用 cost_horizon_eval 的同一套
    exit_at（尾部未走完/空洞超限一律跳过）。净收益扣 30bps 往返成本。
    """
    import cost_horizon_eval as ce

    conn = _ro_conn(db_path)
    try:
        since = int(time.time()) - days * 86400
        entries = ce.load_entries(conn, since_ts=since, limit=5000)
        assets = sorted({e["asset"] for e in entries})
        bars_by_tf: Dict[str, Dict[str, List[Dict[str, float]]]] = {}
        for tf, _, _ in _SIG_HORIZONS.values():
            bars_by_tf[tf] = {a: load_ohlc(conn, a, tf, since - 2 * 86400) for a in assets}

        rows: List[Dict[str, Any]] = []
        for e in entries:
            asset, action = e["asset"], e["action"]
            ts, px = int(e["entry_ts"]), float(e["entry_price"])
            # 20 分钟位置：4 根 5m；同向：30 分钟
            b5 = bars_by_tf["5m"].get(asset) or []
            pos = _bar_position(_prior_bars(b5, ts, 300, 4), px)
            pc = _bar_pre_confirm([b for b in b5 if b["ts"] <= ts], ts, px, action)
            row = {
                "asset": asset, "action": action, "entry_ts": ts,
                "entry_price": px,
                "position": pos.get("position"),
                "position_available": pos.get("available"),
                "pre_confirm": pc.get("pre_confirm"),
                "confirm_move_pct": pc.get("move_pct"),
            }
            reasons: List[str] = []
            p = pos.get("position")
            # v447：位置闸默认不拦单（实测为噪声/反向），诊断里单列 location 一栏
            if POSITION_GATE_ENABLED and p is not None:
                if action == "BUY" and p >= BUY_MAX_POS:
                    reasons.append("追在区间顶部")
                if action == "SELL" and p <= SELL_MIN_POS:
                    reasons.append("追在区间底部")
            if pc.get("pre_confirm") == 0:
                reasons.append("盘面未同向")
            row["block_reasons"] = reasons
            row["block"] = bool(reasons)

            cost = _cost_pct(asset)
            row["net"] = {}
            for label, (tf, interval, sec) in _SIG_HORIZONS.items():
                bars = bars_by_tf[tf].get(asset) or []
                closes = [(b["ts"], b["close"]) for b in bars]
                exit_px = ce.exit_at(closes, ts + sec, interval)
                g = ce.gross_pct(action, px, exit_px) if exit_px else None
                row["net"][label] = (round(g - cost, 4) if g is not None else None)
            rows.append(row)
    finally:
        conn.close()

    # 按持有期汇总：只比较三个读数都齐全的样本，避免"缺数据"混进放行组
    horizons: Dict[str, Any] = {}
    for label in _SIG_HORIZONS:
        qual = [r for r in rows
                if r["net"].get(label) is not None
                and r["position"] is not None
                and r["pre_confirm"] is not None]

        def vals(sel):
            return [float(r["net"][label]) for r in sel]

        def loc_block(r):
            return ((r["action"] == "BUY" and float(r["position"]) >= BUY_MAX_POS)
                    or (r["action"] == "SELL" and float(r["position"]) <= SELL_MIN_POS))

        base = _snapshot(vals(qual))
        loc_b, loc_k = [r for r in qual if loc_block(r)], [r for r in qual if not loc_block(r)]
        conf_b, conf_k = [r for r in qual if r["pre_confirm"] == 0], \
                         [r for r in qual if r["pre_confirm"] == 1]
        both_b = [r for r in qual if loc_block(r) or r["pre_confirm"] == 0]
        both_k = [r for r in qual if not (loc_block(r) or r["pre_confirm"] == 0)]

        def gate(sb, sk):
            b, k = _snapshot(vals(sb)), _snapshot(vals(sk))
            delta = None
            if b["avg"] is not None and k["avg"] is not None:
                delta = round(k["avg"] - b["avg"], 4)   # 放行 - 拦掉
            t = _welch_t(vals(sk), vals(sb))
            v = _gate_verdict(len(vals(sb)) + len(vals(sk)), delta, t, k)
            return {"blocked": b, "kept": k, "delta_kept_minus_blocked": delta,
                    "t_stat": t, "verdict": v}

        buckets = []
        for i in range(5):
            lo, hi = i / 5, (i + 1) / 5
            sel = [r for r in qual
                   if lo <= float(r["position"]) < hi
                   or (i == 4 and float(r["position"]) == 1.0)]
            s = _snapshot(vals(sel))
            s["bucket"] = f"{int(lo*100)}–{int(hi*100)}%"
            buckets.append(s)

        horizons[label] = {
            "qualified": len(qual),
            "baseline": base,
            "location": gate(loc_b, loc_k),
            "confirm": gate(conf_b, conf_k),
            "both": gate(both_b, both_k),
            "buckets": buckets,
        }

    by_asset = []
    for asset in sorted({r["asset"] for r in rows}):
        sel = [r for r in rows if r["asset"] == asset
               and r["net"].get("1h") is not None
               and r["position"] is not None and r["pre_confirm"] is not None]
        s = _snapshot([float(r["net"]["1h"]) for r in sel])
        s["asset"] = asset
        s["blocked"] = sum(1 for r in sel
                           if ((r["action"] == "BUY" and float(r["position"]) >= BUY_MAX_POS)
                               or (r["action"] == "SELL" and float(r["position"]) <= SELL_MIN_POS))
                           or r["pre_confirm"] == 0)
        by_asset.append(s)
    by_asset.sort(key=lambda x: (x.get("n") or 0), reverse=True)

    # 一句话结论：只认统计，不认故事。
    h1 = horizons.get("1h") or {}
    loc_v = (h1.get("location") or {}).get("verdict") or "无数据"
    conf_v = (h1.get("confirm") or {}).get("verdict") or "无数据"
    base1 = (h1.get("baseline") or {}).get("avg")
    headline = (
        f"1 小时档：位置闸={loc_v}；同向闸={conf_v}；"
        f"放行后均值 {base1 if base1 is not None else '—'}%"
        + ("（仍为负，闸门只减亏、不产生盈利）" if isinstance(base1, (int, float)) and base1 < 0 else "")
    )

    return {
        "generated_at": int(time.time()),
        "days": days,
        "mode": MODE,
        "entries": len(rows),
        "with_position": sum(1 for r in rows if r["position"] is not None),
        "with_confirm": sum(1 for r in rows if r["pre_confirm"] is not None),
        "config": {
            "buy_max_pos": BUY_MAX_POS, "sell_min_pos": SELL_MIN_POS,
            "confirm_window_min": CONFIRM_WINDOW_MIN,
            "confirm_min_move_pct": CONFIRM_MIN_MOVE_PCT,
            "cost_bps": 30.0,
        },
        "horizons": horizons,
        "by_asset": by_asset,
        "rows": rows,
        "headline": headline,
        "note": ("样本=t2_sim_trades 已结算历史信号（与『能不能赚钱』同一批）；"
                 "位置只用决策前已收盘 K 线；出场尾部/空洞不合格即跳过；净收益扣 30bps。"),
    }


def _snapshot(xs: List[float]) -> Dict[str, Any]:
    if not xs:
        return {"n": 0, "avg": None, "win_rate": None}
    avg = sum(xs) / len(xs)
    return {
        "n": len(xs),
        "avg": round(avg, 4),
        "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 4),
        "sum": round(sum(xs), 4),
        "best": round(max(xs), 3),
        "worst": round(min(xs), 3),
    }


# ────────────────────────── CLI ──────────────────────────
def _main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="开仓位置闸 / 同向确认闸")
    ap.add_argument("--signals", action="store_true",
                    help="用 2030 笔历史信号分持有期验证闸门")
    ap.add_argument("--report", action="store_true", help="回放影子账本并出报告")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--check", nargs=3, metavar=("ASSET", "ACTION", "TS"),
                    help="单条检查：资产 方向 决策时间戳(秒)")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)

    if a.check:
        asset, action, ts = a.check
        conn = _ro_conn(Path(a.db) if a.db else DB_PATH)
        try:
            info = evaluate(conn, asset, action, ts=float(ts))
        finally:
            conn.close()
        import json
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if a.signals:
        rep = scan_signals(days=a.days, db_path=Path(a.db) if a.db else DB_PATH)
        import json
        slim = {k: v for k, v in rep.items() if k != "rows"}
        print(json.dumps(slim, ensure_ascii=False, indent=2))
        return 0

    rep = scan(days=a.days, db_path=Path(a.db) if a.db else DB_PATH)
    import json
    # 报告不打印逐行明细，避免刷屏
    slim = {k: v for k, v in rep.items() if k != "rows"}
    print(json.dumps(slim, ensure_ascii=False, indent=2))
    print(f"\n结论：{rep['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
