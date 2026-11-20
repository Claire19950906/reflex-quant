#!/usr/bin/env python3
"""evidence_confidence 字段体检（只读）。

为什么要单独体检：
    实测 ai_decisions 327 行里 296 行的 evidence_confidence=0.0，
    而且这 296 行的 sentiment_score 也恰好全为 0 —— 说明不是"模型判断没把握"，
    而是"模型根本没填这两个结构化字段"（reasoning 里只有自然语言的结论）。

    剩下 31 行非零值范围 2.55~27.29，明显是 0~100 口径；
    而 POSITION_SIZING_TIERS（0/0.3/0.7）与 policy REQUIRE_CONFIDENCE(0.6)
    都按 0~1 理解 —— 值域不一致会让分层判断全部撞到边界。

    position_sizing 的既有契约（含单测）是"越界值 → 按基础规模(fallback)"，
    所以那 31 行不会被放大成 1.5 倍；但 296 行 0.0 会走"最低档 0.5x"，
    等于对 90% 的信号系统性砍半仓位。目前只成交 2 笔模拟单、都是 fallback，
    风险是"latent"而非已发生。

用法：
    python3 scripts/audit_confidence_field.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "backend/src_python")
import db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30)
    rows = conn.execute(
        "SELECT evidence_confidence, sentiment_score FROM ai_decisions"
    ).fetchall()

    total = len(rows)
    zero = sum(1 for c, _ in rows if not c)
    zero_both = sum(1 for c, s in rows if not c and not s)
    over1 = [float(c) for c, _ in rows if c and float(c) > 1.0]
    inrange = [float(c) for c, _ in rows if c and 0 < float(c) <= 1.0]

    out = {
        "total": total,
        "zero_or_null": zero,
        "zero_and_sentiment_zero": zero_both,
        "over_unit_scale": len(over1),
        "over_unit_range": [min(over1), max(over1)] if over1 else None,
        "in_unit_scale": len(inrange),
        "verdict": (
            "该字段基本未填（0 值与 sentiment=0 完全重合），"
            "且少量非零值是 0~100 口径，与 0~1 的阈值口径不一致"
            if zero_both >= total * 0.8 else "字段分布正常"
        ),
        "impact_note": "0.0 会按最低档 0.5x 下单；越界值按既有契约走 fallback 1.0x",
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print(f"ai_decisions 总数 {total}")
    print(f"  evidence_confidence = 0/NULL : {zero} ({zero / total:.1%})")
    print(f"  其中 sentiment_score 也为 0 : {zero_both} ({zero_both / total:.1%})")
    print(f"  0~1 区间内的有效值        : {len(inrange)}")
    print(f"  >1（0~100 口径）          : {len(over1)}"
          + (f"  范围 {min(over1):.2f}~{max(over1):.2f}" if over1 else ""))
    print(f"\n判定：{out['verdict']}")
    print(f"影响：{out['impact_note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
