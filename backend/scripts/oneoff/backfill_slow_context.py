"""backfill_slow_context.py —— 给慢异动补齐市场上下文（v336 P1）

问题（实测 2026-11-13 只读）：
  critical_price_moves 里 slow_rise 81 / slow_fall 101，共 182 条，
  context_json 只写了 k_days / window_high / window_low，
  **regime / vol_ratio / crowd_sentiment 覆盖率全部为 0**；
  而 fast_rise / fast_fall 有 61~101 条 / 223 条。

  于是"结合资金流向、成交量、K线形态判断当前市场情况"这件事，
  对慢涨慢跌从来没算过 —— 归因页只能看到"涨了/跌了"。

本脚本做三件事（默认 dry-run，不写库）：
  1) 按 move 的 ts 取"当时及之前"的 市场状态 / 成交量放大倍数 / 舆情 / 强平，
     全部带时间上界，杜绝把事后信息写进历史；
  2) 只补空值，已有真实观测一律不覆盖；
  3) 打印补齐前后覆盖率，供判断值不值得写库。

用法：
  python3 backfill_slow_context.py                 # dry-run，只报数
  python3 backfill_slow_context.py --commit        # 真正 UPDATE context_json
  python3 backfill_slow_context.py --limit 30      # 只试前 N 条
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

FIELDS = ("regime", "vol_ratio", "crowd_sentiment")


def _coverage(rows) -> dict:
    cov = {f: 0 for f in FIELDS}
    for ctx in rows:
        for f in FIELDS:
            if ctx.get(f) is not None:
                cov[f] += 1
    cov["n"] = len(rows)
    return cov


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    args = ap.parse_args()

    try:
        import fast_move_detector as fmd
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 无法导入 fast_move_detector: {type(exc).__name__}: {exc}")
        return 1

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro" if not args.commit else str(DB_PATH),
                           uri=not args.commit, timeout=30)
    conn.row_factory = sqlite3.Row

    sql = ("SELECT id, asset, ts, move_kind, context_json FROM critical_price_moves "
           "WHERE move_kind LIKE 'slow%' ORDER BY ts DESC")
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql).fetchall()

    before, after, updates, by_kind = [], [], [], {}
    for r in rows:
        try:
            ctx = json.loads(r["context_json"] or "{}")
            if not isinstance(ctx, dict):
                ctx = {}
        except (TypeError, ValueError):
            ctx = {}
        before.append(ctx)
        new = fmd.enrich_context(conn, str(r["asset"]).upper(), int(r["ts"]), ctx)
        after.append(new)
        by_kind[r["move_kind"]] = by_kind.get(r["move_kind"], 0) + 1
        if args.commit:
            conn.execute("UPDATE critical_price_moves SET context_json=? WHERE id=?",
                         (json.dumps(new, ensure_ascii=False), r["id"]))
        elif new != ctx:
            updates.append((r["id"], r["asset"], r["move_kind"], ctx, new))

    if args.commit:
        conn.commit()

    cb, ca = _coverage(before), _coverage(after)
    print(f"慢异动 {len(rows)} 条  {by_kind}")
    print(f"{'字段':<16}{'补齐前':>8}{'补齐后':>8}")
    for f in FIELDS:
        print(f"{f:<16}{cb[f]:>8}{ca[f]:>8}")

    changed = len(updates) if not args.commit else len(rows)
    print(f"\n{'已写库' if args.commit else '可补'} {changed} 条")
    if not args.commit:
        print("（dry-run：没有写库。确认后加 --commit）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
