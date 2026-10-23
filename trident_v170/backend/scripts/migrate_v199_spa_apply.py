#!/usr/bin/env python3
"""v199 migration — 打通 SPA→applied→ai_decisions 闭环。

变更:
  1. ai_decisions 加 applied_setting_changes TEXT DEFAULT ''
     （v199 新增 — 让 ai_worker 能把已 applied 的 SPA 调整注入 LLM prompt）
  2. strategy_param_adjustments.applied_setting_change 列已存在（v176+）
     不需要再迁移

回滚: ALTER TABLE ai_decisions DROP COLUMN applied_setting_changes;
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/src_python")
import db, sqlite3


def _has_column(conn, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def main() -> int:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        # 1. ai_decisions.applied_setting_changes
        if not _has_column(conn, "ai_decisions", "applied_setting_changes"):
            conn.execute("ALTER TABLE ai_decisions ADD COLUMN applied_setting_changes TEXT DEFAULT ''")
            conn.commit()
            print("[MIGRATE v199] ai_decisions.applied_setting_changes 添加成功")
        else:
            print("[MIGRATE v199] ai_decisions.applied_setting_changes 已存在 (跳过)")

        # 2. 验证
        if not _has_column(conn, "ai_decisions", "applied_setting_changes"):
            print("[FAIL] 列验证失败")
            return 1
        # 现有 ai_decisions 行数（应该全部 applied_setting_changes='' 默认）
        n = conn.execute("SELECT COUNT(*) AS n FROM ai_decisions").fetchone()["n"]
        nonempty = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_decisions WHERE applied_setting_changes != ''"
        ).fetchone()["n"]
        print(f"[MIGRATE v199] ai_decisions 总行数={n}, 非空 applied_setting_changes={nonempty}")
        print("[MIGRATE v199] OK")
        return 0
    except Exception as exc:
        print(f"[MIGRATE v199] 失败: {type(exc).__name__}: {exc}")
        conn.rollback()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())