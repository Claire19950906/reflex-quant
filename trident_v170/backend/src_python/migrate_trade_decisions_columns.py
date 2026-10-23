"""
migrate_trade_decisions_columns.py — C1 欠账补齐: trade_decisions 结算列
=============================================================
背景:
  生产库 trade_decisions 是 19 列的现行结构(decision_ts / intended_action /
  final_action / decision_type / entry_price / planned_stop / planned_target ...),
  属主模块为 trade_decisions.py, 已有 1999 行真实数据。
  但 bridge_server.py 的 /api/decision/* 与 decision_settle_loop 仍是 v170 世代
  遗留代码, 按 12 列旧结构编写(action/score/confidence/status/settled_at/
  actual_outcome_pct/price_at_decision/rationale/evidence_json)。

  注意: bridge 里的 ensure_trade_decisions_table() 用 CREATE TABLE IF NOT EXISTS
  声明旧结构 —— 表已存在, 该语句静默 no-op, 于是旧列缺失被完全掩盖, 表现为
  运行期 "no such column: status"。

处理原则:
  只补"语义上确实成立、且在现行结构中无对应物"的 3 个结算列;
  不补 action/score/confidence/rationale/evidence_json —— 它们在现行结构里
  分别已由 final_action/news_star/context_json 等表达, 补列会造成语义重复的双轨。

  status               决策生命周期: open / settled / cancelled
  settled_at           结算时刻(epoch 秒)
  actual_outcome_pct   结算时按 entry_price 计算的前瞻涨跌(%)

全部为可空新增列, 不改动任何既有列与数据。

执行:
  cd backend && python3 src_python/migrate_trade_decisions_columns.py
"""
from __future__ import annotations
import datetime
import shutil
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
BACKUP_DIR = BACKEND_DIR / "backups"

NEW_COLUMNS = [
    ("status", "INTEGER", "TEXT    NOT NULL DEFAULT 'open'"),
    ("settled_at", "INTEGER", "INTEGER"),
    ("actual_outcome_pct", "REAL", "REAL"),
]


def _columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(trade_decisions)")}


def main() -> int:
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        have = _columns(conn)
        if not have:
            print("[FATAL] trade_decisions 表不存在或无列, 中止", file=sys.stderr)
            return 2
        if "decision_ts" not in have:
            print("[FATAL] trade_decisions 不是预期的现行结构(缺 decision_ts), "
                  "中止以免改错表", file=sys.stderr)
            return 2

        todo = [(n, t, d) for n, t, d in NEW_COLUMNS if n not in have]
        if not todo:
            print("[OK] 3 个结算列均已存在, 无需变更")
            return 0

        # ALTER TABLE 会改写 sqlite_master, 先留一份 DDL 快照便于回滚
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap = BACKUP_DIR / f"trade_decisions.schema_pre_addcols_{stamp}.sql"
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trade_decisions' "
            "AND type='table'").fetchone()[0]
        snap.write_text(ddl + ";\n", encoding="utf-8")
        print(f"[INFO] DDL 快照: {snap}")

        for name, typ, decl in todo:
            conn.execute(f"ALTER TABLE trade_decisions ADD COLUMN {name} {decl}")
            print(f"  + {name} ({typ})")

        conn.commit()

        after = _columns(conn)
        missing = {n for n, _, _ in NEW_COLUMNS} - after
        if missing:
            print(f"[ERROR] 仍缺列: {sorted(missing)}", file=sys.stderr)
            return 2

        n, n_settled = conn.execute(
            "SELECT COUNT(*), SUM(status='settled') FROM trade_decisions"
        ).fetchone()
        print(f"[OK] trade_decisions 现有 {n} 行, status='settled' {n_settled or 0} 行")
        print("[OK] 既有列与数据未改动; 新列对历史行为 NULL/'open'")
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())