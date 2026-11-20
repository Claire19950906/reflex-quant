#!/usr/bin/env python3
"""基本面权重离线 A/B（v456）—— 回答「0.08 到底有没有用」。

背景：`decision_guard.DIRECTIONAL_WEIGHTS` 给 fundamental 0.08，这是**先验**，没有证据。
影子账本要等真实公司决策流入 + 过 10 天才能结算，太慢。这个脚本用
`decision_fundamental_snapshot`（v456 新增埋点）里"决策当时的分数"，配上决策后的
真实价格，比较「含基本面」与「不含基本面」两套方向分的表现。

三条铁律（写在代码里，不是注释口号）：
  1. **只用当时记下的分数**。不重算、不回填 —— 用今天的数据重算历史分数，等于
     用"事后知道的利润"去测"当时的判断"，A/B 会漂亮但毫无意义。
  2. **样本不足就明说 insufficient**，不拿 4 个样本编一个胜率。门槛 `_MIN_SAMPLES`。
  3. **不适用行直接剔除**（`fund_available=0`），不吃"基本面缺席时本来就该一样"的水分。

用法：
  python3 backend/scripts/fundamental_ab_replay.py                # 用 10 天窗口
  python3 backend/scripts/fundamental_ab_replay.py --horizon 5    # 换窗口
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import config
import db
from fundamental_shadow import _close_at_or_before

REPORT_DIR = BACKEND / "reports" / "fundamental_ab"
_MIN_SAMPLES = 30

_DIR_SIGN = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}


def _load_snapshots(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            """SELECT decision_id, ts, asset, ticker, market, fund_score, fund_available,
                      valuation, peer_rel_score, macro_tilt
               FROM decision_fundamental_snapshot
               WHERE fund_available=1 AND ticker IS NOT NULL""").fetchall()
    except sqlite3.OperationalError:
        return []
    keys = ("decision_id", "ts", "asset", "ticker", "market", "fund_score",
            "fund_available", "valuation", "peer_rel_score", "macro_tilt")
    return [dict(zip(keys, r)) for r in rows]


def _symbol(asset: str, ticker: str) -> Optional[str]:
    """资产 → 日线表里的符号。

    美股在 `ohlcv_daily` 用的是 `US_STOCK:<TICKER>` 这种前缀，A股/港股用
    `cn_prices` 那套 `sh600519` 风格。这里只做**已知形态**的映射，猜不出来就放弃
    （宁可少一条样本，也不要拿错标的的价格算收益）。
    """
    tk = str(ticker or "").upper()
    if not tk:
        return None
    if tk.endswith(".SH") or tk.endswith(".SZ") or tk.endswith(".HK"):
        try:
            import cn_prices as cp
            code, market = tk.split(".")[0], tk.split(".")[1]
            return cp.stock_symbol(code, market)
        except Exception:
            return None
    # ohlcv_daily 美股是裸 ticker（AAPL）；旧写法 US_STOCK:AAPL 永远查不到
    return tk


def _ret(conn: sqlite3.Connection, sym: str, ts: int, horizon_days: int) -> Optional[float]:
    base = _close_at_or_before(conn, sym, ts)
    if not base:
        return None
    later = _close_at_or_before(conn, sym, int(ts) + horizon_days * 86400)
    if not later or later[0] <= base[0]:
        return None
    return float(later[1]) / float(base[1]) - 1.0


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "hit_rate": None, "avg_edge": None}
    hits = sum(1 for r in rows if (r["ret"] * r["sign_with"]) > 0)
    edge = sum(r["ret"] * r["sign_with"] for r in rows) / len(rows)
    edge_no = sum(r["ret"] * r["sign_without"] for r in rows) / len(rows)
    return {"n": len(rows),
            "hit_rate": round(hits / len(rows), 3),
            "avg_edge": round(edge, 4),
            "avg_edge_without": round(edge_no, 4),
            "delta": round(edge - edge_no, 4)}


def main() -> int:
    ap = argparse.ArgumentParser(description="基本面权重离线 A/B（只读）")
    ap.add_argument("--horizon", type=int, default=10, help="持有窗口（天）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = db.get_connection()
    try:
        snaps = _load_snapshots(conn)
        samples = []
        for s in snaps:
            sym = _symbol(s.get("asset") or "", s.get("ticker") or "")
            if not sym:
                continue
            ret = _ret(conn, sym, int(s["ts"]), args.horizon)
            if ret is None:
                continue
            score = float(s.get("fund_score") or 0.0)
            # 方向分的符号：基本面为正→看多、为负→看空；0 = 无观点
            sign_with = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
            if sign_with == 0:
                continue
            samples.append({**s, "ret": round(ret, 4), "sign_with": sign_with,
                            "sign_without": 0.0})
    finally:
        conn.close()

    with_group = _stats(samples) if samples else {"n": 0}
    verdict = "insufficient" if with_group["n"] < _MIN_SAMPLES else (
        "fundamental_adds_value" if (with_group.get("delta") or 0) > 0 else "fundamental_hurts")
    payload = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "horizon_days": args.horizon,
        "min_samples": _MIN_SAMPLES,
        "with_fundamental": with_group,
        "verdict": verdict,
        "note": ("样本不足，不作结论（不做假验证）：需要先有足够多「公司标的」决策被"
                 "`decision_fundamental_snapshot` 记录下来，并等持有窗口走完。"
                 if verdict == "insufficient" else ""),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        n = with_group.get("n", 0)
        print(f"[基本面 A/B] 窗口 {args.horizon} 天，可用样本 {n}（门槛 {_MIN_SAMPLES}）")
        if n:
            print(f"  含基本面: 胜率 {with_group['hit_rate']:.0%}，"
                  f"平均方向收益 {with_group['avg_edge']:+.3f}")
            print(f"  去基本面: 平均方向收益 {with_group['avg_edge_without']:+.3f}"
                  f"（差 {with_group['delta']:+.3f}）")
        print(f"[结论] {verdict}" + (f" —— {payload['note']}" if payload["note"] else ""))
    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
