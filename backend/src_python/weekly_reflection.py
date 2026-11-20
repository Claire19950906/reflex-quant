"""weekly_reflection.py — 每周 AI 反思 + 建议生成

KK 必然 1 (Cognifying) —— 让 AI 帮你分析交易数据、生成"修改建议"。

职责:
  - 每周一次（cron 0 9 * * 1 跑周一早上 9 点）跑数据汇总
  - 生成结构化 prompt 给 AI（你 / 另一个 AI / Claude / GPT）
  - 输出两份：
    1. weekly_report_<date>.md（人类可读）
    2. weekly_ai_prompt_<date>.md（AI 输入）

KK 必然 11 (Tracking) + 必然 1 (Cognifying) + 必然 7 (Filtering) 的联合实践。

用法:
  python3 weekly_reflection.py                   # 跑一次，输出到 /tmp
  python3 weekly_reflection.py --days 30          # 看 30 天窗口
  python3 weekly_reflection.py --out /custom/path # 自定义输出路径
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import db
import trade_decisions


def gather_signals(lookback_days: int = 7) -> Dict[str, Any]:
    """收集本周所有信号 — 给 AI 看的原材料。"""
    conn = db.get_connection()
    trade_decisions.init_schema(conn)

    overall = trade_decisions.get_overall_stats(conn, lookback_days=lookback_days)
    per_asset_action = trade_decisions.get_stats_by_asset_action(
        conn, lookback_days=lookback_days, min_samples=3
    )
    # Plan A 后：区分"真实交易（gate 通过）"与"反事实模拟（gate 失败仍跑）"
    overall_truth = trade_decisions.get_overall_stats(
        conn, lookback_days=lookback_days, gate_passed_only=True
    )
    per_asset_action_truth = trade_decisions.get_stats_by_asset_action(
        conn, lookback_days=lookback_days, min_samples=3, gate_passed_only=True
    )
    mfe_mae = trade_decisions.get_mfe_mae_stats(conn, lookback_days=lookback_days)
    mfe_mae_truth = trade_decisions.get_mfe_mae_stats(
        conn, lookback_days=lookback_days, gate_passed_only=True
    )
    decision_vol = trade_decisions.get_decision_volume(conn, lookback_days=lookback_days)
    quality = trade_decisions.get_data_quality_report(conn, lookback_days=lookback_days)
    gate_breakdown = trade_decisions.get_ai_decision_gate_breakdown(
        conn, lookback_days=lookback_days
    )
    # P2 #12：闸门 / 模型校准 shadow 投影（用历史数据纯只读模拟）
    shadow_projection = trade_decisions.get_gate_shadow_projection(
        conn, lookback_days=lookback_days
    )

    guard_breakdown = conn.execute(
        """SELECT guard, COUNT(*) as n
           FROM trade_decisions
           WHERE decision_ts > strftime('%s','now', ?) AND decision_type = 'REJECT'
           GROUP BY guard ORDER BY n DESC""",
        (f"-{int(lookback_days)} day",),
    ).fetchall()

    top_deny_reasons = conn.execute(
        """SELECT deny_reason, COUNT(*) as n
           FROM trade_decisions
           WHERE decision_ts > strftime('%s','now', ?)
             AND decision_type = 'REJECT' AND deny_reason != ''
           GROUP BY deny_reason ORDER BY n DESC LIMIT 5""",
        (f"-{int(lookback_days)} day",),
    ).fetchall()

    return {
        "lookback_days": lookback_days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "per_asset_action": per_asset_action,
        "overall_truth": overall_truth,
        "per_asset_action_truth": per_asset_action_truth,
        "mfe_mae": mfe_mae,
        "mfe_mae_truth": mfe_mae_truth,
        "decision_volume": decision_vol,
        "quality": {
            "decisions_total": quality["decisions_total"],
            "decisions_empty_ctx_pct": quality["decisions_empty_ctx_pct"],
            "quick_sim_total_days": quality["quick_sim_total_days"],
            "alerts": quality["alerts"],
        },
        "guard_breakdown": [{"guard": g[0], "n": g[1]} for g in guard_breakdown],
        "top_deny_reasons": [{"reason": r[0], "n": r[1]} for r in top_deny_reasons],
        "gate_breakdown": gate_breakdown,
        "shadow_projection": shadow_projection,
    }


def generate_ai_prompt(signals: Dict[str, Any]) -> str:
    """给 AI 的 prompt — 让 AI 基于真实数据生成"修改建议"。"""
    return f"""# 每周交易反思任务

