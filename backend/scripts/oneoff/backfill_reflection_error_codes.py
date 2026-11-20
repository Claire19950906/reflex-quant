#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_reflection_error_codes.py — 重跑反思校验器 + 重算三维细分小类 (G11)。

背景 (G11 红灯的根因)
---------------------
trade_reflections_v2 的 dimension/subcategory 只剩 5 种组合, 看起来像"分类体系
没实现"。真实原因有两条:

  1. **校验器输入缺字段**: review_agent 传给 reflection_checkers 的 dict 里没有
     news_id / event_atom_ids / cross_asset_likely / reasoning_path —— 而 checker
     恰恰要检查这几项 → E001d(缺事件链关联) 对**每一条**决策都误报 → 所有反思都被
     打成 C/feed_missing, 三维细分自然塌成一种。
     (已修: review_agent._fetch_trades + checker_dec 补齐字段)

  2. 历史错误码是**在缺字段的前提下**跑出来的, 所以 decision_errors 里的码分布
     也是歪的 (E001a/E001d 占 97%)。本脚本用**完整字段**重跑一遍校验器, 重写
     decision_errors, 再按 reflection_dimension_map 的权威口径重算维度/小类。

口径: reflection_dimension_map.py (与写入路径完全一致)
用法: python3 backfill_reflection_error_codes.py --limit 4000
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

from review import reflection_checkers as rc  # noqa: E402
from review import reflection_dimension_map as rdm  # noqa: E402

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or str(
    Path(__file__).resolve().parents[2] / "trident_event_bus.db"
)

SELECT = """
SELECT ad.id, ad.suggested_action, ad.sentiment_score, ad.factor_ranking,
       ad.hard_data_evidence, ad.data_anchor_ts, ad.reasoning, ad.target_asset,
       ad.forward_pnl, ad.news_id, ad.event_atom_ids,
       ad.reasoning_path, ad.invalidation_condition, ad.peer_reviews, ad.event_struct,
       ad.asset_reasoning, ad.structured_input, ad.evidence_confidence,
       ad.completeness_score, ad.asset_impact,
       qs.mfe_pct, qs.mae_pct, qs.pnl_pct, qs.verdict,
       rn.content AS news_text, rn.gate_verdict
FROM ai_decisions ad
LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id AND qs.settled = 1
LEFT JOIN raw_news rn ON rn.id = ad.news_id
ORDER BY ad.id DESC
"""


