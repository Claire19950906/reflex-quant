#!/usr/bin/env python3
"""analysis_selector_backtest.py — 旧闸门 vs 新档位的离线对比。

只读。标签用「事后 60 分钟价格是否朝模型方向走」做代理（不是真实成交，
但足以比较两个选择器会不会把好样本挡在门外）。

对比对象：
  旧 selector = 实际发生的结果（`suggested_action` 是 BUY/SELL 就算通过）
  新 selector = `analysis.confidence.rank` 给出的 A / A+B / C

用法：
  python3 backend/scripts/analysis_selector_backtest.py --date 2026-11-16
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_BACKEND, "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from analysis import confidence as cf
from analysis import contract as ac

_BJ = timezone(timedelta(hours=8))
_ONE_MIN = ("BTC", "ETH", "SOL", "XAU")
_COMPRESSED = ("summary_only", "target_none", "llm_empty")


def _connect(p): 
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30.0)
    c.row_factory = sqlite3.Row
    return c


def _ms(s):
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_BJ)
    return int(dt.timestamp() * 1000)


def _close(c, sym, t):
    r = c.execute("SELECT close FROM kline_bars WHERE symbol=? AND timeframe='1m' "
                  "AND ts<=? AND close>0 ORDER BY ts DESC LIMIT 1", (sym, t)).fetchone()
    return float(r[0]) if r else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(_BACKEND, "trident_event_bus.db"))
    ap.add_argument("--date", default=None)
    ap.add_argument("--horizon", type=int, default=60)
    args = ap.parse_args()

    c = _connect(args.db)
    try:
        rows = c.execute(
            "SELECT id, created_at, target_asset, sentiment_score, suggested_action, "
            "  original_action, market_confirmation, hard_data_status, "
            "  analysis_degraded, analysis_degraded_reason, evidence_confidence, "
            "  trade_gate_reason, forward_pnl "
            "FROM ai_decisions WHERE created_at >= ? " +
            ("AND created_at < ? " if args.date else ""),
            ([f"{args.date}T00:00:00", f"{args.date}T23:59:59.999"] if args.date
             else ["2026-11-01T00:00:00"]),
        ).fetchall()
    finally:
        c.close()

    c = _connect(args.db)
    samples = []
    try:
        for r in rows:
            direction = str(r["original_action"] or "").upper()
            if direction not in ("BUY", "SELL"):
                continue
            sym = ac.canonical_asset(r["target_asset"])
            if sym not in _ONE_MIN:
                continue
            p0 = _close(c, sym, _ms(r["created_at"]))
            p1 = _close(c, sym, _ms(r["created_at"]) + args.horizon * 60_000)
            if not p0 or not p1:
                continue
            ret = (p1 - p0) / p0 * 100 * (1 if direction == "BUY" else -1)
            compressed = str(r["analysis_degraded_reason"] or "") in _COMPRESSED
            v = cf.rank(
                score=r["sentiment_score"] or 0.0,
                coverage=cf.coverage_from_hard_status(
                    r["hard_data_status"] or "", r["market_confirmation"] or ""),
                structure=0.3 if compressed else 0.8,
                consistency=1.0,
                compressed=compressed,
                degraded=False,   # 这里只评「有方向」的样本，不看降级标记
            )
            samples.append({
                "id": r["id"], "asset": sym, "dir": direction, "ret": ret,
                "old_pass": str(r["suggested_action"] or "").upper() in ("BUY", "SELL"),
                "tier": v.tier, "conf": v.confidence,
                "compressed": compressed, "pnl": r["forward_pnl"],
            })
    finally:
        c.close()

    n = len(samples)
    print(f"\n== 选择器离线对比 ==  有方向且有 1m 价的样本 = {n}  持有期 = {args.horizon} 分钟\n")
    if not n:
        return 0

    def _stat(title, picked):
        if not picked:
            print(f"  {title:<18} 选中 0")
            return
        win = sum(1 for x in picked if x["ret"] > 0)
        avg = sum(x["ret"] for x in picked) / len(picked)
        print(f"  {title:<18} 选中 {len(picked):>2}/{n}  胜率 {win / len(picked) * 100:5.1f}%"
              f"  平均 {avg:+.3f}%")

    print("旧选择器（实际通过闸门的）")
    _stat("已下单", [x for x in samples if x["old_pass"]])
    _stat("被拦下", [x for x in samples if not x["old_pass"]])
    print("\n新选择器（analysis.confidence.rank）")
    _stat("A 可建仓", [x for x in samples if x["tier"] == cf.TIER_A])
    _stat("A+B 影子", [x for x in samples if x["tier"] in (cf.TIER_A, cf.TIER_B)])
    _stat("C 只观察", [x for x in samples if x["tier"] == cf.TIER_C])
    _stat("A+B+C 有方向", [x for x in samples if x["tier"] != cf.TIER_D])

    print("\n逐条（按新档位、收益排序）")
    for x in sorted(samples, key=lambda z: (z["tier"], -z["ret"])):
        flag = "压缩" if x["compressed"] else "完整"
        print(f"  {x['tier']:<12} id={x['id']:<6} {x['asset']:<4} {x['dir']:<4} "
              f"conf={x['conf']:>5.1f} 收益={x['ret']:+6.2f}% {flag}"
              f"{'  [旧闸门放行]' if x['old_pass'] else ''}")

    print("\n口径：样本只有「模型给过方向」的行；60 分钟单周期、无止损、无成本。"
          "结论只用于判断选择器是否『负向选择』，不作为收益承诺。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
