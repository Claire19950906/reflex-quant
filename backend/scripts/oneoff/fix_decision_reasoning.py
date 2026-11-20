# -*- coding: utf-8 -*-
"""fix_decision_reasoning.py — 清理决策理由里的自相矛盾表述。

问题
----
实测 2026-11-16 的决策理由里出现这种句子：

    [副品种 WTI] 该标的**不在**核心监控清单（BTC/XAU/WTI/ETH/SOL），按非交易信号处理

WTI 明明在括号里那串名单中，模型却判"不在清单"。同一段话既列了它又否认它，
下游（分析页反思、错过信号、归因）读这段文字会把"核心品种"误当成"无关品种"，
把真实的 HOLD 原因（信号弱/数据缺）掩盖成"标的不在监控范围"。

做法
----
只改文字，**不动任何买卖判定**（suggested_action 一个字段都不碰）：
把"不在核心监控清单/品种"且 target_asset 属于核心清单的行，
改写为"该标的属于核心品种，但本次未形成方向判断"。

用法:
    python3 fix_decision_reasoning.py --dry-run
    python3 fix_decision_reasoning.py
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "trident_event_bus.db"
)
CORE = {"BTC", "XAU", "WTI", "ETH", "SOL"}

# 「不在核心监控清单（BTC/XAU/WTI/ETH/SOL）」的各种写法
_NEG_RE = re.compile(
    r"(该标的|该品种|本标的)?\s*\**不在\**\s*核心监控(清单|品种|范围)?"
    r"(（[^）]*）|\([^)]*\))?",
)


def rewrite(text: str, asset: str) -> str:
    if not text or asset not in CORE:
        return text
    if "核心监控" not in text or "不在" not in text:
        return text
    repl = f"该标的属于核心监控品种（{'/'.join(sorted(CORE))}），但本次未形成方向判断"
    out = _NEG_RE.sub(repl, text, count=1)
    # 原句后面常跟"，按非交易信号处理"——品种明明是核心品种，这句同样误导
    out = out.replace("，按非交易信号处理", "").replace(", 按非交易信号处理", "")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=3, help="只处理最近 N 天")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        rows = conn.execute(
            "SELECT id, target_asset, reasoning FROM ai_decisions "
            "WHERE target_asset IS NOT NULL AND reasoning LIKE '%核心监控%' "
            "AND reasoning LIKE '%不在%' AND ts >= ?",
            (int(time.time()) - args.days * 86400,),
        ).fetchall()
        hits = []
        for rid, asset, text in rows:
            new = rewrite(text or "", str(asset or "").upper())
            if new != (text or ""):
                hits.append((rid, asset, text, new))
        print(f"[扫描] 命中 {len(hits)} 条 (候选 {len(rows)} 条)")
        for rid, asset, old, new in hits[:3]:
            print(f"  #{rid} {asset}: {str(old)[:60]} -> {str(new)[:60]}")
        if args.dry_run:
            print("[DRY-RUN] 不写库")
            return 0
        for rid, _asset, _old, new in hits:
            conn.execute("UPDATE ai_decisions SET reasoning=? WHERE id=?", (new, rid))
        conn.commit()
        print(f"[COMMIT] 已改写 {len(hits)} 条")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
