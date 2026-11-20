#!/usr/bin/env python3
"""基本面深度体检（v488）—— 把"能看到什么 / 看不到什么"打成一张表。

只读、零网络：直接读 `equity_fundamentals` / `cn_equity_fundamentals`。
用途不是替代决策，而是让复盘时能一眼看出：
  · 16 只美股里谁有完整的毛利率/经营利润率/FCF/杠杆读数；
  · 谁的 TTM 因跨源期末日漂移而断档（v488 修复前 AAPL/NVDA/MU 就是这种）；
  · 8 只 A股/港股停在"一期主要指标"，缺三表/历史/同业/日历。

用法：
  cd backend/src_python && python3 ../scripts/fundamental_depth_report.py
  cd backend/src_python && python3 ../scripts/fundamental_depth_report.py --out ../reports/fundamental_depth/latest.md
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import company_depth
import config
import equity_fundamentals as ef


def _fmt(value: Any, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any) -> str:
    return _fmt(value, "%")


def _pp(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.2f}pp"
    except (TypeError, ValueError):
        return "—"


def build_report(conn: sqlite3.Connection) -> str:
    pack = ef.db_pack(conn, tickers=ef.universe(conn, days=3650))
    lines: List[str] = []
    lines.append("# 基本面深度体检")
    lines.append("")
    lines.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}（只读，零网络）")
    lines.append("")
    lines.append("## 一、美股 16 只：生意质量读数")
    lines.append("")
    lines.append("| 票 | TTM营收(亿) | 毛利率 | 经营利润率 | FCF率 | 负债/资产 | 毛利率同比 | 经营利润率同比 | 护栏 | 季度数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    coverage: Dict[str, int] = {k: 0 for k in ("rev", "gm", "opm", "fcf", "lev", "trend")}
    for ticker in pack["tickers"]:
        summary = (pack.get("summary") or {}).get(ticker) or {}
        stmt_rows = (pack.get("statements") or {}).get(ticker) or []
        depth = company_depth.readout(stmt_rows, summary.get("yoy"))
        ttm = summary.get("ttm") or {}
        rev = ttm.get("revenue")
        coverage["rev"] += rev is not None
        coverage["gm"] += depth.get("gross_margin_ttm") is not None
        coverage["opm"] += depth.get("operating_margin_ttm") is not None
        coverage["fcf"] += depth.get("fcf_margin_ttm") is not None
        coverage["lev"] += depth.get("leverage") is not None
        coverage["trend"] += (depth.get("gm_delta_pp") is not None
                              or depth.get("opm_delta_pp") is not None)
        flags = "/".join(depth.get("flags") or []) or "—"
        lines.append(
            f"| {ticker} | {_fmt(rev / 1e8 if rev is not None else None, '', 1)} "
            f"| {_pct(depth.get('gross_margin_ttm'))} "
            f"| {_pct(depth.get('operating_margin_ttm'))} "
            f"| {_pct(depth.get('fcf_margin_ttm'))} "
            f"| {_fmt(depth.get('leverage'))} "
            f"| {_pp(depth.get('gm_delta_pp'))} "
            f"| {_pp(depth.get('opm_delta_pp'))} "
            f"| {flags} | {summary.get('n_quarters') or 0} |"
        )
    lines.append("")
    lines.append(
        "覆盖：TTM 营收 {rev}/16 · 毛利率 {gm}/16 · 经营利润率 {opm}/16 · "
        "FCF 率 {fcf}/16 · 杠杆 {lev}/16 · 利润率趋势 {trend}/16".format(**coverage)
    )
    lines.append("")

    lines.append("## 二、A股 / 港股 8 只：多期历史（v489）")
    lines.append("")
    lines.append("| 票 | 市场 | 期数 | 最新报告期 | TTM营收同比 | 毛利率Δ | ROEΔ | 盈利位置 | 护栏 | 缺口 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    try:
        import cn_depth
        import cn_fundamentals as cf
        recs = cf.read_all(conn)
        got_depth = 0
        for rec in recs:
            ticker, market = str(rec.get("ticker") or ""), str(rec.get("market") or "")
            hist = cf.read_history(conn, ticker, market)
            depth = cn_depth.readout(hist)
            got_depth += 1 if depth.get("available") else 0
            flags = "/".join(depth.get("flags") or []) or "—"
            gaps = "；".join((depth.get("gaps") or [])[:2]) or "—"
            pos = depth.get("earnings_position")
            lines.append(
                f"| {ticker} | {market} | {depth.get('points') or 0} "
                f"| {rec.get('report_date') or '—'} "
                f"| {_pct(depth.get('revenue_ttm_yoy'))} "
                f"| {_pp(depth.get('gross_margin_delta_pp'))} "
                f"| {_pp(depth.get('roe_delta_pp'))} "
                f"| {_fmt(pos * 100 if pos is not None else None, '%', 0)}（{depth.get('earnings_zone') or '—'}） "
                f"| {flags} | {gaps} |"
            )
        lines.append("")
        lines.append(f"历史覆盖：{got_depth}/{len(recs)} 只能算趋势与盈利位置（每只最多 16 期）。")
        lines.append("港股 TTM 已按「本期累计 + 上年年报 − 去年同类型累计」还原，不再把三季报当全年。")
    except Exception as exc:
        lines.append(f"| — | — | — | — | — | — | — | — | — | 读取失败：{type(exc).__name__} |")
    lines.append("")

    lines.append("## 三、影子账本进度（深度护栏，v490）")
    lines.append("")
    try:
        total = conn.execute("SELECT COUNT(*) FROM decision_fundamental_snapshot").fetchone()[0]
        avail = conn.execute(
            "SELECT COUNT(*) FROM decision_fundamental_snapshot WHERE fund_available=1"
        ).fetchone()[0]
        with_ticker = conn.execute(
            "SELECT COUNT(*) FROM decision_fundamental_snapshot "
            "WHERE fund_available=1 AND ticker IS NOT NULL").fetchone()[0]
        known = conn.execute(
            "SELECT COUNT(*) FROM decision_fundamental_snapshot "
            "WHERE fund_available=1 AND ticker IS NOT NULL AND depth_flags IS NOT NULL"
        ).fetchone()[0]
        flagged = conn.execute(
            "SELECT COUNT(*) FROM decision_fundamental_snapshot "
            "WHERE fund_available=1 AND ticker IS NOT NULL "
            "AND depth_flags IS NOT NULL AND depth_flags NOT IN ('', '[]')"
        ).fetchone()[0]
        lines.append(f"- 快照总行数 {total}，基本面可用 {avail}，其中有 ticker {with_ticker}。")
        lines.append(f"- 有深度标签且可评估 **{known} / 60**，其中 flagged **{flagged}**。")
        lines.append("- 门槛：总样本 ≥60 且 flagged/clean 各 ≥20；不到就判 `insufficient`，不编胜率。")
        lines.append("- A/B 目前只能结算有本地日线的标的（美股/商品）；A/H 日线未落库，暂不计入。")
        lines.append("- 评估命令：`python3 scripts/fundamental_depth_ab.py --horizon 10`")
    except sqlite3.OperationalError:
        # 新库还没跑过决策 → depth 列未迁移；不是"读取失败"，如实说
        lines.append("- 深度列尚未迁移（新库需先跑一次决策/`fundamental_shadow.record`）。")
    except Exception as exc:
        lines.append(f"- 读取失败：{type(exc).__name__}")
    lines.append("")

    lines.append("## 四、这一层仍然看不到什么")
    lines.append("")
    for gap in company_depth._STATIC_GAPS:
        lines.append(f"- {gap}")
    lines.append("")
    lines.append("> 以上四项读数只回答「生意在变好还是变坏」，不回答「为什么」。")
    lines.append("> 分部、债务到期、指引、电话会、DCF 缺一项，就不能把这里当成基本面研究。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="基本面深度体检（只读）")
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(_HERE), "reports", "fundamental_depth", "latest.md"),
        help="markdown 输出路径；传空字符串则只打印")
    args = parser.parse_args()

    db_path = config.DB_PATH
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        report = build_report(conn)
    finally:
        conn.close()

    print(report)
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n[written] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
