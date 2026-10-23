"""v230.2 P0.1：回填 quick_sim_trades / ai_decisions 中被写死 verdict 错判的记录。

历史 bug：_settle_sl 强制写 verdict='LOSS'，即使 blended pnl > 0。
          _settle_tp2 强制写 verdict='WIN'，即使 blended pnl < 0。

修复策略：用 pnl_pct / forward_pnl 的实际符号决定 verdict。
仅修 in-flight 时被 bug 污染的记录，不动其他正常记录。

用法：
    python3 scripts/backfill_v230p0_1_verdict_sign.py --dry-run
    python3 scripts/backfill_v230p0_1_verdict_sign.py            # 执行
    python3 scripts/backfill_v230p0_1_verdict_sign.py --revert   # 从 .bak 还原

可在 /Users/claire/.cline/data/workspaces/chat/trident_v170/backend 下执行。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
BACKUP_PATH = BACKEND_DIR / f"trident_event_bus.db.v230p0_1_bak_{int(time.time())}"


def _sign_verdict(pnl_pct: float) -> str:
    if pnl_pct > 0:
        return "WIN"
    if pnl_pct < 0:
        return "LOSS"
    return "BREAKEVEN"


def _classify_quick_sim(cur: sqlite3.Cursor):
    """找出所有被 bug 污染的 quick_sim_trades 记录（pnl_pct 符号 ≠ verdict）。"""
    rows = cur.execute(
        """
        SELECT id, asset, pnl_pct, verdict
          FROM quick_sim_trades
         WHERE settled = 1
           AND pnl_pct IS NOT NULL
           AND (
             (pnl_pct > 0 AND verdict = 'LOSS') OR
             (pnl_pct < 0 AND verdict = 'WIN')
           )
         ORDER BY id
        """
    ).fetchall()
    return [
        (int(r[0]), str(r[1]), float(r[2]), str(r[3]),
         _sign_verdict(float(r[2])))
        for r in rows
    ]


def _classify_ai_decisions(cur: sqlite3.Cursor):
    """找出 ai_decisions.is_correct 与 forward_pnl 符号冲突的记录。"""
    rows = cur.execute(
        """
        SELECT id, forward_pnl, is_correct
          FROM ai_decisions
         WHERE forward_pnl IS NOT NULL
           AND is_correct IS NOT NULL
           AND is_correct IN ('WIN', 'LOSS', 'BREAKEVEN')
           AND (
             (forward_pnl > 0 AND is_correct = 'LOSS') OR
             (forward_pnl < 0 AND is_correct = 'WIN')
           )
         ORDER BY id
        """
    ).fetchall()
    return [
        (int(r[0]), float(r[1]), str(r[2]), _sign_verdict(float(r[1])))
        for r in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印要改的记录，不写入")
    parser.add_argument("--revert", action="store_true",
                        help=f"从 {BACKUP_PATH.name} 还原")
    args = parser.parse_args()

    if args.revert:
        if not BACKUP_PATH.exists():
            print(f"❌ 备份文件不存在: {BACKUP_PATH}", file=sys.stderr)
            return 1
        shutil.copy2(BACKUP_PATH, DB_PATH)
        print(f"✅ 已从 {BACKUP_PATH.name} 还原到 {DB_PATH.name}")
        return 0

    if not DB_PATH.exists():
        print(f"❌ DB 不存在: {DB_PATH}", file=sys.stderr)
        return 1

    if not args.dry_run and not BACKUP_PATH.exists():
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print(f"📦 已备份原库到 {BACKUP_PATH.name}")

    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        qs_records = _classify_quick_sim(conn)
        ai_records = _classify_ai_decisions(conn)

        print(f"\n=== quick_sim_trades 待修复: {len(qs_records)} 笔 ===")
        for tid, asset, pnl, old_v, new_v in qs_records:
            print(f"  id={tid:4d}  {asset:6s}  pnl_pct={pnl:+.4f}%  "
                  f"{old_v} -> {new_v}")

        print(f"\n=== ai_decisions 待修复: {len(ai_records)} 笔 ===")
        for did, pnl, old_v, new_v in ai_records:
            print(f"  id={did:5d}  forward_pnl={pnl:+.4f}%  "
                  f"{old_v} -> {new_v}")

        if args.dry_run:
            print(f"\n[DRY-RUN] 模式，未写入。"
                  f"\n重跑时去掉 --dry-run 即可执行。")
            return 0

        if not qs_records and not ai_records:
            print(f"\n[OK] 无需修复（数据已正确）。")
            return 0

        # 实际写回
        for tid, _asset, _pnl, _old, new_v in qs_records:
            conn.execute(
                "UPDATE quick_sim_trades SET verdict = ? WHERE id = ?",
                (new_v, tid),
            )
        for did, _pnl, _old, new_v in ai_records:
            conn.execute(
                "UPDATE ai_decisions SET is_correct = ? WHERE id = ?",
                (new_v, did),
            )
        conn.commit()

        print(f"\n[OK] 修复完成: quick_sim_trades x {len(qs_records)}, "
              f"ai_decisions x {len(ai_records)}")
        print(f"[BAK] 备份位置: {BACKUP_PATH}")
        print(f"[REVERT] 需要回滚: "
              f"python3 scripts/backfill_v230p0_1_verdict_sign.py --revert")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())