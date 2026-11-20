"""新闻驱动方向到底有没有毛利：用真实入场点 + 真实 K 线重结算。

为什么单独建这个脚本：
  前面 v408~v419 验的都是**指标**（偏置、极端局面、波动、赔率）。但从"指标有没有区分力"
  到"这笔单子赚不赚钱"之间还差一层：**真实入场点上的毛利**。
  `t2_sim_trades` 里有一批 `source='news_replay'` 的成交记录（真实新闻时点、真实入场价），
  原来只按 20 分钟结算过一次。这个脚本把同一批入场点按多个持有期重结算，
  并且**用 K 线自己算毛利**，不信任表里的 `pnl_pct`（它有成本口径，混进去就分不清
  毛利和成本各自贡献多少）。

三条硬规矩：
  1. 毛利一律用 K 线收盘价算：(结算价 - 入场价) / 入场价，卖出方向取反；
  2. 成本单独成列，绝不"加回去"当毛利（v420 首版就这么错过：把 30bp 加回来，
     BTC 20 分钟胜率被算成 92.7%，纯属假值）；
  3. 样本 <30 一律报样本不足，不出结论。

用法：
    python3 scripts/news_edge_audit.py                      # 默认 20m/1h/4h/1d/2d
    python3 scripts/news_edge_audit.py --horizons 20,60,240,1440
    python3 scripts/news_edge_audit.py --asset BTC --cost-bps 8
    python3 scripts/news_edge_audit.py --no-write
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sqlite3
import statistics as st
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "trident_event_bus.db")
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "reports", "execution_edge")

DEFAULT_HORIZONS = (20, 60, 240, 1440, 2880)   # 分钟：20m / 1h / 4h / 1d / 2d
MIN_N = 30


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def _ro_conn(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def load_closes(conn: sqlite3.Connection, asset: str, timeframe: str = "1m") -> List[Tuple[int, float]]:
    """按 ts 去重（与 technical_state.load_candles 同口径），返回 (秒, 收盘价)。"""
    rows = conn.execute(
        "SELECT ts, close FROM (SELECT ts, close, "
        "ROW_NUMBER() OVER (PARTITION BY ts ORDER BY rowid DESC) AS rn "
        "FROM kline_bars WHERE symbol=? AND timeframe=? AND close IS NOT NULL) "
        "WHERE rn=1 ORDER BY ts", (asset, timeframe)).fetchall()
    return [(int(t) // 1000, float(p)) for t, p in rows]


def price_at(series: Sequence[Tuple[int, float]], ts: int) -> Optional[float]:
    """取 ts 之后最近一根的收盘价；没有就返回 None（不猜、不外推）。"""
    i = bisect.bisect_left(series, (ts, float("-inf")))
    return series[i][1] if i < len(series) else None


def load_trades(conn: sqlite3.Connection, asset: Optional[str] = None,
                source: str = "news_replay") -> List[Dict[str, Any]]:
    sql = ("SELECT asset, action, entry_ts, entry_price, cost_bps "
           "FROM t2_sim_trades WHERE source=? AND pnl_pct IS NOT NULL")
    args: List[Any] = [source]
    if asset:
        sql += " AND asset=?"
        args.append(asset.upper())
    rows = conn.execute(sql, args).fetchall()
    return [{"asset": a, "action": act, "entry_ts": int(ets or 0),
             "entry_price": float(ep or 0.0), "cost_bps": float(cb or 0.0)}
            for a, act, ets, ep, cb in rows]


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def summarize(vals: List[float], cost_pct: float) -> Dict[str, Any]:
    if not vals:
        return {"n": 0, "note": "无样本"}
    out = {
        "n": len(vals),
        "gross_median": round(st.median(vals), 4),
        "gross_mean": round(st.mean(vals), 4),
        "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100.0, 1),
        "net_median": round(st.median(vals) - cost_pct, 4),
        "cost_pct": round(cost_pct, 4),
    }
    out["verdict"] = _verdict(out)
    return out


def _verdict(s: Dict[str, Any]) -> str:
    """判定只用两条硬线：毛利中位是否为正、正毛利是否盖得住成本。"""
    g = s["gross_median"]
    if g <= 0:
        return "毛利为零或为负：方向没有边际，成本必然吃掉"
    if g < s["cost_pct"]:
        return f"毛利 {g}% 盖不住成本 {s['cost_pct']}%：扣成本后仍亏"
    return f"毛利 {g}% 高于成本 {s['cost_pct']}%：这一档站得住"


def evaluate(conn: sqlite3.Connection, horizons: Sequence[int],
             asset: Optional[str] = None, cost_bps: Optional[float] = None) -> Dict[str, Any]:
    trades = load_trades(conn, asset)
    if not trades:
        return {"asset": asset or "ALL", "error": "没有 news_replay 成交记录"}

    bucket: Dict[str, Dict[str, Any]] = {}
    for tf in ("1m",):
        for a in {t["asset"] for t in trades}:
            s = load_closes(conn, a, tf)
            if len(s) >= 1000:
                bucket[a] = {"tf": tf, "series": s}

    cost = cost_bps
    if cost is None:
        cbs = [t["cost_bps"] for t in trades if t["cost_bps"]]
        cost = st.median(cbs) if cbs else 0.0
    cost_pct = float(cost) / 100.0

    rows: List[Dict[str, Any]] = []
    for h in horizons:
        vals: List[float] = []
        per_asset: Dict[str, List[float]] = {}
        for t in trades:
            sub = bucket.get(t["asset"])
            if not sub or not t["entry_price"]:
                continue
            fwd = price_at(sub["series"], t["entry_ts"] + h * 60)
            if fwd is None:
                continue
            raw = (fwd - t["entry_price"]) / t["entry_price"] * 100.0
            gross = raw if t["action"] == "BUY" else -raw
            vals.append(gross)
            per_asset.setdefault(t["asset"], []).append(gross)
        entry: Dict[str, Any] = {"horizon_min": h, **summarize(vals, cost_pct)}
        entry["per_asset"] = {
            a: {"n": len(v), "gross_median": round(st.median(v), 4),
                "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100.0, 1)}
            for a, v in per_asset.items() if len(v) >= 50
        }
        rows.append(entry)

    # 信心分层：如果信心能分层，高信心那档毛利应该显著为正
    conf_rows: List[Dict[str, Any]] = []
    raw_rows = conn.execute(
        "SELECT confidence, pnl_pct, cost_bps, asset, action, entry_ts, entry_price "
        "FROM t2_sim_trades WHERE source='news_replay' AND pnl_pct IS NOT NULL "
        + ("AND asset=? " if asset else ""),
        ([asset.upper()] if asset else [])).fetchall()
    bins = ((0.0, 0.2, "0~0.2"), (0.2, 0.4, "0.2~0.4"), (0.4, 0.6, "0.4~0.6"),
            (0.6, 0.8, "0.6~0.8"), (0.8, 1.01, "0.8~1.0"))
    for lo, hi, name in bins:
        vals = []
        for conf, _pnl, _cb, a, act, ets, ep in raw_rows:
            if conf is None or not (lo <= float(conf) < hi):
                continue
            sub = bucket.get(a)
            if not sub or not ep:
                continue
            fwd = price_at(sub["series"], int(ets) + 60 * 60)   # 统一用 1h 结算
            if fwd is None:
                continue
            rawp = (fwd - float(ep)) / float(ep) * 100.0
            vals.append(rawp if act == "BUY" else -rawp)
        if len(vals) < MIN_N:
            conf_rows.append({"bucket": name, "n": len(vals), "note": "样本不足"})
            continue
        conf_rows.append({"bucket": name, **summarize(vals, cost_pct)})

    return {
        "asset": asset or "ALL",
        "source": "news_replay",
        "n_trades": len(trades),
        "n_with_series": sum(1 for t in trades if t["asset"] in bucket),
        "assets": sorted(bucket),
        "cost_bps": float(cost),
        "rows": rows,
        "confidence_1h": conf_rows,
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _fmt(v: Any, nd: int = 3) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def _markdown(payload: Dict[str, Any]) -> str:
    lines = [f"# 新闻驱动的毛利审计 · {payload.get('asset')}", "",
             f"- 来源：`t2_sim_trades` 的 `news_replay`（真实新闻时点 + 真实入场价）",
             f"- 样本：{payload.get('n_trades')} 笔，其中 {payload.get('n_with_series')} 笔有 1m K 线可结算",
             f"- 资产：{'、'.join(payload.get('assets') or [])}",
             f"- 成本：{payload.get('cost_bps')} bp（中位，含双边手续费与滑点假设）",
             "- 口径：**毛利一律用 K 线收盘价重算，不给表里的 `pnl_pct` 加回成本**", ""]
    lines += ["| 持有期 | n | 毛中位 | 毛均值 | 毛胜率 | 净中位（扣成本） | 判定 |",
              "|---|---|---|---|---|---|---|"]
    for r in payload.get("rows") or []:
        if not r.get("n"):
            lines.append(f"| {r['horizon_min']} 分 | {r.get('n') or 0} | 样本不足 | — | — | — | — |")
            continue
        lines.append(f"| {r['horizon_min']} 分 | {r['n']} | {_fmt(r['gross_median'])}% | "
                     f"{_fmt(r['gross_mean'])}% | {r['win_rate']}% | {_fmt(r['net_median'])}% | "
                     f"{r.get('verdict', '')} |")
    lines += ["", "### 按资产（以 1d 结算）", ""]
    for r in payload.get("rows") or []:
        if r.get("horizon_min") != 1440 or not r.get("per_asset"):
            continue
        lines += ["| 资产 | n | 毛中位 | 毛胜率 |", "|---|---|---|---|"]
        for a, v in sorted(r["per_asset"].items()):
            lines.append(f"| {a} | {v['n']} | {v['gross_median']}% | {v['win_rate']}% |")
        break
    conf = payload.get("confidence_1h") or []
    if conf:
        lines += ["", "### 按信心分层（统一 1h 结算）：信心有没有区分力", "",
                  "| 信心区间 | n | 毛中位 | 毛胜率 | 判定 |", "|---|---|---|---|---|"]
        for c in conf:
            if not c.get("n") or c.get("note"):
                lines.append(f"| {c['bucket']} | {c.get('n') or 0} | 样本不足 | — | — |")
                continue
            lines.append(f"| {c['bucket']} | {c['n']} | {_fmt(c['gross_median'])}% | "
                         f"{c['win_rate']}% | {c.get('verdict', '')} |")
    lines += ["", f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]
    return "\n".join(lines)


def _write(payload: Dict[str, Any]) -> None:
    os.makedirs(REPORT_DIR, exist_ok=True)
    name = payload.get("asset") or "ALL"
    with open(os.path.join(REPORT_DIR, f"{name}.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(REPORT_DIR, f"{name}.md"), "w", encoding="utf-8") as fh:
        fh.write(_markdown(payload))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="新闻驱动毛利审计")
    ap.add_argument("--asset", default=None, help="不填则全部资产合并")
    ap.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS))
    ap.add_argument("--cost-bps", type=float, default=None, help="不填则取成交记录里成本的中位")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    horizons = tuple(int(x) for x in str(args.horizons).split(",") if x.strip())
    conn = _ro_conn(args.db)
    try:
        payload = evaluate(conn, horizons, args.asset, args.cost_bps)
    finally:
        conn.close()

    if payload.get("error"):
        print(f"{payload.get('asset')}: {payload['error']}")
        return 1
    if not args.no_write:
        _write(payload)
    print(_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