def build_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """完整字段的 checker 输入 (字段名与 reflection_checkers 的读取一致)。"""
    keys = (
        "id", "suggested_action", "sentiment_score", "factor_ranking",
        "hard_data_evidence", "data_anchor_ts", "reasoning", "target_asset",
        "forward_pnl", "mfe_pct", "mae_pct", "news_text", "gate_verdict",
        "news_id", "event_atom_ids", "reasoning_path",
        "invalidation_condition", "peer_reviews", "event_struct",
        "asset_reasoning", "structured_input", "evidence_confidence",
        "completeness_score",
    )
    payload = {k: row.get(k) for k in keys}
    # ai_decisions 没有 cross_asset_likely 列 → 从 asset_reasoning 还原
    payload["cross_asset_likely"] = rc.synthesize_cross_asset_likely(
        row.get("asset_reasoning"))
    return payload



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--limit", type=int, default=4000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    rows = [dict(r) for r in conn.execute(SELECT + " LIMIT ?", (args.limit,)).fetchall()]
    print("[INFO] 待重跑校验器的 AI 决策: %d" % len(rows))

    code_count: Dict[str, int] = {}
    per_decision_codes: Dict[int, List[str]] = {}
    all_errors: List[Dict[str, Any]] = []
    for row in rows:
        payload = build_payload(row)
        try:
            errs = rc.run_all_checkers(payload, conn=conn)
        except Exception as exc:  # noqa: BLE001
            print("[WARN] decision %s checker 异常: %s" % (row.get("id"), exc))
            continue
        codes = []
        for e in errs:
            c = e.get("error_code")
            if not c:
                continue
            # 带严重度: "E021:3" —— resolve_dimension 按严重度优先归类
            try:
                sev = int(e.get("severity", 0) or 0)
            except (TypeError, ValueError):
                sev = 0
            codes.append("%s:%d" % (c, sev))
            code_count[c] = code_count.get(c, 0) + 1
            all_errors.append(e)
        if codes:
            per_decision_codes[int(row["id"])] = codes
    print("[INFO] 错误码分布: %s" % json.dumps(code_count, ensure_ascii=False))
    combos: Dict[str, int] = {}
    for did, codes in per_decision_codes.items():
        hit = rdm.resolve_dimension(error_codes=codes, layer="", sub_layer="", signal_name="")
        if hit:
            combos["%s/%s" % hit] = combos.get("%s/%s" % hit, 0) + 1
    print("[INFO] 重算后的细分小类: %d 种 → %s"
          % (len(combos), json.dumps(combos, ensure_ascii=False)))
    if args.dry_run:
        print("[DRY-RUN] 未写库")
        conn.close()
        return 0

    # 1) 重写这些决策的 decision_errors (先删旧码, 再写新码)
    ids = sorted(per_decision_codes.keys())
    if ids:
        CH = 400
        for i in range(0, len(ids), CH):
            chunk = ids[i:i + CH]
            conn.execute(
                "DELETE FROM decision_errors WHERE decision_id IN (%s)"
                % ",".join("?" for _ in chunk), chunk)
        conn.commit()
        rc.save_errors(all_errors, conn=conn)

    # 2) 重算反思行维度 (按 decision_id → 错误码 → 维度)
    #    用 decision_errors 里**最新的**错误码 (含严重度), 覆盖两种来源:
    #      · 刚重跑校验器写进去的 (ai 空间决策)
    #      · 之前跑出来的 (trade 空间决策) —— 严重度排序后能把"低危数据缺口"
    #        和"高危逻辑断裂"区分开, 三维细分不再塌成一种。
    err_by_dec: Dict[int, List[str]] = {}
    for er in conn.execute(
        "SELECT decision_id, error_code, severity FROM decision_errors"
    ).fetchall():
        did = er["decision_id"]
        if did is None:
            continue
        try:
            sev = int(er["severity"] or 0)
        except (TypeError, ValueError):
            sev = 0
        err_by_dec.setdefault(int(did), []).append("%s:%d" % (er["error_code"], sev))
    refl = conn.execute(
        "SELECT id, decision_id, layer, sub_layer, signal_name, error_layer, "
        "error_sub_layer, error_signal FROM trade_reflections_v2"
    ).fetchall()
    updated = 0
    for r in refl:
        did = int(r["decision_id"] or 0)
        codes = err_by_dec.get(did)
        if not codes:
            continue
        hit = rdm.resolve_dimension(
            error_codes=codes,
            layer=r["layer"] or r["error_layer"] or "",
            sub_layer=r["sub_layer"] or r["error_sub_layer"] or "",
            signal_name=r["signal_name"] or r["error_signal"] or "",
        )
        if not hit:
            continue
        conn.execute(
            "UPDATE trade_reflections_v2 SET dimension=?, subcategory=? WHERE id=?",
            (hit[0], hit[1], int(r["id"])),
        )
        updated += 1
    conn.commit()

    dist = conn.execute(
        "SELECT dimension, subcategory, COUNT(*) n FROM trade_reflections_v2 "
        "GROUP BY 1,2 ORDER BY n DESC"
    ).fetchall()
    print("[OK] 重算维度行数: %d" % updated)
    print("[OK] 反思侧细分小类: %d 种" % len(dist))
    for d in dist:
        print("   %s/%s: %d" % (d[0], d[1], d[2]))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
