"""backfill_impact_score.py —— 用门卫分/强度回填"默认 5.0"的 impact_score（默认 dry-run）

问题（2026-11-16 线上实测，470 条决策）：
  · impact_score 停在默认 5.0：399 条（84.9%）→ S15「影响力维实质化」只有 14.2%
  · 其中 401 条对应的新闻在 raw_news 里**门卫根本没打分**（gate_impact_1to100=0），
    只有 4 条有 1-100 分、9 条有 gate_strength(1-10)
  · 于是 base_mult 有 88.7% 落在同一个值 → 权重分辨率 S14 只有 14.6%（历史口径）

本脚本只做"能救的那部分"：
  1) 逐条走 impact_score_ladder.resolve_impact_score（与引擎同一份实现）
  2) 只改 impact≈5.0 且**有外部证据**（gate 分或 gate strength）的行
  3) field_sources 里记下来源（gate_impact_1to100 / gate_strength），
     5.0 到底是"判断"还是"兜底"，复盘的时侯一眼能分辨
  4) 幂等：已经不是 5.0 的行、已回填过的行都不会被二次改写

跑之前先预估：脚本会打印"S15 分母/分子变化"和"仍无法回填"的原因分布。
剩余那批（门卫没打分）**不会**被编一个数糊过去 —— 唯一正解是补门卫覆盖，
那是上游覆盖率问题，不是回填能解决的。

用法：
  python3 backfill_impact_score.py                 # dry-run（只读，默认）
  python3 backfill_impact_score.py --limit 200     # 只看最近 200 条
  python3 backfill_impact_score.py --commit        # 写库
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from impact_score_ladder import resolve_impact_score, source_label

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"


def _connect(readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(conn: sqlite3.Connection, limit: int) -> List[Dict[str, Any]]:
    sql = """
        SELECT d.id, d.impact_score, d.field_sources, d.target_asset,
               COALESCE(r.gate_impact_1to100, 0) AS gi,
               COALESCE(r.gate_strength, 0) AS gs
        FROM ai_decisions d
        LEFT JOIN raw_news r ON r.id = d.news_id
        WHERE ABS(COALESCE(d.impact_score, 5.0) - 5.0) < 1e-9
        ORDER BY d.id DESC
    """
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def plan(rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    """算出"能回填"的行 + 剩余原因分布（纯逻辑，便于单测）。"""
    todo: List[Dict[str, Any]] = []
    why: Dict[str, int] = {}
    for r in rows:
        score, src = resolve_impact_score(5.0, r["gi"], gate_strength=r["gs"])
        if src in ("default", "unavailable") or score is None:
            why["门卫无分（无 gate 1-100、无 strength）"] = \
                why.get("门卫无分（无 gate 1-100、无 strength）", 0) + 1
            continue
        todo.append({**r, "new_score": score, "source": src})
    return todo, why


def merge_field_sources(raw: Any, label: str) -> str:
    try:
        fs = json.loads(raw or "{}")
        if not isinstance(fs, dict):
            fs = {}
    except (json.JSONDecodeError, TypeError):
        fs = {}
    fs["impact_score"] = label
    return json.dumps(fs, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="写库（默认只读 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理最近 N 条（0=全部）")
    args = ap.parse_args()

    conn = _connect(readonly=not args.commit)
    try:
        rows = load_rows(conn, args.limit)
        total = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
        default_total = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions "
            "WHERE ABS(COALESCE(impact_score, 5.0) - 5.0) < 1e-9").fetchone()[0]

        if not rows:
            print("没有 impact_score=5.0 的决策，无需回填")
            return 0

        todo, why = plan(rows)
        by_src: Dict[str, int] = {}
        for r in todo:
            by_src[r["source"]] = by_src.get(r["source"], 0) + 1

        n = len(rows)
        print(f"决策总数 {total}；impact=5.0 的 {default_total} 条（本次扫描 {n} 条）")
        print(f"可回填 {len(todo)} 条 ({len(todo)/n:.1%} of scanned)，按来源：")
        for k, v in sorted(by_src.items(), key=lambda x: -x[1]):
            print(f"  - {k}: {v}")
        if why:
            print("无法回填：")
            for k, v in sorted(why.items(), key=lambda x: -x[1]):
                print(f"  - {k}: {v}")

        # S15 口径投影（分母 = 全库决策数，分子 = impact != 5.0）
        s15_now = (total - default_total) / total * 100 if total else 0.0
        s15_after = (total - default_total + len(todo)) / total * 100 if total else 0.0
        print(f"\nS15 投影：{s15_now:.1f}% → {s15_after:.1f}%  (目标 80%)")

        if todo:
            print("\n示例（前 5 条）：")
            for r in todo[:5]:
                print(f"  id={r['id']} asset={r['target_asset']} "
                      f"gate={r['gi']} strength={r['gs']} → {r['new_score']} "
                      f"({r['source']})")

        if not args.commit:
            print("\n[dry-run] 未写库。确认后加 --commit")
            return 0

        updated = 0
        for r in todo:
            conn.execute(
                "UPDATE ai_decisions SET impact_score=?, field_sources=? WHERE id=?",
                (r["new_score"],
                 merge_field_sources(r["field_sources"], source_label(r["source"])),
                 r["id"]),
            )
            updated += 1
        conn.commit()
        print(f"\n[commit] 已回填 {updated} 条")
        after = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions "
            "WHERE ABS(COALESCE(impact_score, 5.0) - 5.0) < 1e-9").fetchone()[0]
        print(f"[commit] 剩余 impact=5.0：{after} 条")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
