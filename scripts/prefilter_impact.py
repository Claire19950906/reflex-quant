#!/usr/bin/env python3
"""预筛收益体检：把"如果 LLM 之前就按规则拦掉"这件事算成数字。

背景：`analysis/prefilter.py` 已经在用（默认开），实测判越界 18.3%。
但真正烧钱的是那些**过了预筛、模型仍返回 NONE** 的新闻：
它们占满了 main 的输入 token（均 10.6k/次），却产不出可交易标的。

这里做的是"只记录不拦截"的版本：
  · 拿历史决策 join 回原始新闻正文，用同一个 judge() 复判；
  · 分两组看：NONE 决策里有多少会被预筛拦掉（潜在省下的钱）；
  · 有效判断（BUY/SELL）里有多少会被误拦（**这是安全红线**）。

口径：judge() 只看 raw_news 原文 + 门卫字段，不看模型输出，所以这里是复判，
       不是"事后诸葛" —— 与线上预筛同一条规则。

用法：python3 scripts/prefilter_impact.py [--days 14] [--write]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend", "src_python"))

import db
from analysis.prefilter import judge

REPORT = os.path.join("backend", "reports", "ops", "prefilter_impact.md")


def _rows(conn, where: str, days: int):
    since = int(dt.datetime.now().timestamp()) - days * 86400
    return conn.execute(
        "SELECT d.id, d.suggested_action, d.target_asset, d.decision_kind, "
        "r.id, r.content, r.gate_main_asset, r.gate_assets "
        "FROM ai_decisions d JOIN raw_news r ON r.id = d.news_id "
        f"WHERE d.ts >= ? AND {where}",
        (since,),
    ).fetchall()


def _judge(row) -> tuple:
    payload = {
        "content": row[5] or "",
        "title": "",
        "gate_main_asset": row[6] or "",
        "gate_assets": row[7] or "",
    }
    return judge(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30)

    none_rows = _rows(conn, "UPPER(COALESCE(d.target_asset,'')) IN ('','NONE')", args.days)
    eff_rows = _rows(conn, "COALESCE(d.analysis_degraded,0)=0 "
                           "AND UPPER(d.suggested_action) IN ('BUY','SELL')", args.days)

    none_drop, none_reasons = 0, Counter()
    for r in none_rows:
        dropped, why = _judge(r)
        if dropped:
            none_drop += 1
            none_reasons[why] += 1

    eff_drop, eff_list = 0, []
    for r in eff_rows:
        dropped, why = _judge(r)
        if dropped:
            eff_drop += 1
            if len(eff_list) < 8:
                eff_list.append((r[3] or "?", r[2], why, (r[5] or "")[:60].replace("\n", " ")))

    n_none, n_eff = len(none_rows), len(eff_rows)
    fn_rate = (eff_drop / n_eff * 100) if n_eff else 0.0
    save_rate = (none_drop * 100.0 / n_none) if n_none else 0.0

    lines = [
        "# 预筛收益体检（只记录，不拦截）",
        "",
        f"窗口：最近 {args.days} 天；判据为线上同一份 `analysis.prefilter.judge()`。",
        "",
        "| 分组 | 决策数 | 会被预筛拦掉 | 占比 |",
        "|---|---|---|---|",
        f"| NONE 决策（烧钱但不产标的） | {n_none} | {none_drop} | {save_rate:.1f}% |",
        f"| **有效判断 BUY/SELL**（安全红线） | {n_eff} | **{eff_drop}** | **{fn_rate:.1f}%** |",
        "",
    ]
    if none_reasons:
        lines += ["NONE 组拦掉的原因：", ""]
        lines += [f"- `{k}`: {v}" for k, v in none_reasons.most_common(6)]
        lines.append("")
    if eff_list:
        lines += ["⚠️ 被误拦的有效判断（最多列 8 条）：", "",
                  "| 决策种类 | 标的 | 拦掉原因 | 正文片段 |", "|---|---|---|---|"]
        lines += [f"| {k} | {a} | {w} | {t} |" for k, a, w, t in eff_list]
        lines.append("")
    small = n_eff < 30 or n_none < 30
    lines += [
        "## 结论",
        (f"- **样本不足**（NONE {n_none} / 有效 {n_eff}，均 <30）："
         "上面的比例只能看方向，不能当结论。等数据恢复跑满 3 天再看。") if small else "",
        f"- 若把预筛扩到\"拦掉就调 LLM\"：NONE 组可省 **{save_rate:.1f}%** 的模型调用，",
        f"  但会误杀 **{fn_rate:.1f}%** 的有效判断。",
        ("- **误杀率超过 1% → 不建议扩**，先把误杀样本逐条修判据。" if fn_rate > 1 else
         ("- 样本不足，暂不灰度。" if small else
          "- 误杀率 ≤1% → 可以灰度：先对 NONE 组做\"跳过 LLM\"，观察一周有效判断是否下降。")),
    ]
    text = "\n".join(lines)
    print(text)
    if args.write:
        path = os.path.join(ROOT, REPORT)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n已写入 {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
