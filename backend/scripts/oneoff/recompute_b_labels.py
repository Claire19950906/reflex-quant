#!/usr/bin/env python3
"""v323: 重算 trade_reflections_v2 里 B 维（信息分析方向）的标签 —— 只认证据。

为什么做：
    漏检面板里 B 维有两个数：实时检测 18 条，反思表 1,548 条（真实反思样本），
    差了近 100 倍。之前已经查到 40,508 条 B1_rate_conflict 是"利率变动 ±7 天内
    任意反向行情"笛卡尔展开出来的伪标签，同一套旧逻辑也给反思表贴过 B 标签。
    于是 B 维的"方向错了"里混进了大量没有方向证据的行。

判定（必须有证据，不靠猜）：
    1. 该反思行能关联到 ai_decision（有 ai_decision_id）；
    2. 决策当时给过方向（suggested_action ∈ BUY/SELL）；
    3. 决策时刻价 vs +60 分钟价（market_ticks 逐笔），反向超 0.5% 才算"方向错"。
    三条同时成立 → 保留 B，按证据细分：
        B2_event_misdirection / B3_chain_misuse / B1_macro_conflict（看推理关键词）
    任一条不成立 → 该行没有方向证据，标签改成 'unverified_legacy_label'，
    不删除行、不改 hit/outcome，只让统计不再把它算成"方向错"。

用法：
    python3 recompute_b_labels.py            # 试运行，只打印影响面
    python3 recompute_b_labels.py --commit   # 落库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

HORIZON_SEC = 3600
WRONG_PCT = 0.5

MACRO_KW = ("利率", "美联储", "FOMC", "CPI", "非农", "央行", "加息", "降息", "通胀", "国债收益率")
CHAIN_KW = ("chain", "causal", "链", "因果")


def _px_at(conn, sym: str, ts: int) -> float | None:
    row = conn.execute(
        "SELECT price FROM market_ticks WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (sym.upper(), ts),
    ).fetchone()
    return float(row[0]) if row and row[0] else None


def _sub_label(reasoning: str) -> str:
    low = (reasoning or "").lower()
    if any(k in low for k in CHAIN_KW):
        return "B3_chain_misuse"
    if any(k.lower() in low for k in MACRO_KW):
        return "B1_macro_conflict"
    return "B2_event_misdirection"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="落库改写")
    args = ap.parse_args()

    import db as _db

    dbp = _db.get_db_path()
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ai_decision_id, action, asset, created_at, subcategory, "
        "       substr(coalesce(reasoning_chain,''),1,400) as reasoning "
        "FROM trade_reflections_v2 "
        "WHERE dimension='B' AND ai_decision_id IS NOT NULL"
    ).fetchall()

    decs = {}
    for d in conn.execute(
        "SELECT id, suggested_action, target_asset, created_at FROM ai_decisions"
    ):
        decs[int(d["id"])] = d

    out = Counter()
    updates = []
    for r in rows:
        dec = decs.get(int(r["ai_decision_id"] or 0))
        if dec is None:
            out["无决策记录"] += 1
            updates.append((r["id"], "unverified_legacy_label"))
            continue
        act = (dec["suggested_action"] or "").upper()
        asset = (dec["target_asset"] or r["asset"] or "").upper()
        if act not in ("BUY", "SELL") or not asset or asset == "NONE":
            # HOLD / REJECTED / 无标的 → 决策本身没给方向，谈不上"方向错"
            out[f"决策无方向({act or 'EMPTY'})"] += 1
            updates.append((r["id"], "unverified_legacy_label"))
            continue
        try:
            from datetime import datetime
            t0 = int(datetime.fromisoformat(dec["created_at"]).timestamp())
        except Exception:
            out["决策时间不可解析"] += 1
            updates.append((r["id"], "unverified_legacy_label"))
            continue
        p0 = _px_at(conn, asset, t0)
        p1 = _px_at(conn, asset, t0 + HORIZON_SEC)
        if not p0 or not p1:
            out[f"无行情({asset})"] += 1
            updates.append((r["id"], "unverified_legacy_label"))
            continue
        chg = (p1 - p0) / p0 * 100.0
        wrong = (act == "BUY" and chg <= -WRONG_PCT) or (act == "SELL" and chg >= WRONG_PCT)
        if wrong:
            out["确认方向错"] += 1
            new_sub = _sub_label(r["reasoning"])
            if new_sub != (r["subcategory"] or ""):
                updates.append((r["id"], new_sub))
        else:
            out["方向其实没错"] += 1
            updates.append((r["id"], "unverified_legacy_label"))

    print("=" * 60)
    print(f"B 维可关联决策行: {len(rows)}")
    for k, v in out.most_common():
        print(f"  {k:<24} {v}")
    print(f"  需要改写标签: {len(updates)}")
    if not args.commit:
        print("\n(试运行，未落库 — 加 --commit 生效)")
        return

    w = sqlite3.connect(dbp, timeout=20)
    try:
        cur = w.cursor()
        for rid, sub in updates:
            cur.execute(
                "UPDATE trade_reflections_v2 SET subcategory=? WHERE id=?", (sub, int(rid))
            )
        w.commit()
        print(f"\n✅ 已落库 {len(updates)} 行（subcategory 重标，未改 hit/outcome）")
    finally:
        w.close()


if __name__ == "__main__":
    main()
