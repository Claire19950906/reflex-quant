#!/usr/bin/env python3
"""v230+ 准确率基线评估脚本 (CLI)。

读 quick_sim_trades (已结算) + ai_decisions + raw_news，计算并打印：
  - 总胜率 / 平均盈亏 / 最大回撤 / Sharpe / 盈亏比 / 利润因子
  - 按品种、按 action、按事件强度 分组
  - 按入场延迟分布 (<60s / 60-300s / 300-900s / 900s+)

输出: 人类可读表 + 可选 --json 输出到 stdout/file。
用法:
    python accuracy_baseline.py --days 30
    python accuracy_baseline.py --days 7 --asset BTC --json
    python accuracy_baseline.py --from 2026-09-01 --to 2026-10-07 --output report.json
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import db


def _parse_date(s: str) -> int:
    """YYYY-MM-DD 或 'YYYY-MM-DD HH:MM:SS' → Unix 秒 (本地时区)。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {s}")


def _safe_float(v, default=None):
    try:
        f = float(v) if v is not None else default
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _fetch_trades(
    conn: sqlite3.Connection,
    lo: Optional[int],
    hi: Optional[int],
    asset: Optional[str],
) -> List[Dict[str, Any]]:
    """按时间窗口 + 可选品种拉取已结算 trade。"""
    sql = """SELECT qs.id, qs.decision_id, qs.asset, qs.action,
                    qs.entry_price, qs.exit_price, qs.pnl_pct, qs.pnl_usdt,
                    qs.verdict, qs.entry_ts, qs.exit_ts,
                    qs.max_price, qs.min_price, qs.mfe_pct, qs.mae_pct,
                    qs.horizon_minutes,
                    ad.event_strength,
                    ad.sentiment_score AS score, ad.evidence_confidence,
                    rn.published_at AS news_ts, rn.content AS news_content
             FROM quick_sim_trades qs
             LEFT JOIN ai_decisions ad ON ad.id = qs.decision_id
             LEFT JOIN raw_news rn ON rn.id = qs.news_id
             WHERE qs.settled = 1"""
    params: List[Any] = []
    if lo is not None:
        sql += " AND qs.exit_ts >= ?"
        params.append(lo)
    if hi is not None:
        sql += " AND qs.exit_ts <= ?"
        params.append(hi)
    if asset:
        sql += " AND UPPER(qs.asset) = UPPER(?)"
        params.append(asset)
    sql += " ORDER BY qs.exit_ts ASC"
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"DB 查询失败: {exc}") from exc
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一组 trade 计算胜率/盈亏/Sharpe/回撤等汇总指标。

    所有 pnl_pct 单位都是 percent (= 0.02 表示 +2%)，输出统一保持 percent。
    """
    n = len(rows)
    if n == 0:
        return {"count": 0}
    pnls = [_safe_float(r["pnl_pct"], 0.0) or 0.0 for r in rows]
    pnls_usdt = [_safe_float(r["pnl_usdt"], 0.0) or 0.0 for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    sum_wins = sum(wins)
    sum_losses = abs(sum(losses))
    # 累计 PnL 曲线（单位 percent）→ 最大回撤 / 当前回撤
    cum: List[float] = []
    running = 0.0
    for p in pnls:
        running += p
        cum.append(running)
    peak = cum[0]
    max_dd = 0.0
    for c in cum:
        if c > peak:
            peak = c
        dd = peak - c
        if dd > max_dd:
            max_dd = dd
    cur_dd = max(peak - cum[-1], 0.0) if cum else 0.0
    # Sharpe (按年化，每笔近似 = 1 个 horizon_minutes 时段)
    sharpe = 0.0
    if len(pnls) > 1:
        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std > 0:
            sharpe = (mean / std) * math.sqrt(len(pnls))
    # 其它辅助指标
    avg_hold = sum(_safe_float(r["horizon_minutes"], 0.0) or 0.0 for r in rows) / n
    avg_score = sum(_safe_float(r["score"], 0.0) or 0.0 for r in rows) / n
    avg_mfe = sum(_safe_float(r["mfe_pct"], 0.0) or 0.0 for r in rows) / n
    avg_mae = sum(_safe_float(r["mae_pct"], 0.0) or 0.0 for r in rows) / n
    return {
        "count": n,
        "win_count": len(wins),
        "loss_count": len(losses),
        "breakeven_count": n - len(wins) - len(losses),
        "win_rate": round(len(wins) / n, 4),
        "avg_pnl_pct": round(sum(pnls) / n, 6),
        "median_pnl_pct": round(sorted(pnls)[n // 2], 6),
        "sum_pnl_usdt": round(sum(pnls_usdt), 4),
        "avg_win_pct": round(sum_wins / len(wins), 6) if wins else 0.0,
        "avg_loss_pct": round(-sum_losses / len(losses), 6) if losses else 0.0,
        "profit_factor": round(sum_wins / sum_losses, 4) if sum_losses > 0 else None,
        "payoff_ratio": round((sum_wins / len(wins)) / (sum_losses / len(losses)), 4)
            if wins and losses else None,
        "max_drawdown_pct": round(max_dd, 6),
        "current_drawdown_pct": round(cur_dd, 6),
        "sharpe_per_trade": round(sharpe, 4),
        "avg_hold_minutes": round(avg_hold, 2),
        "avg_score": round(avg_score, 4),
        "avg_mfe_pct": round(avg_mfe, 4),
        "avg_mae_pct": round(avg_mae, 4),
    }


def _bucket_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        v = str(r.get(key) or "unknown").upper()
        buckets.setdefault(v, []).append(r)
    return buckets


def _entry_delay_buckets(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """入场距新闻发布时间分布（秒）。

    news_ts 可能是 Unix 秒或 ISO 时间串；通过 config.parse_time_to_bj 统一解析。
    """
    out = {"<60s": 0, "60-300s": 0, "300-900s": 0, "900s+": 0}
    try:
        import config
        parse_time_to_bj = config.parse_time_to_bj
    except Exception:
        parse_time_to_bj = None
    for r in rows:
        et = _safe_float(r.get("entry_ts"))
        nt_raw = r.get("news_ts")
        if et is None or nt_raw is None:
            continue
        nt: Optional[float]
        if isinstance(nt_raw, (int, float)):
            nt = float(nt_raw)
        elif parse_time_to_bj is not None:
            try:
                dt_obj = parse_time_to_bj(nt_raw)
                nt = float(dt_obj.timestamp()) if dt_obj else None
            except Exception:
                nt = None
        else:
            nt = _safe_float(nt_raw)
        if nt is None:
            continue
        dt = et - nt
        if dt < 60:
            out["<60s"] += 1
        elif dt < 300:
            out["60-300s"] += 1
        elif dt < 900:
            out["300-900s"] += 1
        else:
            out["900s+"] += 1
    return out


def compute_baseline(
    conn: sqlite3.Connection,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    asset: Optional[str] = None,
) -> Dict[str, Any]:
    """计算准确率基线报告 (dict)。"""
    rows = _fetch_trades(conn, from_ts, to_ts, asset)
    overall = _stats(rows)
    by_asset = [
        {"asset": k, **_stats(v)}
        for k, v in sorted(_bucket_by(rows, "asset").items(), key=lambda kv: -len(kv[1]))
    ]
    by_action = [
        {"action": k, **_stats(v)}
        for k, v in sorted(_bucket_by(rows, "action").items(), key=lambda kv: -len(kv[1]))
    ]
    by_strength = [
        {"event_strength": k, **_stats(v)}
        for k, v in sorted(_bucket_by(rows, "event_strength").items(),
                           key=lambda kv: -len(kv[1]))
    ]
    return {
        "window": {
            "from_ts": from_ts,
            "to_ts": to_ts,
            "asset_filter": asset,
            "n_settled": len(rows),
        },
        "overall": overall,
        "by_asset": by_asset,
        "by_action": by_action,
        "by_event_strength": by_strength,
        "entry_delay_buckets": _entry_delay_buckets(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _print_table(report: Dict[str, Any]) -> None:
    """人类可读的报告表格。"""
    print("=" * 72)
    print("📊 准确率基线报告 (quick_sim_trades 已结算)")
    print(f"   窗口: from_ts={report['window']['from_ts']}  to_ts={report['window']['to_ts']}"
          f"  asset={report['window'].get('asset_filter') or '*'}")
    print(f"   生成: {report['generated_at']}")
    print("=" * 72)
    overall = report["overall"]
    if overall.get("count", 0) == 0:
        print("⚠️  该窗口内无已结算交易 (settled=1) — 数据可能未开始积累。")
        return
    print(f"总交易数:        {overall['count']}")
    print(f"胜 / 负 / 平:    {overall['win_count']} / {overall['loss_count']}"
          f" / {overall['breakeven_count']}")
    print(f"胜率:            {overall['win_rate'] * 100:.2f}%")
    print(f"平均盈亏:        {overall['avg_pnl_pct'] * 100:+.4f}%")
    print(f"中位盈亏:        {overall['median_pnl_pct'] * 100:+.4f}%")
    print(f"总盈亏 (USDT):   {overall['sum_pnl_usdt']:+.2f}")
    pf = overall.get('profit_factor')
    pr = overall.get('payoff_ratio')
    print(f"盈亏比:          {pr if pr is not None else 'N/A'}")
    print(f"利润因子:        {pf if pf is not None else 'N/A'}")
    print(f"最大回撤:        {overall['max_drawdown_pct'] * 100:.4f}%")
    print(f"当前回撤:        {overall['current_drawdown_pct'] * 100:.4f}%")
    print(f"Sharpe (年化):   {overall['sharpe_per_trade']}")
    print(f"平均持仓:        {overall['avg_hold_minutes']:.1f} min")
    print(f"平均 score:      {overall['avg_score']:.4f}")
    print(f"平均 MFE/MAE:    {overall['avg_mfe_pct'] * 100:.4f}% / {overall['avg_mae_pct'] * 100:.4f}%")
    print()

    if report["by_asset"]:
        print("─" * 72)
        print("📦 按品种")
        print(f"  {'asset':<8} {'n':>5} {'win_rate':>10} {'avg_pnl':>12}"
              f" {'sum_usdt':>12} {'max_dd':>10}")
        for row in report["by_asset"]:
            print(f"  {row['asset']:<8} {row['count']:>5} "
                  f"{row.get('win_rate', 0) * 100:>9.2f}% "
                  f"{row.get('avg_pnl_pct', 0) * 100:>+11.4f}% "
                  f"{row.get('sum_pnl_usdt', 0):>+11.2f} "
                  f"{row.get('max_drawdown_pct', 0) * 100:>9.4f}%")
    if report["by_action"]:
        print()
        print("─" * 72)
        print("🎬 按 action")
        for row in report["by_action"]:
            print(f"  {row['action']:<6} n={row['count']:>4}  "
                  f"win_rate={row.get('win_rate', 0) * 100:>6.2f}%  "
                  f"avg_pnl={row.get('avg_pnl_pct', 0) * 100:>+8.4f}%")
    if report["by_event_strength"]:
        print()
        print("─" * 72)
        print("💥 按事件强度")
        for row in report["by_event_strength"]:
            print(f"  {row['event_strength']:<8} n={row['count']:>4}  "
                  f"win_rate={row.get('win_rate', 0) * 100:>6.2f}%  "
                  f"avg_pnl={row.get('avg_pnl_pct', 0) * 100:>+8.4f}%")
    if report["entry_delay_buckets"]:
        print()
        print("─" * 72)
        print("⏱ 入场延迟分布 (距新闻发布时间)")
        for k, v in report["entry_delay_buckets"].items():
            print(f"  {k:<10} {v:>5}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="准确率基线评估 (quick_sim_trades)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, help="最近 N 天")
    g.add_argument("--from", dest="from_date", help="起始日期 (YYYY-MM-DD)")
    p.add_argument("--to", dest="to_date", help="结束日期 (YYYY-MM-DD)")
    p.add_argument("--asset", help="仅看某品种 (例: BTC)")
    p.add_argument("--json", action="store_true", help="同时输出 JSON 格式到 stdout")
    p.add_argument("--output", help="写入 JSON 报告到文件 (默认 stdout)")
    p.add_argument("--quiet", action="store_true",
                   help="只打印表，不打印 JSON 到 stdout")
    args = p.parse_args(argv)

    from_ts: Optional[int] = None
    to_ts: Optional[int] = None
    if args.days is not None:
        to_ts = int(time.time())
        from_ts = to_ts - args.days * 86400
    elif args.from_date:
        from_ts = _parse_date(args.from_date)
        if args.to_date:
            to_ts = _parse_date(args.to_date) + 86399  # 含当天

    conn = db.get_connection()
    try:
        try:
            report = compute_baseline(conn, from_ts, to_ts, args.asset)
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    if not args.quiet:
        _print_table(report)
    if args.json or args.output:
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fp:
                fp.write(text + "\n")
            print(f"\n✅ 报告写入 {args.output}", file=sys.stderr)
        elif args.json:
            print("\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
