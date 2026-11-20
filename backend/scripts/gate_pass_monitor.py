#!/usr/bin/env python3
"""闸门通过 → 是否真的建仓 的对账监控。

为什么需要它：平台最容易骗人的地方不是"判断错"，而是"看起来在跑，实际什么都没做"。
一条决策写了 `trade_gate_reason=证据充分…`（闸门通过），但 quick_sim 可能因为
品种白名单、时间窗、置信度、降级标记等任何一条静默跳过。没有对账就发现不了。

本脚本对最近 N 分钟内的"通过闸门"决策逐条核对 quick_sim 的五道建仓条件，
输出「建仓 / 未建仓 + 首条卡点」，并追加到日志。

用法：
    python3 scripts/gate_pass_monitor.py            # 默认看最近 180 分钟
    python3 scripts/gate_pass_monitor.py --minutes 720
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BACKEND, "src_python"))

import config

DB = os.path.join(BACKEND, "trident_event_bus.db")
LOG = os.path.expanduser("~/.trident/log/gate_pass_monitor.log")
WINDOW_MIN = 120          # quick_sim 自己的时间窗
TRADABLE_STATUS = ("UNREAD", "APPROVED", "REVIEWED", "AUTO_APPROVED")


def block_reason(row: sqlite3.Row, now: datetime) -> str | None:
    """返回第一条卡点；全部满足返回 None（应当建仓）。"""
    asset = str(row["target_asset"] or "").upper()
    if asset not in config.SIM_ALLOWED_ASSETS:
        return f"品种不在白名单({asset})"
    if str(row["suggested_action"] or "").upper() not in ("BUY", "SELL"):
        return "动作非方向"
    if str(row["evidence_action"] or "HOLD").upper() == "HOLD":
        return "证据动作=HOLD"
    if (row["evidence_confidence"] or 0) < config.EVIDENCE_CONFIDENCE_GATE:
        return f"置信度 {row['evidence_confidence']} < {config.EVIDENCE_CONFIDENCE_GATE:g}"
    if row["trade_gate_reason"] != config.TRADE_GATE_PASSED_REASON:
        return "闸门原因非通过"
    if row["analysis_degraded"]:
        return "分析降级"
    if str(row["status"] or "") not in TRADABLE_STATUS:
        return f"状态 {row['status']} 不可交易"
    created = datetime.fromisoformat(str(row["created_at"]))
    if (now - created) > timedelta(minutes=WINDOW_MIN):
        return f"超出 {WINDOW_MIN} 分钟窗口（已过期）"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=180)
    args = ap.parse_args()

    now = datetime.now()
    since = (now - timedelta(minutes=args.minutes)).isoformat(sep=" ")

    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ad.id, ad.target_asset, ad.suggested_action, ad.evidence_action,
               ad.evidence_confidence, ad.trade_gate_reason, ad.analysis_degraded,
               ad.status, ad.created_at,
               qs.id AS trade_id, qs.entry_price, qs.entry_ts,
               qs.exit_price, qs.pnl_pct, qs.settled, qs.verdict
        FROM ai_decisions ad
        LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
        WHERE ad.created_at >= ?
          AND ad.trade_gate_reason = ?
        ORDER BY ad.id DESC
        """,
        (since, config.TRADE_GATE_PASSED_REASON),
    ).fetchall()

    lines = [f"== 闸门对账 {now.strftime('%Y-%m-%d %H:%M:%S')}  最近 {args.minutes} 分钟 =="]
    if not rows:
        lines.append("窗口内没有通过闸门的决策。")
    traded = 0
    for r in rows:
        if r["trade_id"]:
            traded += 1
            lines.append(
                f"{r['id']:>6} {r['target_asset']:<5} {r['suggested_action']:<4} "
                f"conf={r['evidence_confidence']:<6} → 已建仓 #{r['trade_id']} @ {r['entry_price']}"
                + (f" 结算 {r['pnl_pct']:+.3f}% {r['verdict']}" if r["settled"]
                   else " 持仓中")
            )
            continue
        why = block_reason(r, now) or "全部条件满足但尚未建仓（quick_sim 未跑到）"
        lines.append(
            f"{r['id']:>6} {r['target_asset']:<5} {r['suggested_action']:<4} "
            f"conf={r['evidence_confidence']:<6} → 未建仓：{why}"
        )
    settled = [r for r in rows if r["trade_id"] and r["settled"]]
    wins = sum(1 for r in settled if (r["pnl_pct"] or 0) > 0)
    pnl_sum = sum(float(r["pnl_pct"] or 0) for r in settled)
    lines.append(f"合计：通过闸门 {len(rows)} 条，已建仓 {traded} 条，未建仓 {len(rows) - traded} 条")
    if settled:
        lines.append(
            f"已结算 {len(settled)} 笔：胜 {wins} 负 {len(settled) - wins}"
            f"（胜率 {wins / len(settled) * 100:.0f}%），合计 {pnl_sum:+.3f}%，"
            f"单笔均值 {pnl_sum / len(settled):+.3f}%"
        )

    out = "\n".join(lines)
    print(out)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(out + "\n\n")
    except OSError as exc:
        print(f"[warn] 日志写入失败：{exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
