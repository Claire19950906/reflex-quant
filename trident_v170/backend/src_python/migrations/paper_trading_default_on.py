# -*- coding: utf-8 -*-
"""paper_trading_default_on.py — 把 paper_trading_settings.is_running 默认值改成 ON (v172)。

背景:
- v172 起,新建表 / 新 INSERT 行的 is_running 默认改为 1 (开启模拟盘)。
- 本迁移负责 backfill 旧 install:把所有 is_running=0 的现有行翻成 1。
- 幂等:已经 is_running=1 的行不受影响 (WHERE 限定)。
- 不会改 tracks / 其他列,也不主动开 run;engine 启动时见 is_running=1 会自动 create run (见 paper_trading.py 第 59-66 行的自愈逻辑)。

用法:
    # 默认数据库
    python3 -m migrations.paper_trading_default_on

    # 指定数据库 + dry-run
    python3 -m migrations.paper_trading_default_on --db /path/to.db --dry-run

约束:
- 仅依赖 stdlib + db.get_connection / config (不破坏 v170 旧环境)。
- best-effort:单行失败不阻断,最后 print summary。
"""
from __future__ import annotations

import argparse
import os
import sys

# 让 python3 -m migrations.paper_trading_default_on 能 import 同目录的 db / config
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _connect(db_path: str):
    """Open a connection to the given DB path. Avoids importing db.get_connection
    so we work even if the v170 app code paths break on this Python."""
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def main(db_path: str | None = None, dry_run: bool = False) -> int:
    if db_path is None:
        try:
            import config as _cfg
            db_path = _cfg.DB_PATH
        except Exception:
            # 回退到与 paper_trading.py 一致的默认路径
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "trident_event_bus.db",
            )

    print(f"[paper_trading_default_on] db={db_path} dry_run={dry_run}")

    conn = _connect(db_path)
    try:
        # 1. 确认表存在
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_trading_settings'"
        ).fetchone()
        if not exists:
            print("[paper_trading_default_on] paper_trading_settings 表不存在 — 跳过(全新 install 会在 db.py 启动时建表,默认已是 1)")
            return 0

        # 2. 读现状
        row = conn.execute(
            "SELECT id, is_running, tracks, started_at, active_run_id, updated_at "
            "FROM paper_trading_settings WHERE id=1"
        ).fetchone()
        if row is None:
            print("[paper_trading_default_on] id=1 行不存在 — 跳过(将在下次 get_settings 时按 v172 默认 INSERT)")
            return 0

        before_is_running = int(row["is_running"])
        before_updated_at = row["updated_at"]
        print(f"[paper_trading_default_on] before: is_running={before_is_running} updated_at={before_updated_at}")

        if before_is_running == 1:
            print("[paper_trading_default_on] is_running 已经为 1,无需 backfill — exit 0")
            return 0

        # 3. backfill
        if dry_run:
            print("[paper_trading_default_on] dry-run: 将执行 UPDATE paper_trading_settings SET is_running=1 WHERE id=1")
            return 0

        cur = conn.execute(
            "UPDATE paper_trading_settings SET is_running=1, updated_at=datetime('now') WHERE id=1 AND is_running=0"
        )
        conn.commit()
        print(f"[paper_trading_default_on] UPDATE affected rows: {cur.rowcount}")

        # 4. 验证
        row2 = conn.execute(
            "SELECT is_running, updated_at FROM paper_trading_settings WHERE id=1"
        ).fetchone()
        print(f"[paper_trading_default_on] after:  is_running={row2['is_running']} updated_at={row2['updated_at']}")
        if int(row2["is_running"]) != 1:
            print("[paper_trading_default_on] ERROR: 迁移后 is_running 仍不是 1,人工检查!", file=sys.stderr)
            return 1

        print("[paper_trading_default_on] OK — 旧 install 模拟盘已默认开启")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backfill paper_trading_settings.is_running=1 for old installs")
    p.add_argument("--db", dest="db_path", default=None, help="DB path (default: from config)")
    p.add_argument("--dry-run", action="store_true", help="只打印不写入")
    args = p.parse_args()
    sys.exit(main(db_path=args.db_path, dry_run=args.dry_run))