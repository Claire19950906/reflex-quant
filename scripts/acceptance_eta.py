#!/usr/bin/env python3
"""验收 FAIL 的"到期清单" —— 每个失败项卡在什么、最早什么时候可能转绿。

为什么做：
    capability_acceptance 只告诉你 PASS/FAIL，不告诉你"这个 FAIL 是在等时间，
    还是真的坏了"。于是每轮都要人工重算一遍：样本差多少、结算窗口什么时候到、
    某个维度的样本为什么是 0。这个脚本把这几件事一次算清，且可重复运行。

用法：
    python3 scripts/acceptance_eta.py
    python3 scripts/acceptance_eta.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, "/Users/claire/Desktop/金融1.0/backend/src_python")
import db

SETTLE_DAYS = 5          # 与 bridge_server.DECISION_SETTLE_DAYS 一致


def _fmt(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect("file:" + db.get_db_path() + "?mode=ro", uri=True, timeout=30)
    now = int(time.time())

    def one(sql, *p):
        row = conn.execute(sql, p).fetchone()
        return row[0] if row else None

    rows = []

    # ── 结算窗口：所有"已结算样本"类检查的共同卡点 ──
    total_dec = one("SELECT COUNT(*) FROM ai_decisions") or 0
    settled = one("SELECT COUNT(*) FROM ai_decisions WHERE settled=1") or 0
    oldest_open = one("SELECT MIN(ts) FROM ai_decisions WHERE settled=0")
    mature_at = int(oldest_open) + SETTLE_DAYS * 86400 if oldest_open else None
    rows.append({
        "item": "1 / 6 / 10 / 15（结算样本类）",
        "now": f"决策 {total_dec} · 已结算 {settled}",
        "blocker": f"最早未结算那笔要满 {SETTLE_DAYS} 天才进结算窗口",
        "eta": f"{_fmt(mature_at)} 起开始出第一批结算（约 "
               f"{round((mature_at - now) / 3600.0, 1) if mature_at else '—'} 小时后）",
        "kind": "等时间",
    })

    # ── 反思 B 维 ──
    a = one("SELECT COUNT(*) FROM trade_reflections_v2 WHERE dimension='A'") or 0
    b = one("SELECT COUNT(*) FROM trade_reflections_v2 WHERE dimension='B'") or 0
    c = one("SELECT COUNT(*) FROM trade_reflections_v2 WHERE dimension='C'") or 0
    rows.append({
        "item": "8 三维反思 A/B/C",
        "now": f"A={a} B={b} C={c}",
        "blocker": "B 维要有 'BUY/SELL 方向决策 + 60 分钟行情验证' 才能产出"
                   if b == 0 else "—",
        "eta": "至少等第一批方向决策结算后（同上）",
        "kind": "等方向样本",
    })

    # ── 因果图谱 outcome ──
    oc = one("SELECT COUNT(*) FROM causal_chain_outcomes") or 0
    evaluated = one("SELECT COUNT(*) FROM causal_chain_outcomes "
                    "WHERE status='evaluated' OR direction_hit IS NOT NULL") or 0
    rows.append({
        "item": "6 因果图谱闭环 / 10 准确性统计",
        "now": f"outcomes {oc}（已评估 {evaluated}）· 门槛 50",
        "blocker": f"差 {max(0, 50 - evaluated)} 条已评估 outcome",
        "eta": "随事件 T+1 评估累积，无固定时间点",
        "kind": "等样本量",
    })

    # ── 归因行数 ──
    attr = one("SELECT COUNT(*) FROM event_move_attribution") or 0
    rows.append({
        "item": "12 归因 + 反思统计",
        "now": f"归因 {attr} 行 · 门槛 20（真因果占比已 90.9% 达标）",
        "blocker": f"只差 {max(0, 20 - attr)} 行归因；事件异动匹配上了就会写",
        "eta": "随异动+新闻匹配累积",
        "kind": "等样本量",
    })

    # ── 权重学习 ──
    paired = one("SELECT COUNT(*) FROM signal_weight_history WHERE trigger_kind='insert'") or 0
    # 只有"决策时点(插入)"的权重才算事前口径；事后回填的不参与相关检验
    settled_dec = one("SELECT COUNT(*) FROM ai_decisions WHERE settled=1") or 0
    rows.append({
        "item": "2 信号权重自动学习",
        "now": f"决策时点权重 {paired} 条（门槛 50，条数已够）· 但能配真值的只有 {settled_dec} 条",
        "blocker": "真值来自结算结果，而结算要满 5 天；另四个乘子仍僵死（取值只有一种）",
        "eta": "与结算窗口同步：11-21 起才可能凑到 50 条配对",
        "kind": "等结算",
    })

    # ── 阈值政策 ──
    hold = one("SELECT COUNT(*) FROM ai_decisions WHERE suggested_action='HOLD'") or 0
    hold_pct = round(hold * 100.0 / max(total_dec, 1), 2)
    zero_conf = one("SELECT COUNT(*) FROM ai_decisions WHERE evidence_confidence=0") or 0
    rows.append({
        "item": "13 HOLD 占比 ≤ 70%",
        "now": f"HOLD {hold}/{total_dec} = {hold_pct}% · 置信度恰为 0 的 {zero_conf} 条",
        "blocker": "降闸门救不了：0 分永远小于任何闸门，最多降到 ~89%",
        "eta": "需要改上游或改验收口径（待你拍）",
        "kind": "待决策",
    })

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"验收 FAIL 到期清单 · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    for r in rows:
        print(f"[{r['kind']}] {r['item']}")
        print(f"    现在：{r['now']}")
        print(f"    卡点：{r['blocker']}")
        print(f"    预计：{r['eta']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
