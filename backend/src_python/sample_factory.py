# -*- coding: utf-8 -*-
"""样本工厂（v461，只读）：把历史信号切成独立的时间段，再逐段结算。

要回答的问题只有一个：**v449 那格「BTC 20 分钟 / ≤2bps 可开火」是真的，还是
一段行情里的运气？**

做法：
  - 入场价用 t2_sim_trades 的真实 entry_price（不拿信号自己的价格自我实现）；
  - 出场只认**入场之后**的第一根 5m K 线收盘，缺口超过 10 分钟就跳过，
    绝不前视、绝不补数据；
  - 按时间等分成 N 段（walk-forward），每段单独算净期望、胜率、t；
  - 只有「合并后 t≥2 + 多数分段为正 + 最差分段不崩」才判可开火。

不写库、不发网络请求。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

DEFAULT_ASSET = "BTC"
DEFAULT_DAYS = 60
DEFAULT_FOLDS = 4
DEFAULT_COST_BPS = 2
MIN_N = 30
MIN_T = 2.0
# 单段最差也不能低于这个（否则"多数为正"只是被一段暴赚拉回来的）
WORST_FOLD_FLOOR = -0.50
# 出场缺口容忍：超过 horizon + 10 分钟就认为这根 K 线不可用
EXIT_GRACE_S = 600
HORIZONS: Tuple[Tuple[str, int], ...] = (("20m", 20 * 60), ("1h", 3600), ("4h", 4 * 3600))


def _ro_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


# ────────────────────────── 基础 ──────────────────────────
def load_bars(conn: sqlite3.Connection, asset: str, tf: str,
              since_ts: int) -> List[Dict[str, float]]:
    rows = conn.execute(
        "SELECT ts, open, high, low, close FROM kline_bars "
        "WHERE symbol=? AND timeframe=? AND ts>=? ORDER BY ts ASC",
        (asset.upper(), tf, int(since_ts) * 1000),
    ).fetchall()
    out: List[Dict[str, float]] = []
    for r in rows:
        ts = int(r["ts"])
        ts = ts // 1000 if ts > 10_000_000_000 else ts
        if r["close"]:
            out.append({"ts": ts, "open": float(r["open"] or r["close"]),
                        "high": float(r["high"] or r["close"]),
                        "low": float(r["low"] or r["close"]),
                        "close": float(r["close"])})
    return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _load_source(conn: sqlite3.Connection, table: str, asset: str,
                 since_ts: int, limit: int) -> List[Dict[str, Any]]:
    """按表结构取已结算的真实/回放样本；只认有入场价与方向的。"""
    if not _table_exists(conn, table):
        return []
    if table == "shadow_ledger":
        sql = ("SELECT COALESCE(decision_id, 0) AS id, asset, direction AS action, "
               "entry_ts, entry_price, confidence FROM shadow_ledger "
               "WHERE settled_at IS NOT NULL AND direction IN ('BUY','SELL') "
               "AND asset=? AND entry_ts>=? ORDER BY entry_ts ASC LIMIT ?")
        src = "shadow"
    elif table == "trade_decisions":
        sql = ("SELECT id, asset, COALESCE(final_action, intended_action) AS action, "
               "CAST(decision_ts AS INTEGER) AS entry_ts, entry_price, confidence "
               "FROM trade_decisions WHERE status='settled' "
               "AND COALESCE(final_action, intended_action) IN ('BUY','SELL') "
               "AND asset=? AND CAST(decision_ts AS INTEGER)>=? "
               "ORDER BY decision_ts ASC LIMIT ?")
        src = "live"
    elif table == "t2_sim_trades":
        sql = ("SELECT id, asset, action, entry_ts, entry_price, confidence "
               "FROM t2_sim_trades WHERE settled=1 AND action IN ('BUY','SELL') "
               "AND asset=? AND entry_ts>=? ORDER BY entry_ts ASC LIMIT ?")
        src = "sim"
    else:
        return []
    rows = conn.execute(sql, (asset.upper(), int(since_ts), int(limit))).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        px = r["entry_price"]
        if not px or float(px) <= 0:
            continue
        ts = int(r["entry_ts"])
        # trade_decisions.decision_ts 可能是毫秒
        if ts > 10_000_000_000:
            ts //= 1000
        out.append({"id": r["id"], "asset": r["asset"], "action": r["action"],
                    "entry_ts": ts, "entry_price": float(px),
                    "confidence": r["confidence"], "source": src})
    return out


def load_entries(conn: sqlite3.Connection, asset: str, since_ts: int,
                 limit: int = 5000, prefer_sim: bool = False) -> tuple[List[Dict[str, Any]], str]:
    """优先真实账本（影子账本 → 实盘），没有才退回回放模拟；返回值带来源标签。"""
    if prefer_sim:
        ents = _load_source(conn, "t2_sim_trades", asset, since_ts, limit)
        return ents, ("sim" if ents else "none")
    for table in ("shadow_ledger", "trade_decisions"):
        ents = _load_source(conn, table, asset, since_ts, limit)
        if ents:
            return ents, ents[0]["source"]
    ents = _load_source(conn, "t2_sim_trades", asset, since_ts, limit)
    return ents, ("sim" if ents else "none")


def _confirm_ok(action: str, entry_price: float, base_close: float,
                min_move_pct: float) -> bool:
    if not base_close or base_close <= 0:
        return False
    move = (float(entry_price) / float(base_close) - 1.0) * 100.0
    return move >= min_move_pct if action == "BUY" else move <= -min_move_pct


def apply_gate(entries: List[Dict[str, Any]], bars5: List[Dict[str, float]],
               window_min: int = 20, min_move_pct: float = 0.10) -> List[Dict[str, Any]]:
    """只留"决策前窗口起点已完成 5m bar 已同向走够"的信号；没有 bar 就丢，不猜。

    基准取窗口内**最早**一根已完成 bar（而不是最近一根）：要量的是"这 20 分钟"
    的方向，不是"最近 5 分钟"的方向。
    """
    kept: List[Dict[str, Any]] = []
    for e in entries:
        start = e["entry_ts"] - window_min * 60
        lo, hi = 0, len(bars5)
        while lo < hi:
            mid = (lo + hi) // 2
            if bars5[mid]["ts"] < start:
                lo = mid + 1
            else:
                hi = mid
        if lo >= len(bars5):
            continue
        base = bars5[lo]
        # 窗口起点那根 bar 必须已经收盘；否则它自己就是"未来"
        if base["ts"] + 300 > e["entry_ts"]:
            continue
        if _confirm_ok(e["action"], e["entry_price"], base["close"], min_move_pct):
            kept.append(dict(e, gate_base_ts=base["ts"]))
    return kept


def price_at(bars5: List[Dict[str, float]], target: int,
             grace_s: int = EXIT_GRACE_S) -> Optional[float]:
    """目标时刻的价格：夹在两根 5m 收盘之间就线性插值，避免"提前 5 分钟"或
    "延后 5 分钟"把持有期算短/算长；缺口超过 grace 就返回 None，不补数据。"""
    lo, hi = 0, len(bars5)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars5[mid]["ts"] < target:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(bars5):
        return None
    after = bars5[lo]
    if after["ts"] - target > grace_s:
        return None
    if lo == 0:
        return after["close"] if after["ts"] == target else None
    before = bars5[lo - 1]
    span = after["ts"] - before["ts"]
    if span <= 0 or target < before["ts"]:
        return before["close"]
    if target == before["ts"]:
        return before["close"]
    w = (target - before["ts"]) / span
    return before["close"] + w * (after["close"] - before["close"])


def exit_net(entry: Dict[str, Any], bars5: List[Dict[str, float]],
             horizon_s: int, cost_bps: float) -> Optional[float]:
    """入场后持有到 horizon 的精确价格（5m 收盘线性插值）结算。"""
    target = entry["entry_ts"] + horizon_s
    px = entry["entry_price"]
    if not px or px <= 0:
        return None
    exit_px = price_at(bars5, target)
    if exit_px is None:
        return None
    gross = (exit_px / px - 1.0) * 100.0
    if entry["action"] == "SELL":
        gross = -gross
    return gross - cost_bps / 100.0


def stats(vals: Sequence[float]) -> Dict[str, Any]:
    n = len(vals)
    if n == 0:
        return {"n": 0, "avg": None, "median": None, "win_rate": None, "t_stat": None,
                "best": None, "worst": None}
    avg = sum(vals) / n
    s = sorted(vals)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    win = sum(1 for v in vals if v > 0) / n
    var = sum((v - avg) ** 2 for v in vals) / n if n > 1 else 0.0
    sd = math.sqrt(var)
    t = avg / (sd / math.sqrt(n)) if sd > 0 and n > 1 else None
    return {"n": n, "avg": round(avg, 4), "median": round(med, 4),
            "win_rate": round(win, 4), "t_stat": round(t, 2) if t is not None else None,
            "best": round(max(vals), 3), "worst": round(min(vals), 3)}


def fold_bounds(start: int, end: int, folds: int) -> List[Tuple[int, int]]:
    if folds <= 0 or end <= start:
        return []
    step = (end - start) / folds
    return [(int(start + i * step), int(start + (i + 1) * step)) for i in range(folds)]


def fold_index(ts: int, bounds: Sequence[Tuple[int, int]]) -> int:
    for i, (a, b) in enumerate(bounds):
        if a <= ts < b:
            return i
    return -1


def promote(pooled: Dict[str, Any], folds: List[Dict[str, Any]]) -> Dict[str, Any]:
    """晋升门槛：样本够、合并 t≥2、多数分段为正、最差分段不崩。"""
    nonempty = [f for f in folds if f["stats"]["n"] > 0]
    pos = sum(1 for f in nonempty if (f["stats"]["avg"] or 0) > 0)
    need = math.ceil(0.75 * len(nonempty)) if nonempty else 0
    worst = min([f["stats"]["avg"] for f in nonempty], default=None)
    reasons: List[str] = []
    if (pooled.get("n") or 0) < MIN_N:
        reasons.append(f"样本 {pooled.get('n') or 0} < {MIN_N}")
    if (pooled.get("t_stat") or 0) < MIN_T:
        reasons.append(f"合并 t={pooled.get('t_stat')} < {MIN_T}")
    if not nonempty or pos < need:
        reasons.append(f"正收益分段 {pos}/{len(nonempty)}，需要 ≥{need}")
    if worst is not None and worst < WORST_FOLD_FLOOR:
        reasons.append(f"最差分段 {worst:.3f}% < {WORST_FOLD_FLOOR}%")
    return {"pass": not reasons, "positive_folds": pos, "nonempty_folds": len(nonempty),
            "need_positive": need, "worst_fold": worst, "reasons": reasons}


# ────────────────────────── 主报告 ──────────────────────────
def build(asset: str = DEFAULT_ASSET, days: int = DEFAULT_DAYS,
          folds: int = DEFAULT_FOLDS, cost_bps: int = DEFAULT_COST_BPS,
          db_path: Optional[Path] = None, prefer_sim: bool = False) -> Dict[str, Any]:
    now = int(time.time())
    since = now - days * 86400
    conn = _ro_conn(db_path)
    try:
        bars5 = load_bars(conn, asset, "5m", since - 86400)
        entries, source = load_entries(conn, asset, since, prefer_sim=prefer_sim)
    finally:
        conn.close()

    if not bars5:
        return {"asset": asset, "coverage": None, "source": source,
                "verdict": "样本不足",
                "headline": f"{asset} 没有 5m K 线，样本工厂无法结算（不补数据）"}
    bar_start, bar_end = bars5[0]["ts"], bars5[-1]["ts"]
    span_days = (bar_end - bar_start) / 86400.0
    expected = max(1, int(span_days * 24 * 12))
    coverage_ratio = len(bars5) / expected

    entries = [e for e in entries if bar_start <= e["entry_ts"] <= bar_end]
    gated = apply_gate(entries, bars5)
    bounds = fold_bounds(bar_start, bar_end, folds)

    cohorts: Dict[str, List[Dict[str, Any]]] = {"raw": entries, "gated": gated}
    results: List[Dict[str, Any]] = []
    for cohort, items in cohorts.items():
        for label, horizon_s in HORIZONS:
            nets: List[float] = []
            fold_nets: Dict[int, List[float]] = {i: [] for i in range(len(bounds))}
            skipped = 0
            for e in items:
                v = exit_net(e, bars5, horizon_s, cost_bps)
                if v is None:
                    skipped += 1
                    continue
                nets.append(v)
                fi = fold_index(e["entry_ts"], bounds)
                if fi >= 0:
                    fold_nets[fi].append(v)
            pooled = stats(nets)
            fold_rows = []
            for i, (a, b) in enumerate(bounds):
                fold_rows.append({
                    "fold": i + 1,
                    "start": a, "end": b,
                    "stats": stats(fold_nets[i]),
                })
            prom = promote(pooled, fold_rows)
            results.append({"cohort": cohort, "horizon": label, "horizon_s": horizon_s,
                            "pooled": pooled, "skipped": skipped, "folds": fold_rows,
                            "promotion": prom})

    # 选最优候选：通过晋升的里面取合并 t 最高；都不通过则取合并 t 最高
    passed = [r for r in results if r["promotion"]["pass"]]
    pool = passed or results
    best = max(pool, key=lambda r: (r["pooled"].get("t_stat") or -9e9)) if pool else None

    n = (best or {}).get("pooled", {}).get("n") or 0
    if n == 0:
        verdict = "样本不足"
        headline = (f"{asset} 没有可结算样本（来源 {source}）；"
                    f"真实账本从 0 开始积累，不拿回放模拟数据当开火依据")
    elif source == "sim":
        verdict = "仅观察仓"
        headline = (f"{asset} 当前只有回放模拟样本（{n} 笔），"
                    f"最多只能观察；模拟数据不能作为开火依据")
    elif n < MIN_N:
        verdict = "样本不足"
        headline = (f"{asset} 只有 {n} 笔真实样本（<{MIN_N}），"
                    f"不够分段；先积累、不开火")
    elif best and best["promotion"]["pass"]:
        verdict = "可开火"
        headline = (f"{asset} {best['horizon']} / {cost_bps}bps 通过分段："
                    f"{best['pooled']['n']} 笔，{best['pooled']['avg']:+.3f}%/笔，"
                    f"t={best['pooled']['t_stat']}，正分段 "
                    f"{best['promotion']['positive_folds']}/{best['promotion']['nonempty_folds']}")
    elif best and (best["pooled"].get("avg") or 0) > 0:
        verdict = "仅观察仓"
        headline = (f"{asset} {best['horizon']} 看着为正（{best['pooled']['avg']:+.3f}%/笔），"
                    f"但没过分段门槛：{'；'.join(best['promotion']['reasons'])}")
    else:
        verdict = "停单"
        if best:
            headline = (f"{asset} 在 {cost_bps}bps 下没有候选通过分段；"
                        f"最接近的是 {best['horizon']}（{best['pooled']['avg']:+.3f}%/笔）")
        else:
            headline = f"{asset} 没有候选可评估"

    return {
        "generated_at": now,
        "asset": asset,
        "source": source,
        "window_days": days,
        "folds": folds,
        "cost_bps": cost_bps,
        "coverage": {
            "bar_from": bar_start, "bar_to": bar_end,
            "span_days": round(span_days, 1),
            "bars": len(bars5), "expected_bars": expected,
            "coverage_ratio": round(coverage_ratio, 3),
            "entries": len(entries), "gated": len(gated),
        },
        "verdict": verdict,
        "headline": headline,
        "best": best,
        "candidates": results,
        "criteria": {"min_n": MIN_N, "min_t": MIN_T,
                     "worst_fold_floor": WORST_FOLD_FLOOR,
                     "exit_grace_s": EXIT_GRACE_S},
        "note": ("出场只认入场后第一根 5m 收盘；缺口>10 分钟直接跳过，不补数据、不前视。"
                 "分段按时间等分，每段单独结算，避免一段行情冒充全部证据。"),
    }


def _main() -> int:
    ap = argparse.ArgumentParser(description="样本工厂：分段验证")
    ap.add_argument("--asset", default=DEFAULT_ASSET)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--folds", type=int, default=DEFAULT_FOLDS)
    ap.add_argument("--cost", type=int, default=DEFAULT_COST_BPS)
    ap.add_argument("--prefer-sim", action="store_true",
                    help="只用 t2 回放样本（取证用；结论最多观察）")
    ap.add_argument("--db", default=None, help="指定数据库（默认线上只读）")
    a = ap.parse_args()
    rep = build(asset=a.asset, days=a.days, folds=a.folds, cost_bps=a.cost,
                db_path=Path(a.db) if a.db else None, prefer_sim=a.prefer_sim)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
