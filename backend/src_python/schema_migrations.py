# -*- coding: utf-8 -*-
"""schema_migrations.py — 轻量 schema 增量迁移注册表（P2-1）。

背景（2026-11-19 工程体检）
--------------------------
此前 schema 演进散落在 `db.migrate()` 里的一串 `CREATE TABLE IF NOT EXISTS`
和 `_ensure_columns`，外加一批手工 `migrate_*.py` 脚本；环境间是否跑过哪一步
没有统一记录，回滚靠备份。

本模块只补三件事，不重写既有 migrate：
  1. 用 SQLite 标准的 `PRAGMA user_version` 记录「已应用到哪个迁移版本」；
  2. 维护一张有序注册表 `MIGRATIONS`，每条带版本号 / 描述 / 幂等函数；
  3. `run_pending(conn)` 按版本号顺序执行尚未应用的迁移，每条独立 commit，
     失败不抬升版本号，下次启动可重跑。

约定（新增迁移时照此办理）
--------------------------
* 版本号严格递增，不复用、不跳号；当前基线见 `BASELINE_VERSION`。
* 迁移函数必须**幂等**：可重复执行不出错（先查列/表是否存在再 ALTER/CREATE）。
* 迁移函数只做 schema 变更，不写业务数据；数据回填另走一次性脚本。
* 每加一条：在 `MIGRATIONS` 追加 `Migration(version=..., description=..., up=fn)`。
* 现有 `db.P2_USER_VERSION=23` 是历史标记；本模块从 `BASELINE_VERSION=23` 起步，
  不回退、不重跑已完成的历史变更。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


BASELINE_VERSION = 23


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    up: Callable[[sqlite3.Connection], None]


# 迁移注册表：按 version 升序维护。新增条目追加到列表末尾即可。
MIGRATIONS: List[Migration] = [
    # 示例（保留为空，首条真实迁移在此登记）：
    # Migration(
    #     version=24,
    #     description="示例：给 xxx 表补 yyy 列",
    #     up=lambda conn: _ensure_column(conn, "xxx", "yyy", "TEXT DEFAULT ''"),
    # ),
]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """幂等补列：列已存在则跳过。供迁移函数复用。"""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def get_user_version(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def pending_migrations(conn: sqlite3.Connection) -> List[Migration]:
    """返回尚未应用（version > 当前 user_version）的迁移，按版本升序。"""
    current = get_user_version(conn)
    return [m for m in sorted(MIGRATIONS, key=lambda m: m.version) if m.version > current]


def run_pending(conn: sqlite3.Connection, *, real_run: bool = True) -> Dict[str, object]:
    """执行所有待应用迁移。

    Args:
        real_run=False：dry-run，只返回将要执行的清单，不改库。
        real_run=True：逐条执行，每条成功后 commit 并抬升 user_version。

    Returns:
        {"applied": [...版本号...], "skipped": 版本号, "current": 版本号,
         "dry_run": bool, "error": 失败摘要（如有）}
    """
    pending = pending_migrations(conn)
    if not real_run:
        return {"applied": [], "dry_run": True,
                "current": get_user_version(conn),
                "pending": [m.version for m in pending]}

    applied: List[int] = []
    error: Optional[str] = None
    for m in pending:
        try:
            m.up(conn)
            set_user_version(conn, m.version)
            conn.commit()
            applied.append(m.version)
        except sqlite3.Error as exc:
            error = f"v{m.version} {type(exc).__name__}: {exc}"
            conn.rollback()
            break
    return {"applied": applied, "dry_run": False,
            "current": get_user_version(conn), "error": error}


def describe() -> List[Dict[str, object]]:
    """列出注册表全部迁移与描述（供巡检脚本/文档生成）。"""
    return [{"version": m.version, "description": m.description}
            for m in sorted(MIGRATIONS, key=lambda m: m.version)]
