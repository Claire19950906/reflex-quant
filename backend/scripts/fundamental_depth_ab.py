#!/usr/bin/env python3
"""深度护栏影子评估（v490）—— 回答「gm_down / opm_down / negative_fcf 该不该进分」。

为什么单独做：
  · v488/v489 的深度读数只进解释、不进方向分。要给它权重，必须先证明
    「被护栏标记的票，之后确实跑得更差」；否则就是拿"看起来合理"替代验证。
  · 决策样本很稀（`decision_fundamental_snapshot` 目前只有十几行，且多数没有 ticker），
    所以门槛写死：**总可评估样本 ≥60 且两组各 ≥20** 才下结论；不到就写 insufficient。

口径：
  · 只用决策当时记下的 `depth_flags`，不回填、不重算；
  · 前向收益用 `ohlcv_daily` 的 +10 天收盘（与 v454 的主判据一致），缺行情就剔除；
  · 比较 `flagged`（有护栏）与 `clean`（无护栏）的平均前向收益与下跌比例；
  · 只读，不改任何决策、不改方向分。

用法：
  cd backend && python3 scripts/fundamental_depth_ab.py
  cd backend && python3 scripts/fundamental_depth_ab.py --horizon 20 --json
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

REPORT_DIR = BACKEND / "reports" / "fundamental_depth_ab"
_MIN_TOTAL = 60
_MIN_GROUP = 20


def _load(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            """SELECT decision_id, ts, asset, ticker, market, fund_score, fund_available,
                      depth_flags, depth_zone, depth_points
               FROM decision_fundamental_snapshot
               WHERE fund_available=1 AND ticker IS NOT NULL""").fetchall()
    except sqlite3.OperationalError:
        return []
    keys = ("decision_id", "ts", "asset", "ticker", "market", "fund_score",
            "fund_available", "depth_flags", "depth_zone", "depth_points")
    return [dict(zip(keys, r)) for r in rows]


def _symbol(asset: str, ticker: str) -> Optional[str]:
    """资产 → 日线符号；只认已知形态，猜不出就放弃（宁可少样本也不配错价格）。"""
    tk = str(ticker or "").upper()
    if not tk:
        return None
    if tk.endswith((".SH", ".SZ", ".HK")):
        try:
            import cn_prices as cp
            code, market = tk.split(".")[0], tk.split(".")[1]
            return cp.stock_symbol(code, market)
        except Exception:
            return None
    # `ohlcv_daily` 美股存的是裸 ticker（AAPL），不是 US_STOCK:AAPL；
    # 旧写法会让所有美股样本永远找不到行情。
    return tk


def _ret(conn: sqlite3.Connection, sym: str, ts: int, horizon: int) -> Optional[float]:
    base = _close_at_or_before(conn, sym, ts)
    if not base:
        return None
    later = _close_at_or_before(conn, sym, int(ts) + horizon * 86400)
    if not later or later[0] <= base[0]:
        return None
    return float(later[1]) / float(base[1]) - 1.0


def _flags(raw: Any) -> List[str]:
    if not raw:
        return []
    try:
        got = json.loads(raw) if isinstance(raw, str) else raw
        return [str(f) for f in got] if isinstance(got, list) else []
    except (TypeError, ValueError):
        return []


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "avg_ret": None, "median_ret": None, "down_rate": None}
    rets = sorted(float(r["ret"]) for r in rows)
    n = len(rets)
    mid = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2.0
    return {
        "n": n,
        "avg_ret": round(sum(rets) / n, 4),
        "median_ret": round(mid, 4),
        "down_rate": round(sum(1 for x in rets if x < 0) / n, 3),
    }


def build(conn: sqlite3.Connection, horizon: int = 10) -> Dict[str, Any]:
    snaps = _load(conn)
    try:
        total_rows = conn.execute(
            "SELECT COUNT(*) FROM decision_fundamental_snapshot").fetchone()[0]
        with_ticker = conn.execute(
            "SELECT COUNT(*) FROM decision_fundamental_snapshot "
            "WHERE fund_available=1 AND ticker IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        total_rows = with_ticker = len(snaps)
    flagged: List[Dict[str, Any]] = []
    clean: List[Dict[str, Any]] = []
    unknown = 0
    no_symbol = 0
    no_price = 0
    for s in snaps:
        raw = s.get("depth_flags")
        if raw is None:
            unknown += 1
            continue
        sym = _symbol(s.get("asset") or "", s.get("ticker") or "")
        if not sym:
            no_symbol += 1
            continue
        ret = _ret(conn, sym, int(s["ts"]), horizon)
        if ret is None:
            # A/H 目前没有本地日线历史（ohlcv_daily 只有美股/商品），先如实计入"无价格"
            no_price += 1
            continue
        row = {**s, "ret": round(ret, 4), "flags": _flags(raw)}
        (flagged if row["flags"] else clean).append(row)

    f_stats, c_stats = _stats(flagged), _stats(clean)
    total = len(flagged) + len(clean)
    enough = total >= _MIN_TOTAL and len(flagged) >= _MIN_GROUP and len(clean) >= _MIN_GROUP
    verdict = "insufficient"
    note = (f"可评估样本 {total}（门槛 {_MIN_TOTAL}），"
            f"flagged {len(flagged)} / clean {len(clean)}（每组门槛 {_MIN_GROUP}）")
    if enough:
        edge = (f_stats["avg_ret"] or 0.0) - (c_stats["avg_ret"] or 0.0)
        down_gap = (f_stats["down_rate"] or 0.0) - (c_stats["down_rate"] or 0.0)
        if edge <= -0.005 and down_gap >= 0.05:
            verdict = "flags_predict_underperformance"
            note = f"flagged 平均收益比 clean 低 {edge:+.2%}，下跌比例高 {down_gap:+.1%}"
        else:
            verdict = "no_edge"
            note = f"flagged 与 clean 差 {edge:+.2%}，下跌比例差 {down_gap:+.1%}，不足以支持入分"

    by_flag: Dict[str, int] = {}
    for row in flagged:
        for f in row["flags"]:
            by_flag[f] = by_flag.get(f, 0) + 1

    return {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "horizon_days": horizon,
        "min_total": _MIN_TOTAL,
        "min_group": _MIN_GROUP,
        "snapshots": total_rows,
        "snapshots_with_ticker": with_ticker,
        "unknown_depth": unknown,
        "no_symbol": no_symbol,
        "no_price": no_price,
        "flagged": f_stats,
        "clean": c_stats,
        "by_flag": dict(sorted(by_flag.items(), key=lambda kv: -kv[1])),
        "verdict": verdict,
        "note": note,
        "samples": {
            "flagged": [{k: r.get(k) for k in ("decision_id", "ts", "ticker", "ret", "flags")}
                        for r in flagged[:20]],
            "clean": [{k: r.get(k) for k in ("decision_id", "ts", "ticker", "ret")}
                      for r in clean[:20]],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="深度护栏影子评估（只读）")
    ap.add_argument("--horizon", type=int, default=10, help="持有窗口（天），默认 10")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = db.get_connection()
    try:
        payload = build(conn, args.horizon)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(f"[深度护栏 A/B] 窗口 {args.horizon} 天，快照 {payload['snapshots']} 行"
              f"（其中有 ticker {payload['snapshots_with_ticker']}，"
              f"无价格 {payload['no_price']}）")
        print(f"  flagged: n={payload['flagged']['n']} 平均收益={payload['flagged']['avg_ret']} "
              f"下跌比例={payload['flagged']['down_rate']}")
        print(f"  clean:   n={payload['clean']['n']} 平均收益={payload['clean']['avg_ret']} "
              f"下跌比例={payload['clean']['down_rate']}")
        print(f"[结论] {payload['verdict']} —— {payload['note']}")

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
