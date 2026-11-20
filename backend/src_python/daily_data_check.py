"""daily_data_check.py — 每日数据质量自检 + 异常告警

KK 必然 11 (Tracking) —— 数据质量是所有系统智能的前提。
每天跑一次，发现：
  1. trade_decisions 是否在打点（如果 0 → 打点挂了）
  2. quick_sim_trades 是否有数据（如果某天 0 → 漏结算）
  3. asset 名是否一致（XAU vs xau → 数据脏）
  4. 当前真实数据 vs 上次跑的变化趋势

用法:
  python3 daily_data_check.py          # 跑一次，输出报告
  python3 daily_data_check.py --json   # 输出 JSON 格式
  python3 daily_data_check.py --strict # 任一 alert 就 exit 1（给监控用）

Cron 建议（每天早上 8 点跑一次）:
  0 8 * * * cd /path && python3 daily_data_check.py >> /tmp/data_check.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, Dict

import db
import trade_decisions


def run_report(lookback_days: int = 30) -> Dict[str, Any]:
    conn = db.get_connection()
    trade_decisions.init_schema(conn)
    return trade_decisions.get_data_quality_report(conn, lookback_days=lookback_days)


def format_report(report: Dict[str, Any]) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"数据质量自检报告 @ {datetime.now().isoformat(timespec='seconds')}")
    lines.append("=" * 60)
    lines.append("")
    lines.append("📊 打点量 (trade_decisions):")
    if report["decision_counts"]:
        for k, v in sorted(report["decision_counts"].items()):
            lines.append(f"  {k:15s} {v:5d}")
    else:
        lines.append("  (无)")
    lines.append("")
    lines.append(f"📅 quick_sim_trades 各天分布（{report['quick_sim_total_days']} 天）:")
    days = report["quick_sim_by_day"]
    if days:
        sorted_days = sorted(days.items(), reverse=True)
        for d, n in sorted_days[:10]:
            lines.append(f"  {d}  {n:4d}")
        if len(sorted_days) > 10:
            lines.append(f"  ... ({len(sorted_days) - 10} more)")
    else:
        lines.append("  (无)")
    lines.append("")
    lines.append("💎 quick_sim_trades 各资产:")
    if report["quick_sim_assets"]:
        for asset, n in sorted(report["quick_sim_assets"].items()):
            lines.append(f"  {asset:10s} {n:5d}")
    else:
        lines.append("  (无)")
    lines.append("")
    lines.append("📦 Context 填充率:")
    lines.append(
        f"  total={report['decisions_total']} "
        f"empty={report['decisions_empty_ctx']} "
        f"({report['decisions_empty_ctx_pct']}%)"
    )
    lines.append("")
    if report["alerts"]:
        lines.append("🚨 Alerts:")
        for a in report["alerts"]:
            lines.append(f"  {a}")
    else:
        lines.append("✅ No alerts.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily data quality check")
    parser.add_argument("--days", type=int, default=30, help="lookback days")
    parser.add_argument("--json", action="store_true", help="output as JSON")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any alert")
    args = parser.parse_args()

    report = run_report(lookback_days=args.days)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))

    if args.strict and report["alerts"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())