你是一个**客观的交易策略审计师**。基于以下真实数据，给出**最多 3 条**可执行的"修改建议"。

## 输入数据（最近 {signals['lookback_days']} 天）

### 整体
```
笔数: {signals['overall']['n']}
胜率: {signals['overall']['win_rate']}%
平均每笔盈亏: {signals['overall']['avg_pnl']}%
总盈亏: {signals['overall']['sum_pnl']}%
```

### 各资产-动作统计
```
{chr(10).join(
    f"  {r['asset']:6s} {r['action']:5s} n={r['n']:4d} 胜率={r['win_rate']:5.1f}% 平均={r['avg_pnl']:7.2f}% 最差={r['worst']:7.2f}% 最好={r['best']:7.2f}%"
    for r in signals['per_asset_action']
)}
```

### 决策打点
```
{json.dumps(signals['decision_volume'], ensure_ascii=False)}
```

### 各 guard 拒绝分布
```
{chr(10).join(f"  {g['guard']:20s} {g['n']:5d}" for g in signals['guard_breakdown'])}
```

### 数据质量告警
```
{chr(10).join('  ' + a for a in signals['quality']['alerts'])}
```

## 你的任务

针对以上数据，**最多给 3 条建议**。每条建议必须满足：
1. **基于数据** —— 不能凭印象
2. **可执行** —— 不是"加强风控"，是"XAU BUY 必须 R:R ≥ 2:1"
3. **带证据** —— 引用上面的具体数字

输出 JSON 数组，每个元素：
```json
{{
  "rule_type": "FORBID_ACTION" | "REQUIRE_CONDITION" | "ADJUST_SIZE",
  "asset": "XAU" | "ALL" | null,
  "proposed_change": {{ "action": "SELL" }},
  "evidence": "XAU BUY 51 笔，胜率 43%，平均亏 -11%",
  "expected_impact": "禁用 XAU BUY 后预期月节省 -5.5%",
  "downside": "如果 XAU 突然转涨，可能错过 +3% 收益",
  "risk_level": "LOW" | "MEDIUM" | "HIGH"
}}
```

## 不要做的事

- ❌ 不要建议"做加法"（加新策略、新指标）—— 只做减法
- ❌ 不要泛泛建议"加强风控"
- ❌ 不要在样本 < 5 笔的资产-动作上下结论
- ❌ 不要忽视数据质量告警 —— 如果 alerts 不空，先说"数据有问题，建议谨慎"

