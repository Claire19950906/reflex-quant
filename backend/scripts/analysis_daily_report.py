#!/usr/bin/env python3
"""analysis_daily_report.py — 把四张只读报表合成一份日报。

过去「准确度」没法看，是因为四个口径分散在四张表里、且 HOLD 从不结算。
本脚本只读、只产出 Markdown，不写库、不发请求：

  1. 终态分布        analysis_state_audit.py
  2. 前置路由回放    analysis_route_shadow.py
  3. HOLD 反事实     analysis_hold_counterfactual.py
  4. 选择器对比      analysis_selector_backtest.py

用法：
  python3 backend/scripts/analysis_daily_report.py --date 2026-11-16
  python3 backend/scripts/analysis_daily_report.py            # 默认昨天
输出：
  docs/reports/analysis-daily-YYYY-MM-DD.md
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_BACKEND)
_REPORTS = os.path.join(_ROOT, "docs", "reports")

_SCRIPTS = (
    ("终态分布（六终态复算）", "analysis_state_audit.py"),
    ("前置路由回放（W1 上线前评估）", "analysis_route_shadow.py"),
    ("HOLD 反事实结算（准确度的分母）", "analysis_hold_counterfactual.py"),
    ("选择器对比（旧闸门 vs 新档位）", "analysis_selector_backtest.py"),
)


def _run(script: str, day: str) -> str:
    path = os.path.join(_BACKEND, "scripts", script)
    try:
        out = subprocess.run(
            [sys.executable, path, "--date", day],
            capture_output=True, text=True, timeout=600,
        )
        if out.returncode != 0:
            return f"（{script} 退出码 {out.returncode}）\n\n```\n{out.stderr[-600:]}\n```"
        return out.stdout.strip()
    except Exception as exc:
        return f"（{script} 执行失败：{type(exc).__name__}: {exc}）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认昨天")
    args = ap.parse_args()
    day = args.date or (date.today() - timedelta(days=1)).isoformat()

    parts = [
        f"# 分析链日报 {day}",
        "",
        "> 只读统计。四个口径同源（`backend/src_python/analysis/` 的纯函数），",
        "> 与运行时落库的 `no_trade_reason` / `analysis_state` 保持一致。",
        "",
    ]
    for title, script in _SCRIPTS:
        parts.append(f"## {title}")
        parts.append("")
        parts.append("```")
        parts.append(_run(script, day))
        parts.append("```")
        parts.append("")

    parts += [
        "## 读法",
        "",
        "- **终态**：先看「真实失败」占比，它才是需要修的部分；`out_of_scope` 不是失败。",
        "- **路由**：只看「误杀」是否为 0；`unknown` 占比高说明判据不足，不是要强行判死。",
        "- **HOLD 反事实**：给 HOLD 补分母，回答「观望到底对不对」。",
        "- **选择器**：比较旧闸门与新档位在同一批样本上的胜率；样本 <60 时只记录、不调阈值。",
        "",
    ]

    os.makedirs(_REPORTS, exist_ok=True)
    path = os.path.join(_REPORTS, f"analysis-daily-{day}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
