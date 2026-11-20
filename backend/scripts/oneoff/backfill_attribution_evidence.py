"""backfill_attribution_evidence.py —— 给历史归因补"证据来源"标记（v336 P1-1）

只做一件事：在 event_move_attribution.algo_reasons 末尾追加
`; evidence=headline|cross_asset|body_only|macro`。
**不改分数、不改筛选、不删行**，幂等（已标过的只替换标记）。

实测背景（242 行）：
  标题主语=标的 34.7% / 跨品种 16.9% / 标题无主语 48.3%。
  近一半"原因"其实是无主语的宏观背景，而系统此前不区分。

用法：
  python3 backfill_attribution_evidence.py              # dry-run
  python3 backfill_attribution_evidence.py --commit     # 写库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--table", default="event_move_attribution")
    args = ap.parse_args()

    try:
        import evidence_source as es
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 无法导入 evidence_source: {exc}")
        return 1

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro" if not args.commit else str(DB_PATH),
                           uri=not args.commit, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT id, asset, algo_reasons, ev_news_id FROM {args.table}").fetchall()
    ctx = es.fetch_news_ctx(conn, [r["ev_news_id"] for r in rows])

    updates, levels, changed = [], [], 0
    for r in rows:
        hl, bd = ctx.get(int(r["ev_news_id"] or 0), ("", ""))
        lv = es.classify_evidence(hl, bd, str(r["asset"] or "").upper())
        levels.append(lv)
        old = str(r["algo_reasons"] or "")
        new = es.tag_reason(old, lv)
        if new != old:
            changed += 1
            updates.append((new, r["id"]))

    s = es.summarize(levels)
    print(f"{args.table}: {s['total']} 行")
    for k in es.LEVELS:
        pct = 100.0 * s[k] / s["total"] if s["total"] else 0.0
        print(f"  {k:<12} {s[k]:>4}  {pct:>5.1f}%")

    if args.commit:
        conn.executemany(f"UPDATE {args.table} SET algo_reasons=? WHERE id=?", updates)
        conn.commit()
        print(f"\n已更新 {len(updates)} 行（其中 {changed} 行标记有变化）")
    else:
        print(f"\n可更新 {len(updates)} 行（dry-run：没有写库。确认后加 --commit）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
