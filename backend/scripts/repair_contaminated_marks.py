#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理「不可能发生」的污染否决标记（counter_factual 里的 XXX_CONTAMINATED）。

背景（2026-11-18 实测）
----------------------
`verifier_feedback._find_related_insights` 原来的关联窗口是 **±48h**（决策前后各 48h），
并且 asset='' 的全局洞察匹配任何资产。结果：
  · 96/97 条洞察都带 `E014_CONTAMINATED` / `E020_CONTAMINATED` 标记；
  · 35 条被 veto（active=0），其中 17 条**根本没有一条同资产事故决策发生在它之后**
    —— 一条洞察不可能去带偏一条比它还早出生的决策。

v409.6 已把窗口改成单向（洞察必须早于决策）。本脚本负责把历史上写错方向的标记洗掉：
  · 逐码重算：存在「同资产（或全局）且 决策时刻 ∈ [洞察创建, +48h]」的该码事故 → 该码标记保留；
  · 不成立的码从 counter_factual 里删掉；全删空则把 counter_factual 置空；
  · `active` 不动（交给 review_agent 的"无样本自愈"按新规则决定，避免与胜率停用逻辑打架）。

用法：
    python3 scripts/repair_contaminated_marks.py            # 干跑
    python3 scripts/repair_contaminated_marks.py --commit
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Dict

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB = os.path.join(_BACKEND, "trident_event_bus.db")
_CODES = ("E014", "E020")


def _legit_codes(conn: sqlite3.Connection, insight_id: int, asset: str,
                 created_at: str) -> Dict[str, int]:
    """返回 {code: 合法关联的决策条数}（按单向因果 + 48h 窗口重算）。"""
    out: Dict[str, int] = {}
    for code in _CODES:
        n = conn.execute(
            "SELECT COUNT(*) FROM decision_errors e"
            " JOIN ai_decisions d ON d.id = e.decision_id"
            " WHERE e.error_code = ?"
            "   AND (d.target_asset = ? OR ? = '' OR ? IS NULL)"
            "   AND julianday(d.created_at) >= julianday(?)"
            "   AND julianday(d.created_at) <= julianday(?) + 2.0",
            (code, asset, asset, asset, created_at, created_at),
        ).fetchone()[0]
        out[code] = int(n)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--restore-inactive", action="store_true",
                    help="把『非人工停用 + 未被验证否证 + 当前 active=0』的洞察恢复生效"
                         "（这些是被污染否决误杀的，v409.6 起该码不再 veto）")
    args = ap.parse_args()
    if not os.path.exists(_DB):
        print(f"库不存在：{_DB}")
        return 2

    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, asset, created_at, active, counter_factual"
            " FROM review_insights WHERE counter_factual LIKE '%CONTAMINATED%'"
        ).fetchall()
        print(f"[SCAN] 带污染标记的洞察 {len(rows)} 条")
        changed = 0
        cleared = 0
        for r in rows:
            legit = _legit_codes(conn, int(r["id"]), r["asset"] or "", r["created_at"])
            keep = [c for c in _CODES if legit.get(c)]
            note = r["counter_factual"] or ""
            drop = [c for c in _CODES if c + "_CONTAMINATED" in note and c not in keep]
            if not drop:
                continue
            # 重建 note：只留仍成立的码（保留原文里的 基于 N 个 样本 描述）
            rebuilt = ""
            for c in keep:
                marker = c + "_CONTAMINATED: "
                if marker in note:
                    tail = note.split(marker, 1)[1]
                    tail = tail.split("E0", 1)[0]  # 截到下个码之前
                    rebuilt += marker + tail
            if not rebuilt.strip():
                cleared += 1
            changed += 1
            if changed <= 8:
                print(f"  #{r['id']} active={r['active']} asset={r['asset'] or '全局'}"
                      f" 删掉={','.join(drop)} 保留={','.join(keep) or '无'}")
            if args.commit:
                conn.execute(
                    "UPDATE review_insights SET counter_factual=? WHERE id=?",
                    (rebuilt.strip(), int(r["id"])),
                )
        if args.commit:
            conn.commit()
            print(f"\n[COMMIT] 重写 {changed} 条（其中标记被完全清空 {cleared} 条）")
            left = conn.execute(
                "SELECT COUNT(*) FROM review_insights WHERE counter_factual LIKE '%CONTAMINATED%'"
            ).fetchone()[0]
            print(f"[CHECK] 仍带标记的还剩 {left} 条")
        else:
            print(f"\n[DRY-RUN] 会重写 {changed} 条（其中完全清空 {cleared} 条）；加 --commit 落库")

        if args.restore_inactive:
            rows2 = conn.execute(
                "SELECT id FROM review_insights WHERE active=0"
                " AND COALESCE(manual_deactivated,0)=0"
                " AND COALESCE(verification_verdict,'') <> 'failed' ORDER BY id"
            ).fetchall()
            print(f"[RESTORE] 待恢复 {len(rows2)} 条（非人工停用、未被否证）")
            if args.commit and rows2:
                for r2 in rows2:
                    conn.execute("UPDATE review_insights SET active=1 WHERE id=?", (int(r2["id"]),))
                conn.commit()
                print(f"[RESTORE] 已恢复 {len(rows2)} 条")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