## 现在输出（只输出 JSON 数组，不要其他文字）
"""


def format_human_report(signals: Dict[str, Any]) -> str:
    """人类可读的报告。"""
    lines = []
    lines.append("=" * 60)
    lines.append(f"周报 @ {signals['generated_at']}")
    lines.append(f"窗口: 最近 {signals['lookback_days']} 天")
    lines.append("=" * 60)
    lines.append("")
    lines.append("📊 整体:")
    o = signals["overall"]
    lines.append(f"  笔数: {o['n']}")
    lines.append(f"  胜率: {o['win_rate']}%")
    lines.append(f"  平均每笔: {o['avg_pnl']}%")
    lines.append(f"  总盈亏: {o['sum_pnl']}%")
    lines.append("")
    # 真实 vs 反事实 对比段 — Plan A 后才能区分真实交易胜率
    ot = signals.get("overall_truth", {})
    paat = signals.get("per_asset_action_truth", [])
    lines.append("🔍 真实 vs 反事实（gate_passed=1 vs gate_passed=0）:")
    n_total = o["n"] or 0
    n_truth = ot.get("n", 0) or 0
    n_counter = max(n_total - n_truth, 0)
    truth_pct = (n_truth / n_total * 100) if n_total else 0.0
    lines.append(
        f"  真实交易:   n={n_truth:>3d} ({truth_pct:>5.1f}% of 总)  "
        f"胜率={ot.get('win_rate', 0) if ot.get('win_rate') is not None else 'N/A':<5}  "
        f"平均={ot.get('avg_pnl', 0) if ot.get('avg_pnl') is not None else 'N/A':<7}%"
    )
    lines.append(
        f"  反事实模拟: n={n_counter:>3d} ({100 - truth_pct:>5.1f}% of 总)"
    )
    if paat:
        lines.append("  按资产 × 动作（真实交易）:")
        lines.append(f"    {'asset':<8s} {'action':<6s} {'n':>5s}  {'胜率':>6s}  {'平均':>8s}")
        for r in paat:
            lines.append(
                f"    {r['asset']:<8s} {r['action']:<6s} {r['n']:>5d}  "
                f"{(r['win_rate'] or 0):>5.1f}%  {(r['avg_pnl'] or 0):>7.2f}%"
            )
    elif n_total > 0 and n_truth == 0:
        lines.append("  ⚠️ 全部交易都是反事实（gate 拒绝），真实交易胜率待闸门放宽后才有数据")
    lines.append("")
    lines.append("")
    lines.append("📈 分资产:")
    lines.append(f"  {'asset':<8s} {'action':<6s} {'n':>5s}  {'胜率':>6s}  {'平均':>8s}  {'最差':>8s}  {'最好':>8s}")
    for r in signals["per_asset_action"]:
        lines.append(
            f"  {r['asset']:<8s} {r['action']:<6s} {r['n']:>5d}  "
            f"{r['win_rate']:>5.1f}%  {r['avg_pnl']:>7.2f}%  {r['worst']:>7.2f}%  {r['best']:>7.2f}%"
        )
    # MFE/MAE 段 — 仓位生命周期极值（仓位管理政策依据）
    mfe_rows = signals.get("mfe_mae", [])
    if mfe_rows:
        lines.append("")
        lines.append("📐 MFE/MAE（仓位周期最大浮盈/浮亏）:")
        lines.append(
            f"  {'asset':<6s} {'act':<5s} {'n':>4s}  {'avg_MFE%':>9s}  {'avg_MAE%':>9s}  {'realiz%':>8s}  {'avg_pnl%':>9s}"
        )
        for r in mfe_rows:
            mfe = f"{r['avg_mfe_pct']:.2f}" if r["avg_mfe_pct"] is not None else "  N/A"
            mae = f"{r['avg_mae_pct']:.2f}" if r["avg_mae_pct"] is not None else "  N/A"
            real = f"{r['mfe_realization_pct']:.1f}" if r["mfe_realization_pct"] is not None else "  N/A"
            pnl = f"{r['avg_pnl_pct']:.2f}" if r["avg_pnl_pct"] is not None else "  N/A"
            lines.append(
                f"  {r['asset']:<6s} {r['action']:<5s} {r['n']:>4d}  "
                f"{mfe:>9s}  {mae:>9s}  {real:>8s}  {pnl:>9s}"
            )
        lines.append("  (MFE=最大浮盈 MAE=最大浮亏 realiz=avg_pnl/avg_MFE*100)")
    lines.append("")
    lines.append("🚦 决策打点:")
    if signals["decision_volume"]:
        for k, v in sorted(signals["decision_volume"].items()):
            lines.append(f"  {k:<15s} {v:>5d}")
    else:
        lines.append("  (无)")
    lines.append("")
    if signals["guard_breakdown"]:
        lines.append("🛡️ 各 guard 拒绝:")
        for g in signals["guard_breakdown"]:
            lines.append(f"  {g['guard']:<20s} {g['n']:>5d}")
        lines.append("")
    # 闸门拒绝聚合 — 找"为什么 99% BUY/SELL 被拒"
    gb = signals.get("gate_breakdown", {})
    if gb and gb.get("buy_or_sell", 0) > 0:
        lines.append("🚧 AI 决策闸门聚合（找拒绝瓶颈）:")
        lines.append(
            f"  BUY/SELL 总数: {gb.get('buy_or_sell', 0):>4d}  |  "
            f"被拒: {gb.get('gate_rejected', 0):>3d}  |  "
            f"通过: {gb.get('gate_passed', 0):>3d}  |  "
            f"拒绝率: {gb.get('rejection_rate_pct', 0)}%"
        )
        # 拒绝原因模板
        lines.append("  拒绝原因（数字归并为 N）:")
        _n_bs = gb.get("buy_or_sell", 0) or 0
        for t in gb.get("by_reason_template", [])[:5]:
            _cnt = t.get("count", 0)
            _pct = round(_cnt / _n_bs * 100, 1) if _n_bs else 0.0
            lines.append(
                f"    [{_cnt:>3d} ({_pct:>4.1f}%)] {t.get('template', '')}"
            )
        # 置信度区间
        cb = gb.get("confidence_buckets", [])
        if cb:
            ranges = " ".join(f"{c['range']}:{c['n']}" for c in cb)
            lines.append(f"  置信度区间分布: {ranges}")
            # 关键洞察
            high = sum(
                c.get("n", 0) for c in cb
                if int(str(c.get("range", "0")).rstrip("+").split("-")[0]) >= 20
            )
            high_pct = round(high / gb["buy_or_sell"] * 100, 1) if gb["buy_or_sell"] else 0.0
            if high_pct < 5:
                lines.append(
                    f"  ⚠️ 仅 {high_pct}% 信号 confidence ≥ 20 — 闸门放宽也救不了，"
                    f"需要先调 prompt/模型 calibration"
                )
        # 按资产
        if gb.get("by_asset"):
            lines.append("  按资产拒绝（top 5）:")
            for a in gb["by_asset"][:5]:
                lines.append(
                    f"    {a['asset']:<8s}  n={a['n']:>3d}  "
                    f"被拒={a['rejected']:>3d}  通过={a['passed']:>3d}"
                )
        lines.append("")

    # P2 #12：闸门 / 模型校准 shadow 投影（dry-run 模式）
    sp = signals.get("shadow_projection", {})
    if sp and sp.get("scenarios"):
        lines.append("📊 Shadow 投影（dry-run，不改数据，只模拟）:")
        lines.append(
            f"  基线: BUY/SELL 总 {sp.get('total_buy_sell', 0)} 笔"
            f"（最近 {sp.get('lookback_days', 0)} 天）"
        )
        _sp_tot = sp.get("total_buy_sell", 0) or 0
        for s in sp["scenarios"]:
            _wp = s.get("would_pass", 0)
            _pr = round(_wp / _sp_tot * 100, 1) if _sp_tot else 0.0
            lines.append(
                f"  • {s.get('label', ''):<60.60s} → 通过 {_wp:>3d} "
                f"({_pr:>5.1f}%)  投影 PnL {s.get('projected_pnl_pct', 0.0):>+8.1f}%  "
                f"胜率 {s.get('win_rate_pct', 0)}%"
            )
        # 给出建议
        if len(sp["scenarios"]) >= 4:
            gate_only = sp["scenarios"][1]
            boosted = sp["scenarios"][2]
            # 比较四种策略的 PnL，找出最佳
            best = max(sp["scenarios"], key=lambda s: s.get("projected_pnl_pct", 0.0))
            lines.append(
                f"  💡 最佳: {best.get('label', '')[:50]:<50.50s} → "
                f"PnL {best.get('projected_pnl_pct', 0.0):+.1f}% / "
                f"胜率 {best.get('win_rate_pct', 0)}%"
            )
            if (boosted.get("projected_pnl_pct", 0.0) < 0
                    and gate_only.get("projected_pnl_pct", 0.0)
                    >= boosted.get("projected_pnl_pct", 0.0)):
                lines.append(
                    "  ⚠️ 提示: P2 #10 改 prompt 的 5x 投影为负 PnL，说明多过的信号是低胜率垃圾。"
                    " 真正可控的步骤是「先仅放宽闸门」观察，反向证明 prompt 改的价值。"
                )
        lines.append("")
    if signals["quality"]["alerts"]:
        lines.append("⚠️ 数据质量告警:")
        for a in signals["quality"]["alerts"]:
            lines.append(f"  {a}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly AI reflection")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", default="/tmp")
    parser.add_argument("--no-ai", action="store_true", help="don't generate AI prompt")
    args = parser.parse_args()

    signals = gather_signals(lookback_days=args.days)
    human_report = format_human_report(signals)

    today = datetime.now().strftime("%Y-%m-%d")
    report_path = f"{args.out}/weekly_report_{today}.md"
    ai_prompt_path = f"{args.out}/weekly_ai_prompt_{today}.md"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(human_report)

    if not args.no_ai:
        prompt = generate_ai_prompt(signals)
        with open(ai_prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)

    print(human_report)
    print(f"\n✅ Saved: {report_path}")
    if not args.no_ai:
        print(f"🤖 AI prompt: {ai_prompt_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())