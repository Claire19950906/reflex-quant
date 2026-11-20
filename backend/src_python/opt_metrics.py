# -*- coding: utf-8 -*-
"""opt_metrics.py — v336 度量口径修正（让准确率第一次可比）

三个问题，三个纯函数：

1. **同日重复计数** —— 一次行情被算成几百个样本。
   实测 2026-11-05 有 305 条 BUY、只中 2 条；11-08 有 413 条 BUY、一条没中。
   同一条新闻在同小时对同一品种发出 300 次同向建议，本质是 1 个独立信号。
   → `dedupe_independent(rows)`：按 (asset, direction, ts//3600) 去重，只留首条。

2. **桶标的混进分母** —— target_asset = NONE / US_STOCK / INDIVIDUAL_STOCK
   这类"品种桶"无法结算，却把分母撑大、把命中率稀释。
   → `split_tradable(rows)`：桶标的单独计数，不进主指标。

3. **区间缺 Wilson** —— 小样本点估计没有可信区间，结论必然摇摆。
   → `wilson(k, n)`：95% 区间。

只读：本模块不写库。CLI 直接读 `trade_decisions` / `shadow_ledger`。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"

# 不能结算的"品种桶"（有方向但拿不到对应价格序列）
BUCKET_ASSETS = frozenset({
    "NONE", "", "UNKNOWN", "US_STOCK", "INDIVIDUAL_STOCK", "CRYPTO_MAJOR",
    "CN_STOCK", "ETF", "INDEX", "OTHER", "多品种", "美股", "个股", "加密主流",
})


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """Wilson 95% 区间（百分比）。n=0 返回 (None, None)。"""
    if n <= 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0)) / den
    lo = max(0.0, centre - half) * 100
    hi = min(1.0, centre + half) * 100
    return round(lo, 1), round(hi, 1)


def normalize_direction(raw: Any) -> Optional[str]:
    d = str(raw or "").strip().upper()
    if d in ("BUY", "LONG", "做多", "买入"):
        return "BUY"
    if d in ("SELL", "SHORT", "做空", "卖出"):
        return "SELL"
    return None


def split_tradable(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按 asset 是否可结算拆成 (可交易, 桶标的)。"""
    ok, bucket = [], []
    for r in rows:
        a = str(r.get("asset") or "").strip().upper()
        (bucket if a in BUCKET_ASSETS else ok).append(r)
    return ok, bucket


def dedupe_independent(rows: Iterable[Dict[str, Any]],
                       bucket_seconds: int = 3600) -> List[Dict[str, Any]]:
    """同一小时内、同品种、同方向只保留首个独立信号。

    输入每行至少要有 asset / direction / ts / hit。
    去重后每条"独立信号"代表一次真实的市场判断，而不是一次批量重发。
    """
    seen: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    order: List[Tuple[str, str, int]] = []
    for r in rows:
        asset = str(r.get("asset") or "").strip().upper()
        direction = normalize_direction(r.get("direction"))
        if direction is None:
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        key = (asset, direction, int(ts // bucket_seconds))
        if key not in seen:
            seen[key] = r
            order.append(key)
    return [seen[k] for k in order]


def summarize(rows: Sequence[Dict[str, Any]], label: str = "") -> Dict[str, Any]:
    """点估计 + Wilson 区间 + 独立信号口径，一起给出。"""
    ok, bucket = split_tradable(rows)
    scored = [r for r in ok if r.get("hit") is not None]
    dice = dedupe_independent(scored)
    k = sum(1 for r in dice if int(r.get("hit") or 0) == 1)
    raw_k = sum(1 for r in scored if int(r.get("hit") or 0) == 1)
    lo, hi = wilson(k, len(dice))
    rlo, rhi = wilson(raw_k, len(scored))
    return {
        "label": label,
        "naive": {"k": raw_k, "n": len(scored),
                  "pct": round(100.0 * raw_k / len(scored), 1) if scored else None,
                  "ci": [rlo, rhi] if scored else None},
        "independent": {"k": k, "n": len(dice),
                        "pct": round(100.0 * k / len(dice), 1) if dice else None,
                        "ci": [lo, hi] if dice else None},
        "bucket_assets": {"n": len(bucket),
                          "sample": sorted({str(r.get("asset") or "") for r in bucket})[:8]},
        "compression": round(len(scored) / len(dice), 1) if dice else None,
    }


# ---------------------------------------------------------------- 数据读取

def _ro() -> sqlite3.Connection:
    if not DB.exists():
        raise SystemExit(f"DB 不存在: {DB}")
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def load_trade_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = []
    for r in conn.execute(
        "SELECT asset, intended_action, decision_ts, actual_outcome_pct FROM trade_decisions "
        "WHERE status='settled' AND actual_outcome_pct IS NOT NULL "
        "AND intended_action IN ('BUY','SELL')"
    ):
        hit = None
        d = normalize_direction(r["intended_action"])
        pct = float(r["actual_outcome_pct"])
        if d == "BUY":
            hit = 1 if pct > 0 else 0
        elif d == "SELL":
            hit = 1 if pct < 0 else 0
        rows.append({"asset": r["asset"], "direction": d,
                     "ts": r["decision_ts"], "hit": hit, "source": "trade"})
    return rows


def load_shadow_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = []
    for r in conn.execute(
        "SELECT asset, direction, decided_ts, hit FROM shadow_ledger WHERE hit IS NOT NULL"
    ):
        rows.append({"asset": r["asset"], "direction": normalize_direction(r["direction"]),
                     "ts": r["decided_ts"], "hit": r["hit"], "source": "shadow"})
    return rows


def format_report(rep: Dict[str, Any]) -> str:
    n = rep["naive"]; i = rep["independent"]
    def f(x):
        if not x["pct"]:
            return "—"
        return f"{x['pct']:.1f}% [{x['ci'][0]:.1f}, {x['ci'][1]:.1f}] (n={x['n']})"
    return (
        f"{rep['label']}\n"
        f"  朴素口径   {f(n)}\n"
        f"  独立信号   {f(i)}   ← 同小时同品种同方向只算一次\n"
        f"  压缩倍数   {rep['compression']}x   桶标的剔除 {rep['bucket_assets']['n']} 条"
        f" {rep['bucket_assets']['sample']}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = _ro()
    try:
        reps = [
            summarize(load_trade_rows(conn), "真实决策（trade_decisions 已结算）"),
            summarize(load_shadow_rows(conn), "影子账本（shadow_ledger 已结算）"),
        ]
    finally:
        conn.close()
    if args.json:
        print(json.dumps(reps, ensure_ascii=False, indent=2))
    else:
        for r in reps:
            print(format_report(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
